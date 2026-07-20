from __future__ import annotations

import hashlib
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
from typing import TextIO

from .platform import (
    detached_process_kwargs,
    lock_file,
    lock_file_nonblocking,
    process_command,
    process_exists,
    process_identity,
    unlock_file,
)
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id

UPDATE_DEBOUNCE_SECONDS = 60 * 60
UPDATE_RETRY_COOLDOWN_SECONDS = 60 * 60
UPDATE_MAX_AUTOMATIC_ATTEMPTS = 2
WORKER_IDLE_POLL_SECONDS = 30
STATUS_MANUAL_RECOVERY = "needs_manual_recovery"
MANUAL_RECOVERY_WARNING = (
    "CRITICAL: this async RightMemory update session is blocked after "
    f"{UPDATE_MAX_AUTOMATIC_ATTEMPTS} failed attempts.\n"
    "The new candidate was saved, but this session will not be processed until manual recovery runs.\n"
    "Agent: report this issue to the user and suggest `rightmemory update retry`."
)


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
    attempts: int = 0
    next_retry_at: str | None = None
    last_error: str | None = None
    next_flush_at: str | None = None
    current_batch: list[AsyncUpdateJob] = field(default_factory=list)
    pending: list[AsyncUpdateJob] = field(default_factory=list)
    next_id: int = 1


@dataclass(frozen=True)
class AsyncUpdateSessionBatch:
    session_id: str
    ready_at: datetime
    jobs: list[AsyncUpdateJob]


@dataclass(frozen=True)
class AsyncUpdateWorkerResult:
    status: str
    processed: int = 0
    failed: bool = False


@dataclass(frozen=True)
class AsyncUpdateRetryResult:
    requeued_sessions: int = 0
    requeued_candidates: int = 0
    skipped_sessions: int = 0
    worker_pid: int | None = None
    worker_action: str = "not started"
    worker_error: str | None = None


@dataclass(frozen=True)
class _WorkerSnapshot:
    pid: int | None = None
    batch_id: str | None = None
    session_ids: frozenset[str] = frozenset()
    dead_pid: int | None = None


class AsyncUpdateStore:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "async" / role
        self.worker_root = self.root / "_worker"

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

    def retry_manual_recovery(self) -> AsyncUpdateRetryResult:
        now = _now_dt()
        requeued_sessions = 0
        requeued_candidates = 0
        skipped_sessions = 0
        first_session_id: str | None = None
        requeued_session_ids: list[str] = []

        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                try:
                    state = self._read_checked_locked(session_id)
                except (OSError, json.JSONDecodeError, ValueError):
                    skipped_sessions += 1
                    continue
                if state.status != STATUS_MANUAL_RECOVERY:
                    continue
                retry_pending = [*state.current_batch, *state.pending]
                if not retry_pending:
                    skipped_sessions += 1
                    continue
                next_state = replace(
                    state,
                    status="failed",
                    phase=None,
                    finished_at=None,
                    pid=None,
                    error=None,
                    attempts=0,
                    next_retry_at=_format_time(now),
                    last_error=None,
                    current_batch=[],
                    pending=retry_pending,
                )
                self._write(session_id, next_state)
                requeued_sessions += 1
                requeued_candidates += len(retry_pending)
                requeued_session_ids.append(session_id)
                if first_session_id is None:
                    first_session_id = session_id

        worker_pid = None
        worker_action = "not started"
        worker_error = None
        if first_session_id is not None:
            try:
                before_pid = self._active_worker_pid()
                self._start_worker_if_needed(first_session_id)
            except Exception as exc:
                worker_error = f"{type(exc).__name__}: {exc}"
                self._restore_manual_recovery(requeued_session_ids, worker_error)
                requeued_sessions = 0
                requeued_candidates = 0
                worker_action = "failed"
            else:
                worker_pid = self._active_worker_pid()
                if worker_pid is not None:
                    worker_action = "woken" if before_pid is not None else "started"
        return AsyncUpdateRetryResult(
            requeued_sessions=requeued_sessions,
            requeued_candidates=requeued_candidates,
            skipped_sessions=skipped_sessions,
            worker_pid=worker_pid,
            worker_action=worker_action,
            worker_error=worker_error,
        )

    def submit(self, session_id: str, message: str) -> AsyncUpdateState:
        now = _now_dt()
        with self._locked(session_id):
            current = self._read_checked_locked(session_id)
            job = AsyncUpdateJob(id=current.next_id, message=message, submitted_at=_format_time(now))
            worker_pid = self._active_worker_pid()
            state = self._enqueue_locked(current, job, now=now, worker_pid=worker_pid)
            self._write(session_id, state)

        if state.status != STATUS_MANUAL_RECOVERY:
            self._start_worker_if_needed(session_id)
        return state

    def ensure_worker(self, session_id: str) -> AsyncUpdateState:
        state = self.read(session_id)
        if not state.pending and not state.current_batch:
            return state
        if state.status != STATUS_MANUAL_RECOVERY:
            self._start_worker_if_needed(session_id)
        return self.read(session_id)

    def run_pending_batches(
        self,
        run_message: Callable[[str, str], str],
        *,
        target_batch_candidates: int,
        max_wait_seconds: int,
        sleep_until: Callable[[datetime], None] | None = None,
        on_batch_success: Callable[[int], None] | None = None,
    ) -> AsyncUpdateWorkerResult:
        leader_handle = self._try_acquire_worker_leader()
        if leader_handle is None:
            return AsyncUpdateWorkerResult(status="idle")
        sleep_until = _sleep_until if sleep_until is None else sleep_until
        with self._worker_locked():
            self._write_worker_locked(
                status="running",
                pid=os.getpid(),
                batch_id=None,
                session_ids=[],
                error=None,
            )

        processed = 0
        manual_failure = False
        worker_state_cleared = False
        try:
            while True:
                wake_counter = self._read_wake_counter()
                batch, deadline = self._next_batch(target_batch_candidates, max_wait_seconds)
                if batch is None:
                    if deadline is None:
                        with self._worker_locked():
                            if self._read_wake_counter_locked() != wake_counter:
                                continue
                            self._clear_current_worker_locked()
                            worker_state_cleared = True
                        status = "failed" if manual_failure else "succeeded" if processed else "idle"
                        return AsyncUpdateWorkerResult(status=status, processed=processed, failed=manual_failure)
                    sleep_until(min(deadline, _now_dt() + timedelta(seconds=WORKER_IDLE_POLL_SECONDS)))
                    continue

                batch_id = _batch_session_id(batch)
                session_ids = [item.session_id for item in batch]
                with self._worker_locked():
                    self._write_worker_locked(
                        status="running",
                        pid=os.getpid(),
                        batch_id=batch_id,
                        session_ids=session_ids,
                        error=None,
                    )
                started = self._start_cross_session_batch(batch)
                if not started:
                    with self._worker_locked():
                        self._write_worker_locked(
                            status="running",
                            pid=os.getpid(),
                            batch_id=None,
                            session_ids=[],
                            error=None,
                        )
                    continue
                batch = started

                batch_id = _batch_session_id(batch)
                session_ids = [item.session_id for item in batch]
                with self._worker_locked():
                    self._write_worker_locked(
                        status="running",
                        pid=os.getpid(),
                        batch_id=batch_id,
                        session_ids=session_ids,
                        error=None,
                    )

                try:
                    result = run_message(batch_id, _format_batch_message(batch))
                except Exception as exc:
                    failed_states = self._fail_cross_session_batch(batch, str(exc))
                    manual_failure = manual_failure or any(
                        state.status == STATUS_MANUAL_RECOVERY for state in failed_states
                    )
                    with self._worker_locked():
                        self._write_worker_locked(
                            status="running",
                            pid=os.getpid(),
                            batch_id=None,
                            session_ids=[],
                            error=None,
                        )
                    if any(state.status == "failed" and state.pending for state in failed_states):
                        continue
                    return AsyncUpdateWorkerResult(status="failed", processed=processed, failed=True)

                accepted_count = self._finish_cross_session_batch(batch, result)
                if accepted_count:
                    processed += accepted_count
                    if on_batch_success is not None:
                        on_batch_success(accepted_count)
        finally:
            try:
                if not worker_state_cleared:
                    with self._worker_locked():
                        self._clear_current_worker_locked()
            finally:
                unlock_file(leader_handle)
                leader_handle.close()

    def _next_batch(
        self,
        target_batch_candidates: int,
        max_wait_seconds: int,
    ) -> tuple[list[AsyncUpdateSessionBatch] | None, datetime | None]:
        now = _now_dt()
        recovery: list[AsyncUpdateSessionBatch] = []
        eligible: list[AsyncUpdateSessionBatch] = []
        future_deadlines: list[datetime] = []

        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                if state.role != self.role:
                    continue
                if state.current_batch or not state.pending:
                    continue
                if state.status == "failed":
                    ready_at = _required_time(state.next_retry_at, "next_retry_at")
                    if ready_at <= now:
                        recovery.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                    else:
                        future_deadlines.append(ready_at)
                    continue
                if state.status != "running" or state.phase != "waiting":
                    continue
                ready_at = _required_time(state.next_flush_at, "next_flush_at")
                pressure_ready = len(state.pending) >= target_batch_candidates
                if ready_at <= now or pressure_ready:
                    eligible.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                else:
                    future_deadlines.append(ready_at)

        recovery.sort(key=lambda item: (item.ready_at, item.session_id))
        if recovery:
            return recovery, None

        eligible.sort(key=lambda item: (item.ready_at, item.session_id))
        if not eligible:
            return None, min(future_deadlines) if future_deadlines else None

        selected: list[AsyncUpdateSessionBatch] = []
        total = 0
        for item in eligible:
            selected.append(item)
            total += len(item.jobs)
            if total >= target_batch_candidates:
                return selected, None

        fallback_at = eligible[0].ready_at + timedelta(seconds=max_wait_seconds)
        if now >= fallback_at:
            return selected, None

        deadlines = [fallback_at, *future_deadlines]
        return None, min(deadlines)

    def _start_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch]) -> list[AsyncUpdateSessionBatch]:
        started: list[AsyncUpdateSessionBatch] = []
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.pending[: len(expected_ids)]] != expected_ids:
                    continue
                current_batch = state.pending[: len(expected_ids)]
                pending = state.pending[len(expected_ids):]
                next_state = replace(
                    state,
                    status="running",
                    phase="running",
                    started_at=_now(),
                    finished_at=None,
                    current_batch=current_batch,
                    pending=pending,
                    next_flush_at=state.next_flush_at if pending else None,
                    result=None,
                    error=None,
                    pid=os.getpid(),
                )
                self._write(item.session_id, next_state)
                started.append(AsyncUpdateSessionBatch(item.session_id, item.ready_at, current_batch))
        return started

    def _finish_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch], result: str) -> int:
        accepted = 0
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.current_batch] != expected_ids:
                    continue
                accepted += len(state.current_batch)
                if state.pending:
                    next_flush_at = state.next_flush_at or _format_time(
                        _now_dt() + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS)
                    )
                    next_state = replace(
                        state,
                        status="running",
                        phase="waiting",
                        started_at=_now(),
                        finished_at=None,
                        pid=os.getpid(),
                        current_batch=[],
                        next_flush_at=next_flush_at,
                        result=result,
                        error=None,
                        attempts=0,
                        next_retry_at=None,
                        last_error=None,
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
                        attempts=0,
                        next_retry_at=None,
                        last_error=None,
                    )
                self._write(item.session_id, next_state)
        return accepted

    def _fail_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch], error: str) -> list[AsyncUpdateState]:
        failed_states: list[AsyncUpdateState] = []
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.current_batch] != expected_ids:
                    continue
                failed_states.append(self._fail_locked(item.session_id, error))
        return failed_states

    def _worker_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "rightmemory.cli",
            self.role,
            "_async-worker",
        ]

    def _worker_state_path(self) -> Path:
        return self.worker_root / "state.json"

    def _worker_lock_path(self) -> Path:
        return self.worker_root / "state.lock"

    def _worker_wake_path(self) -> Path:
        return self.worker_root / "wake.json"

    def _worker_leader_lock_path(self) -> Path:
        return self.worker_root / "leader.lock"

    def _try_acquire_worker_leader(self) -> TextIO | None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.worker_root.mkdir(parents=True, exist_ok=True)
        handle = self._worker_leader_lock_path().open("a+", encoding="utf-8")
        try:
            lock_file_nonblocking(handle)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    @contextmanager
    def _worker_locked(self):
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.worker_root.mkdir(parents=True, exist_ok=True)
        lock_path = self._worker_lock_path()
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _read_worker_locked(self) -> dict[str, object]:
        path = self._worker_state_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("async update worker state must be a JSON object")
        return data

    def _read_wake_counter(self) -> int:
        with self._worker_locked():
            return self._read_wake_counter_locked()

    def _read_wake_counter_locked(self) -> int:
        path = self._worker_wake_path()
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("async update worker wake state must be a JSON object")
        counter = data.get("counter")
        if not isinstance(counter, int):
            raise ValueError("async update worker wake state must contain integer field: counter")
        return counter

    def _increment_wake_counter_locked(self) -> int:
        counter = self._read_wake_counter_locked() + 1
        path = self._worker_wake_path()
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        content = json.dumps({"counter": counter}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
        return counter

    def _write_worker_locked(
        self,
        *,
        status: str,
        pid: int | None,
        batch_id: str | None,
        session_ids: list[str],
        error: str | None,
    ) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.worker_root.mkdir(parents=True, exist_ok=True)
        state_path = self._worker_state_path()
        tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {
                "status": status,
                "pid": pid,
                "identity": process_identity(pid) if pid is not None else None,
                "started_at": _now(),
                "batch_id": batch_id,
                "session_ids": session_ids,
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        _fsync_directory(state_path.parent)

    def _clear_worker_locked(self) -> None:
        self._write_worker_locked(status="idle", pid=None, batch_id=None, session_ids=[], error=None)

    def _clear_current_worker_locked(self) -> None:
        state = self._read_worker_locked()
        pid = state.get("pid")
        if pid is None or pid == os.getpid():
            self._clear_worker_locked()

    def _active_worker_pid(self) -> int | None:
        return self._worker_snapshot().pid

    def _worker_snapshot(self) -> _WorkerSnapshot:
        with self._worker_locked():
            return self._worker_snapshot_locked()

    def _worker_snapshot_locked(self) -> _WorkerSnapshot:
        state = self._read_worker_locked()
        pid = state.get("pid")
        if not isinstance(pid, int):
            return _WorkerSnapshot()
        identity = state.get("identity")
        if not _is_async_worker_process(pid, self.role, identity=identity if isinstance(identity, str) else None):
            self._clear_worker_locked()
            return _WorkerSnapshot(dead_pid=pid)
        batch_id = state.get("batch_id")
        raw_session_ids = state.get("session_ids")
        session_ids: frozenset[str] = frozenset()
        if isinstance(raw_session_ids, list):
            session_ids = frozenset(item for item in raw_session_ids if isinstance(item, str))
        return _WorkerSnapshot(
            pid=pid,
            batch_id=batch_id if isinstance(batch_id, str) else None,
            session_ids=session_ids,
        )

    def _start_worker_if_needed(self, session_id: str) -> None:
        with self._worker_locked():
            self._increment_wake_counter_locked()
            state = self._read_worker_locked()
            pid = state.get("pid")
            identity = state.get("identity")
            if isinstance(pid, int) and _is_async_worker_process(
                pid,
                self.role,
                identity=identity if isinstance(identity, str) else None,
            ):
                return
            if isinstance(pid, int):
                self._clear_worker_locked()
            try:
                process = subprocess.Popen(
                    self._worker_command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=self.memory_root,
                    env=os.environ.copy(),
                    **detached_process_kwargs(),
                )
            except Exception as exc:
                self._fail(session_id, str(exc))
                raise
            self._write_worker_locked(
                status="running",
                pid=process.pid,
                batch_id=None,
                session_ids=[],
                error=None,
            )

    def _session_state_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(path for path in self.root.glob("*.json") if path.is_file())

    def _read_checked_locked(self, session_id: str) -> AsyncUpdateState:
        state = self._read_raw(session_id)
        if _is_legacy_failed_pending_state(state):
            return self._manual_recovery_locked(session_id, state)
        if state.status != "running":
            return state
        worker = self._worker_snapshot()
        if state.phase == "running" and state.current_batch:
            if worker.pid is not None and state.pid == worker.pid:
                return state
            return self._fail_locked(session_id, "worker process exited before writing result")
        if worker.pid is None and (state.pid is not None or worker.dead_pid is not None):
            return self._fail_locked(session_id, "worker process exited before writing result")
        return state

    def _enqueue_locked(
        self,
        state: AsyncUpdateState,
        job: AsyncUpdateJob,
        *,
        now: datetime,
        worker_pid: int | None,
    ) -> AsyncUpdateState:
        next_id = max(state.next_id, job.id + 1)
        next_flush_at = _format_time(now + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS))
        if state.status == "failed" and not state.current_batch and not state.pending:
            return AsyncUpdateState(
                status="running",
                session_id=state.session_id,
                role=self.role,
                phase="waiting",
                started_at=_format_time(now),
                pid=worker_pid,
                next_flush_at=next_flush_at,
                pending=[job],
                next_id=next_id,
            )
        if state.status in {"failed", STATUS_MANUAL_RECOVERY}:
            pending = [*state.current_batch, *state.pending, job]
            return replace(
                state,
                phase=None,
                pid=worker_pid,
                current_batch=[],
                pending=pending,
                next_id=next_id,
            )
        if (
            state.status == "running"
            and state.phase == "running"
            and state.current_batch
            and worker_pid is not None
            and state.pid == worker_pid
        ):
            return replace(
                state,
                pid=worker_pid,
                pending=[*state.pending, job],
                next_id=next_id,
                next_flush_at=next_flush_at,
                error=None,
            )
        pending = [*state.current_batch, *state.pending, job]
        return AsyncUpdateState(
            status="running",
            session_id=state.session_id,
            role=self.role,
            phase="waiting",
            started_at=state.started_at or _format_time(now),
            pid=worker_pid,
            next_flush_at=next_flush_at,
            pending=pending,
            next_id=next_id,
        )

    def _fail(self, session_id: str, error: str) -> AsyncUpdateState:
        with self._locked(session_id):
            return self._fail_locked(session_id, error)

    def _fail_locked(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        retry_pending = [*current.current_batch, *current.pending]
        attempts = current.attempts + 1
        manual = attempts >= UPDATE_MAX_AUTOMATIC_ATTEMPTS
        now = _now_dt()
        state = replace(
            current,
            status=STATUS_MANUAL_RECOVERY if manual else "failed",
            phase=None,
            finished_at=_format_time(now),
            pid=os.getpid(),
            error=error,
            attempts=attempts,
            next_retry_at=None if manual else _format_time(now + timedelta(seconds=UPDATE_RETRY_COOLDOWN_SECONDS)),
            last_error=error,
            next_flush_at=None,
            current_batch=[],
            pending=retry_pending,
        )
        self._write(session_id, state)
        return state

    def _manual_recovery_locked(self, session_id: str, state: AsyncUpdateState) -> AsyncUpdateState:
        retry_pending = [*state.current_batch, *state.pending]
        next_state = replace(
            state,
            status=STATUS_MANUAL_RECOVERY,
            phase=None,
            finished_at=state.finished_at or _now(),
            pid=os.getpid(),
            attempts=max(state.attempts, UPDATE_MAX_AUTOMATIC_ATTEMPTS),
            next_retry_at=None,
            last_error=state.last_error or state.error,
            current_batch=[],
            pending=retry_pending,
        )
        self._write(session_id, next_state)
        return next_state

    def _restore_manual_recovery(self, session_ids: list[str], error: str) -> None:
        for session_id in session_ids:
            with self._locked(session_id):
                state = self._read_raw(session_id)
                pending = [*state.current_batch, *state.pending]
                if not pending:
                    continue
                next_state = replace(
                    state,
                    status=STATUS_MANUAL_RECOVERY,
                    phase=None,
                    finished_at=_now(),
                    pid=os.getpid(),
                    error=error,
                    attempts=max(state.attempts, UPDATE_MAX_AUTOMATIC_ATTEMPTS),
                    next_retry_at=None,
                    last_error=error,
                    current_batch=[],
                    pending=pending,
                )
                self._write(session_id, next_state)

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
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)


def _state_from_json(data: object) -> AsyncUpdateState:
    if not isinstance(data, dict):
        raise ValueError("async update state must be a JSON object")
    if "current" in data or "queued" in data:
        raise ValueError("async update state uses unsupported legacy job fields")
    status = _required_state_str(data, "status")
    session_id = _required_state_str(data, "session_id")
    role = _required_state_str(data, "role")
    next_id = data.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        raise ValueError("async update state must contain positive integer field: next_id")
    current_batch = _required_job_list(data, "current_batch")
    pending = _required_job_list(data, "pending")
    job_ids = [job.id for job in [*current_batch, *pending]]
    if any(first >= second for first, second in zip(job_ids, job_ids[1:])):
        raise ValueError("async update job ids must be unique and strictly increasing")
    if job_ids and next_id <= job_ids[-1]:
        raise ValueError("async update next_id must be greater than every live job id")
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
        attempts=_optional_nonnegative_int(data.get("attempts"), "attempts"),
        next_retry_at=_optional_str(data.get("next_retry_at")),
        last_error=_optional_str(data.get("last_error")),
        next_flush_at=_optional_str(data.get("next_flush_at")),
        current_batch=current_batch,
        pending=pending,
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
    if (
        isinstance(job_id, bool)
        or not isinstance(job_id, int)
        or job_id < 1
        or not isinstance(message, str)
        or not isinstance(submitted_at, str)
    ):
        return None
    return AsyncUpdateJob(id=job_id, message=message, submitted_at=submitted_at)


def _required_state_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"async update state must contain string field: {key}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_nonnegative_int(value: object, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"async update state must contain nonnegative integer field: {field}")
    return value


def _is_legacy_failed_pending_state(state: AsyncUpdateState) -> bool:
    return (
        state.status == "failed"
        and state.attempts == 0
        and state.next_retry_at is None
        and bool(state.current_batch or state.pending)
    )


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


def format_retry_result(result: AsyncUpdateRetryResult) -> str:
    lines = [
        f"requeued sessions: {result.requeued_sessions}",
        f"requeued candidates: {result.requeued_candidates}",
        f"skipped sessions: {result.skipped_sessions}",
    ]
    if result.worker_error:
        lines.append(f"worker: failed: {result.worker_error}")
    elif result.worker_pid is None:
        lines.append("worker: not started")
    else:
        lines.append(f"worker: {result.worker_action} pid {result.worker_pid}")
    return "\n".join(lines)


def manual_recovery_warning(state: AsyncUpdateState) -> str | None:
    if state.status != STATUS_MANUAL_RECOVERY:
        return None
    return MANUAL_RECOVERY_WARNING


def _format_batch_message(batches: list[AsyncUpdateSessionBatch]) -> str:
    lines = [
        "Process the following submitted RightMemory candidates as one ordered batch.",
        "Treat them as evidence about evolving tasks, possible durable context, and explicit corrections.",
        "Use the update instructions to decide what belongs in live Pursuit, durable Memory, both, or neither.",
        "",
        "Candidates:",
    ]
    for batch in batches:
        for job in batch.jobs:
            lines.append(
                f"[update session: {batch.session_id} | "
                f"candidate: {job.id} | submitted_at: {job.submitted_at}]"
            )
            lines.extend(job.message.splitlines() or [""])
            lines.append("")
    return "\n".join(lines).rstrip()


def _batch_session_id(batches: list[AsyncUpdateSessionBatch]) -> str:
    parts = []
    for batch in batches:
        for job in batch.jobs:
            parts.append(f"{batch.session_id}:{job.id}:{job.submitted_at}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"update-batch-{digest}"


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
    return process_exists(pid)


def _is_async_worker_process(pid: int, role: str | None = None, *, identity: str | None = None) -> bool:
    if not _process_exists(pid):
        return False
    if identity is not None:
        return process_identity(pid) == identity
    if pid == os.getpid():
        return True
    command = process_command(pid)
    if command is None:
        return True
    if "_async-worker" not in command:
        return False
    if "rightmemory.cli" not in command:
        return False
    return role is None or role in command
