from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .session import MemoryWriteLock, _ensure_runtime_gitignore
from .tools import DREAM_LOG_FILE_RE, MEMORY_DETAIL_FILE_RE, MemoryTools


GIT_TIMEOUT_SECONDS = 30
MEMORY_WRITE_PATHS = ("MEMORY.md", "MEMORY_*.md", "dream_logs/*.md")
ROLE_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
TEMP_BRANCH_PREFIX = "rightmemory-isolated-"


class MainMemoryDirtyError(RuntimeError):
    def __init__(self, paths: list[str]):
        self.paths = tuple(paths)
        detail = ", ".join(paths) if paths else "memory files"
        super().__init__(f"main memory files have uncommitted changes: {detail}")


@dataclass(frozen=True)
class IsolatedWriteResult:
    output: Any
    commits_landed: int


class IsolatedWriteSupervisor:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = Path(memory_root).resolve()
        self.role = role

    def run(self, run_in_worktree: Callable[[Path], Any]) -> IsolatedWriteResult:
        self._ensure_repo_root()
        dirty = self._dirty_memory_files()
        if dirty:
            raise MainMemoryDirtyError(dirty)

        start_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
        role_slug = _safe_role_slug(self.role)
        identifier = uuid.uuid4().hex
        branch = f"{TEMP_BRANCH_PREFIX}{role_slug}-{identifier}"
        worktree = self.memory_root / ".runtime" / "worktrees" / f"{role_slug}-{identifier}"

        self._ensure_runtime_ignored(worktree)
        try:
            self._run_git(self.memory_root, "worktree", "add", "-b", branch, str(worktree), start_head)
            output = run_in_worktree(worktree)
            status = self._git_stdout(worktree, "status", "--porcelain")
            if status:
                raise RuntimeError(f"isolated worktree has uncommitted changes:\n{status}")

            commits = self._temp_commits(worktree, start_head)
            if commits:
                self._validate_commits(worktree, commits)
                validation = MemoryTools(worktree).validate_memory()
                if validation.startswith("validation failed:"):
                    raise RuntimeError(validation)

            with MemoryWriteLock(self.memory_root):
                dirty = self._dirty_memory_files()
                if dirty:
                    raise MainMemoryDirtyError(dirty)
                current_head = self._git_stdout(self.memory_root, "rev-parse", "HEAD")
                if current_head != start_head:
                    raise RuntimeError("main HEAD changed during isolated memory write")
                if commits:
                    self._land_commits(commits)
            return IsolatedWriteResult(output=output, commits_landed=len(commits))
        finally:
            self._cleanup(worktree, branch)

    def cleanup_stale(self) -> None:
        role_slug = _safe_role_slug(self.role)
        runtime_worktrees = self.memory_root / ".runtime" / "worktrees"
        for worktree in self._stale_worktrees(runtime_worktrees, role_slug):
            self._run_git(self.memory_root, "worktree", "remove", "--force", str(worktree), check=False)

        self._run_git(self.memory_root, "worktree", "prune", check=False)

        for branch in self._temp_branches(role_slug):
            self._run_git(self.memory_root, "branch", "-D", branch, check=False)

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

    def _dirty_memory_files(self) -> list[str]:
        status = self._git_stdout(self.memory_root, "status", "--porcelain", "--", *MEMORY_WRITE_PATHS)
        return _porcelain_paths(status)

    def _temp_commits(self, worktree: Path, start_head: str) -> list[str]:
        output = self._git_stdout(worktree, "rev-list", "--reverse", f"{start_head}..HEAD")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _validate_commits(self, worktree: Path, commits: list[str]) -> None:
        for commit in commits:
            changed_paths = self._commit_paths(worktree, commit)
            if not changed_paths:
                self._validate_empty_commit(worktree, commit)
            invalid_paths = {path for path in changed_paths if not _is_memory_write_path(path)}
            if invalid_paths:
                paths = ", ".join(sorted(invalid_paths))
                raise RuntimeError(f"isolated commit touches non-memory paths: {paths}")
            self._validate_commit_tree(worktree, commit, set(changed_paths))

    def _validate_empty_commit(self, worktree: Path, commit: str) -> None:
        subject = self._git_stdout(worktree, "log", "--max-count=1", "--format=%s", commit)
        if self.role == "pruner" and subject == "prune: checkpoint":
            return
        raise RuntimeError("isolated empty commits are limited to pruner `prune: checkpoint` commits")

    def _validate_commit_tree(self, worktree: Path, commit: str, changed_paths: set[str]) -> None:
        self._validate_regular_memory_path(worktree, commit, "MEMORY.md", required=True)
        for path in sorted(changed_paths - {"MEMORY.md"}):
            self._validate_regular_memory_path(worktree, commit, path, required=False)

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

    def _cleanup(self, worktree: Path, branch: str) -> None:
        self._run_git(self.memory_root, "worktree", "remove", "--force", str(worktree), check=False)
        self._run_git(self.memory_root, "branch", "-D", branch, check=False)

    def _stale_worktrees(self, runtime_worktrees: Path, role_slug: str) -> list[Path]:
        result = self._run_git(self.memory_root, "worktree", "list", "--porcelain", check=False)
        if result.returncode != 0:
            return []

        worktrees: list[Path] = []
        for entry in _worktree_entries(result.stdout):
            worktree = entry.get("worktree")
            branch_ref = entry.get("branch", "")
            branch = branch_ref.removeprefix("refs/heads/")
            if worktree is None or not _is_temp_branch_for_role(branch, role_slug):
                continue
            path = Path(worktree).resolve()
            try:
                path.relative_to(runtime_worktrees.resolve())
            except ValueError:
                continue
            worktrees.append(path)
        return worktrees

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


def _safe_role_slug(role: str) -> str:
    slug = ROLE_SAFE_RE.sub("-", role.strip()).strip(".-")
    if not slug:
        return "role"
    return slug[:48].rstrip(".-") or "role"


def _is_temp_branch_for_role(branch: str, role_slug: str) -> bool:
    return bool(
        re.fullmatch(
            rf"{re.escape(TEMP_BRANCH_PREFIX)}{re.escape(role_slug)}-[0-9a-f]{{32}}",
            branch,
        )
    )


def _is_memory_write_path(path: str) -> bool:
    return (
        path == "MEMORY.md"
        or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))
        or bool(DREAM_LOG_FILE_RE.fullmatch(path))
    )


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
