"""Small Git primitives for program-owned state transactions."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def require_repository_root(root: Path) -> None:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise RuntimeError(f"memory root is not the git repository root: {root}")


def git(cwd: Path, *args: str) -> str:
    result = git_result(cwd, *args)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="strict",
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, check=False, timeout=30,
    )
