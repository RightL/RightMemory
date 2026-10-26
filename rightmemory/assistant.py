from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .assistant_context import prepare_retrieve_context
from .assistant_prompt import build_assistant_update_instructions
from .assistant_write import AssistantWriteManager
from .async_update import AsyncUpdateStore
from .config import load_async_update_config, load_sync_config
from .isolated_write import IsolatedWriteResult
from .platform import lock_file, unlock_file
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id
from .sync import SyncManager
from .update_queue import (
    SCHEMA_VERSION, parse_update_candidate_data, parse_update_queue_lease_json,
    update_candidate_batch_id, update_candidate_data,
)
from .update_queue_git import (
    QUEUE_FINALIZER, QUEUE_LEASE_SECONDS, ClaimedUpdateBatch, GitUpdateQueueCoordinator,
    UpdateQueueLeaseLost, UpdateQueueSemanticBaseChanged,
)


class AssistantClaimLost(RuntimeError):
    """The task was replaced; its edits cannot be published."""


class AssistantService:
    """Model-free entry points for native assistant tasks and durable batching."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        if not self.memory_root.is_dir():
            raise FileNotFoundError(f"RightMemory memory root does not exist: {self.memory_root}")
        self.root = self.memory_root / ".runtime" / "assistant"
        _check_path(self.root)
        self.store = AsyncUpdateStore(self.memory_root, "update", executor="assistant")
        self.writer = AssistantWriteManager(self.memory_root)
        self.sync_config = load_sync_config(self.memory_root)
        config = load_async_update_config(self.memory_root)
        self.batch_options = {
            "trigger_candidates": config.trigger_candidates,
            "target_batch_candidates": config.target_batch_candidates,
            "max_wait_seconds": config.max_wait_seconds,
        }

    def context(self, session_id: str) -> dict[str, Any]:
        _safe_session_id(session_id)
        # Completion and queue acknowledgement may have been interrupted. Only
        # proven terminal outcomes are settled here; no model work is resumed.
        with self._locked():
            self._settle_terminal_active()
            queue = self._publish_outbox()
        result = prepare_retrieve_context(
            self.memory_root, excluded_candidate_uids=self._completed_candidates(),
        )
        return self._response({"session_id": session_id, **result, "queue": queue})

    def submit(self, session_id: str, request_id: str, evidence: str) -> dict[str, Any]:
        _safe_session_id(session_id)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty stable submission identifier")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("evidence must not be empty")
        uid = hashlib.sha256(json.dumps([session_id, request_id]).encode()).hexdigest()[:32]
        path = self.root / "submissions" / f"{uid}.json"
        with self._locked():
            identity = {"session_id": session_id, "request_id": request_id, "evidence": evidence}
            previous = _read_json(path)
            if previous is not None and previous != identity:
                raise ValueError("request_id already belongs to different update evidence")
            _write_json(path, identity)
            state = self.store.submit(session_id, evidence, candidate_uid=uid)
            self._settle_terminal_active()
            queue = self._publish_outbox()
        return self._response({
            "saved": True, "candidate_uid": uid, "duplicate": previous is not None,
            "session_id": session_id, "session_status": state.status, "queue": queue,
        })

    def begin_update(self, retry_task: str | None = None) -> dict[str, Any]:
        with self._locked():
            active = self._active()
            if retry_task is not None:
                requested = self._task(retry_task)
                if requested.get("receipt") is not None:
                    return self._response(requested["receipt"])
                if active is None or active["task_id"] != retry_task:
                    raise AssistantClaimLost("only the current task can be retried")
            if active is not None:
                settled = self._settle_terminal_active()
                if settled is None and active["backend"] == "git" and (
                    retry_task is not None or _time(active["expires_at"]) <= _now()
                ):
                    settled = self._settle_git_terminal(active)
                if settled is not None:
                    if retry_task is not None:
                        return self._response(settled)
                    active = None
                elif retry_task is None and _time(active["expires_at"]) > _now():
                    return self._response(self._prepare(active, claimed=False))
                else:
                    # A prepared result might already have landed before its
                    # receipt. Recover it before permitting a replacement task.
                    operation = self._operation(active)
                    if operation is not None and operation.phase == "prepared":
                        try:
                            return self._response(self._complete(active))
                        except (UpdateQueueSemanticBaseChanged, UpdateQueueLeaseLost):
                            pass
                        except RuntimeError as exc:
                            from .assistant_write import AssistantStaleVersionError
                            if not isinstance(exc, AssistantStaleVersionError):
                                raise
                    if retry_task is None:
                        # Lease expiry is not permission to abandon native edits.
                        # Keep the task identity until an explicit retry, while
                        # publication still enforces the shared queue's fence.
                        return self._response(self._prepare(active, claimed=False))

            sync_result = SyncManager(self.sync_config).pull()
            if sync_result.status not in {"disabled", "synced", "ahead", "pushed"}:
                return self._response({"status": "sync_required", "sync": asdict(sync_result)})
            publication = self._publish_outbox()
            if self.sync_config.enabled:
                result = self._coordinator().claim_next(**self.batch_options)
                claim = result.claim
                if claim is None:
                    return self._response({"status": "not_ready", "queue": publication,
                                           "online": result.online})
                candidates = claim.candidates
                batch_id = claim.batch_id
                backend = "git"
            else:
                batch, _deadline = self.store.ready_batch(**self.batch_options)
                if not batch:
                    return self._response({"status": "not_ready"})
                reserved = self.store.reserve_batch(batch)
                if reserved is None:
                    return self._response({"status": "not_ready"})
                batch_id = reserved.operation_id
                candidates = self.store.start_reserved_batch(batch_id)
                claim = None
                backend = "local"
            if active is not None and active["batch_id"] != batch_id:
                raise AssistantClaimLost("active batch was replaced; inspect update status before proceeding")
            record = {
                "task_id": uuid.uuid4().hex, "batch_id": batch_id, "backend": backend,
                "created_at": _now().isoformat(),
                "expires_at": (_now() + timedelta(seconds=QUEUE_LEASE_SECONDS)).isoformat(),
                "candidates": [update_candidate_data(item) for item in candidates],
                "claim": None if claim is None else {
                    "lease": asdict(claim.lease), "lease_commit": claim.lease_commit,
                },
                "status": "running",
            }
            self._save_task(record)
            _write_json(self.root / "active.json", {"task_id": record["task_id"]})
            if active is not None:
                active["status"] = "superseded"
                active["replacement_task"] = record["task_id"]
                self._save_task(active)
            return self._response(self._prepare(record, claimed=True))

    def complete_update(self, task_id: str, output: str = "") -> dict[str, Any]:
        with self._locked():
            record = self._task(task_id)
            if record.get("receipt") is not None:
                receipt = record["receipt"]
                if receipt["status"] == "completed" and output and output != receipt["output"]:
                    raise ValueError("assistant completion already has a different output")
                return self._response(receipt)
            self._check_current(task_id)
            return self._response(self._complete(record, output=output))

    def update_status(self, task_id: str | None = None) -> dict[str, Any]:
        with self._locked():
            self._settle_terminal_active()
            record = self._task(task_id) if task_id is not None else self._active()
            if record is None:
                return self._response({"status": "idle"})
            if record.get("receipt") is not None:
                return self._response(record["receipt"])
            status = {key: record[key] for key in ("task_id", "batch_id", "status", "expires_at")}
            try:
                task = self.writer.read(record["task_id"])
                status.update(worktree=str(task.worktree), base_commit=task.base_commit)
                operation = self._operation(record)
                status["completion_phase"] = None if operation is None else operation.phase
            except FileNotFoundError:
                status["completion_phase"] = "preparing"
            status["complete_argv"] = self._argv("update", "complete", "--task", record["task_id"])
            return self._response(status)

    def sync(self, action: str = "refresh") -> dict[str, Any]:
        manager = SyncManager(self.sync_config)
        actions = {"refresh": manager.refresh_for_retrieve, "pull": manager.pull, "push": manager.push}
        if action not in actions:
            raise ValueError("sync action must be refresh, pull, or push")
        result = actions[action]()
        with self._locked():
            self._settle_terminal_active()
            queue = self._publish_outbox()
        return self._response({"sync": asdict(result), "queue": queue})

    def _prepare(self, record: dict, *, claimed: bool) -> dict:
        task_id = record["task_id"]
        task = self.writer.prepare(
            task_id, record["batch_id"], self._candidates(record),
            check_current=lambda: self._check_current(task_id),
        )
        folder = self.root / "tasks" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        instructions = folder / "UPDATE.md"
        instructions.write_text(build_assistant_update_instructions(
            task.worktree, task_id, active_root=self.memory_root,
        ), encoding="utf-8")
        evidence = folder / "CANDIDATES.json"
        _write_json(evidence, {"batch_id": task.batch_id, "candidates": record["candidates"]})
        argv = self._argv("update", "complete", "--task", task_id)
        return {
            "status": "ready" if claimed else "active", "claimed": claimed,
            "task_id": task_id, "batch_id": task.batch_id, "worktree": str(task.worktree),
            "base_commit": task.base_commit, "expires_at": record["expires_at"],
            "instructions_file": str(instructions), "candidates_file": str(evidence),
            "complete_argv": argv, "complete_command": shlex.join(argv),
        }

    def _complete(self, record: dict, output: str = "") -> dict:
        task_id = record["task_id"]
        self._check_current(task_id)
        operation = self._operation(record)
        if operation is not None and operation.phase in FINAL_PHASES:
            task = self.writer.read(task_id)
            if output and output != task.output:
                raise ValueError("assistant completion already has a different output")
            return self._finish_terminal(record, operation)
        result = self.writer.complete(
            task_id, output=output, check_current=lambda: self._check_current(task_id),
            external_finalizer=QUEUE_FINALIZER if record["backend"] == "git" else None,
        )
        if record["backend"] == "git" and result.prepared:
            self._check_current(task_id)
            try:
                landed = self._coordinator().finalize(
                    self._claim(record), result.candidate_commit,
                    prepared_start_commit=result.start_commit,
                )
            except UpdateQueueLeaseLost:
                settled = self._settle_git_terminal(record)
                if settled is not None:
                    return settled
                raise
            result = self.writer.complete_external(
                task_id, external_finalizer=QUEUE_FINALIZER, landed_commit=landed,
            )
        return self._finish(record, result)

    def _finish(self, record: dict, result) -> dict:
        if record["backend"] == "local":
            self.store.finish_reserved_batch(record["batch_id"], str(result.output))
        else:
            uids = tuple(item.uid for item in self._candidates(record))
            self.store.acknowledge_synchronized(set(uids), result=str(result.output))
            self._coordinator().clear_local_candidates(uids)
        receipt = {
            "status": "completed_elsewhere" if record.get("completed_elsewhere") else "completed",
            "task_id": record["task_id"], "batch_id": record["batch_id"],
            "landed_commit": result.landed_commit, "changed_paths": list(result.changed_paths),
            "output": result.output, "sync": "published" if record["backend"] == "git" else "disabled",
        }
        record.update(status="completed", receipt=receipt)
        self._save_task(record)
        if self._active_id() == record["task_id"]:
            _write_json(self.root / "active.json", {"task_id": None})
        # Keep a workspace with unexpected later edits; successful publication
        # must not turn into a failed request merely because cleanup is deferred.
        try:
            self.writer.cleanup(record["task_id"])
        except (OSError, RuntimeError) as exc:
            receipt["cleanup_warning"] = str(exc)
            self._save_task(record)
        return receipt

    def _settle_git_terminal(self, record: dict) -> dict | None:
        coordinator = self._coordinator()
        operation = self._operation(record)
        own_landed = coordinator.fetch_and_integrate_finalized_batch(
            record["batch_id"], token=self._claim(record).lease.token,
        )
        if own_landed is not None and operation is not None and operation.phase == "prepared":
            self._check_current(record["task_id"])
            result = self.writer.complete_external(
                record["task_id"], external_finalizer=QUEUE_FINALIZER, landed_commit=own_landed,
            )
            return self._finish(record, result)
        landed = coordinator.fetch_and_integrate_finalized_batch(record["batch_id"])
        if landed is None:
            landed = coordinator.fetch_and_integrate_superseded_batch(record["batch_id"])
        if landed is None:
            return None
        self._check_current(record["task_id"])
        store = SemanticOperationStore(self.memory_root)
        if operation is not None:
            if operation.phase == "prepared":
                self.writer.supervisor.supersede_external(
                    operation.operation_id, external_finalizer=QUEUE_FINALIZER, landed_commit=landed,
                )
            elif operation.phase == "running":
                with store.execution_locked():
                    store.supersede_running(
                        operation.operation_id, landed_commit=landed,
                        reason="batch settled by another synchronized queue owner",
                    )
        record["completed_elsewhere"] = True
        return self._finish(record, IsolatedWriteResult(
            output="batch already settled by the synchronized queue", commits_landed=0,
            landed_commit=landed,
        ))

    def _settle_terminal_active(self) -> dict | None:
        record = self._active()
        if record is None:
            return None
        if record.get("receipt") is not None:
            _write_json(self.root / "active.json", {"task_id": None})
            return record["receipt"]
        operation = self._operation(record)
        if operation is not None and operation.phase in FINAL_PHASES:
            return self._finish_terminal(record, operation)
        return None

    def _finish_terminal(self, record: dict, operation) -> dict:
        outcome = operation.outcome
        if outcome is None:
            raise RuntimeError("terminal assistant operation has no durable outcome")
        if outcome.metadata.get("superseded"):
            record["completed_elsewhere"] = True
        return self._finish(record, IsolatedWriteResult(
            output=outcome.output, commits_landed=int(operation.phase == "committed"),
            start_commit=outcome.start_commit, landed_commit=outcome.landed_commit or "",
            changed_paths=outcome.changed_paths, operation_id=operation.operation_id,
        ))

    def _operation(self, record: dict):
        try:
            task = self.writer.read(record["task_id"])
        except FileNotFoundError:
            return None
        return SemanticOperationStore(self.memory_root).read(task.operation_id)

    def _publish_outbox(self) -> dict:
        if not self.sync_config.enabled:
            return {"status": "local"}
        try:
            coordinator = self._coordinator()
            result = coordinator.publish_outbox()
            if result.settled_uids:
                pulled = SyncManager(self.sync_config).pull()
                if pulled.status not in {"synced", "ahead"}:
                    # Keep the local evidence until its authoritative queue
                    # state is also visible to native Retrieve and notices.
                    return {**asdict(result), "status": "pending", "sync": asdict(pulled)}
            self.store.acknowledge_synchronized(set(result.settled_uids))
            coordinator.clear_local_candidates(result.settled_uids)
            return asdict(result)
        except (OSError, RuntimeError, ValueError) as exc:
            # Evidence is durable before network publication. A later normal
            # call retries publication using the same candidate identity.
            return {"status": "pending", "error": str(exc)}

    def _response(self, result: dict) -> dict:
        response = {"ok": True, "active_root": str(self.memory_root), **result}
        try:
            response["update"] = self._notice()
        except (OSError, ValueError, RuntimeError) as exc:
            response["update"] = {"ready": False, "status": "error", "error": str(exc)}
        return response

    def _notice(self) -> dict:
        active = self._active()
        if active is not None and active.get("receipt") is None:
            expired = _time(active["expires_at"]) <= _now()
            if not expired:
                return {"ready": False, "status": "active", "task_id": active["task_id"]}
            notice = {"ready": True, "status": "resume", "task_id": active["task_id"]}
        else:
            if self.sync_config.enabled:
                candidates, deadline = self._coordinator().ready_candidates(**self.batch_options)
            else:
                batches, deadline = self.store.ready_batch(**self.batch_options)
                candidates = tuple(
                    self.store._candidate_from_job(batch.session_id, job)
                    for batch in batches or () for job in batch.jobs
                )
            if not candidates:
                return {"ready": False, "status": "waiting",
                        "next_ready_at": None if deadline is None else deadline.isoformat()}
            notice = {"ready": True, "status": "ready", "batch_id": update_candidate_batch_id(candidates),
                      "candidate_count": len(candidates)}
        notice["begin_argv"] = self._argv("update", "begin")
        notice["instruction"] = (
            "If you are a child agent or task, relay this entire update notice to the main agent "
            "alongside your result. Main agent: delegate one Update child using the returned "
            "begin command (or rightmemory_assistant_begin_update), then its instructions_file "
            "and candidates_file. An active task must be resumed, not delegated twice. "
            "Do not poll for readiness."
        )
        return notice

    def _completed_candidates(self) -> set[str]:
        return {
            item["uid"] for path in (self.root / "tasks").glob("*.json")
            for record in [_read_json(path)] if record is not None and record.get("receipt") is not None
            for item in record["candidates"]
        }

    def _check_current(self, task_id: str) -> None:
        if self._active_id() != task_id or self._task(task_id)["status"] != "running":
            raise AssistantClaimLost("assistant task was replaced; its edits cannot be completed")

    def _active_id(self) -> str | None:
        record = _read_json(self.root / "active.json")
        return None if record is None else record["task_id"]

    def _active(self) -> dict | None:
        task_id = self._active_id()
        return None if task_id is None else self._task(task_id)

    def _task(self, task_id: str) -> dict:
        if not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
            raise ValueError("task_id must be a task identifier returned by update begin")
        record = _read_json(self.root / "tasks" / f"{task_id}.json")
        if record is None:
            raise FileNotFoundError(f"assistant task does not exist: {task_id}")
        return record

    def _save_task(self, record: dict) -> None:
        _write_json(self.root / "tasks" / f"{record['task_id']}.json", record)

    def _coordinator(self) -> GitUpdateQueueCoordinator:
        coordinator = GitUpdateQueueCoordinator(self.sync_config)
        # The old worker resumes leases held by its device. Give the native
        # executor its own stable holder so it cannot share that live claim.
        coordinator.device_id = hashlib.sha256(
            f"{coordinator.device_id}:assistant".encode("utf-8")
        ).hexdigest()[:32]
        return coordinator

    @staticmethod
    def _candidates(record: dict):
        return tuple(parse_update_candidate_data(item) for item in record["candidates"])

    def _claim(self, record: dict) -> ClaimedUpdateBatch:
        data = record["claim"]
        return ClaimedUpdateBatch(
            parse_update_queue_lease_json(json.dumps({"schema_version": SCHEMA_VERSION, **data["lease"]})),
            self._candidates(record), data["lease_commit"],
        )

    def _argv(self, *args: str) -> list[str]:
        return ["rightmemory", "assistant", "--root", str(self.memory_root), *args]

    @contextmanager
    def _locked(self):
        _check_path(self.root / "state.lock")
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "state.lock").open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _read_json(path: Path) -> dict | None:
    _check_path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"assistant state is not a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    _check_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _check_path(path: Path) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise ValueError(f"assistant runtime path must not contain a symbolic link: {path}")
