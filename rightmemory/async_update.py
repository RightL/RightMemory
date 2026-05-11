from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


@dataclass(frozen=True)
class AsyncUpdateState:
    status: str
    session_id: str
    role: str
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    result: str | None = None
    error: str | None = None


class AsyncUpdateStore:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "async" / role

    def read(self, session_id: str) -> AsyncUpdateState:
        state = self._read_raw(session_id)
        if state.status == "running" and state.pid is not None and not _process_exists(state.pid):
            return self.fail(session_id, f"worker process exited before writing result: pid {state.pid}")
        return state

    def submit(self, session_id: str, message: str) -> AsyncUpdateState:
        with self._locked(session_id):
            current = self.read(session_id)
            if current.status == "running":
                raise ValueError(f"update already running for session: {session_id}")

            state = AsyncUpdateState(
                status="running",
                session_id=session_id,
                role=self.role,
                started_at=_now(),
                pid=None,
            )
            self._write(session_id, state)

            command = [
                sys.executable,
                "-m",
                "rightmemory.cli",
                self.role,
                "_submitted-worker",
                "--session",
                session_id,
                message,
            ]
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=self.memory_root,
                    env=os.environ.copy(),
                )
            except Exception as exc:
                self.fail(session_id, str(exc))
                raise
            state = AsyncUpdateState(
                status="running",
                session_id=session_id,
                role=self.role,
                started_at=state.started_at,
                pid=process.pid,
            )
            self._write(session_id, state)
            return state

    def finish(self, session_id: str, result: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        state = AsyncUpdateState(
            status="succeeded",
            session_id=session_id,
            role=self.role,
            started_at=current.started_at,
            finished_at=_now(),
            pid=os.getpid(),
            result=result,
        )
        self._write(session_id, state)
        return state

    def fail(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        state = AsyncUpdateState(
            status="failed",
            session_id=session_id,
            role=self.role,
            started_at=current.started_at,
            finished_at=_now(),
            pid=os.getpid(),
            error=error,
        )
        self._write(session_id, state)
        return state

    def _state_path(self, session_id: str) -> Path:
        safe_id = _safe_session_id(session_id)
        return self.root / f"{safe_id}.json"

    def _read_raw(self, session_id: str) -> AsyncUpdateState:
        state_path = self._state_path(session_id)
        if not state_path.exists():
            return AsyncUpdateState(status="idle", session_id=session_id, role=self.role)
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return AsyncUpdateState(**data)

    def _lock_path(self, session_id: str) -> Path:
        safe_id = _safe_session_id(session_id)
        return self.root / f"{safe_id}.lock"

    def _write(self, session_id: str, state: AsyncUpdateState) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.root.mkdir(parents=True, exist_ok=True)
        state_path = self._state_path(session_id)
        tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        _fsync_directory(state_path.parent)

    @contextmanager
    def _locked(self, session_id: str):
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(session_id)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def format_state(state: AsyncUpdateState) -> str:
    lines = [f"status: {state.status}", f"session: {state.session_id}"]
    if state.started_at:
        lines.append(f"started_at: {state.started_at}")
    if state.finished_at:
        lines.append(f"finished_at: {state.finished_at}")
    if state.pid is not None and state.status == "running":
        lines.append(f"pid: {state.pid}")
    if state.result:
        lines.append(f"result: {state.result}")
    if state.error:
        lines.append(f"error: {state.error}")
    return "\n".join(lines)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
