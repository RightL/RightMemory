from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .assistant_write import AssistantStaleVersionError, AssistantWriteManager
from .automatic_effects import replay_memory_operation_effects
from .config import load_sync_config
from .curation_batch import select_curation_batch
from .curation_budget import domain_budget
from .isolated_write import WorktreeLease
from .git_storage import git
from .learning import (
    DOMAIN_ROLES, LEARNING_DOMAINS, LearningCandidate, LearningClaim, learning_claims,
    learning_batch_candidate_ids, list_learning_candidates, parse_learning_claim, submit_learning,
    CURATION_SCOPES, curation_domains,
)
from .learning_coordinator import (
    LearningClaimLost, LearningCoordinator, LearningSemanticBaseChanged,
)
from .platform import lock_file, unlock_file
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .session import _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory
from .sync import SyncManager


class LearningService:
    """Shared candidate capture, curation, and recovery for all three domains."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        if not self.memory_root.is_dir():
            raise FileNotFoundError(f"RightMemory memory root does not exist: {self.memory_root}")
        self.root = self.memory_root / ".runtime" / "learning"
        self.sync_config = load_sync_config(self.memory_root)
        self.coordinator = LearningCoordinator(self.sync_config)

    def submit(
        self, session_id: str, evidence: str, request_id: str | None = None,
        *, executor: str = "native",
    ) -> dict[str, Any]:
        _validate_executor(executor)
        result = submit_learning(self.memory_root, session_id, evidence, request_id=request_id)
        response = {"saved": True, **asdict(result), "sync": self._push()}
        if executor == "configured":
            from .curation_worker import CurationWorker

            try:
                response["worker"] = CurationWorker(self.memory_root).wake()
            except (OSError, ValueError, RuntimeError) as exc:
                response["worker"] = {"status": "error", "error": str(exc)}
        return self._response(response)

    def withdraw(self, candidate_id: str) -> dict[str, Any]:
        status = self.coordinator.withdraw(candidate_id)
        return self._response({"candidate_id": candidate_id, "status": status})

    def count(self, domain: str, task_id: str | None = None) -> dict[str, Any]:
        """Count retained text in the active root or an existing task worktree."""
        _validate_domain(domain)
        source = self.memory_root
        if task_id is not None:
            record = self._task(task_id)
            if domain not in curation_domains(record["domain"]):
                raise ValueError("curator task belongs to a different domain")
            writer = self._writer(domain)
            task = writer.read(task_id)
            writer._verify_worktree(task)
            source = task.worktree
        return {
            "ok": True, "active_root": str(self.memory_root), "task_id": task_id,
            **domain_budget(source, domain, state_root=self.memory_root),
        }

    def begin(
        self, domain: str = "all", *, mode: str = "update", scope: str = "", retry_task: str | None = None,
        _executor: str = "native",
    ) -> dict[str, Any]:
        curation_domains(domain)
        _validate_executor(_executor)
        if mode not in {"update", "maintain"}:
            raise ValueError("curation mode must be update or maintain")
        if mode == "update" and domain != "all" and retry_task is None:
            raise ValueError("Update is coordinated across all domains; use --domain all")
        if not isinstance(scope, str):
            raise ValueError("curation scope must be text")
        with self._locked():
            active = self._active(domain)
            retry_input = None
            replacing = None
            if retry_task is not None:
                try:
                    requested = self._task(retry_task)
                except FileNotFoundError:
                    replacing = self.coordinator.current_claim(domain)
                    if replacing is None or replacing.task_id != retry_task:
                        raise LearningClaimLost("only the current curator task can be retried")
                    mode, scope = replacing.mode, replacing.scope
                    sources = {item.candidate_id: item for item in list_learning_candidates(self.memory_root)}
                    retry_input = {
                        "domain": domain, "mode": mode, "scope": scope,
                        "candidates": [_candidate_input(sources[item]) for item in replacing.candidate_ids],
                    }
                    active = None
                else:
                    if requested["domain"] != domain:
                        raise ValueError("retry task belongs to a different domain")
                    if requested.get("receipt") is not None:
                        return self._response(self._retry_effects(requested))
                    if active is None or active["task_id"] != retry_task:
                        raise LearningClaimLost("only the current curator task can be retried")
                    if requested["executor"] == "configured" and self._lease(requested).is_live():
                        raise RuntimeError("stop the configured curator worker before replacing its task")
                    if any(self._child_lease(requested, item).is_live() for item in requested["children"]):
                        raise RuntimeError("stop running domain workers before replacing their parent task")
                    mode, scope = requested["input"]["mode"], requested["input"]["scope"]
                    _executor = requested["executor"]
                    retry_input = requested["input"]
                    replacing = self._claim(requested)
            if active is not None:
                if self._is_finalized(active):
                    settled = self._complete(active)
                    if retry_task is not None:
                        return self._response(settled)
                    active = None
                elif self._retire_fenced_task(active):
                    if retry_task is not None:
                        raise LearningClaimLost("the requested task no longer owns the domain claim")
                    active = None
                elif retry_task is None:
                    prepared = self._prepare(active, claimed=False)
                    return self._requested_result(prepared, mode, scope, domain)
                elif (operation := self._operation(active)) is not None and operation.phase == "prepared":
                    # Recover an uncertain publication before abandoning semantic work.
                    try:
                        return self._response(self._complete(active))
                    except (AssistantStaleVersionError, LearningSemanticBaseChanged):
                        pass

            task_id = f"{domain}-{uuid.uuid4().hex}"
            planned = {
                "task_id": task_id, "domain": domain, "status": "claiming",
                "executor": _executor, "claim": None,
                "input": retry_input or {"domain": domain, "mode": mode, "scope": scope, "candidates": []},
                "children": {item: {"status": "pending"} for item in curation_domains(domain)},
            }
            # Persist executor ownership before a remote claim can succeed.
            self._save_task(planned)
            selection = None if retry_input is None else tuple(
                item["candidate_id"] for item in retry_input["candidates"]
            )
            claimed = self.coordinator.claim(
                domain, mode=mode, scope=scope, task_id=task_id,
                candidate_ids=() if mode == "maintain" else selection,
                replace_token=None if replacing is None else replacing.token,
            )
            claim = claimed.claim
            if claim is None or claimed.foreign:
                self._task_path(task_id).unlink(missing_ok=True)
                if claim is None:
                    return self._response({"status": "not_ready", "domain": domain, "reason": claimed.reason})
                return self._response({
                    "status": "active", "domain": claim.domain, "task_id": claim.task_id,
                    "mode": claim.mode, "scope": claim.scope, "claimed": False,
                    "foreign": True, "executor": None, "reason": claimed.reason,
                })
            proof = [_candidate_input(candidate) for candidate in claimed.candidates]
            data = {"domain": domain, "mode": claim.mode, "scope": claim.scope, "candidates": proof}
            if claim.task_id == task_id:
                record = planned
                if retry_input is not None and data != retry_input:
                    raise AssistantStaleVersionError("curator input evidence changed since task preparation")
            else:
                self._task_path(task_id).unlink(missing_ok=True)
                record = self._task(claim.task_id)
                if record["status"] != "claiming" and record["input"] != data:
                    raise ValueError("claimed task no longer matches its frozen input")
            record.update(status="running", claim=asdict(claim), input=data)
            self._save_task(record)
            self._write(self.root / "active.json", {"task_id": claim.task_id})
            if active is not None and active["task_id"] != claim.task_id:
                active["status"] = "superseded"
                active["replacement_task"] = claim.task_id
                self._save_task(active)
            prepared = self._prepare(record, claimed=claimed.claimed)
            return self._requested_result(prepared, mode, scope, domain)

    def complete(self, task_id: str, output: str = "") -> dict[str, Any]:
        with self._locked():
            record = self._task(task_id)
            receipt = record.get("receipt")
            if receipt is not None:
                if output and output != receipt["output"]:
                    raise ValueError("curation task already completed with a different output")
                return self._response(self._retry_effects(record))
            self._check_current(record, require_claim=False)
            return self._response(self._complete(record, output=output))

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        if task_id is None:
            return self._response({})
        with self._locked():
            try:
                record = self._task(task_id)
            except FileNotFoundError:
                claim = next((item for item in learning_claims(self.memory_root) if item.task_id == task_id), None)
                if claim is None:
                    raise
                return self._response({
                    "status": "active", "task_id": task_id, "domain": claim.domain,
                    "mode": claim.mode, "scope": claim.scope, "executor": None,
                    "foreign": claim.holder != self.coordinator.holder_id,
                    "selected_candidate_count": len(claim.candidate_ids),
                    "retry_argv": self._argv("begin", "--domain", claim.domain, "--retry", task_id),
                })
            if record.get("receipt") is not None:
                return self._response(record["receipt"])
            if self._is_finalized(record):
                return self._response(self._complete(record))
            if record["status"] == "running":
                self._retire_fenced_task(record)
            operation = self._operation(record)
            return self._response({
                "task_id": task_id, "domain": record["domain"], "status": record["status"],
                "executor": record["executor"],
                "mode": record["input"]["mode"], "scope": record["input"]["scope"],
                "selected_candidate_count": len(record["input"]["candidates"]),
                "operation_phase": None if operation is None else operation.phase,
                "replacement_task": record.get("replacement_task"),
                "children": record["children"],
                "begin_argv": self._task_argv("begin", record),
                "complete_argv": self._argv("complete", "--task", task_id),
                "retry_argv": self._argv("begin", "--domain", record["domain"], "--retry", task_id),
            })

    def notices(self) -> list[dict[str, Any]]:
        """Read local readiness and shared claims without model, sync, or writes."""
        result = []
        shared = {claim.domain: claim for claim in learning_claims(self.memory_root)}
        holder = self.coordinator.holder_id
        active_parent = self._active("all")
        if (
            active_parent is not None
            and shared.get(active_parent["domain"]) != self._claim(active_parent)
            and not self._is_finalized(active_parent)
        ):
            active_parent = None
        domain = active_parent["domain"] if active_parent is not None else next(iter(shared), "all")
        for domain in (domain,):
            claim = shared.get(domain)
            active = self._active(domain)
            pending = list_learning_candidates(self.memory_root, domain=domain)
            if active is not None and claim != self._claim(active) and not self._is_finalized(active):
                active = None
            if claim is not None and (active is None or active["task_id"] != claim.task_id):
                path = self._task_path(claim.task_id)
                active = self._task(claim.task_id) if claim.holder == holder and path.is_file() else None
            if active is not None and (claim is None or claim.holder == holder):
                operation = self._operation(active)
                phase = None if operation is None else operation.phase
                resumable = active["executor"] == "configured" and not self._lease(active).is_live()
                result.append({
                    "domain": domain, "ready": resumable, "status": "resume" if resumable else "active",
                    "task_id": active["task_id"], "operation_phase": phase,
                    "executor": active["executor"], "foreign": False,
                    "mode": active["input"]["mode"], "scope": active["input"]["scope"],
                    "candidate_count": len(pending),
                    "selected_candidate_count": len(active["input"]["candidates"]),
                    "begin_argv": self._task_argv("begin", active),
                    "status_argv": self._argv("status", "--task", active["task_id"]),
                    "complete_argv": self._argv("complete", "--task", active["task_id"]),
                    "run_argv": self._task_argv("run", active),
                    "retry_argv": self._argv("begin", "--domain", domain, "--retry", active["task_id"]),
                })
            elif claim is not None:
                result.append({
                    "domain": domain, "ready": False, "status": "active",
                    "task_id": claim.task_id, "executor": None, "foreign": claim.holder != holder,
                    "mode": claim.mode, "scope": claim.scope,
                    "candidate_count": len(pending), "selected_candidate_count": len(claim.candidate_ids),
                    "status_argv": self._argv("status", "--task", claim.task_id),
                    "retry_argv": self._argv("begin", "--domain", domain, "--retry", claim.task_id),
                })
            elif (effect_task := self._pending_effect_task(domain)) is not None:
                result.append({
                    "domain": domain, "ready": True, "status": "effects_pending",
                    "task_id": effect_task["task_id"], "executor": effect_task["executor"],
                    "mode": effect_task["input"]["mode"], "scope": effect_task["input"]["scope"],
                    "candidate_count": len(pending), "selected_candidate_count": 0,
                    "complete_argv": self._argv("complete", "--task", effect_task["task_id"]),
                    "status_argv": self._argv("status", "--task", effect_task["task_id"]),
                })
            else:
                batch = select_curation_batch(
                    pending, self.coordinator.batch_config,
                    admitted_candidate_ids=learning_batch_candidate_ids(self.memory_root, domain),
                )
                result.append({
                    "domain": domain, "ready": bool(batch.candidates), "mode": "update", "scope": "",
                    "status": "ready" if batch.candidates else "waiting", "candidate_count": len(pending),
                    "next_ready_at": None if batch.next_ready_at is None else batch.next_ready_at.isoformat(),
                    "batch_id": _pending_batch_id(domain, pending),
                    "begin_argv": self._argv("begin", "--domain", domain),
                    "run_argv": self._argv("run", "--domain", domain),
                })
        return result

    def run(self, domain: str = "all", *, mode: str = "update", scope: str = "") -> dict[str, Any]:
        """Run the parent over one shared candidate, then publish its reviewed result."""
        curation_domains(domain)
        if mode == "update" and domain != "all":
            raise ValueError("Update is coordinated across all domains; use --domain all")
        active = self._active(domain)
        if active is not None and self._is_finalized(active):
            # Finish a recovered publication as this call's task. The worker must
            # observe its completion before it starts another batch's retry budget.
            return self.complete(active["task_id"])
        if active is None and self.coordinator.current_claim(domain) is None:
            pending = self._pending_effect_task(domain)
            if pending is not None:
                completed = self.complete(pending["task_id"])
                if completed.get("effects_error") or any(effect["status"] != "done" for effect in completed.get("effects", ())):
                    return {**completed, "status": "effects_pending", "reason": "published memory has pending side effects"}
                return completed
        from .config import load_config
        from .runtime import RightMemoryRuntime

        prepared = self.begin(domain, mode=mode, scope=scope, _executor="configured")
        if prepared.get("status") != "prepared":
            return prepared
        task_id = prepared["task_id"]
        with self._locked():
            record = self._task(task_id)
            lease = self._lease(record)
            if record["executor"] != "configured" or lease.is_live():
                return {**prepared, "status": "active"}
            self._check_current(record)
            lease.acquire()
        writer = self._writer(domain)
        try:
            task = writer.read(task_id)
            if task.source_commit is not None:
                return self.complete(task_id)
            config = load_config("curation-parent", memory_root=self.memory_root)
            config = replace(
                config, memory_root=task.worktree, state_root=self.memory_root,
                fresh_provider_session=True, sync=replace(config.sync, enabled=False),
            )
            runtime = RightMemoryRuntime(config, curation_mode=task.input_data["mode"], curation_task_id=task_id)
            try:
                caller = self._parent_input(record)
                if task.input_data["mode"] == "maintain":
                    caller += "\n\nMaintenance budgets by domain:\n" + json.dumps(prepared["token_budgets"], ensure_ascii=False)
                output = runtime.run_session_turn(task_id, caller)
            finally:
                runtime.cleanup()
            return self.complete(task_id, output)
        except (AssistantStaleVersionError, LearningSemanticBaseChanged) as exc:
            lease.release()
            replacement = self.begin(domain, retry_task=task_id, _executor="configured")
            return {**replacement, "status": "retry_ready", "reason": str(exc)}
        finally:
            lease.release()

    def begin_child(
        self, task_id: str, domain: str, *, instructions: str = "", retry: bool = False,
        _executor: str = "native",
    ) -> dict[str, Any]:
        """Prepare a domain contribution in its parent's existing worktree."""
        from .prompt import build_cli_agent_instructions

        _validate_domain(domain)
        _validate_executor(_executor)
        if not isinstance(instructions, str):
            raise ValueError("domain instructions must be text")
        with self._locked():
            record = self._task(task_id)
            self._check_current(record)
            if domain not in record["children"]:
                raise ValueError("domain is outside the parent task's requested scope")
            task = self._writer(record["domain"]).read(task_id)
            self._writer(record["domain"])._verify_worktree(task)
            if task.source_commit is not None:
                raise RuntimeError("parent publication has already been prepared")
            child = record["children"][domain]
            if child["status"] == "running" and not retry:
                return self._child_result(record, domain)
            if self._child_lease(record, domain).is_live():
                raise RuntimeError("the domain worker is still running")
            attempt = uuid.uuid4().hex
            child = {
                "status": "running", "attempt": attempt, "executor": _executor,
                "head": git(task.worktree, "rev-parse", "HEAD"),
                "index": _index_signature(task.worktree), "instructions": instructions,
            }
            record["children"][domain] = child
            self._save_task(record)
            data = {**record["input"], "domain": domain}
            text = build_cli_agent_instructions(task.worktree, DOMAIN_ROLES[domain], curation_mode=data["mode"])
            text += f"\n\n{_curation_input(data)}\n\n{instructions}\n"
            text += (
                "\nDo your assigned domain work yourself, without further delegation. "
                "Edit only your assigned files in the shared worktree and return your findings. "
                "Leave Git writes and publication to the parent.\n"
            )
            if data["mode"] == "maintain":
                text += "\nMaintenance budget:\n" + json.dumps(
                    domain_budget(task.worktree, domain, state_root=self.memory_root), ensure_ascii=False,
                )
                text += "\nCount with: " + shlex.join(self._argv("count", "--task", task_id, "--domain", domain))
            directory = self.root / "tasks" / task_id
            path = directory / f"{domain}.md"
            self._check_path(path)
            path.write_text(text, encoding="utf-8")
            return self._child_result(record, domain)

    def complete_child(
        self, task_id: str, domain: str, attempt: str, output: str = "", *,
        _lease: WorktreeLease | None = None,
    ) -> dict[str, Any]:
        """Record a returned contribution; do not publish or consume evidence."""
        _validate_domain(domain)
        if not isinstance(output, str):
            raise ValueError("domain output must be text")
        with self._locked():
            record = self._task(task_id)
            self._check_current(record)
            child = record["children"].get(domain)
            if child is None or child.get("attempt") != attempt:
                raise LearningClaimLost("domain contribution belongs to a replaced attempt")
            if child["status"] == "completed":
                if output != child["output"]:
                    raise ValueError("domain attempt already completed with different output")
                return self._child_result(record, domain)
            if child["status"] != "running":
                raise RuntimeError("domain attempt is not running")
            current_lease = self._child_lease(record, domain)
            if current_lease.is_live() and not (
                _lease is not None and _lease._owned and _lease.path == current_lease.path
            ):
                raise RuntimeError("wait for the configured domain worker to return before completing it")
            task = self._writer(record["domain"]).read(task_id)
            if git(task.worktree, "rev-parse", "HEAD") != child["head"] or _index_signature(task.worktree) != child["index"]:
                child.update(status="failed", error="domain work changed shared Git history or staging")
                self._save_task(record)
                raise RuntimeError(child["error"])
            child.update(status="completed", output=output)
            self._save_task(record)
            return self._child_result(record, domain)

    def run_child(self, task_id: str, domain: str, *, instructions: str = "") -> dict[str, Any]:
        """Run one configured domain model without finalizing its parent's work."""
        from .config import load_config
        from .runtime import RightMemoryRuntime

        prepared = self.begin_child(task_id, domain, instructions=instructions, _executor="configured", retry=True)
        attempt = prepared["attempt"]
        with self._locked():
            record = self._task(task_id)
            self._check_current(record)
            child = record["children"][domain]
            if child.get("attempt") != attempt:
                raise LearningClaimLost("domain contribution was replaced before execution")
            lease = self._child_lease(record, domain)
            lease.acquire()
        worktree = Path(prepared["worktree"])
        try:
            config = load_config(DOMAIN_ROLES[domain], memory_root=self.memory_root)
            config = replace(config, memory_root=worktree, state_root=self.memory_root,
                             fresh_provider_session=True, sync=replace(config.sync, enabled=False))
            runtime = RightMemoryRuntime(config, curation_mode=record["input"]["mode"], curation_task_id=task_id)
            try:
                output = runtime.run_session_turn(f"{task_id}-{domain}-{attempt}", Path(prepared["instructions_file"]).read_text(encoding="utf-8"))
            finally:
                runtime.cleanup()
            return self.complete_child(task_id, domain, attempt, output, _lease=lease)
        except BaseException as exc:
            with self._locked():
                latest = self._task(task_id)
                if latest["children"][domain].get("attempt") == attempt:
                    latest["children"][domain].update(status="failed", error=str(exc))
                    self._save_task(latest)
            raise
        finally:
            lease.release()

    def _child_lease(self, record: dict, domain: str) -> WorktreeLease:
        return WorktreeLease(self.memory_root, DOMAIN_ROLES[domain], record["task_id"])

    def _child_result(self, record: dict, domain: str) -> dict[str, Any]:
        child = record["children"][domain]
        task = self._writer(record["domain"]).read(record["task_id"])
        result = {
            "task_id": record["task_id"], "domain": domain, **child,
            "worktree": str(task.worktree),
            "instructions_file": str(self.root / "tasks" / record["task_id"] / f"{domain}.md"),
        }
        if child.get("attempt"):
            result["complete_argv"] = self._argv("worker-complete", "--task", record["task_id"],
                                                  "--domain", domain, "--attempt", child["attempt"])
        return result

    def _parent_input(self, record: dict) -> str:
        task_id = record["task_id"]
        result = _curation_input(record["input"])
        result += "\n\nParent task: " + task_id + "\nDomain contributions:\n"
        result += json.dumps(record["children"], ensure_ascii=False, indent=2)
        result += (
            "\nFor configured domain work, use run_curator(domain, instructions), or the CLI commands below. "
            "For native delegation, worker-begin returns the shared worktree and domain instructions. "
            "After that agent returns, record its result with the returned worker-complete command. "
            "Run the selected domain agents in parallel on their assigned files, "
            "then wait for all of them to return before reviewing or editing the combined result.\n"
        )
        for domain in curation_domains(record["domain"]):
            for action in ("worker-begin", "worker-run"):
                result += shlex.join(self._argv(action, "--task", task_id, "--domain", domain)) + "\n"
        result += (
            "\nAfter the domain agents finish, review the files and their diff, "
            "make any needed corrections yourself, then stage and commit the combined result."
        )
        return result

    def _prepare(self, record: dict, *, claimed: bool) -> dict[str, Any]:
        from .prompt import build_cli_agent_instructions

        domain = record["domain"]
        data = record["input"]
        mode = data["mode"]
        task = self._writer(domain).prepare(
            record["task_id"], data, check_current=lambda: self._check_current(record),
        )
        directory = self.root / "tasks" / record["task_id"]
        candidates_file = directory / "candidates.json"
        instructions_file = directory / "instructions.md"
        self._write(candidates_file, data)
        canonical = build_cli_agent_instructions(task.worktree, "curation-parent", curation_mode=mode)
        budget_context = ""
        budget_info = {}
        if mode == "maintain":
            budgets = {item: domain_budget(task.worktree, item, state_root=self.memory_root)
                       for item in curation_domains(domain)}
            budget_context = "Maintenance budgets by domain:\n" + json.dumps(budgets, ensure_ascii=False) + "\n"
            budget_info = {"token_budgets": budgets}
        complete = self._argv("complete", "--task", task.task_id)
        status = self._argv("status", "--task", task.task_id)
        instructions = (
            f"{canonical}\n\n{self._parent_input(record)}\n\n{budget_context}"
            f"Native parent task {task.task_id}: own the combined edits in {task.worktree}. "
            f"Read the frozen candidate manifest from {candidates_file}. "
            "Source_file paths are relative to the candidate worktree; read their decoded evidence "
            "with native file tools. "
            f"Validate using {shlex.join(['rightmemory', 'validate', '--root', str(task.worktree)])}. "
            "Candidate evidence, claims, and processing receipts belong to program finalization.\n"
            f"After the candidate worktree is clean, run {shlex.join(complete)} even for no change. "
            "Only its receipt confirms publication; successful completion removes the worktree. "
            f"If interrupted, use {shlex.join(status)} and retry completion with the same task id. "
            "Keep these control commands bound to the active root.\n"
        )
        self._check_path(instructions_file)
        instructions_file.write_text(instructions, encoding="utf-8")
        return {
            "status": "prepared", "claimed": claimed, "task_id": task.task_id,
            "domain": domain, "executor": record["executor"],
            "mode": mode, "scope": data["scope"],
            "selected_candidate_count": len(data["candidates"]),
            "worktree": str(task.worktree), "instructions_file": str(instructions_file),
            "candidates_file": str(candidates_file), "complete_argv": complete, "status_argv": status,
            "children": record["children"],
            **budget_info,
        }

    def _complete(self, record: dict, output: str = "") -> dict[str, Any]:
        writer = self._writer(record["domain"])
        task = writer.read(record["task_id"])
        if task.input_data != record["input"]:
            raise ValueError("curator task input changed after preparation")
        claim = self._claim(record)
        finalizer = f"curation:{claim.token}"
        landed = self.coordinator.finalized_commit(claim)
        if landed is None:
            self._check_current(record)
            unfinished = [domain for domain, child in record["children"].items()
                          if child["status"] != "completed" or self._child_lease(record, domain).is_live()]
            if unfinished:
                raise RuntimeError("parent review requires completed domain work: " + ", ".join(unfinished))
            prepared = writer.complete(
                record["task_id"], output=output, external_finalizer=finalizer,
                check_current=lambda: self._check_current(record),
            )
            landed = self.coordinator.finalize(
                claim, prepared.candidate_commit, prepared_start_commit=prepared.start_commit,
            )
        result = writer.complete_external(
            record["task_id"], external_finalizer=finalizer, landed_commit=landed,
        )
        receipt = {
            "status": "completed", "task_id": record["task_id"], "domain": record["domain"],
            "mode": record["input"]["mode"], "scope": record["input"]["scope"],
            "executor": record["executor"], "output": result.output,
            "selected_candidate_count": len(record["input"]["candidates"]),
            "changed_paths": list(result.changed_paths), "landed_commit": result.landed_commit,
            "operation_id": result.operation_id,
            "sync": {"status": "pushed" if self.sync_config.enabled else "disabled"},
        }
        record["status"] = "completed"
        record["receipt"] = receipt
        self._save_task(record)
        if self._active_id(record["domain"]) == record["task_id"]:
            self._write(self.root / "active.json", {"task_id": None})
        try:
            writer.cleanup(record["task_id"])
            directory = self.root / "tasks" / record["task_id"]
            for name in ("candidates.json", "instructions.md", *(f"{domain}.md" for domain in curation_domains(record["domain"]))):
                path = directory / name
                self._check_path(path)
                path.unlink(missing_ok=True)
            if directory.is_dir():
                directory.rmdir()
        except (OSError, RuntimeError, ValueError) as exc:
            receipt["cleanup_error"] = str(exc)
            self._save_task(record)
        return self._retry_effects(record)

    def _retry_effects(self, record: dict) -> dict:
        receipt = record["receipt"]
        if "memory" in curation_domains(record["domain"]):
            try:
                receipt["effects"] = replay_memory_operation_effects(self.memory_root, receipt["operation_id"])
            except (OSError, ValueError, RuntimeError) as exc:
                receipt["effects_error"] = str(exc)
            else:
                receipt.pop("effects_error", None)
            self._save_task(record)
        return receipt

    def _pending_effect_task(self, domain: str) -> dict | None:
        if "memory" not in curation_domains(domain):
            return None
        store = SemanticOperationStore(self.memory_root)
        outstanding = [
            record for record in store.list_outstanding_records(read_only=True)
            if record.phase in FINAL_PHASES and record.input_data.get("kind") == "curation"
            and record.input_data.get("role") == "curation-parent" and any(effect.status != "done" for effect in record.effects)
        ]
        outstanding.sort(key=lambda record: (record.outcome.sequence, record.operation_id))
        for operation in outstanding:
            task_id = operation.input_data.get("task_id")
            if isinstance(task_id, str):
                task = self._task(task_id)
                if task.get("receipt") is not None:
                    return task
        return None

    def _retire_fenced_task(self, record: dict) -> bool:
        current = self.coordinator.current_claim(record["domain"])
        if current == self._claim(record):
            return False
        record["status"] = "superseded"
        record["replacement_task"] = None if current is None else current.task_id
        self._save_task(record)
        if self._active_id(record["domain"]) == record["task_id"]:
            self._write(self.root / "active.json", {"task_id": None})
        return True

    def _writer(self, domain: str) -> AssistantWriteManager:
        return AssistantWriteManager(self.memory_root, role="curation-parent")

    def _lease(self, record: dict) -> WorktreeLease:
        return WorktreeLease(self.memory_root, "curation-parent", record["task_id"])

    def _operation(self, record: dict):
        try:
            task = self._writer(record["domain"]).read(record["task_id"])
        except FileNotFoundError:
            return None
        return SemanticOperationStore(self.memory_root).peek(task.operation_id)

    def _is_finalized(self, record: dict) -> bool:
        operation = self._operation(record)
        return (operation is not None and operation.phase in FINAL_PHASES) or (
            record.get("claim") is not None and self.coordinator.finalized_commit(self._claim(record)) is not None
        )

    def _claim(self, record: dict) -> LearningClaim:
        claim = parse_learning_claim(record["claim"])
        candidates = record["input"]["candidates"]
        if (
            claim.domain != record["domain"] or claim.task_id != record["task_id"]
            or claim.mode != record["input"]["mode"] or claim.scope != record["input"]["scope"]
            or claim.candidate_ids != tuple(item["candidate_id"] for item in candidates)
            or claim.candidate_hashes != tuple(item["evidence_sha256"] for item in candidates)
        ):
            raise ValueError("curator task does not match its shared claim")
        return claim

    def _check_current(self, record: dict, *, require_claim: bool = True) -> None:
        if self._active_id(record["domain"]) != record["task_id"] or self._task(record["task_id"])["status"] != "running":
            raise LearningClaimLost("curator task was replaced; inspect its status before proceeding")
        if require_claim and self.coordinator.current_claim(record["domain"]) != self._claim(record):
            raise LearningClaimLost("curator shared claim was replaced or completed")

    def _active_id(self, domain: str) -> str | None:
        record = self._read(self.root / "active.json")
        if record is not None and set(record) != {"task_id"}:
            raise ValueError("invalid active curator task state")
        return None if record is None else record["task_id"]

    def _active(self, domain: str) -> dict | None:
        task_id = self._active_id(domain)
        if task_id is None:
            return None
        record = self._task(task_id)
        return record if record["status"] == "running" else None

    def _task_path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or re.fullmatch(r"(?:all|memory|guidance|skills)-[0-9a-f]{32}", task_id) is None:
            raise ValueError("invalid curator task id")
        return self.root / "tasks" / f"{task_id}.json"

    def _task(self, task_id: str) -> dict:
        record = self._read(self._task_path(task_id))
        if record is None:
            raise FileNotFoundError(f"curator task does not exist: {task_id}")
        domain = record.get("domain")
        data = record.get("input")
        if (
            record.get("task_id") != task_id or domain not in CURATION_SCOPES
            or not task_id.startswith(f"{domain}-")
            or record.get("status") not in {"claiming", "running", "completed", "superseded"}
            or record.get("executor") not in {"native", "configured"}
            or not isinstance(data, dict) or set(data) != {"domain", "mode", "scope", "candidates"}
            or data["domain"] != domain or data["mode"] not in {"update", "maintain"}
            or not isinstance(data["scope"], str) or not isinstance(data["candidates"], list)
            or any(not isinstance(item, dict) or "evidence" in item for item in data["candidates"])
            or not isinstance(record.get("children"), dict)
            or set(record["children"]) != set(curation_domains(domain))
            or any(not isinstance(child, dict) or child.get("status") not in {"pending", "running", "completed", "failed"}
                   for child in record["children"].values())
        ):
            raise ValueError("invalid curator task state")
        if record["status"] != "claiming":
            self._claim(record)
        return record

    def _save_task(self, record: dict) -> None:
        self._write(self._task_path(record["task_id"]), record)

    def _requested_result(self, prepared: dict, mode: str, scope: str, domain: str) -> dict:
        if (prepared["mode"], prepared["scope"], prepared["domain"]) != (mode, scope, domain):
            prepared = {
                **prepared, "status": "active", "requested_mode": mode, "requested_scope": scope,
                "requested_domain": domain,
                "reason": "Finish the active domain task before preparing the requested scope.",
            }
        return self._response(prepared)

    def _response(self, result: dict) -> dict:
        from .native_sync import NativeSyncRepair

        response = {"ok": True, "active_root": str(self.memory_root), **result}
        response["sync_repair"] = NativeSyncRepair(self.memory_root).notice()
        try:
            response["curation"] = self.notices()
        except (OSError, ValueError, RuntimeError) as exc:
            response["curation"] = {"status": "error", "error": str(exc)}
        return response

    def _argv(self, *arguments: str) -> list[str]:
        return ["rightmemory", "curate", "--root", str(self.memory_root), *arguments]

    def _task_argv(self, action: str, record: dict) -> list[str]:
        command = self._argv(action, "--domain", record["domain"], "--mode", record["input"]["mode"])
        if record["input"]["scope"]:
            command.extend(["--scope", record["input"]["scope"]])
        return command

    def _push(self) -> dict:
        try:
            return asdict(SyncManager(self.sync_config).push())
        except (OSError, ValueError, RuntimeError) as exc:
            return {"status": "pending", "error": str(exc)}

    @contextmanager
    def _locked(self):
        path = self.root / "control.lock"
        self._check_path(path)
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        _ensure_durable_directory(path.parent)
        with path.open("a+b") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _check_path(self, path: Path) -> None:
        current = self.memory_root
        for part in path.relative_to(self.memory_root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"learning runtime path must not be a symlink: {current}")

    def _read(self, path: Path) -> dict | None:
        self._check_path(path)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid curation task state")
        return data

    def _write(self, path: Path, data: dict) -> None:
        self._check_path(path)
        _ensure_durable_directory(path.parent)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _validate_domain(domain: str) -> None:
    if domain not in LEARNING_DOMAINS:
        raise ValueError("curation domain must be memory, guidance, or skills")


def _validate_executor(executor: str) -> None:
    if executor not in {"native", "configured"}:
        raise ValueError("curation executor must be native or configured")


def _curation_input(data: dict) -> str:
    result = f"Curation domain: {data['domain']}\nMode: {data['mode']}\nScope: {data['scope'] or 'related current material'}"
    if data["mode"] == "maintain":
        return result + (
            "\nStart from existing material within this scope. This task has no pending-candidate batch; "
            "its completion leaves pending evidence for a later Update. Inspect related sources as needed."
        )
    return result + (
        "\n\nSelected candidate sources follow. Read their evidence with read_learning(source_id) and its "
        "next offsets, or inspect the decoded source_file with native file tools. "
        "Source files are relative to the candidate worktree.\n"
        + json.dumps(data["candidates"], ensure_ascii=False, indent=2)
    )


def _pending_batch_id(domain: str, candidates: tuple[LearningCandidate, ...]) -> str | None:
    if not candidates:
        return None
    identities = sorted((item.candidate_id, item.evidence_sha256) for item in candidates)
    return hashlib.sha256(json.dumps([domain, identities]).encode("utf-8")).hexdigest()


def _candidate_input(candidate: LearningCandidate) -> dict[str, Any]:
    relative = f"candidates/{candidate.candidate_id}.json"
    return {
        "candidate_id": candidate.candidate_id, "session_id": candidate.session_id,
        "request_id": candidate.request_id, "evidence_sha256": candidate.evidence_sha256,
        "evidence_chars": len(candidate.evidence),
        "source_id": f"L#{relative}", "source_file": f"learning/{relative}",
    }


def _index_signature(worktree: Path) -> str:
    # Read staged entries without taking Git's index lock. Ordinary status reads
    # by another worker may refresh index metadata without changing those entries.
    entries = git(worktree, "ls-files", "--stage", "-z")
    return hashlib.sha256(entries.encode("utf-8")).hexdigest()
