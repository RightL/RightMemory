from __future__ import annotations

import os
import subprocess
from pathlib import Path


GIT_TIMEOUT_SECONDS = 30
ACTIVE_MEMORY_PATHS = ("MEMORY.md", ":(glob)MEMORY_*.md")


def current_active_memory_commit(memory_root: Path) -> str:
    root = Path(memory_root).resolve()
    return _git_stdout(root, "log", "-1", "--format=%H", "HEAD", "--", *ACTIVE_MEMORY_PATHS)


def active_memory_commit_count(memory_root: Path, revision: str) -> int:
    root = Path(memory_root).resolve()
    output = _git_stdout(root, "rev-list", "--count", revision, "--", *ACTIVE_MEMORY_PATHS)
    return int(output or "0")


def first_generation_active_memory_boundary(memory_root: Path, generation_commits: int) -> str:
    if generation_commits < 1:
        raise ValueError("generation_commits must be positive")
    root = Path(memory_root).resolve()
    commits = _git_stdout(root, "rev-list", "--reverse", "HEAD", "--", *ACTIVE_MEMORY_PATHS).splitlines()
    if not commits:
        return _git_stdout(root, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]
    if len(commits) <= generation_commits:
        return commits[0]
    return commits[-generation_commits - 1]


def _git_stdout(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    return result.stdout.strip()


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result
