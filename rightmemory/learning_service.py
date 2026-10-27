from __future__ import annotations

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
from .config import load_sync_config
from .isolated_write import WorktreeLease
from .learning import LEARNING_DOMAINS, LearningCandidate, list_learning_candidates, record_learning_processed, submit_learning
from .platform import lock_file, unlock_file
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .session import _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory
from .sync import SyncManager


LEARNING_UPDATE_MAX_CANDIDATES = 20
LEARNING_UPDATE_EVIDENCE_CHARS = 24_000


class LearningClaimLost(RuntimeError):
    """A replaced curator task cannot publish its candidate work."""


class LearningService:
    """One durable curation lifecycle for native and configured executors."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        if not self.memory_root.is_dir():
            raise FileNotFoundError(f"RightMemory memory root does not exist: {self.memory_root}")
        self.root = self.memory_root / ".runtime" / "learning"
        self.sync_config = load_sync_config(self.memory_root)

    def submit(self, session_id: str, evidence: str, request_id: str | None = None) -> dict[str, Any]:
        result = submit_learning(self.memory_root, session_id, evidence, request_id=request_id)
        return self._response({"saved": True, **asdict(result), "sync": self._push()})

    def begin(
        self, domain: str, *, mode: str = "update", scope: str = "", retry_task: str | None = None,
        _executor: str = "native",
    ) -> dict[str, Any]:
        _validate_domain(domain)
        if mode not in {"update", "maintain"}:
            raise ValueError("curation mode must be update or maintain")
        if not isinstance(scope, str):
            raise ValueError("curation scope must be text")
        with self._locked():
            active = self._active(domain)
            retry_input = None
            if retry_task is not None:
                requested = self._task(retry_task)
                if requested["domain"] != domain:
                    raise ValueError("retry task belongs to a different domain")
                if requested.get("receipt") is not None:
                    return self._response(requested["receipt"])
                if active is None or active["task_id"] != retry_task:
                    raise LearningClaimLost("only the current curator task can be retried")
                mode, scope = requested["input"]["mode"], requested["input"]["scope"]
                _executor = requested["executor"]
                retry_input = requested["input"]
            if active is not None:
                operation = self._operation(active)
                if operation is not None and operation.phase in FINAL_PHASES:
                    settled = self._complete(active)
                    if retry_task is not None:
                        return self._response(settled)
                    active = None
                elif retry_task is None:
                    prepared = self._prepare(active, claimed=False)
                    if (active["input"]["mode"], active["input"]["scope"]) != (mode, scope):
                        return self._response({
                            **prepared, "status": "active", "requested_mode": mode,
                            "requested_scope": scope,
                            "reason": "Finish the active domain task before preparing the requested scope.",
                        })
                    return self._response(prepared)
                elif operation is not None and operation.phase == "prepared":
                    # A response can be lost after publication. Recover before
                    # replacing any task with a durable candidate outcome.
                    try:
                        return self._response(self._complete(active))
                    except AssistantStaleVersionError:
                        pass

            pulled = SyncManager(self.sync_config).pull()
            if pulled.status not in {"disabled", "synced", "ahead", "pushed"}:
                return self._response({"status": "sync_required", "sync": asdict(pulled)})
            if retry_input is not None:
                candidates = retry_input["candidates"]
            elif mode == "maintain":
                candidates = []
            else:
                candidates = _select_update_batch(list_learning_candidates(self.memory_root, domain=domain))
            if mode == "update" and not candidates:
                return self._response({"status": "not_ready", "domain": domain})
            task_id = f"{domain}-{uuid.uuid4().hex}"
            record = {
                "task_id": task_id, "domain": domain, "status": "running",
                "executor": _executor,
                "input": {
                    "domain": domain, "mode": mode, "scope": scope,
                    "candidates": candidates,
                },
            }
            self._save_task(record)
            self._write(self.root / f"{domain}-active.json", {"task_id": task_id})
            if active is not None:
                active["status"] = "superseded"
                active["replacement_task"] = task_id
                self._save_task(active)
            return self._response(self._prepare(record, claimed=True))

    def complete(self, task_id: str, output: str = "") -> dict[str, Any]:
        with self._locked():
            record = self._task(task_id)
            receipt = record.get("receipt")
            if receipt is not None:
                if output and output != receipt["output"]:
                    raise ValueError("curation task already completed with a different output")
                if receipt["sync"].get("status") not in {"disabled", "pushed", "synced", "ahead"}:
                    receipt["sync"] = self._push()
                    self._save_task(record)
                return self._response(receipt)
            self._check_current(record)
            return self._response(self._complete(record, output=output))

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        if task_id is None:
            return self._response({})
        with self._locked():
            record = self._task(task_id)
            if record.get("receipt") is not None:
                return self._response(record["receipt"])
            operation = self._operation(record)
            if operation is not None and operation.phase in FINAL_PHASES:
                return self._response(self._complete(record))
            return self._response({
                "task_id": task_id, "domain": record["domain"], "status": record["status"],
                "mode": record["input"]["mode"], "scope": record["input"]["scope"],
                "selected_candidate_count": len(record["input"]["candidates"]),
                "operation_phase": None if operation is None else operation.phase,
                "replacement_task": record.get("replacement_task"),
                "begin_argv": self._task_argv("begin", record),
                "complete_argv": self._argv("complete", "--task", task_id),
                "retry_argv": self._argv("begin", "--domain", record["domain"], "--retry", task_id),
            })

    def notices(self) -> list[dict[str, Any]]:
        """Read durable ready state without invoking a model or starting a worker."""
        result = []
        for domain in LEARNING_DOMAINS:
            active = self._active(domain)
            pending = list_learning_candidates(self.memory_root, domain=domain)
            if active is not None:
                operation = self._operation(active)
                phase = None if operation is None else operation.phase
                resumable = active.get("executor") == "configured" and not self._lease(active).is_live()
                result.append({
                    "domain": domain, "ready": resumable, "status": "resume" if resumable else "active",
                    "task_id": active["task_id"], "operation_phase": phase,
                    "mode": active["input"]["mode"], "scope": active["input"]["scope"],
                    "candidate_count": len(pending),
                    "selected_candidate_count": len(active["input"]["candidates"]),
                    "begin_argv": self._task_argv("begin", active),
                    "status_argv": self._argv("status", "--task", active["task_id"]),
                    "complete_argv": self._argv("complete", "--task", active["task_id"]),
                    "run_argv": self._task_argv("run", active),
                    "retry_argv": self._argv("begin", "--domain", domain, "--retry", active["task_id"]),
                })
            else:
                result.append({
                    "domain": domain, "ready": bool(pending),
                    "mode": "update", "scope": "",
                    "status": "ready" if pending else "waiting", "candidate_count": len(pending),
                    "begin_argv": self._argv("begin", "--domain", domain),
                    "run_argv": self._argv("run", "--domain", domain),
                })
        return result

    def run(self, domain: str, *, mode: str = "update", scope: str = "") -> dict[str, Any]:
        """Use a configured executor on the same prepared native task."""
        from .config import load_config
        from .runtime import RightMemoryRuntime

        prepared = self.begin(domain, mode=mode, scope=scope, _executor="configured")
        if prepared.get("status") != "prepared":
            return prepared
        task_id = prepared["task_id"]
        with self._locked():
            record = self._task(task_id)
            lease = self._lease(record)
            if record.get("executor") != "configured" or lease.is_live():
                return {**prepared, "status": "active"}
            self._check_current(record)
            lease.acquire()
        writer = self._writer(domain)
        try:
            task = writer.read(task_id)
            if task.source_commit is not None:
                return self.complete(task_id)
            config = load_config(f"{domain}-curator", memory_root=self.memory_root)
            config = replace(
                config, memory_root=task.worktree, state_root=self.memory_root,
                fresh_provider_session=True, sync=replace(config.sync, enabled=False),
            )
            runtime = RightMemoryRuntime(config)
            try:
                # The canonical domain core is loaded by Runtime. Completion
                # stays program-owned in command-backed execution.
                message = _curation_input(task.input_data or {})
                output = runtime.run_session_turn(task_id, message)
            finally:
                runtime.cleanup()
            return self.complete(task_id, output)
        except AssistantStaleVersionError as exc:
            replacement = self.begin(domain, retry_task=task_id, _executor="configured")
            return {**replacement, "status": "retry_ready", "reason": str(exc)}
        finally:
            lease.release()

    def _prepare(self, record: dict, *, claimed: bool) -> dict[str, Any]:
        from .prompt import build_cli_agent_instructions

        task = self._writer(record["domain"]).prepare_external(
            record["task_id"], record["input"], check_current=lambda: self._check_current(record),
        )
        directory = self.root / "tasks" / record["task_id"]
        candidates_file = directory / "candidates.json"
        instructions_file = directory / "instructions.md"
        self._write(candidates_file, record["input"])
        canonical = build_cli_agent_instructions(task.worktree, f"{record['domain']}-curator")
        complete = self._argv("complete", "--task", task.task_id)
        status = self._argv("status", "--task", task.task_id)
        instructions = (
            f"{canonical}\n\n{_curation_input(record['input'], include_evidence=False)}\n\n"
            f"Native task {task.task_id}: edit and commit only allowed files in {task.worktree}. "
            f"Read the frozen candidate manifest from {candidates_file}. "
            "Referenced source_file paths are relative to the candidate worktree; inspect their decoded evidence "
            "with native file tools when the manifest does not include an inline body. "
            f"Validate using {shlex.join(['rightmemory', 'validate', '--root', str(task.worktree)])}. "
            "Keep raw evidence unchanged during an update. During maintenance, retire raw evidence "
            "only after both curators have processed it and its independent value has gone. "
            "Processing receipts belong to program finalization.\n"
            f"After the candidate worktree is clean, run {shlex.join(complete)} even for no change. "
            "Only its receipt confirms publication; successful completion removes the worktree. "
            f"If interrupted, use {shlex.join(status)} and retry completion with the same task id. "
            "Keep these control commands bound to the active root.\n"
        )
        self._check_path(instructions_file)
        instructions_file.write_text(instructions, encoding="utf-8")
        return {
            "status": "prepared", "claimed": claimed, "task_id": task.task_id,
            "domain": record["domain"], "mode": record["input"]["mode"], "scope": record["input"]["scope"],
            "selected_candidate_count": len(record["input"]["candidates"]),
            "worktree": str(task.worktree), "instructions_file": str(instructions_file),
            "candidates_file": str(candidates_file), "complete_argv": complete, "status_argv": status,
        }

    def _complete(self, record: dict, output: str = "") -> dict[str, Any]:
        writer = self._writer(record["domain"])
        if writer.read(record["task_id"]).input_data != record["input"]:
            raise ValueError("curator task input changed after preparation")

        def retain_processed(worktree: Path, *_args: object) -> tuple[str, ...]:
            selected = record["input"]["candidates"]
            if not selected:
                return ()
            current = {candidate.candidate_id: candidate for candidate in list_learning_candidates(worktree)}
            for candidate in selected:
                found = current.get(candidate["candidate_id"])
                if found is None or _candidate_input(found, inline="evidence" in candidate) != candidate:
                    raise AssistantStaleVersionError("curator input evidence changed since task preparation")
            # Only new paths are returned: the supervisor verifies that this
            # callback changed exactly the declared program-owned artifacts.
            existing = set(worktree.glob(f"learning/processed/{record['domain']}/*.json"))
            paths = record_learning_processed(worktree, record["domain"], [item["candidate_id"] for item in selected])
            return tuple(path.relative_to(worktree).as_posix() for path in paths if path not in existing)

        result = writer.complete(
            record["task_id"], output=output, check_current=lambda: self._check_current(record),
            prepare_managed_artifacts=retain_processed,
        )
        receipt = {
            "status": "completed", "task_id": record["task_id"], "domain": record["domain"],
            "mode": record["input"]["mode"], "output": result.output,
            "selected_candidate_count": len(record["input"]["candidates"]),
            "changed_paths": list(result.changed_paths), "landed_commit": result.landed_commit,
            "operation_id": result.operation_id, "sync": self._push(),
        }
        record["status"] = "completed"
        record["receipt"] = receipt
        self._save_task(record)
        if self._active_id(record["domain"]) == record["task_id"]:
            self._write(self.root / f"{record['domain']}-active.json", {"task_id": None})
        try:
            writer.cleanup(record["task_id"])
        except (OSError, RuntimeError, ValueError) as exc:
            receipt["cleanup_error"] = str(exc)
            self._save_task(record)
        return receipt

    def _writer(self, domain: str) -> AssistantWriteManager:
        return AssistantWriteManager(self.memory_root, role=f"{domain}-curator")

    def _lease(self, record: dict) -> WorktreeLease:
        return WorktreeLease(self.memory_root, f"{record['domain']}-curator", record["task_id"])

    def _operation(self, record: dict):
        try:
            task = self._writer(record["domain"]).read(record["task_id"])
        except FileNotFoundError:
            return None
        return SemanticOperationStore(self.memory_root).read(task.operation_id)

    def _check_current(self, record: dict) -> None:
        if self._active_id(record["domain"]) != record["task_id"] or self._task(record["task_id"])["status"] != "running":
            raise LearningClaimLost("curator task was replaced; inspect its status before proceeding")

    def _active_id(self, domain: str) -> str | None:
        record = self._read(self.root / f"{domain}-active.json")
        if record is not None and set(record) != {"task_id"}:
            raise ValueError("invalid active curator task state")
        return None if record is None else record["task_id"]

    def _active(self, domain: str) -> dict | None:
        task_id = self._active_id(domain)
        if task_id is None:
            return None
        record = self._task(task_id)
        if record["domain"] != domain:
            raise ValueError("active curator task belongs to a different domain")
        return record if record["status"] == "running" else None

    def _task(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or re.fullmatch(r"(?:guidance|skills)-[0-9a-f]{32}", task_id) is None:
            raise ValueError("invalid curator task id")
        record = self._read(self.root / "tasks" / f"{task_id}.json")
        if record is None:
            raise FileNotFoundError(f"curator task does not exist: {task_id}")
        domain = record.get("domain")
        data = record.get("input")
        if (
            record.get("task_id") != task_id or domain not in LEARNING_DOMAINS
            or not task_id.startswith(f"{domain}-")
            or record.get("status") not in {"running", "completed", "superseded"}
            or record.get("executor") not in {"native", "configured"}
            or not isinstance(data, dict) or set(data) != {"domain", "mode", "scope", "candidates"}
            or data["domain"] != domain or data["mode"] not in {"update", "maintain"}
            or not isinstance(data["scope"], str) or not isinstance(data["candidates"], list)
            or any(not isinstance(item, dict) for item in data["candidates"])
        ):
            raise ValueError("invalid curator task state")
        return record

    def _save_task(self, record: dict) -> None:
        self._write(self.root / "tasks" / f"{record['task_id']}.json", record)

    def _response(self, result: dict) -> dict:
        response = {"ok": True, "active_root": str(self.memory_root), **result}
        try:
            response["learning"] = self.notices()
        except (OSError, ValueError, RuntimeError) as exc:
            response["learning"] = {"status": "error", "error": str(exc)}
        return response

    def _argv(self, *arguments: str) -> list[str]:
        return ["rightmemory", "learning", "--root", str(self.memory_root), *arguments]

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
            raise ValueError("invalid learning task state")
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
        raise ValueError("curation domain must be guidance or skills")


def _curation_input(data: dict, *, include_evidence: bool = True) -> str:
    result = f"Curation domain: {data['domain']}\nMode: {data['mode']}\nScope: {data['scope'] or 'related current material'}"
    if data["mode"] == "maintain":
        return result + (
            "\nStart from existing material within this scope. This task has no automatic pending-candidate batch; "
            "its completion leaves pending evidence for a later Update. Inspect related sources as needed."
        )
    if include_evidence:
        result += (
            "\n\nSelected candidate evidence follows. For referenced evidence, use read_learning(source_id) "
            "and its next offsets, or inspect the decoded source_file with native file tools. "
            "Source files are relative to the candidate worktree.\n"
            + json.dumps(data["candidates"], ensure_ascii=False, indent=2)
        )
    return result


def _select_update_batch(candidates: tuple[LearningCandidate, ...]) -> list[dict[str, Any]]:
    selected = []
    evidence_chars = 0
    for candidate in candidates:
        size = len(candidate.evidence)
        if len(selected) >= LEARNING_UPDATE_MAX_CANDIDATES:
            break
        if selected and evidence_chars + size > LEARNING_UPDATE_EVIDENCE_CHARS:
            break
        inline = size <= LEARNING_UPDATE_EVIDENCE_CHARS
        selected.append(_candidate_input(candidate, inline=inline))
        evidence_chars += size
        if not inline:
            break
    return selected


def _candidate_input(candidate: LearningCandidate, *, inline: bool) -> dict[str, Any]:
    relative = f"candidates/{candidate.candidate_id}.json"
    result = {
        "candidate_id": candidate.candidate_id, "session_id": candidate.session_id,
        "request_id": candidate.request_id, "submitted_at": candidate.submitted_at,
        "evidence_sha256": candidate.evidence_sha256, "evidence_chars": len(candidate.evidence),
        "source_id": f"L#{relative}", "source_file": f"learning/{relative}",
    }
    if inline:
        result["evidence"] = candidate.evidence
    return result
