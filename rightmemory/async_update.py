from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


@dataclass(frozen=True)
class AsyncUpdateJob:
    id: int
    message: str
    submitted_at: str


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
    current: AsyncUpdateJob | None = None
    queued: list[AsyncUpdateJob] = field(default_factory=list)
    next_id: int = 1


class AsyncUpdateStore:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "async" / role

    def read(self, session_id: str) -> AsyncUpdateState:
        with self._locked(session_id):
            return self._read_checked_locked(session_id)

    def submit(self, session_id: str, message: str) -> AsyncUpdateState:
        with self._locked(session_id):
            current = self._read_checked_locked(session_id)
            job = AsyncUpdateJob(id=current.next_id, message=message, submitted_at=_now())
            should_start_worker = not self._has_active_worker(current)
            state = self._enqueue_locked(current, job, start_worker=should_start_worker)
            self._write(session_id, state)

        if not should_start_worker:
            return state

        try:
            process = subprocess.Popen(
                self._worker_command(session_id),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=self.memory_root,
                env=os.environ.copy(),
            )
        except Exception as exc:
            self._fail_if_current(session_id, job.id, str(exc))
            raise
        return self._record_worker_pid(session_id, job.id, process.pid)

    def run_queued(self, session_id: str, run_message: Callable[[str], str]) -> AsyncUpdateState:
        self._record_worker_pid(session_id, None, os.getpid())
        while True:
            with self._locked(session_id):
                state = self._read_raw(session_id)
                if state.status != "running":
                    return state
                if state.current is None:
                    if not state.queued:
                        idle = replace(state, status="idle", pid=None, started_at=None, finished_at=None)
                        self._write(session_id, idle)
                        return idle
                    state = self._start_next_locked(state)
                    self._write(session_id, state)
                job = state.current
            try:
                result = run_message(job.message)
            except Exception as exc:
                return self._fail_if_current(session_id, job.id, str(exc))
            state = self._finish_current(session_id, job.id, result)
            if state.status != "running":
                return state

    def _worker_command(self, session_id: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "rightmemory.cli",
            self.role,
            "_submitted-worker",
            "--session",
            session_id,
        ]

    def _read_checked_locked(self, session_id: str) -> AsyncUpdateState:
        state = self._read_raw(session_id)
        if state.status == "running" and state.pid is not None and not _process_exists(state.pid):
            return self._fail_locked(session_id, f"worker process exited before writing result: pid {state.pid}")
        return state

    def _enqueue_locked(self, state: AsyncUpdateState, job: AsyncUpdateJob, *, start_worker: bool) -> AsyncUpdateState:
        next_id = max(state.next_id, job.id + 1)
        if not start_worker:
            return replace(state, queued=[*state.queued, job], next_id=next_id)
        jobs = [*state.queued, job]
        return AsyncUpdateState(
            status="running",
            session_id=state.session_id,
            role=self.role,
            started_at=_now(),
            pid=None,
            current=jobs[0],
            queued=jobs[1:],
            next_id=next_id,
        )

    def _has_active_worker(self, state: AsyncUpdateState) -> bool:
        if state.status != "running":
            return False
        return state.pid is None or _process_exists(state.pid)

    def _start_next_locked(self, state: AsyncUpdateState) -> AsyncUpdateState:
        return replace(
            state,
            status="running",
            started_at=_now(),
            finished_at=None,
            current=state.queued[0],
            queued=state.queued[1:],
            error=None,
        )

    def _finish_current(self, session_id: str, job_id: int, result: str) -> AsyncUpdateState:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            if state.current is None or state.current.id != job_id:
                return state
            if state.queued:
                next_state = replace(
                    state,
                    status="running",
                    started_at=_now(),
                    finished_at=None,
                    pid=os.getpid(),
                    current=state.queued[0],
                    queued=state.queued[1:],
                    result=result,
                    error=None,
                )
            else:
                next_state = replace(
                    state,
                    status="succeeded",
                    finished_at=_now(),
                    pid=os.getpid(),
                    current=None,
                    queued=[],
                    result=result,
                    error=None,
                )
            self._write(session_id, next_state)
            return next_state

    def _record_worker_pid(self, session_id: str, job_id: int | None, pid: int) -> AsyncUpdateState:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            current_matches = job_id is None or (state.current is not None and state.current.id == job_id)
            if state.status == "running" and current_matches:
                state = replace(state, pid=pid)
                self._write(session_id, state)
            return state

    def _fail_if_current(self, session_id: str, job_id: int, error: str) -> AsyncUpdateState:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            if state.current is not None and state.current.id != job_id:
                return state
            return self._fail_locked(session_id, error)

    def _fail_locked(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        state = replace(
            current,
            status="failed",
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
        return _state_from_json(data, fallback_session_id=session_id, fallback_role=self.role)

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


def _state_from_json(data: dict[str, object], *, fallback_session_id: str, fallback_role: str) -> AsyncUpdateState:
    current = _job_from_json(data.get("current"))
    queued = [_job for item in data.get("queued", []) if (_job := _job_from_json(item)) is not None]
    next_id = data.get("next_id")
    if not isinstance(next_id, int):
        known_ids = [job.id for job in queued]
        if current is not None:
            known_ids.append(current.id)
        next_id = max(known_ids, default=0) + 1
    return AsyncUpdateState(
        status=str(data.get("status", "idle")),
        session_id=str(data.get("session_id", fallback_session_id)),
        role=str(data.get("role", fallback_role)),
        started_at=_optional_str(data.get("started_at")),
        finished_at=_optional_str(data.get("finished_at")),
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        result=_optional_str(data.get("result")),
        error=_optional_str(data.get("error")),
        current=current,
        queued=queued,
        next_id=next_id,
    )


def _job_from_json(data: object) -> AsyncUpdateJob | None:
    if not isinstance(data, dict):
        return None
    job_id = data.get("id")
    message = data.get("message")
    submitted_at = data.get("submitted_at")
    if not isinstance(job_id, int) or not isinstance(message, str) or not isinstance(submitted_at, str):
        return None
    return AsyncUpdateJob(id=job_id, message=message, submitted_at=submitted_at)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def format_state(state: AsyncUpdateState) -> str:
    lines = [f"status: {state.status}", f"session: {state.session_id}"]
    if state.started_at:
        lines.append(f"started_at: {state.started_at}")
    if state.finished_at:
        lines.append(f"finished_at: {state.finished_at}")
    if state.pid is not None and state.status == "running":
        lines.append(f"pid: {state.pid}")
    if state.current is not None:
        lines.append(f"current_id: {state.current.id}")
    lines.append(f"queued: {len(state.queued)}")
    if state.queued:
        lines.append(f"queued_ids: {', '.join(str(job.id) for job in state.queued)}")
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
