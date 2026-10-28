from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .automatic_effects import memory_operation_effects, prepare_memory_operation_effects
from .isolated_write import (
    IsolatedWriteResult,
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
    MemoryVersionConflictError,
)
from .platform import lock_file, unlock_file
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .session import MemoryWriteLock, _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class AssistantStaleVersionError(MemoryVersionConflictError):
    """The external task must be reconsidered against newer memory."""


@dataclass(frozen=True)
class AssistantWriteTask:
    task_id: str
    batch_id: str
    worktree: Path
    base_commit: str
    branch: str
    operation_id: str
    input_data: dict
    source_commit: str | None = None
    output: str | None = None
    external_finalizer: str | None = None


class AssistantWriteManager:
    """Keep native-edit worktrees until a fenced, validated completion is durable."""

    def __init__(self, memory_root: Path, *, role: str = "update"):
        if role not in {"update", "guidance-curator", "skills-curator"}:
            raise ValueError(f"unsupported prepared write role: {role}")
        self.memory_root = Path(memory_root).resolve()
        self.role = role
        self.state_root = self.memory_root / ".runtime" / "assistant-write" / role
        self.supervisor = IsolatedWriteSupervisor(self.memory_root, role)

    def prepare(
        self,
        task_id: str,
        input_data: dict,
        *,
        check_current: Callable[[], None] | None = None,
    ) -> AssistantWriteTask:
        """Prepare any domain from immutable task identity and candidate proofs."""
        batch_id = _input_digest(input_data)
        self.supervisor._ensure_repo_root()
        with self._task_locked(task_id):
            task = self._read(task_id)
            if task is not None:
                if task.batch_id != batch_id or task.input_data != input_data:
                    raise ValueError("assistant task already belongs to different frozen input")
                if check_current is not None:
                    check_current()
                receipt = SemanticOperationStore(self.memory_root).read(task.operation_id)
                if receipt is not None and receipt.phase in FINAL_PHASES:
                    return task
                self._ensure_worktree(task)
                return task
            store = SemanticOperationStore(self.memory_root)
            with store.execution_locked(), MemoryWriteLock(self.memory_root):
                if check_current is not None:
                    check_current()
                dirty = self.supervisor._dirty_memory_files()
                if dirty:
                    raise MainMemoryDirtyError(dirty)
                task = self._task(
                    task_id,
                    batch_id,
                    self.supervisor._git_stdout(self.memory_root, "rev-parse", "HEAD"),
                    input_data=input_data,
                )
                self._write(task)
                self._ensure_worktree(task)
                return task

    def read(self, task_id: str) -> AssistantWriteTask:
        task = self._read(task_id)
        if task is None:
            raise FileNotFoundError(f"assistant write task does not exist: {task_id}")
        return task

    def complete(
        self,
        task_id: str,
        *,
        external_finalizer: str,
        output: str = "",
        check_current: Callable[[], None] | None = None,
    ) -> IsolatedWriteResult:
        """Freeze and validate semantic edits for the shared claim coordinator."""
        if not isinstance(output, str):
            raise ValueError("assistant output must be a string")
        finalizer = external_finalizer
        if not isinstance(finalizer, str) or not finalizer.strip():
            raise ValueError("external_finalizer must be a non-empty string")
        with self._task_locked(task_id):
            task = self.read(task_id)
            if task.source_commit is not None:
                if task.external_finalizer != finalizer:
                    raise ValueError("assistant completion belongs to a different finalizer")
                if output and output != task.output:
                    raise ValueError("assistant completion already has a different output")
            else:
                self._verify_worktree(task)
                if self.supervisor._git_stdout(task.worktree, "status", "--porcelain", "--untracked-files=all"):
                    raise RuntimeError("assistant worktree has uncommitted changes")
                source_commit = self.supervisor._git_stdout(task.worktree, "rev-parse", "HEAD")
                self._source_commits(task, source_commit)
                task = replace(task, source_commit=source_commit, output=output, external_finalizer=finalizer)
                self._write(task)

            def import_commits(worktree: Path) -> str:
                current = self.supervisor._git_stdout(worktree, "rev-parse", "HEAD")
                changed = self.supervisor._semantic_changes_between(task.base_commit, current)
                if changed:
                    raise AssistantStaleVersionError("memory changed since assistant task began: " + ", ".join(changed))
                for commit in self._source_commits(task, task.source_commit):
                    self.supervisor._run_git(worktree, "cherry-pick", "--allow-empty", commit)
                # The no-change path must also pass complete graph validation.
                self.supervisor._validate_candidate(worktree, [])
                return task.output or ""

            try:
                return self.supervisor.run(
                    import_commits,
                    operation_id=task.operation_id,
                    operation_input={
                        "kind": "curation", "role": self.role,
                        "task_id": task.task_id, "batch_id": task.batch_id,
                        "base_commit": task.base_commit, "input": task.input_data,
                    },
                    effects_for_outcome=(
                        lambda paths, commits: memory_operation_effects(self.memory_root, paths, commits)
                    ) if self.role == "update" else None,
                    prepare_effects=(
                        lambda operation_id, worktree: prepare_memory_operation_effects(
                            self.memory_root, operation_id, worktree,
                        )
                    ) if self.role == "update" else None,
                    external_finalizer=finalizer,
                    check_current=check_current,
                )
            except Exception as exc:
                receipt = SemanticOperationStore(self.memory_root).read(task.operation_id)
                if receipt is None or receipt.phase == "running":
                    # Rejected edits may be corrected in the same native worktree.
                    self._write(replace(task, source_commit=None, output=None, external_finalizer=None))
                if isinstance(exc, MemoryVersionConflictError) and not isinstance(exc, AssistantStaleVersionError):
                    raise AssistantStaleVersionError(str(exc)) from exc
                raise

    def complete_external(
        self,
        task_id: str,
        *,
        external_finalizer: str,
        landed_commit: str,
    ) -> IsolatedWriteResult:
        with self._task_locked(task_id):
            task = self.read(task_id)
            if task.external_finalizer != external_finalizer:
                raise ValueError("assistant completion belongs to a different finalizer")
            return self.supervisor.complete_external(
                task.operation_id,
                external_finalizer=external_finalizer,
                landed_commit=landed_commit,
            )

    def cleanup(self, task_id: str) -> None:
        """Remove a settled workspace, retaining its task identity and receipt."""
        with self._task_locked(task_id):
            task = self.read(task_id)
            receipt = SemanticOperationStore(self.memory_root).read(task.operation_id)
            if receipt is None or receipt.phase not in FINAL_PHASES:
                raise RuntimeError("assistant worktree cannot be removed before completion")
            if not task.worktree.exists():
                return
            self._verify_worktree(task)
            if self.supervisor._git_stdout(task.worktree, "status", "--porcelain", "--untracked-files=all"):
                raise RuntimeError("assistant worktree has new uncommitted changes; retained for review")
            if self.supervisor._git_stdout(task.worktree, "rev-parse", "HEAD") != task.source_commit:
                raise RuntimeError("assistant worktree changed after completion; retained for review")
            self.supervisor._run_git(self.memory_root, "worktree", "remove", str(task.worktree))
            self.supervisor._run_git(self.memory_root, "branch", "-D", task.branch)

    def _source_commits(self, task: AssistantWriteTask, source_commit: str | None) -> list[str]:
        if source_commit is None or _OID.fullmatch(source_commit) is None:
            raise ValueError("assistant source must be a full commit id")
        if not self.supervisor._is_ancestor(task.base_commit, source_commit):
            raise RuntimeError("assistant worktree no longer descends from its prepared base")
        commits = self.supervisor._git_stdout(
            self.memory_root, "rev-list", "--reverse", f"{task.base_commit}..{source_commit}"
        ).splitlines()
        parent = task.base_commit
        for commit in commits:
            parents = self.supervisor._git_stdout(
                self.memory_root, "rev-list", "--parents", "-1", commit
            ).split()
            if parents != [commit, parent]:
                raise RuntimeError("assistant worktree must have linear commits from its prepared base")
            self.supervisor._validate_commits(self.memory_root, [commit])
            parent = commit
        return commits

    def _ensure_worktree(self, task: AssistantWriteTask) -> None:
        self._check_runtime_paths(task.worktree)
        if (task.worktree / ".git").exists():
            self._verify_worktree(task)
            return
        if task.worktree.exists() and any(task.worktree.iterdir()):
            raise RuntimeError("assistant worktree path contains unregistered files; left unchanged")
        self.supervisor._ensure_runtime_ignored(task.worktree)
        branch_exists = self.supervisor._run_git(
            self.memory_root, "show-ref", "--verify", "--quiet", f"refs/heads/{task.branch}", check=False
        ).returncode == 0
        args = ["worktree", "add"]
        if branch_exists:
            args.extend([str(task.worktree), task.branch])
        else:
            args.extend(["-b", task.branch, str(task.worktree), task.base_commit])
        self.supervisor._run_git(self.memory_root, *args)
        self.supervisor._seed_untracked_publish_artifacts(task.worktree)
        self._verify_worktree(task)

    def _verify_worktree(self, task: AssistantWriteTask) -> None:
        self._check_runtime_paths(task.worktree)
        if not task.worktree.is_dir():
            raise FileNotFoundError(f"assistant worktree is missing: {task.worktree}")
        top = Path(self.supervisor._git_stdout(task.worktree, "rev-parse", "--show-toplevel")).resolve()
        common = self.supervisor._git_stdout(task.worktree, "rev-parse", "--git-common-dir")
        active_common = self.supervisor._git_stdout(self.memory_root, "rev-parse", "--git-common-dir")
        branch = self.supervisor._git_stdout(task.worktree, "symbolic-ref", "--short", "HEAD")
        if (
            top != task.worktree
            or (task.worktree / common).resolve() != (self.memory_root / active_common).resolve()
            or branch != task.branch
        ):
            raise RuntimeError("assistant worktree identity does not match its prepared task")

    def _task(
        self, task_id: str, batch_id: str, base_commit: str,
        *, source_commit: str | None = None, output: str | None = None,
        external_finalizer: str | None = None,
        input_data: dict,
    ) -> AssistantWriteTask:
        digest = _task_digest(task_id)
        return AssistantWriteTask(
            task_id=task_id, batch_id=batch_id, base_commit=base_commit,
            worktree=self.memory_root / ".runtime" / "worktrees" / f"assistant-{digest}",
            branch=f"rightmemory-assistant-{digest}", operation_id=f"assistant-{self.role}-{digest}",
            source_commit=source_commit, output=output, external_finalizer=external_finalizer,
            input_data=input_data,
        )

    def _record_path(self, task_id: str) -> Path:
        return self.state_root / f"{_task_digest(task_id)}.json"

    def _read(self, task_id: str) -> AssistantWriteTask | None:
        path = self._record_path(task_id)
        self._check_runtime_paths(path)
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError("assistant task record must be a regular file")
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = {"version", "task_id", "batch_id", "base_commit", "input", "source_commit", "output", "external_finalizer"}
        if not isinstance(data, dict) or set(data) != keys or type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("invalid assistant task record")
        if data["task_id"] != task_id or not isinstance(data["base_commit"], str) or not _OID.fullmatch(data["base_commit"]):
            raise ValueError("invalid assistant task identity or base commit")
        if _input_digest(data["input"]) != data["batch_id"]:
            raise ValueError("assistant task batch does not match its frozen input")
        source = data["source_commit"]
        if source is None:
            if data["output"] is not None or data["external_finalizer"] is not None:
                raise ValueError("unsubmitted assistant task has completion fields")
        elif (
            not isinstance(source, str) or not _OID.fullmatch(source)
            or not isinstance(data["output"], str)
            or not isinstance(data["external_finalizer"], str) or not data["external_finalizer"].strip()
        ):
            raise ValueError("invalid assistant task completion")
        return self._task(
            task_id, data["batch_id"], data["base_commit"],
            source_commit=source, output=data["output"], external_finalizer=data["external_finalizer"],
            input_data=data["input"],
        )

    def _write(self, task: AssistantWriteTask) -> None:
        path = self._record_path(task.task_id)
        self._check_runtime_paths(path)
        data = {
            "version": 1, "task_id": task.task_id, "batch_id": task.batch_id,
            "base_commit": task.base_commit,
            "input": task.input_data,
            "source_commit": task.source_commit, "output": task.output,
            "external_finalizer": task.external_finalizer,
        }
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

    @contextmanager
    def _task_locked(self, task_id: str) -> Iterator[None]:
        path = self.state_root / f"{_task_digest(task_id)}.lock"
        self._check_runtime_paths(path)
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        _ensure_durable_directory(self.state_root)
        with path.open("a+b") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _check_runtime_paths(self, path: Path) -> None:
        relative = path.relative_to(self.memory_root)
        current = self.memory_root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError(f"assistant runtime path must not be a symlink: {current}")


def _task_digest(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 200 or any(c in task_id for c in "\0\r\n"):
        raise ValueError("assistant task id must be a non-empty single-line string of at most 200 characters")
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()


def _input_digest(input_data: dict) -> str:
    if not isinstance(input_data, dict):
        raise ValueError("prepared task input must be an object")
    encoded = json.dumps(input_data, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
