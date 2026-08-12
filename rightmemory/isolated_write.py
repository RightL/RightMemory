from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .platform import process_exists, process_identity
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory
from .semantic_operation import (
    FINAL_PHASES,
    OperationEffect,
    SemanticOperationRecord,
    SemanticOperationStore,
)
from .graph import MEMORY_DETAIL_FILE_RE, PURSUIT_DETAIL_FILE_RE
from .tools import (
    CORRECTIONS_PATH,
    FIXED_CORRECTION_COLLECTION_PATHS,
    INSIGHT_LOG_FILE_RE,
    SHARED_VIEW_DEFINITION_FILE_RE,
    SHARE_REGISTRY_PATH,
    SHARED_VIEW_REGISTRY_PATH,
    MemoryTools,
)
from .update_coordination import UPDATE_RECORD_PATH_RE
from .update_record import validate_update_records


GIT_TIMEOUT_SECONDS = 30
OPERATION_TRAILER = "RightMemory-Operation"
ACTIVE_MEMORY_WRITE_PATHS = ("MEMORY.md", "MEMORY_*.md")
ACTIVE_PURSUIT_WRITE_PATHS = ("PURSUITS.md", "PURSUIT_*.md")
PROTECTED_RIGHTMEMORY_PATHS = (
    *ACTIVE_MEMORY_WRITE_PATHS,
    *ACTIVE_PURSUIT_WRITE_PATHS,
    CORRECTIONS_PATH,
)
INSIGHT_WRITE_PATHS = ("insight_logs/*.md",)
LEGACY_ROOT_REFERENCE_PATHS = frozenset(
    {"PURSUIT_RULES.md", "AGENT_CORRECTION_MEMORY_RULES.md"}
)
ROLE_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
TEMP_BRANCH_PREFIX = "rightmemory-isolated-"
WORKTREE_LEASE_DIR = "worktree-leases"


class WorktreeLease:
    """PID-identity lease shared by every temporary RightMemory worktree."""

    def __init__(self, memory_root: Path, role: str, identifier: str):
        self.memory_root = Path(memory_root).resolve()
        self.role = _safe_role_slug(role)
        self.identifier = identifier.strip()
        if not self.identifier or any(character in self.identifier for character in "/\\\0\r\n"):
            raise ValueError("worktree lease identifier must be one safe path segment")
        self.path = (
            self.memory_root
            / ".runtime"
            / WORKTREE_LEASE_DIR
            / f"{self.role}-{self.identifier}.json"
        )
        self._owned = False

    def acquire(self) -> None:
        if _worktree_lease_is_live(self.path) and not _worktree_lease_owned_by_current_process(
            self.path
        ):
            raise RuntimeError(f"temporary worktree is owned by another live process: {self.identifier}")
        _write_worktree_lease(self.path)
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        if _worktree_lease_owned_by_current_process(self.path):
            self.path.unlink(missing_ok=True)
        self._owned = False

    def is_live(self) -> bool:
        return _worktree_lease_is_live(self.path)


class MainMemoryDirtyError(RuntimeError):
    def __init__(self, paths: list[str]):
        self.paths = tuple(paths)
        detail = ", ".join(paths) if paths else "memory files"
        super().__init__(f"main memory files have uncommitted changes: {detail}")


@dataclass(frozen=True)
class IsolatedWriteResult:
    output: Any
    commits_landed: int
    start_commit: str = ""
    landed_commit: str = ""
    changed_paths: tuple[str, ...] = ()
    operation_id: str | None = None
    candidate_commit: str | None = None
    prepared: bool = False


class IsolatedWriteSupervisor:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = Path(memory_root).resolve()
        self.role = role

    def recover_prepared(self) -> None:
        """Finish durable outcomes before a caller starts another model turn."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            self._drain_prepared_operations(store)

    def complete_external(
        self,
        operation_id: str,
        *,
        external_finalizer: str,
        landed_commit: str,
    ) -> IsolatedWriteResult:
        """Complete a prepared operation only after its external publication lands."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            operation = store.read(operation_id)
            if operation is None:
                raise FileNotFoundError(f"semantic operation does not exist: {operation_id}")
            self._require_external_finalizer(operation, external_finalizer)
            if operation.phase in FINAL_PHASES:
                return self._result_from_operation(operation)
            operation = store.claim_prepared(operation_id)
            current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
            if not self._is_ancestor(landed_commit, current_head):
                raise RuntimeError("externally finalized commit is not present in the active checkout")
            outcome = operation.outcome
            if outcome is None:
                raise RuntimeError(f"prepared operation has no outcome: {operation_id}")
            if outcome.metadata.get("candidate_commit") is None:
                completed = store.complete_no_change(operation_id, landed_commit)
            else:
                completed = store.complete_commit(operation_id, landed_commit)
            self._delete_operation_ref(operation_id)
            return self._result_from_operation(completed)

    def restart_external(
        self,
        operation_id: str,
        *,
        external_finalizer: str,
        reason: str,
    ) -> None:
        """Discard a stale prepared result while retaining its durable identity."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            operation = store.claim_prepared(operation_id)
            self._require_external_finalizer(operation, external_finalizer)
            store.restart_prepared(
                operation_id,
                expected_metadata={"external_finalizer": external_finalizer},
                reason=reason,
            )
            self._delete_operation_ref(operation_id)

    def supersede_external(
        self,
        operation_id: str,
        *,
        external_finalizer: str,
        landed_commit: str,
    ) -> None:
        """Settle a stale prepared result after another fenced owner publishes."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            operation = store.claim_prepared(operation_id)
            self._require_external_finalizer(operation, external_finalizer)
            current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
            if not self._is_ancestor(landed_commit, current_head):
                raise RuntimeError("superseding commit is not present in the active checkout")
            store.supersede_prepared(
                operation_id,
                expected_metadata={"external_finalizer": external_finalizer},
                landed_commit=landed_commit,
            )
            self._delete_operation_ref(operation_id)

    def supersede_running(
        self,
        operation_id: str,
        *,
        landed_commit: str,
        reason: str,
    ) -> None:
        """Settle abandoned execution after Git proves its batch is terminal."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
            if not self._is_ancestor(landed_commit, current_head):
                raise RuntimeError("superseding commit is not present in the active checkout")
            store.supersede_running(
                operation_id,
                landed_commit=landed_commit,
                reason=reason,
            )
            self._delete_operation_ref(operation_id)

    def run(
        self,
        run_in_worktree: Callable[[Path], Any],
        *,
        operation_id: str | None = None,
        operation_input: Mapping[str, Any] | None = None,
        effects_for_outcome: Callable[[tuple[str, ...], int], Iterable[OperationEffect]] | None = None,
        prepare_effects: Callable[[str, Path], None] | None = None,
        prepare_managed_artifacts: Callable[
            [Path, str, str | None, tuple[str, ...], Any], Iterable[str]
        ]
        | None = None,
        external_finalizer: str | None = None,
    ) -> IsolatedWriteResult:
        if external_finalizer is not None:
            if not isinstance(external_finalizer, str) or not external_finalizer.strip():
                raise ValueError("external_finalizer must be a non-empty string")
            if operation_id is None:
                raise ValueError("external_finalizer requires operation_id")
        if operation_id is None:
            return self._run_claimed(
                run_in_worktree,
                operation_input=operation_input,
                effects_for_outcome=effects_for_outcome,
                prepare_effects=prepare_effects,
                prepare_managed_artifacts=prepare_managed_artifacts,
                external_finalizer=external_finalizer,
            )
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            self._drain_prepared_operations(store)
            return self._run_claimed(
                run_in_worktree,
                operation_id=operation_id,
                operation_input=operation_input,
                effects_for_outcome=effects_for_outcome,
                prepare_effects=prepare_effects,
                prepare_managed_artifacts=prepare_managed_artifacts,
                external_finalizer=external_finalizer,
            )

    def _run_claimed(
        self,
        run_in_worktree: Callable[[Path], Any],
        *,
        operation_id: str | None = None,
        operation_input: Mapping[str, Any] | None = None,
        effects_for_outcome: Callable[[tuple[str, ...], int], Iterable[OperationEffect]] | None = None,
        prepare_effects: Callable[[str, Path], None] | None = None,
        prepare_managed_artifacts: Callable[
            [Path, str, str | None, tuple[str, ...], Any], Iterable[str]
        ]
        | None = None,
        external_finalizer: str | None = None,
    ) -> IsolatedWriteResult:
        self._ensure_repo_root()
        operation_store: SemanticOperationStore | None = None
        if operation_id is not None:
            operation_store = SemanticOperationStore(self.memory_root)
            input_data = dict(operation_input or {})
            input_data["role"] = self.role
            operation = operation_store.begin(
                operation_id,
                input_data,
            )
            if operation.phase in FINAL_PHASES:
                self._delete_operation_ref(operation_id)
                return self._result_from_operation(operation)
            if operation.phase == "prepared":
                recorded_finalizer = operation.outcome.metadata.get("external_finalizer")
                if recorded_finalizer is not None:
                    if recorded_finalizer != external_finalizer:
                        raise RuntimeError(
                            f"prepared operation requires external finalizer: {recorded_finalizer}"
                        )
                    return self._result_from_operation(operation)
                return self._resume_prepared_operation(operation_store, operation)
            self._delete_operation_ref(operation_id)

        dirty = self._dirty_memory_files()
        if dirty:
            raise MainMemoryDirtyError(dirty)

        start_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
        role_slug = _safe_role_slug(self.role)
        identifier = uuid.uuid4().hex
        branch = f"{TEMP_BRANCH_PREFIX}{role_slug}-{identifier}"
        worktree = self.memory_root / ".runtime" / "worktrees" / f"{role_slug}-{identifier}"
        lease = WorktreeLease(self.memory_root, role_slug, identifier)

        self._ensure_runtime_ignored(worktree)
        lease.acquire()
        try:
            self._run_git(self.memory_root, "worktree", "add", "-b", branch, str(worktree), start_head)
            self._seed_untracked_publish_artifacts(worktree)
            output = run_in_worktree(worktree)
            status = self._git_stdout(worktree, "status", "--porcelain")
            if status:
                raise RuntimeError(f"isolated worktree has uncommitted changes:\n{status}")

            commits = self._temp_commits(worktree, start_head)
            if commits:
                if operation_id is not None:
                    commits = [self._collapse_operation_commits(worktree, start_head, commits, operation_id)]
                self._validate_candidate(worktree, commits)

            changed_paths = tuple(
                sorted({path for commit in commits for path in self._commit_paths(worktree, commit)})
            )

            with MemoryWriteLock(self.memory_root):
                dirty = self._dirty_memory_files()
                if dirty:
                    raise MainMemoryDirtyError(dirty)
                current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
                if current_head != start_head:
                    if operation_store is None or operation_id is None:
                        raise RuntimeError("main HEAD changed during isolated memory write")
                    semantic_changes = self._semantic_changes_between(start_head, current_head)
                    if semantic_changes:
                        detail = ", ".join(semantic_changes)
                        raise RuntimeError(
                            "main semantic state changed during isolated memory write: " + detail
                        )
                    if commits:
                        commits = [self._rebase_operation_commit(worktree, commits[0], current_head)]
                        self._validate_candidate(worktree, commits)
                    else:
                        self._run_git(worktree, "reset", "--hard", current_head)
                    start_head = current_head
                    changed_paths = tuple(
                        sorted({path for commit in commits for path in self._commit_paths(worktree, commit)})
                    )
                managed_paths: tuple[str, ...] = ()
                if prepare_managed_artifacts is not None:
                    managed_paths = self._prepare_managed_artifacts(
                        worktree,
                        start_head,
                        commits[0] if commits else None,
                        changed_paths,
                        output,
                        prepare_managed_artifacts,
                        operation_id=operation_id,
                    )
                    if managed_paths:
                        commits = [self._git_stdout(worktree, "rev-parse", "HEAD")]
                        self._validate_candidate(
                            worktree,
                            commits,
                            managed_paths=frozenset(managed_paths),
                        )
                if operation_store is not None and operation_id is not None:
                    if commits:
                        self._pin_operation_ref(operation_id, commits[0])
                    if prepare_effects is not None:
                        prepare_effects(operation_id, worktree)
                    effects = () if effects_for_outcome is None else tuple(
                        effects_for_outcome(changed_paths, len(commits))
                    )
                    operation_store.prepare_outcome(
                        operation_id,
                        output=_output_text(output),
                        start_commit=start_head,
                        changed_paths=changed_paths,
                        effects=effects,
                        metadata={
                            "candidate_commit": commits[0] if commits else None,
                            **(
                                {"external_finalizer": external_finalizer}
                                if external_finalizer is not None
                                else {}
                            ),
                        },
                    )
                if external_finalizer is not None:
                    return IsolatedWriteResult(
                        output=output,
                        commits_landed=0,
                        start_commit=start_head,
                        changed_paths=changed_paths,
                        operation_id=operation_id,
                        candidate_commit=commits[0] if commits else None,
                        prepared=True,
                    )
                if commits:
                    if operation_store is None:
                        self._land_commits(commits)
                    else:
                        self._land_operation_commit(commits[0])
                landed_commit = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
                if operation_store is not None and operation_id is not None:
                    if commits:
                        operation_store.complete_commit(operation_id, landed_commit)
                    else:
                        operation_store.complete_no_change(operation_id, landed_commit)
                    self._delete_operation_ref(operation_id)
            return IsolatedWriteResult(
                output=output,
                commits_landed=len(commits),
                start_commit=start_head,
                landed_commit=landed_commit,
                changed_paths=changed_paths,
                operation_id=operation_id,
                candidate_commit=commits[0] if commits else None,
            )
        except Exception as exc:
            if operation_store is not None and operation_id is not None:
                record = operation_store.read(operation_id)
                if record is not None and record.phase not in FINAL_PHASES:
                    operation_store.record_failure(operation_id, f"{type(exc).__name__}: {exc}")
                    if record.phase == "running":
                        self._delete_operation_ref(operation_id)
            raise
        finally:
            try:
                self._cleanup(worktree, branch)
            finally:
                lease.release()

    def _resume_prepared_operation(
        self,
        store: SemanticOperationStore,
        operation: SemanticOperationRecord,
    ) -> IsolatedWriteResult:
        outcome = operation.outcome
        if outcome is None:
            raise RuntimeError(f"prepared operation has no outcome: {operation.operation_id}")
        candidate = outcome.metadata.get("candidate_commit")
        with MemoryWriteLock(self.memory_root):
            current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
            if candidate is not None:
                if not isinstance(candidate, str) or not candidate:
                    raise RuntimeError(f"prepared operation has no candidate commit: {operation.operation_id}")
                if not self._is_ancestor(candidate, current_head):
                    recovered_commit = self._find_landed_operation_commit(
                        operation.operation_id,
                        outcome.start_commit,
                        current_head,
                    )
                    if recovered_commit is not None:
                        landed_commit = recovered_commit
                    else:
                        dirty = self._dirty_memory_files()
                        if dirty:
                            raise MainMemoryDirtyError(dirty)
                        if current_head == outcome.start_commit:
                            self._land_operation_commit(candidate)
                        else:
                            semantic_changes = self._semantic_changes_between(
                                outcome.start_commit,
                                current_head,
                            )
                            if semantic_changes:
                                detail = ", ".join(semantic_changes)
                                raise RuntimeError(
                                    "main semantic state changed before prepared memory write could land: "
                                    + detail
                                )
                            self._land_commits([candidate])
                        landed_commit = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
                else:
                    landed_commit = candidate
                commits_landed = 1
            else:
                if current_head != outcome.start_commit:
                    semantic_changes = self._semantic_changes_between(
                        outcome.start_commit,
                        current_head,
                    )
                    if semantic_changes:
                        detail = ", ".join(semantic_changes)
                        raise RuntimeError(
                            "main semantic state changed before prepared no-change outcome completed: "
                            + detail
                        )
                landed_commit = outcome.start_commit
                commits_landed = 0
            if candidate is not None:
                store.complete_commit(operation.operation_id, landed_commit)
            else:
                store.complete_no_change(operation.operation_id, landed_commit)
        completed = store.read(operation.operation_id)
        if completed is None:
            raise RuntimeError(f"semantic operation disappeared: {operation.operation_id}")
        self._delete_operation_ref(operation.operation_id)
        return IsolatedWriteResult(
            output=outcome.output,
            commits_landed=commits_landed,
            start_commit=outcome.start_commit,
            landed_commit=landed_commit,
            changed_paths=outcome.changed_paths,
            operation_id=operation.operation_id,
            candidate_commit=candidate if isinstance(candidate, str) else None,
        )

    def _drain_prepared_operations(self, store: SemanticOperationStore) -> None:
        prepared = [
            record
            for record in store.list_outstanding_records()
            if record.phase == "prepared" and record.outcome is not None
        ]
        prepared.sort(key=lambda record: (record.outcome.sequence, record.operation_id))
        for record in prepared:
            if record.input_data.get("kind") == "sync-repair":
                # Sync owns candidate validation and exact fast-forward publication.
                continue
            if record.outcome.metadata.get("external_finalizer") is not None:
                # The external owner must prove publication before this can land.
                continue
            role = record.input_data.get("role")
            if not isinstance(role, str):
                raise RuntimeError(
                    f"prepared operation has invalid routing data: {record.operation_id}"
                )
            claimed = store.claim_prepared(record.operation_id)
            supervisor = IsolatedWriteSupervisor(self.memory_root, role)
            supervisor._resume_prepared_operation(store, claimed)

    def _result_from_operation(self, operation: SemanticOperationRecord) -> IsolatedWriteResult:
        outcome = operation.outcome
        if outcome is None:
            raise RuntimeError(f"completed operation has no outcome: {operation.operation_id}")
        candidate = outcome.metadata.get("candidate_commit")
        return IsolatedWriteResult(
            output=outcome.output,
            commits_landed=1 if operation.phase == "committed" else 0,
            start_commit=outcome.start_commit,
            landed_commit=outcome.landed_commit or outcome.start_commit,
            changed_paths=outcome.changed_paths,
            operation_id=operation.operation_id,
            candidate_commit=candidate if isinstance(candidate, str) else None,
            prepared=operation.phase == "prepared",
        )

    def _require_external_finalizer(
        self,
        operation: SemanticOperationRecord,
        expected: str,
    ) -> None:
        outcome = operation.outcome
        actual = None if outcome is None else outcome.metadata.get("external_finalizer")
        if actual != expected:
            raise RuntimeError(
                f"operation {operation.operation_id} is not owned by external finalizer {expected}"
            )

    def cleanup_stale(self) -> None:
        role_slug = _safe_role_slug(self.role)
        runtime_worktrees = self.memory_root / ".runtime" / "worktrees"
        for worktree, identifier in self._stale_worktrees(runtime_worktrees, role_slug):
            self._run_git(self.memory_root, "worktree", "remove", "--force", str(worktree), check=False)
            self._lease_path(role_slug, identifier).unlink(missing_ok=True)

        self._run_git(self.memory_root, "worktree", "prune", check=False)

        for branch in self._temp_branches(role_slug):
            identifier = _temp_identifier_for_role(branch, role_slug)
            if identifier is None or self._lease_is_live(role_slug, identifier):
                continue
            self._run_git(self.memory_root, "branch", "-D", branch, check=False)
            self._lease_path(role_slug, identifier).unlink(missing_ok=True)

        self._cleanup_orphaned_leases(role_slug)

    def _ensure_repo_root(self) -> None:
        top_level = self._git_stdout(self.memory_root, "rev-parse", "--show-toplevel")
        if Path(top_level).resolve() != self.memory_root:
            raise RuntimeError(f"memory root is not the git repository root: {self.memory_root}")

    def _ensure_runtime_ignored(self, worktree: Path) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        relative_path = worktree.relative_to(self.memory_root).as_posix()
        result = self._run_git(self.memory_root, "check-ignore", "-q", relative_path, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"runtime worktree path is not ignored by git: {relative_path}")

    def _seed_untracked_publish_artifacts(self, worktree: Path) -> None:
        views_root = self.memory_root / "shared_views"
        if not views_root.is_dir():
            return
        for source in sorted(views_root.glob("*/dist")):
            if not source.is_dir():
                continue
            destination = worktree / source.relative_to(self.memory_root)
            shutil.copytree(source, destination, dirs_exist_ok=True)

    def _dirty_memory_files(self) -> list[str]:
        status = self._git_stdout(self.memory_root, "status", "--porcelain", "--", *self._write_paths())
        return [
            path
            for path in _porcelain_paths(status)
            if path not in LEGACY_ROOT_REFERENCE_PATHS
        ]

    def _write_paths(self) -> tuple[str, ...]:
        if self.role == "insight":
            return (*PROTECTED_RIGHTMEMORY_PATHS, *INSIGHT_WRITE_PATHS)
        return PROTECTED_RIGHTMEMORY_PATHS

    def _temp_commits(self, worktree: Path, start_head: str) -> list[str]:
        output = self._git_stdout(worktree, "rev-list", "--reverse", f"{start_head}..HEAD")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _validate_commits(
        self,
        worktree: Path,
        commits: list[str],
        *,
        managed_paths: frozenset[str] = frozenset(),
    ) -> None:
        if self.role == "update" and len(commits) > 1:
            raise RuntimeError(f"one {self.role} turn must land at most one commit")
        for commit in commits:
            changed_paths = self._commit_paths(worktree, commit)
            if not changed_paths:
                self._validate_empty_commit(worktree, commit)
            invalid_paths = {
                path
                for path in changed_paths
                if path not in managed_paths and not self._is_role_write_path(path)
            }
            if invalid_paths:
                paths = ", ".join(sorted(invalid_paths))
                label = "non-insight paths" if self.role == "insight" else "non-memory paths"
                raise RuntimeError(f"isolated commit touches {label}: {paths}")
            self._validate_commit_tree(worktree, commit, set(changed_paths))

    def _validate_candidate(
        self,
        worktree: Path,
        commits: list[str],
        *,
        managed_paths: frozenset[str] = frozenset(),
    ) -> None:
        self._validate_commits(worktree, commits, managed_paths=managed_paths)
        if managed_paths:
            if self.role != "update" or any(
                not _is_managed_update_artifact_path(path) for path in managed_paths
            ):
                raise RuntimeError("runtime-managed Update artifacts use an invalid path")
            if any(UPDATE_RECORD_PATH_RE.fullmatch(path) for path in managed_paths):
                diagnostics = validate_update_records(worktree)
                if diagnostics:
                    raise RuntimeError(
                        "invalid runtime-managed update record:\n"
                        + "\n".join(f"- {item}" for item in diagnostics)
                    )
        if self.role == "insight":
            return
        validation = MemoryTools(worktree, role=self.role).validate_memory(
            # A pre-existing sync union must not block an unrelated semantic update.
            enforce_correction_capacity=False
        )
        if validation.startswith("validation failed:"):
            raise RuntimeError(validation)

    def _prepare_managed_artifacts(
        self,
        worktree: Path,
        start_commit: str,
        candidate_commit: str | None,
        changed_paths: tuple[str, ...],
        output: Any,
        prepare: Callable[[Path, str, str | None, tuple[str, ...], Any], Iterable[str]],
        *,
        operation_id: str | None,
    ) -> tuple[str, ...]:
        paths = tuple(
            dict.fromkeys(
                prepare(
                    worktree,
                    start_commit,
                    candidate_commit,
                    changed_paths,
                    output,
                )
            )
        )
        if not paths:
            return ()
        if self.role != "update" or any(
            not isinstance(path, str) or not _is_managed_update_artifact_path(path)
            for path in paths
        ):
            raise RuntimeError("runtime-managed Update artifacts use an invalid path")
        dirty = set(
            _porcelain_paths(
                self._git_stdout(worktree, "status", "--porcelain", "--untracked-files=all")
            )
        )
        if dirty != set(paths):
            detail = ", ".join(sorted(dirty ^ set(paths))) or "unknown paths"
            raise RuntimeError(f"runtime-managed Update artifacts changed unexpected paths: {detail}")
        self._run_git(worktree, "add", "-f", "-A", "--", *paths)
        if candidate_commit is None:
            if operation_id is None:
                raise RuntimeError("a managed no-change Update artifact requires an operation id")
            self._run_git(
                worktree,
                "commit",
                "-m",
                "\n".join(
                    (
                        "update: record no-change candidate batch",
                        "",
                        f"{OPERATION_TRAILER}: {operation_id}",
                    )
                ),
            )
        else:
            self._run_git(worktree, "commit", "--amend", "--no-edit")
        return paths

    def _rebase_operation_commit(self, worktree: Path, commit: str, new_base: str) -> str:
        self._run_git(worktree, "reset", "--hard", new_base)
        result = self._run_git(worktree, "cherry-pick", "--allow-empty", commit, check=False)
        if result.returncode != 0:
            self._run_git(worktree, "cherry-pick", "--abort", check=False)
            raise RuntimeError(_git_error_message(result))
        return self._git_stdout(worktree, "rev-parse", "HEAD")

    def _semantic_changes_between(self, start_commit: str, end_commit: str) -> list[str]:
        if not self._is_ancestor(start_commit, end_commit):
            raise RuntimeError("main history diverged during isolated memory write")
        output = self._git_stdout(
            self.memory_root,
            "diff",
            "--name-status",
            "-M",
            "-z",
            start_commit,
            end_commit,
        )
        return sorted(path for path in _name_status_paths(output) if self._is_semantic_read_path(path))

    def _find_landed_operation_commit(
        self,
        operation_id: str,
        start_commit: str,
        current_commit: str,
    ) -> str | None:
        if not self._is_ancestor(start_commit, current_commit):
            return None
        commits = self._git_stdout(
            self.memory_root,
            "rev-list",
            "--first-parent",
            f"{start_commit}..{current_commit}",
        ).splitlines()
        trailer = f"{OPERATION_TRAILER}: {operation_id}"
        for commit in commits:
            message = self._git_stdout(self.memory_root, "show", "-s", "--format=%B", commit)
            if trailer in (line.strip() for line in message.splitlines()):
                return commit
        return None

    def _is_semantic_read_path(self, path: str) -> bool:
        return (
            self._is_graph_rightmemory_path(path)
            or path in FIXED_CORRECTION_COLLECTION_PATHS
            or path
            in {
                CORRECTIONS_PATH,
                SHARED_VIEW_REGISTRY_PATH,
                SHARE_REGISTRY_PATH,
            }
            or bool(SHARED_VIEW_DEFINITION_FILE_RE.fullmatch(path))
            or bool(INSIGHT_LOG_FILE_RE.fullmatch(path))
        )

    def _validate_empty_commit(self, worktree: Path, commit: str) -> None:
        subject = self._git_stdout(worktree, "log", "--max-count=1", "--format=%s", commit)
        if self.role == "pruner" and subject == "prune: checkpoint":
            return
        raise RuntimeError("isolated empty commits are limited to pruner `prune: checkpoint` commits")

    def _validate_commit_tree(self, worktree: Path, commit: str, changed_paths: set[str]) -> None:
        if self.role != "insight":
            self._validate_regular_memory_path(worktree, commit, "MEMORY.md", required=True)
            self._validate_regular_memory_path(worktree, commit, "PURSUITS.md", required=True)
        for path in sorted(changed_paths - {"MEMORY.md", "PURSUITS.md"}):
            self._validate_regular_memory_path(worktree, commit, path, required=False)

    def _is_role_write_path(self, path: str) -> bool:
        if self.role == "insight":
            return bool(INSIGHT_LOG_FILE_RE.fullmatch(path))
        if self.role == "reviewer":
            return False
        if path in FIXED_CORRECTION_COLLECTION_PATHS:
            return self.role in {"update", "sync-reconciler"}
        if self.role == "update":
            return self._is_graph_rightmemory_path(path)
        if self.role == "sync-reconciler":
            return (
                self._is_graph_rightmemory_path(path)
                or path == CORRECTIONS_PATH
                or path in {SHARED_VIEW_REGISTRY_PATH, SHARE_REGISTRY_PATH}
                or bool(SHARED_VIEW_DEFINITION_FILE_RE.fullmatch(path))
                or bool(INSIGHT_LOG_FILE_RE.fullmatch(path))
            )
        return path == "MEMORY.md" or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))

    def _is_graph_rightmemory_path(self, path: str) -> bool:
        if path in FIXED_CORRECTION_COLLECTION_PATHS:
            return False
        return (
            path == "MEMORY.md"
            or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))
            or path == "PURSUITS.md"
            or bool(PURSUIT_DETAIL_FILE_RE.fullmatch(path))
        )

    def _validate_regular_memory_path(self, worktree: Path, commit: str, path: str, required: bool) -> None:
        tree_entry = self._tree_entry(worktree, commit, path)
        if tree_entry is None:
            if required:
                raise RuntimeError("isolated commit must keep MEMORY.md as a regular file")
            return

        mode, kind = tree_entry
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RuntimeError(f"memory path is not a regular file: {path}")

    def _tree_entry(self, worktree: Path, commit: str, path: str) -> tuple[str, str] | None:
        output = self._git_stdout(worktree, "ls-tree", "-z", commit, "--", path)
        if not output:
            return None
        record = output.split("\0", 1)[0]
        metadata, separator, _entry_path = record.partition("\t")
        if not separator:
            raise RuntimeError(f"could not inspect memory path in git tree: {path}")
        parts = metadata.split()
        if len(parts) < 2:
            raise RuntimeError(f"could not inspect memory path in git tree: {path}")
        return parts[0], parts[1]

    def _commit_paths(self, worktree: Path, commit: str) -> list[str]:
        output = self._git_stdout(
            worktree,
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-M",
            "-z",
            commit,
        )
        return _name_status_paths(output)

    def _land_commits(self, commits: list[str]) -> None:
        result = self._run_git(self.memory_root, "cherry-pick", "--allow-empty", *commits, check=False)
        if result.returncode == 0:
            return
        self._run_git(self.memory_root, "cherry-pick", "--abort", check=False)
        raise RuntimeError(_git_error_message(result))

    def _collapse_operation_commits(
        self,
        worktree: Path,
        start_commit: str,
        commits: list[str],
        operation_id: str,
    ) -> str:
        message = self._git_stdout(worktree, "log", "-1", "--format=%B", commits[-1])
        prefix = f"{OPERATION_TRAILER}:"
        body = "\n".join(line for line in message.splitlines() if not line.startswith(prefix)).rstrip()
        operation_message = f"{body}\n\n{OPERATION_TRAILER}: {operation_id}"
        if len(commits) == 1:
            self._run_git(worktree, "commit", "--amend", "--allow-empty", "-m", operation_message)
        else:
            self._run_git(worktree, "reset", "--soft", start_commit)
            self._run_git(worktree, "commit", "--allow-empty", "-m", operation_message)
        return self._git_stdout(worktree, "rev-parse", "HEAD")

    def _land_operation_commit(self, commit: str) -> None:
        result = self._run_git(self.memory_root, "merge", "--ff-only", commit, check=False)
        if result.returncode != 0:
            raise RuntimeError(_git_error_message(result))

    def _pin_operation_ref(self, operation_id: str, commit: str) -> None:
        self._run_git(self.memory_root, "update-ref", _operation_ref(operation_id), commit)

    def _delete_operation_ref(self, operation_id: str) -> None:
        self._run_git(self.memory_root, "update-ref", "-d", _operation_ref(operation_id), check=False)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run_git(
            self.memory_root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        return result.returncode == 0

    def _cleanup(self, worktree: Path, branch: str) -> None:
        self._run_git(self.memory_root, "worktree", "remove", "--force", str(worktree), check=False)
        self._run_git(self.memory_root, "branch", "-D", branch, check=False)

    def _stale_worktrees(self, runtime_worktrees: Path, role_slug: str) -> list[tuple[Path, str]]:
        result = self._run_git(self.memory_root, "worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return []

        worktrees: list[tuple[Path, str]] = []
        for entry in _worktree_entries(result.stdout):
            worktree = entry.get("worktree")
            branch_ref = entry.get("branch", "")
            branch = branch_ref.removeprefix("refs/heads/")
            identifier = _temp_identifier_for_role(branch, role_slug)
            if worktree is None or identifier is None or self._lease_is_live(role_slug, identifier):
                continue
            path = Path(worktree).resolve()
            try:
                path.relative_to(runtime_worktrees.resolve())
            except ValueError:
                continue
            worktrees.append((path, identifier))
        return worktrees

    def _lease_path(self, role_slug: str, identifier: str) -> Path:
        return self.memory_root / ".runtime" / WORKTREE_LEASE_DIR / f"{role_slug}-{identifier}.json"

    def _write_lease(self, path: Path) -> None:
        _write_worktree_lease(path)

    def _lease_is_live(self, role_slug: str, identifier: str) -> bool:
        return _worktree_lease_is_live(self._lease_path(role_slug, identifier))

    def _cleanup_orphaned_leases(self, role_slug: str) -> None:
        lease_root = self.memory_root / ".runtime" / WORKTREE_LEASE_DIR
        for path in lease_root.glob(f"{role_slug}-*.json"):
            identifier = _lease_identifier(path.name, role_slug)
            if identifier is not None and not self._lease_is_live(role_slug, identifier):
                path.unlink(missing_ok=True)

    def _temp_branches(self, role_slug: str) -> list[str]:
        result = self._run_git(
            self.memory_root,
            "branch",
            "--list",
            f"{TEMP_BRANCH_PREFIX}{role_slug}-*",
            "--format=%(refname:short)",
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if _is_temp_branch_for_role(line.strip(), role_slug)]

    def _git_stdout(self, cwd: Path, *args: str) -> str:
        return self._run_git(cwd, *args).stdout.strip()

    def _run_git(self, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            result = subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr)
            stderr = f"{stderr}\ngit command timed out after {GIT_TIMEOUT_SECONDS} seconds".strip()
            result = subprocess.CompletedProcess(["git", *args], 124, stdout, stderr)

        if check and result.returncode != 0:
            raise RuntimeError(_git_error_message(result))
        return result


def _output_text(output: Any) -> str:
    value = getattr(output, "output", None)
    value = value if value is not None else output
    text = str(value)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.lstrip()


def _write_worktree_lease(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    payload = {"pid": pid, "process_identity": process_identity(pid)}
    tmp_path = path.with_name(f".{path.name}.{pid}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_worktree_lease(path: Path) -> tuple[int, str | None] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    owner_identity = payload.get("process_identity")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return None
    if owner_identity is not None and (not isinstance(owner_identity, str) or not owner_identity):
        return None
    return pid, owner_identity


def _worktree_lease_is_live(path: Path) -> bool:
    owner = _read_worktree_lease(path)
    if owner is None:
        return False
    pid, owner_identity = owner
    current_identity = process_identity(pid)
    if owner_identity is None or current_identity is None:
        # A temporary identity lookup failure must not delete a live writer.
        return process_exists(pid)
    return current_identity == owner_identity


def _worktree_lease_owned_by_current_process(path: Path) -> bool:
    owner = _read_worktree_lease(path)
    if owner is None:
        return False
    pid, owner_identity = owner
    if pid != os.getpid():
        return False
    current_identity = process_identity(pid)
    if owner_identity is None or current_identity is None:
        return True
    return owner_identity == current_identity


def _operation_ref(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"refs/rightmemory/operations/{digest}"


def _safe_role_slug(role: str) -> str:
    slug = ROLE_SAFE_RE.sub("-", role.strip()).strip(".-")
    if not slug:
        return "role"
    return slug[:48].rstrip(".-") or "role"


def _is_managed_update_artifact_path(path: str) -> bool:
    return UPDATE_RECORD_PATH_RE.fullmatch(path) is not None


def _is_temp_branch_for_role(branch: str, role_slug: str) -> bool:
    return _temp_identifier_for_role(branch, role_slug) is not None


def _temp_identifier_for_role(branch: str, role_slug: str) -> str | None:
    match = re.fullmatch(
        rf"{re.escape(TEMP_BRANCH_PREFIX)}{re.escape(role_slug)}-([0-9a-f]{{32}})",
        branch,
    )
    return match.group(1) if match is not None else None


def _lease_identifier(filename: str, role_slug: str) -> str | None:
    match = re.fullmatch(rf"{re.escape(role_slug)}-([0-9a-f]{{32}})\.json", filename)
    return match.group(1) if match is not None else None


def _name_status_paths(output: str) -> list[str]:
    paths: list[str] = []
    tokens = [token for token in output.split("\0") if token]
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        paths.extend(tokens[index : index + path_count])
        index += path_count
    return paths


def _porcelain_paths(output: str) -> list[str]:
    paths: set[str] = set()
    for line in output.splitlines():
        if len(line) < 3:
            continue
        path = line[2:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            paths.add(path)
    return sorted(paths)


def _worktree_entries(output: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if separator:
            current[key] = value
    if current:
        entries.append(current)
    return entries


def _git_error_message(result: subprocess.CompletedProcess[str]) -> str:
    command = " ".join(result.args) if isinstance(result.args, list) else str(result.args)
    detail = result.stderr.strip() or result.stdout.strip()
    if detail:
        return f"git command failed ({command}):\n{detail}"
    return f"git command failed ({command})"


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
