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
    PURSUIT_RULES_PATH,
    SHARED_VIEW_DEFINITION_FILE_RE,
    SHARE_REGISTRY_PATH,
    SHARED_VIEW_REGISTRY_PATH,
    MemoryTools,
)
from .update_review import validate_corrections_markdown


GIT_TIMEOUT_SECONDS = 30
ACTIVE_MEMORY_WRITE_PATHS = ("MEMORY.md", "MEMORY_*.md")
ACTIVE_PURSUIT_WRITE_PATHS = ("PURSUITS.md", "PURSUIT_*.md")
PROTECTED_RIGHTMEMORY_PATHS = (
    *ACTIVE_MEMORY_WRITE_PATHS,
    *ACTIVE_PURSUIT_WRITE_PATHS,
    PURSUIT_RULES_PATH,
    CORRECTIONS_PATH,
)
INSIGHT_WRITE_PATHS = ("insight_logs/*.md",)
ROLE_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
TEMP_BRANCH_PREFIX = "rightmemory-isolated-"
OPERATION_TRAILER = "RightMemory-Operation"
WORKTREE_LEASE_DIR = "worktree-leases"


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


class IsolatedWriteSupervisor:
    def __init__(self, memory_root: Path, role: str, *, update_mode: str = "normal"):
        self.memory_root = Path(memory_root).resolve()
        self.role = role
        self.update_mode = update_mode
        if update_mode not in {"normal", "review-correction"}:
            raise ValueError("update_mode must be normal or review-correction")
        if update_mode != "normal" and role != "update":
            raise ValueError("non-normal update modes require the update role")

    def recover_prepared(self) -> None:
        """Finish durable outcomes before a caller starts another model turn."""
        self._ensure_repo_root()
        store = SemanticOperationStore(self.memory_root)
        with store.execution_locked():
            self._drain_prepared_operations(store)

    def run(
        self,
        run_in_worktree: Callable[[Path], Any],
        *,
        operation_id: str | None = None,
        operation_input: Mapping[str, Any] | None = None,
        effects_for_outcome: Callable[[tuple[str, ...], int], Iterable[OperationEffect]] | None = None,
        prepare_effects: Callable[[str, Path], None] | None = None,
    ) -> IsolatedWriteResult:
        if operation_id is None:
            return self._run_claimed(
                run_in_worktree,
                operation_input=operation_input,
                effects_for_outcome=effects_for_outcome,
                prepare_effects=prepare_effects,
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
            )

    def _run_claimed(
        self,
        run_in_worktree: Callable[[Path], Any],
        *,
        operation_id: str | None = None,
        operation_input: Mapping[str, Any] | None = None,
        effects_for_outcome: Callable[[tuple[str, ...], int], Iterable[OperationEffect]] | None = None,
        prepare_effects: Callable[[str, Path], None] | None = None,
    ) -> IsolatedWriteResult:
        self._ensure_repo_root()
        operation_store: SemanticOperationStore | None = None
        if operation_id is not None:
            operation_store = SemanticOperationStore(self.memory_root)
            input_data = dict(operation_input or {})
            input_data["role"] = self.role
            input_data["update_mode"] = self.update_mode
            operation = operation_store.begin(
                operation_id,
                input_data,
            )
            if operation.phase in FINAL_PHASES:
                self._delete_operation_ref(operation_id)
                return self._result_from_operation(operation)
            if operation.phase == "prepared":
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
        lease = self._lease_path(role_slug, identifier)

        self._ensure_runtime_ignored(worktree)
        self._write_lease(lease)
        try:
            self._run_git(self.memory_root, "worktree", "add", "-b", branch, str(worktree), start_head)
            self._seed_untracked_publish_artifacts(worktree)
            output = run_in_worktree(worktree)
            status = self._git_stdout(worktree, "status", "--porcelain")
            if status:
                raise RuntimeError(f"isolated worktree has uncommitted changes:\n{status}")

            commits = self._temp_commits(worktree, start_head)
            if self.role == "update" and self.update_mode == "review-correction":
                outcome = _output_text(output).strip()
                if outcome.startswith(("Needs input:", "No correction needed:")):
                    commits = []
                elif not commits:
                    raise RuntimeError(
                        "review correction made no state commit; reply `Needs input: ...` or "
                        "`No correction needed: ...` when a commit is intentionally unnecessary"
                    )
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
                    if operation_store is None or operation_id is None or not commits:
                        raise RuntimeError("main HEAD changed during isolated memory write")
                    semantic_changes = self._semantic_changes_between(start_head, current_head)
                    if semantic_changes:
                        detail = ", ".join(semantic_changes)
                        raise RuntimeError(
                            "main semantic state changed during isolated memory write: " + detail
                        )
                    commits = [self._rebase_operation_commit(worktree, commits[0], current_head)]
                    self._validate_candidate(worktree, commits)
                    start_head = current_head
                    changed_paths = tuple(
                        sorted({path for commit in commits for path in self._commit_paths(worktree, commit)})
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
                        metadata={"candidate_commit": commits[0] if commits else None},
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
                lease.unlink(missing_ok=True)

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
        )

    def _drain_prepared_operations(self, store: SemanticOperationStore) -> None:
        prepared = [
            record
            for record in store.list_outstanding_records()
            if record.phase == "prepared" and record.outcome is not None
        ]
        prepared.sort(key=lambda record: (record.outcome.sequence, record.operation_id))
        for record in prepared:
            role = record.input_data.get("role")
            update_mode = record.input_data.get("update_mode", "normal")
            if not isinstance(role, str) or not isinstance(update_mode, str):
                raise RuntimeError(
                    f"prepared operation has invalid routing data: {record.operation_id}"
                )
            claimed = store.claim_prepared(record.operation_id)
            supervisor = IsolatedWriteSupervisor(
                self.memory_root,
                role,
                update_mode=update_mode,
            )
            supervisor._resume_prepared_operation(store, claimed)

    def _result_from_operation(self, operation: SemanticOperationRecord) -> IsolatedWriteResult:
        outcome = operation.outcome
        if outcome is None:
            raise RuntimeError(f"completed operation has no outcome: {operation.operation_id}")
        return IsolatedWriteResult(
            output=outcome.output,
            commits_landed=1 if operation.phase == "committed" else 0,
            start_commit=outcome.start_commit,
            landed_commit=outcome.landed_commit or outcome.start_commit,
            changed_paths=outcome.changed_paths,
            operation_id=operation.operation_id,
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
        return _porcelain_paths(status)

    def _write_paths(self) -> tuple[str, ...]:
        if self.role == "insight":
            return (*PROTECTED_RIGHTMEMORY_PATHS, *INSIGHT_WRITE_PATHS)
        return PROTECTED_RIGHTMEMORY_PATHS

    def _temp_commits(self, worktree: Path, start_head: str) -> list[str]:
        output = self._git_stdout(worktree, "rev-list", "--reverse", f"{start_head}..HEAD")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _validate_commits(self, worktree: Path, commits: list[str]) -> None:
        if self.role == "update" and len(commits) > 1:
            raise RuntimeError("one update turn must land at most one commit")
        for commit in commits:
            changed_paths = self._commit_paths(worktree, commit)
            if not changed_paths:
                self._validate_empty_commit(worktree, commit)
            invalid_paths = {path for path in changed_paths if not self._is_role_write_path(path)}
            if invalid_paths:
                paths = ", ".join(sorted(invalid_paths))
                label = "non-insight paths" if self.role == "insight" else "non-memory paths"
                raise RuntimeError(f"isolated commit touches {label}: {paths}")
            self._validate_commit_tree(worktree, commit, set(changed_paths))
        if self.role == "update" and self.update_mode == "review-correction" and commits:
            changed_paths = set(self._commit_paths(worktree, commits[0]))
            if not any(self._is_rightmemory_path(path) for path in changed_paths):
                raise RuntimeError(
                    "review correction must change Memory or Pursuit; "
                    "corrections.md-only commits are not allowed"
                )
            if CORRECTIONS_PATH in changed_paths:
                self._validate_corrections_file(worktree, commits[0])

    def _validate_candidate(self, worktree: Path, commits: list[str]) -> None:
        self._validate_commits(worktree, commits)
        if self.role == "insight":
            return
        tool_role = "update-correction" if self.update_mode == "review-correction" else self.role
        validation = MemoryTools(worktree, role=tool_role).validate_memory(
            enforce_correction_capacity=(
                self.role == "update" and self.update_mode == "review-correction"
            )
        )
        if validation.startswith("validation failed:"):
            raise RuntimeError(validation)

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
            self._is_rightmemory_path(path)
            or path in FIXED_CORRECTION_COLLECTION_PATHS
            or path in {PURSUIT_RULES_PATH, CORRECTIONS_PATH, SHARED_VIEW_REGISTRY_PATH, SHARE_REGISTRY_PATH}
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
        if self.role == "update":
            return self._is_rightmemory_path(path) or (
                self.update_mode == "review-correction" and path == CORRECTIONS_PATH
            )
        if self.role == "sync-reconciler":
            return (
                self._is_rightmemory_path(path)
                or path in {PURSUIT_RULES_PATH, CORRECTIONS_PATH}
                or path in {SHARED_VIEW_REGISTRY_PATH, SHARE_REGISTRY_PATH}
                or bool(SHARED_VIEW_DEFINITION_FILE_RE.fullmatch(path))
                or bool(INSIGHT_LOG_FILE_RE.fullmatch(path))
            )
        if path in FIXED_CORRECTION_COLLECTION_PATHS:
            return False
        return path == "MEMORY.md" or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))

    def _is_rightmemory_path(self, path: str) -> bool:
        return (
            path == "MEMORY.md"
            or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))
            or path == "PURSUITS.md"
            or bool(PURSUIT_DETAIL_FILE_RE.fullmatch(path))
        )

    def _validate_corrections_file(self, worktree: Path, commit: str) -> None:
        tree_entry = self._tree_entry(worktree, commit, CORRECTIONS_PATH)
        if tree_entry is None:
            return
        text = self._git_stdout(worktree, "show", f"{commit}:{CORRECTIONS_PATH}")
        errors = validate_corrections_markdown(text)
        if errors:
            raise RuntimeError("invalid corrections.md:\n" + "\n".join(f"- {error}" for error in errors))

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

    def _lease_is_live(self, role_slug: str, identifier: str) -> bool:
        path = self._lease_path(role_slug, identifier)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        pid = payload.get("pid")
        owner_identity = payload.get("process_identity")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            return False
        if owner_identity is not None and (not isinstance(owner_identity, str) or not owner_identity):
            return False
        current_identity = process_identity(pid)
        if owner_identity is None or current_identity is None:
            # A temporary identity lookup failure must not delete a live writer.
            return process_exists(pid)
        return current_identity == owner_identity

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
    text = str(value if value is not None else output)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.lstrip()


def _operation_ref(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    return f"refs/rightmemory/operations/{digest}"


def _safe_role_slug(role: str) -> str:
    slug = ROLE_SAFE_RE.sub("-", role.strip()).strip(".-")
    if not slug:
        return "role"
    return slug[:48].rstrip(".-") or "role"


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
