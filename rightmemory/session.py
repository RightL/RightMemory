from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionPaths:
    runtime_root: Path
    history: Path
    lock: Path


class MessageSessionStore:
    def __init__(self, memory_root: Path, role: str):
        self.root = memory_root / ".runtime" / "sessions" / role

    def paths(self, session_id: str) -> SessionPaths:
        safe_id = _safe_session_id(session_id)
        runtime_root = self.root.parent.parent
        return SessionPaths(
            runtime_root=runtime_root,
            history=self.root / f"{safe_id}.json",
            lock=self.root / f"{safe_id}.lock",
        )

    def locked(self, session_id: str) -> LockedMessageSession:
        return LockedMessageSession(self.paths(session_id))


class MemoryWriteLock:
    def __init__(self, memory_root: Path):
        self.runtime_root = memory_root / ".runtime"
        self.lock_path = self.runtime_root / "memory.lock"
        self._lock_handle: Any | None = None

    def __enter__(self) -> MemoryWriteLock:
        _ensure_memory_gitignore(self.runtime_root.parent)
        _ensure_runtime_gitignore(self.runtime_root)
        self._lock_handle = self.lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None


class LockedMessageSession:
    def __init__(self, paths: SessionPaths):
        self.paths = paths
        self._lock_handle: Any | None = None

    def __enter__(self) -> LockedMessageSession:
        _ensure_runtime_gitignore(self.paths.runtime_root)
        self.paths.lock.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.paths.lock.open("a+", encoding="utf-8")
        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None

    def load_json(self) -> bytes | None:
        if not self.paths.history.exists():
            return None
        data = self.paths.history.read_bytes()
        if not data:
            raise ValueError(f"session history is empty: {self.paths.history}")
        return data

    def save_json(self, data: bytes) -> None:
        if not data:
            raise ValueError("session history must not be empty")
        self.paths.history.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.paths.history.with_name(f".{self.paths.history.name}.{os.getpid()}.tmp")
        with tmp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.paths.history)
        _fsync_directory(self.paths.history.parent)


def _safe_session_id(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        raise ValueError("session id must not be empty")
    if value in {".", ".."}:
        raise ValueError("session id must not be a relative path segment")
    if any(character in value for character in "/\\"):
        raise ValueError("session id must not contain path separators")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_memory_gitignore(memory_root: Path) -> None:
    _write_gitignore_if_missing(
        memory_root,
        b"*\n!MEMORY.md\n!MEMORY_*.md\n!shared_views.toml\n!insight_logs/\n!insight_logs/*.md\n",
    )


def _ensure_runtime_gitignore(runtime_root: Path) -> None:
    _write_gitignore_if_missing(runtime_root, b"*\n")


def _write_gitignore_if_missing(directory: Path, content: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if gitignore.exists():
        return
    tmp_path = directory / f".gitignore.{os.getpid()}.tmp"
    with tmp_path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp_path, gitignore)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(directory)
