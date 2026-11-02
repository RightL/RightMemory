"""Host-owned semantic sync repair over the shared sync transaction boundary."""
from __future__ import annotations

import re
import shlex
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import load_sync_config
from .semantic_operation import FINAL_PHASES, SemanticOperationRecord, SemanticOperationStore
from .session import MemoryWriteLock
from .sync import SyncManager, SyncRepairStaleError, SyncResult, read_sync_state


class NativeSyncRepair:
    """Persist a repair between host calls; never create a model or host worker."""

    def __init__(self, memory_root: Path):
        self.root = Path(memory_root).expanduser().resolve()
        self.manager = SyncManager(load_sync_config(self.root))
        self.store = SemanticOperationStore(self.root)

    def begin(self, retry_task: str | None = None) -> dict[str, Any]:
        # Serialize preparation (including fetch), never the host's editing turn.
        with self.store.execution_locked():
            result = self._begin_locked(retry_task)
        if result.get("applied"):
            return self.complete(result["task_id"])
        return result

    def _begin_locked(self, retry_task: str | None) -> dict[str, Any]:
        # Resuming an existing task does not require a working network.
        with MemoryWriteLock(self.root):
            active = self.manager.native_repair_record()
            if retry_task is None and active is not None:
                return self._handoff(active, claimed=False)
            if retry_task is not None:
                requested = self._task(retry_task)
                if requested.phase == "committed":
                    return self._receipt(requested)
                if active is None or active.operation_id != retry_task:
                    raise ValueError("only the current native sync repair can be replaced")
                # A lost completion reply is not permission to redo semantic work.
                if active.phase == "prepared":
                    try:
                        self.manager._recover_repair(self.store, active)
                    except SyncRepairStaleError:
                        pass
                    else:
                        return self._receipt(self._task(retry_task))

        if not self.manager.config.enabled:
            return self._not_needed(SyncResult("disabled", "sync disabled"))
        upstream = self.manager._upstream()
        if upstream is None:
            return self._blocked(SyncResult("unconfigured", "sync unconfigured"))
        if self.manager._git("fetch").returncode:
            return self._blocked(SyncResult("offline", "sync offline: git fetch failed"))
        incoming = self.manager._resolve_commit(upstream)
        if incoming is None:
            return self._blocked(SyncResult("error", "upstream commit is unavailable"))

        with MemoryWriteLock(self.root):
            active = self.manager.native_repair_record()
            if retry_task is None and active is not None:
                return self._handoff(active, claimed=False)
            retained = None
            if retry_task is not None:
                if active is None or active.operation_id != retry_task:
                    raise ValueError("native sync repair changed while preparing its replacement")
                # The host confirms its old worker ended before asking to replace.
                # Preserve unpublished files, but permanently fence their admission.
                retained = str(self.manager._recorded_candidate_identity(active)[1])
                head = self.manager._required_head(self.root)
                if active.phase == "prepared":
                    self.store.claim_prepared(retry_task)
                    self.store.supersede_prepared(
                        retry_task, expected_metadata=active.outcome.metadata, landed_commit=head,
                    )
                else:
                    self.store.supersede_running(
                        retry_task, landed_commit=head, reason="native sync repair explicitly replaced",
                    )
            prepared = self.manager._prepare_incoming_repair(incoming, self.store, executor="native")
            if isinstance(prepared, SyncResult):
                result = self._not_needed(prepared) if prepared.status == "synced" else self._blocked(prepared)
            else:
                try:
                    result = self._handoff(prepared.record, claimed=True)
                finally:
                    # CLI lifetime is not native worker lifetime. The durable
                    # operation, not this process lease, fences ordinary sync.
                    prepared.candidate.lease.release()
            if retained is not None:
                result["retained_worktree"] = retained
            return result

    def complete(self, task_id: str) -> dict[str, Any]:
        with self.store.execution_locked(), MemoryWriteLock(self.root):
            record = self._task(task_id)
            if record.phase not in FINAL_PHASES:
                active = self.manager.native_repair_record()
                if active is None or active.operation_id != task_id:
                    raise ValueError("native sync repair is no longer current")
                if record.phase == "prepared":
                    self.manager._recover_repair(self.store, record)
                else:
                    record = self.store.begin(task_id, record.input_data)
                    candidate = self._open(record)
                    try:
                        if self.manager._required_head(candidate.path) == record.input_data["pre_repair_tip"]:
                            raise ValueError("repair is not committed; keep the existing task and worktree")
                        if self.manager._required_head(self.root) != record.input_data["active_start_commit"]:
                            raise SyncRepairStaleError("active HEAD changed; reconcile the task against current state")
                        self.manager._finish_repair(
                            self.store, record, candidate, self._diagnostic(record),
                        )
                    finally:
                        candidate.lease.release()
                record = self._task(task_id)
            receipt = self._receipt(record)
            if record.phase == "committed":
                directory = self.root / ".runtime" / "sync" / "tasks" / task_id
                try:
                    self._check_paths(directory / "instructions.md")
                    (directory / "instructions.md").unlink(missing_ok=True)
                    if directory.is_dir():
                        directory.rmdir()
                except (OSError, ValueError) as exc:
                    receipt["cleanup_warning"] = str(exc)
        # Local admission and remote transport are different outcomes. A failed
        # push retries the transport, never the already committed semantic repair.
        if record.phase == "committed":
            receipt["sync"] = asdict(self.manager.push())
        receipt["sync_repair"] = self.notice()
        return receipt

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        with self.store.execution_locked():
            record = self._task(task_id) if task_id else self.manager.native_repair_record()
            if record is None:
                return {"ok": True, "status": "idle", "sync_repair": self.notice()}
            if record.phase in FINAL_PHASES:
                return self._receipt(record)
            return {"ok": True, **self._task_details(record), "sync_repair": self.notice()}

    def validate(self, task_id: str) -> dict[str, Any]:
        """Validate candidate state without applying it or completing its task."""
        with self.store.execution_locked():
            record = self._task(task_id)
            if record.phase != "running":
                raise ValueError("only an editing repair task can be validated")
            candidate = self._open(record)
            try:
                invalid = self.manager._validate_candidate(
                    candidate.path, record.input_data["active_start_commit"],
                    self.manager._required_head(candidate.path),
                )
                return {
                    "ok": invalid is None, "task_id": task_id,
                    "status": "valid" if invalid is None else "invalid",
                    "validation": None if invalid is None else asdict(invalid),
                }
            finally:
                candidate.lease.release()

    def notice(self) -> dict[str, Any] | None:
        record = self.manager.native_repair_record()
        if record is not None:
            return self._task_details(record)
        state = read_sync_state(self.root)
        if state.get("last_repairable"):
            return {
                "status": "required", "files": state.get("last_files", []),
                "begin_argv": self._argv("begin"),
            }
        return None

    def _handoff(self, record: SemanticOperationRecord, *, claimed: bool) -> dict[str, Any]:
        from .prompt import build_cli_agent_instructions

        _branch, worktree, _lease = self.manager._recorded_candidate_identity(record)
        directory = self.root / ".runtime" / "sync" / "tasks" / record.operation_id
        self._check_paths(directory)
        directory.mkdir(parents=True, exist_ok=True)
        instructions = directory / "instructions.md"
        self._check_paths(instructions)
        controls = self._task_details(record)
        canonical = build_cli_agent_instructions(worktree, "sync-reconciler")
        instructions.write_text(
            f"{canonical}\n\nNative sync repair task: {record.operation_id}\n"
            f"Active root for control commands: {self.root}\n"
            f"Candidate worktree: {worktree}\n"
            f"Local starting commit: {record.input_data['active_start_commit']}\n"
            f"Incoming commit: {record.input_data['upstream_commit']}\n"
            f"Diagnostic: {self._diagnostic(record).message}\n"
            f"Affected files: {', '.join(self._diagnostic(record).files)}\n\n"
            "Use native file and Git tools in the candidate. Both parents and their common "
            "ancestor are available in its Git history. Keep program-owned coordination "
            "records unchanged; resolve only the supplied semantic repair.\n"
            f"Validate with {shlex.join(controls['validate_argv'])}, then commit the repair. "
            f"After committing, call {shlex.join(controls['complete_argv'])}. "
            "This control command is the handoff to program validation and publication, "
            "not permission to push or modify the active root yourself. "
            "Report its local application and remote sync outcomes separately. "
            "The program's receipt, not your worktree commit, confirms application.\n"
            "A lost completion reply uses the same task's status and completion controls. "
            "Relay stale-base or unresolved semantic issues to the main agent. It owns "
            "worker recovery and explicit replacement; do not launch another repair worker.\n",
            encoding="utf-8",
        )
        return {"ok": True, **controls, "status": "prepared" if claimed else "active",
                "claimed": claimed, "instructions_file": str(instructions)}

    def _task_details(self, record: SemanticOperationRecord) -> dict[str, Any]:
        _branch, worktree, _lease = self.manager._recorded_candidate_identity(record)
        return {
            "status": "active", "operation_phase": record.phase, "task_id": record.operation_id,
            "worktree": str(worktree), "active_root": str(self.root),
            "base_commit": record.input_data["active_start_commit"],
            "upstream_commit": record.input_data["upstream_commit"],
            "files": self._diagnostic(record).files,
            "begin_argv": self._argv("begin"),
            "status_argv": self._argv("status", "--task", record.operation_id),
            "validate_argv": self._argv("validate", "--task", record.operation_id),
            "complete_argv": self._argv("complete", "--task", record.operation_id),
            "retry_argv": self._argv("begin", "--retry", record.operation_id),
        }

    def _task(self, task_id: str) -> SemanticOperationRecord:
        if not isinstance(task_id, str) or re.fullmatch(r"sync-repair-[0-9a-f]{64}", task_id) is None:
            raise ValueError("invalid native sync repair task id")
        record = self.store.read(task_id)
        if record is None or record.input_data.get("executor") != "native":
            raise ValueError("native sync repair task does not exist")
        return record

    def _open(self, record: SemanticOperationRecord):
        branch, worktree, _lease = self.manager._recorded_candidate_identity(record)
        self._check_paths(worktree)
        # Do not reconstruct an absent editing workspace and silently lose its
        # uncommitted work. Explicit replacement is the recovery for lost files.
        if not worktree.is_dir():
            raise FileNotFoundError("native sync worktree is missing; inspect status and explicitly replace its task")
        candidate = self.manager._open_recorded_candidate(record)
        try:
            git = self.manager._git_stdout
            common = (worktree / git(worktree, "rev-parse", "--git-common-dir")).resolve()
            active_common = (self.root / git(self.root, "rev-parse", "--git-common-dir")).resolve()
            if (common != active_common or git(worktree, "symbolic-ref", "--short", "HEAD") != branch
                    or Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree):
                raise RuntimeError("native sync worktree identity changed")
            return candidate
        except Exception:
            candidate.lease.release()
            raise

    def _receipt(self, record: SemanticOperationRecord) -> dict[str, Any]:
        outcome = record.outcome
        if outcome is None or record.phase not in FINAL_PHASES:
            raise ValueError("native sync repair has no completion receipt")
        superseded = bool(outcome.metadata.get("superseded"))
        return {
            "ok": True, "task_id": record.operation_id,
            "status": "superseded" if superseded else "completed",
            "applied": record.phase == "committed",
            "landed_commit": outcome.landed_commit if record.phase == "committed" else None,
            "changed_paths": list(outcome.changed_paths), "active_root": str(self.root),
        }

    def _not_needed(self, result: SyncResult) -> dict[str, Any]:
        self.manager._record_success("pull", result)
        return {"ok": True, "status": "not_needed", "sync": asdict(result)}

    def _blocked(self, result: SyncResult) -> dict[str, Any]:
        self.manager._record_failure(result)
        return {"ok": False, "status": "blocked", "sync": asdict(result)}

    @staticmethod
    def _diagnostic(record: SemanticOperationRecord) -> SyncResult:
        return SyncResult(**record.input_data["repair_diagnostic"])

    def _argv(self, *args: str) -> list[str]:
        return ["rightmemory", "assistant", "--root", str(self.root), "sync-repair", *args]

    def _check_paths(self, path: Path) -> None:
        current = self.root
        for part in path.relative_to(self.root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("native sync runtime path must not traverse symbolic links")
