from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id

UPDATE_DEBOUNCE_SECONDS = 60 * 60


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
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    result: str | None = None
    error: str | None = None
    next_flush_at: str | None = None
    current_batch: list[AsyncUpdateJob] = field(default_factory=list)
    pending: list[AsyncUpdateJob] = field(default_factory=list)
    next_id: int = 1


class AsyncUpdateStore:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "async" / role

    def read(self, session_id: str) -> AsyncUpdateState:
        with self._locked(session_id):
            return self._read_checked_locked(session_id)

    def cancel_pending(self, session_id: str, candidate_id: int) -> tuple[AsyncUpdateState, bool]:
        if not isinstance(candidate_id, int) or isinstance(candidate_id, bool) or candidate_id < 1:
            raise ValueError("candidate id must be a positive integer")
        with self._locked(session_id):
            state = self._read_checked_locked(session_id)
            pending = [job for job in state.pending if job.id != candidate_id]
            canceled = len(pending) != len(state.pending)
            if canceled:
                state = replace(state, pending=pending)
                self._write(session_id, state)
            return state, canceled

    def submit(self, session_id: str, message: str) -> AsyncUpdateState:
        now = _now_dt()
        with self._locked(session_id):
            current = self._read_checked_locked(session_id)
            job = AsyncUpdateJob(id=current.next_id, message=message, submitted_at=_format_time(now))
            should_start_worker = not self._has_active_worker(current)
            state = self._enqueue_locked(current, job, start_worker=should_start_worker, now=now)
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
            self._fail(session_id, str(exc))
            raise
        return self._record_worker_pid(session_id, process.pid)

    def run_pending_batches(
        self,
        session_id: str,
        run_message: Callable[[str], str],
        *,
        sleep_until: Callable[[datetime], None] | None = None,
        on_batch_success: Callable[[int], None] | None = None,
    ) -> AsyncUpdateState:
        sleep_until = _sleep_until if sleep_until is None else sleep_until
        self._record_worker_pid(session_id, os.getpid())
        while True:
            batch: list[AsyncUpdateJob] | None = None
            deadline: datetime | None = None
            with self._locked(session_id):
                state = self._read_raw(session_id)
                if state.status != "running":
                    return state
                if state.phase == "waiting":
                    if not state.pending:
                        idle = replace(
                            state,
                            status="idle",
                            phase=None,
                            pid=None,
                            started_at=None,
                            finished_at=None,
                            next_flush_at=None,
                            current_batch=[],
                        )
                        self._write(session_id, idle)
                        return idle
                    deadline = _required_time(state.next_flush_at, "next_flush_at")
                    if deadline <= _now_dt():
                        state = self._start_pending_batch_locked(state)
                        self._write(session_id, state)
                        batch = state.current_batch
                elif state.phase == "running":
                    if not state.current_batch:
                        state = self._start_wait_or_idle_locked(state)
                        self._write(session_id, state)
                        continue
                    batch = state.current_batch
                else:
                    return self._fail_locked(session_id, f"invalid async update phase: {state.phase}")

            if deadline is not None and batch is None:
                sleep_until(deadline)
                continue
            if batch is None:
                continue

            batch_ids = [job.id for job in batch]
            try:
                result = run_message(_format_batch_message(batch))
            except Exception as exc:
                return self._fail_if_current_batch(session_id, batch_ids, str(exc))
            state, accepted = self._finish_current(session_id, batch_ids, result)
            if accepted and on_batch_success is not None:
                on_batch_success(len(batch))
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

    def _enqueue_locked(
        self,
        state: AsyncUpdateState,
        job: AsyncUpdateJob,
        *,
        start_worker: bool,
        now: datetime,
    ) -> AsyncUpdateState:
        pending = [*state.current_batch, *state.pending, job] if start_worker else [*state.pending, job]
        next_id = max(state.next_id, job.id + 1)
        next_flush_at = _format_time(now + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS))
        if not start_worker:
            return replace(state, pending=pending, next_id=next_id, next_flush_at=next_flush_at)
        return AsyncUpdateState(
            status="running",
            session_id=state.session_id,
            role=self.role,
            phase="waiting",
            started_at=_format_time(now),
            pid=None,
            next_flush_at=next_flush_at,
            pending=pending,
            next_id=next_id,
        )

    def _has_active_worker(self, state: AsyncUpdateState) -> bool:
        if state.status != "running":
            return False
        return state.pid is None or _process_exists(state.pid)

    def _start_pending_batch_locked(self, state: AsyncUpdateState) -> AsyncUpdateState:
        return replace(
            state,
            phase="running",
            started_at=_now(),
            finished_at=None,
            current_batch=state.pending,
            pending=[],
            next_flush_at=None,
            result=None,
            error=None,
        )

    def _start_wait_or_idle_locked(self, state: AsyncUpdateState) -> AsyncUpdateState:
        if not state.pending:
            return replace(
                state,
                status="idle",
                phase=None,
                pid=None,
                started_at=None,
                finished_at=None,
                next_flush_at=None,
            )
        next_flush_at = state.next_flush_at or _format_time(_now_dt() + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS))
        return replace(
            state,
            phase="waiting",
            started_at=_now(),
            finished_at=None,
            current_batch=[],
            next_flush_at=next_flush_at,
            error=None,
        )

    def _finish_current(self, session_id: str, batch_ids: list[int], result: str) -> tuple[AsyncUpdateState, bool]:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            if [job.id for job in state.current_batch] != batch_ids:
                return state, False
            if state.pending:
                next_flush_at = state.next_flush_at or _format_time(
                    _now_dt() + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS)
                )
                next_state = replace(
                    state,
                    phase="waiting",
                    started_at=_now(),
                    finished_at=None,
                    pid=os.getpid(),
                    current_batch=[],
                    next_flush_at=next_flush_at,
                    result=result,
                    error=None,
                )
            else:
                next_state = replace(
                    state,
                    status="succeeded",
                    phase=None,
                    finished_at=_now(),
                    pid=os.getpid(),
                    current_batch=[],
                    pending=[],
                    next_flush_at=None,
                    result=result,
                    error=None,
                )
            self._write(session_id, next_state)
            return next_state, True

    def _record_worker_pid(self, session_id: str, pid: int) -> AsyncUpdateState:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            if state.status == "running":
                state = replace(state, pid=pid)
                self._write(session_id, state)
            return state

    def _fail(self, session_id: str, error: str) -> AsyncUpdateState:
        with self._locked(session_id):
            return self._fail_locked(session_id, error)

    def _fail_if_current_batch(self, session_id: str, batch_ids: list[int], error: str) -> AsyncUpdateState:
        with self._locked(session_id):
            state = self._read_raw(session_id)
            if [job.id for job in state.current_batch] != batch_ids:
                return state
            return self._fail_locked(session_id, error)

    def _fail_locked(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        retry_pending = [*current.current_batch, *current.pending]
        state = replace(
            current,
            status="failed",
            phase=None,
            finished_at=_now(),
            pid=os.getpid(),
            error=error,
            next_flush_at=None,
            current_batch=[],
            pending=retry_pending,
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
        return _state_from_json(data)

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


def _state_from_json(data: dict[str, object]) -> AsyncUpdateState:
    if "current" in data or "queued" in data:
        raise ValueError("async update state uses unsupported legacy job fields")
    status = _required_state_str(data, "status")
    session_id = _required_state_str(data, "session_id")
    role = _required_state_str(data, "role")
    next_id = data.get("next_id")
    if not isinstance(next_id, int):
        raise ValueError("async update state must contain integer field: next_id")
    return AsyncUpdateState(
        status=status,
        session_id=session_id,
        role=role,
        phase=_optional_str(data.get("phase")),
        started_at=_optional_str(data.get("started_at")),
        finished_at=_optional_str(data.get("finished_at")),
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        result=_optional_str(data.get("result")),
        error=_optional_str(data.get("error")),
        next_flush_at=_optional_str(data.get("next_flush_at")),
        current_batch=_required_job_list(data, "current_batch"),
        pending=_required_job_list(data, "pending"),
        next_id=next_id,
    )


def _required_job_list(data: dict[str, object], key: str) -> list[AsyncUpdateJob]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"async update state must contain list field: {key}")
    jobs = [_job_from_json(item) for item in value]
    if any(job is None for job in jobs):
        raise ValueError(f"async update state must contain valid job list field: {key}")
    return [job for job in jobs if job is not None]


def _job_from_json(data: object) -> AsyncUpdateJob | None:
    if not isinstance(data, dict):
        return None
    job_id = data.get("id")
    message = data.get("message")
    submitted_at = data.get("submitted_at")
    if not isinstance(job_id, int) or not isinstance(message, str) or not isinstance(submitted_at, str):
        return None
    return AsyncUpdateJob(id=job_id, message=message, submitted_at=submitted_at)


def _required_state_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"async update state must contain string field: {key}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def format_state(state: AsyncUpdateState) -> str:
    lines = [f"status: {state.status}", f"session: {state.session_id}"]
    if state.phase:
        lines.append(f"phase: {state.phase}")
    if state.started_at:
        lines.append(f"started_at: {state.started_at}")
    if state.next_flush_at:
        lines.append(f"next_flush_at: {state.next_flush_at}")
    if state.finished_at:
        lines.append(f"finished_at: {state.finished_at}")
    if state.pid is not None and state.status == "running":
        lines.append(f"pid: {state.pid}")
    lines.append(f"current_batch: {len(state.current_batch)}")
    if state.current_batch:
        lines.append(f"current_batch_ids: {', '.join(str(job.id) for job in state.current_batch)}")
    lines.append(f"pending: {len(state.pending)}")
    if state.pending:
        lines.append(f"pending_ids: {', '.join(str(job.id) for job in state.pending)}")
    if state.result:
        lines.append(f"result: {state.result}")
    if state.error:
        lines.append(f"error: {state.error}")
    return "\n".join(lines)


def _format_batch_message(jobs: list[AsyncUpdateJob]) -> str:
    lines = [
        "Process the following submitted memory update candidates as one batch.",
        "Use the standalone update instructions to decide what should become durable memory.",
        "",
        "Candidates:",
    ]
    for job in jobs:
        lines.append(f"[candidate {job.id} | submitted_at: {job.submitted_at}]")
        lines.extend(job.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


def _required_time(value: str | None, field: str) -> datetime:
    if value is None:
        raise ValueError(f"async update state must contain datetime field: {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"async update state must contain datetime field: {field}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sleep_until(deadline: datetime) -> None:
    seconds = (deadline - _now_dt()).total_seconds()
    if seconds > 0:
        time.sleep(seconds)


def _now() -> str:
    return _format_time(_now_dt())


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
