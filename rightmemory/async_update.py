from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO

from .platform import (
    detached_process_kwargs,
    lock_file,
    lock_file_nonblocking,
    process_command,
    process_exists,
    process_identity,
    unlock_file,
)
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id
from .update_queue import (
    PublicationMarker,
    UpdateCandidate,
    UpdateQueueStore,
    update_candidate_batch_id,
)

UPDATE_DEBOUNCE_SECONDS = 60 * 60
UPDATE_RETRY_COOLDOWN_SECONDS = 60 * 60
UPDATE_MAX_AUTOMATIC_ATTEMPTS = 2
WORKER_IDLE_POLL_SECONDS = 30
STATUS_MANUAL_RECOVERY = "needs_manual_recovery"
CANDIDATE_UID_RE = re.compile(r"[0-9a-f]{32}")
MANUAL_RECOVERY_WARNING = (
    "CRITICAL: this async RightMemory update session is blocked after "
    f"{UPDATE_MAX_AUTOMATIC_ATTEMPTS} failed attempts.\n"
    "The new candidate was saved, but this session will not be processed until manual recovery runs.\n"
    "Agent: report this issue to the user and suggest `rightmemory update retry`."
)


@dataclass(frozen=True)
class AsyncUpdateJob:
    id: int
    candidate_uid: str
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
    current_operation_id: str | None = None
    last_operation_id: str | None = None
    accepted_candidate_uids: list[str] = field(default_factory=list)
    current_batch: list[AsyncUpdateJob] = field(default_factory=list)
    pending: list[AsyncUpdateJob] = field(default_factory=list)
    next_id: int = 1


@dataclass(frozen=True)
class AsyncUpdateSessionBatch:
    session_id: str
    ready_at: datetime
    jobs: list[AsyncUpdateJob]
    operation_id: str | None = None


@dataclass(frozen=True)
class AsyncBatchReservation:
    operation_id: str
    created_at: str
    participants: list[AsyncUpdateSessionBatch]


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
class AsyncUpdateCancelResult:
    state: AsyncUpdateState
    candidate: AsyncUpdateJob | None
    outcome: Literal["canceled", "publication_started", "not_pending"]


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
        self.reservations_root = self.root / "_batches"

    def read(self, session_id: str) -> AsyncUpdateState:
        with self._reservations_locked():
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                state = self._reconcile_session_outbox_locked(state)
        if state.last_operation_id is not None:
            self._clear_reservation_if_acknowledged(state.last_operation_id)
        return state

    def cancel_pending(self, session_id: str, candidate_id: int) -> tuple[AsyncUpdateState, bool]:
        result = self.cancel_pending_candidate(session_id, candidate_id)
        return result.state, result.outcome == "canceled"

    def cancel_pending_candidate(
        self,
        session_id: str,
        candidate_id: int,
    ) -> AsyncUpdateCancelResult:
        """Cancel locally or report the exact candidate that crossed into Git authority."""
        if not isinstance(candidate_id, int) or isinstance(candidate_id, bool) or candidate_id < 1:
            raise ValueError("candidate id must be a positive integer")
        with self._reservations_locked():
            reserved_ids = self._reserved_candidate_ids_locked(session_id)
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                selected = next(
                    (job for job in state.pending if job.id == candidate_id),
                    None,
                )
                queue_store = UpdateQueueStore(self.memory_root)
                if (
                    selected is not None
                    and queue_store.publication_state(selected.candidate_uid) == "attempted"
                ):
                    return AsyncUpdateCancelResult(
                        state=state,
                        candidate=selected,
                        outcome="publication_started",
                    )
                pending = [
                    job
                    for job in state.pending
                    if job.id != candidate_id or job.id in reserved_ids
                ]
                canceled = len(pending) != len(state.pending)
                if canceled:
                    if selected is not None:
                        # Removing the outbox first makes an interrupted cancel retryable:
                        # the still-pending state recreates it on the next reconciled read.
                        queue_store.remove_outbox(selected.candidate_uid)
                    state = replace(state, pending=pending)
                    self._write(session_id, state)
        if state.last_operation_id is not None:
            self._clear_reservation_if_acknowledged(state.last_operation_id)
        return AsyncUpdateCancelResult(
            state=state,
            candidate=selected,
            outcome="canceled" if canceled else "not_pending",
        )

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
                retry_count = len(state.current_batch) + len(state.pending)
                if not retry_count:
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
                )
                self._write(session_id, next_state)
                requeued_sessions += 1
                requeued_candidates += retry_count
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

    def submit(
        self,
        session_id: str,
        message: str,
        *,
        candidate_uid: str | None = None,
    ) -> AsyncUpdateState:
        candidate_uid = (
            new_candidate_uid()
            if candidate_uid is None
            else normalize_candidate_uid(candidate_uid)
        )
        now = _now_dt()
        with self._reservations_locked():
            with self._locked(session_id):
                current = self._read_checked_locked(session_id)
                current = self._reconcile_session_outbox_locked(current)
                matching = [
                    job
                    for job in [*current.current_batch, *current.pending]
                    if job.candidate_uid == candidate_uid
                ]
                if candidate_uid in current.accepted_candidate_uids:
                    if any(job.message != message for job in matching):
                        raise ValueError("candidate uid already belongs to different update evidence")
                    state = current
                else:
                    job = AsyncUpdateJob(
                        id=current.next_id,
                        candidate_uid=candidate_uid,
                        message=message,
                        submitted_at=_format_time(now),
                    )
                    UpdateQueueStore(self.memory_root).write_outbox(
                        self._candidate_from_job(session_id, job)
                    )
                    worker_pid = self._active_worker_pid()
                    current = replace(
                        current,
                        accepted_candidate_uids=[
                            *current.accepted_candidate_uids,
                            candidate_uid,
                        ],
                    )
                    state = self._enqueue_locked(current, job, now=now, worker_pid=worker_pid)
                    self._write(session_id, state)

        if state.status != STATUS_MANUAL_RECOVERY:
            self._start_worker_if_needed(session_id)
        return state

    def acknowledge_synchronized(
        self,
        candidate_uids: set[str] | frozenset[str],
        *,
        result: str = "candidate handed to synchronized Git queue",
    ) -> int:
        """Remove published candidates from the local scheduling lane."""
        normalized = {normalize_candidate_uid(uid) for uid in candidate_uids}
        if not normalized:
            return 0
        removed = 0
        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                if any(job.candidate_uid in normalized for job in state.current_batch):
                    raise RuntimeError("cannot publish an update candidate after local processing starts")
                pending = [job for job in state.pending if job.candidate_uid not in normalized]
                removed += len(state.pending) - len(pending)
                if len(pending) != len(state.pending):
                    if pending:
                        state = replace(state, pending=pending)
                    else:
                        state = replace(
                            state,
                            status="succeeded",
                            phase=None,
                            finished_at=_now(),
                            pid=None,
                            result=result,
                            error=None,
                            attempts=0,
                            next_retry_at=None,
                            last_error=None,
                            next_flush_at=None,
                            current_operation_id=None,
                            pending=[],
                        )
                    self._write(session_id, state)
        return removed

    def publishable_candidate_uids(self) -> frozenset[str]:
        """Return candidates whose Git publication may safely start or resume."""
        candidate_uids: set[str] = set()
        queue_store = UpdateQueueStore(self.memory_root)
        with self._reservations_locked():
            outbox_by_session: dict[str, list[UpdateCandidate]] = {}
            for candidate in queue_store.outbox_candidates():
                outbox_by_session.setdefault(candidate.session_id, []).append(candidate)

            session_ids = {path.stem for path in self._session_state_paths()}
            session_ids.update(outbox_by_session)
            for session_id in sorted(session_ids):
                reserved_uids = self._reserved_candidate_uids_locked(session_id)
                with self._locked(session_id):
                    state = self._read_raw(session_id)
                    state = self._reconcile_session_outbox_locked(
                        state,
                        candidates=outbox_by_session.get(session_id, ()),
                    )
                    current_uids = {job.candidate_uid for job in state.current_batch}
                    for job in state.pending:
                        if job.candidate_uid in reserved_uids:
                            continue
                        publication_state = queue_store.publication_state(job.candidate_uid)
                        if publication_state == "attempted" or (
                            publication_state == "never_attempted"
                            and state.status == "running"
                            and state.phase == "waiting"
                        ):
                            candidate_uids.add(job.candidate_uid)

                # A crash after local acknowledgement can leave only an attempted
                # outbox record. It still needs one final Git settlement pass.
                for candidate in outbox_by_session.get(session_id, ()):
                    if (
                        candidate.uid not in current_uids
                        and candidate.uid not in reserved_uids
                        and queue_store.publication_state(candidate.uid) == "attempted"
                    ):
                        candidate_uids.add(candidate.uid)
        return frozenset(candidate_uids)

    def begin_publication(
        self,
        candidate: UpdateCandidate,
        *,
        attempted_at: str,
    ) -> PublicationMarker | None:
        """Cross the Git-authority boundary while excluding undo and local reservation."""
        queue_store = UpdateQueueStore(self.memory_root)
        with self._reservations_locked():
            reserved_uids = self._reserved_candidate_uids_locked(candidate.session_id)
            with self._locked(candidate.session_id):
                stored = queue_store.read_outbox(candidate.uid)
                if stored is None:
                    return None
                if stored != candidate:
                    raise RuntimeError("publication candidate conflicts with local outbox")
                state = self._read_raw(candidate.session_id)
                state = self._reconcile_session_outbox_locked(
                    state,
                    candidates=(candidate,),
                )
                current_uids = {job.candidate_uid for job in state.current_batch}
                if candidate.uid in current_uids or candidate.uid in reserved_uids:
                    return None
                pending = next(
                    (job for job in state.pending if job.candidate_uid == candidate.uid),
                    None,
                )
                marker = queue_store.read_publication_marker(candidate.uid)
                if pending is None and marker is None:
                    return None
                if pending is not None:
                    self._require_candidate_matches_job(candidate, state.session_id, pending)
                return queue_store.begin_publication(
                    candidate.uid,
                    attempted_at=attempted_at,
                )

    def wake_worker(self) -> None:
        """Wake the shared worker even when this device has no local session state."""
        self._start_worker_if_needed(None)

    def local_work_schedule(
        self,
        *,
        target_batch_candidates: int,
        max_wait_seconds: int,
    ) -> tuple[bool, datetime | None]:
        """Peek at local-only readiness without reserving or starting a batch."""
        batch, deadline = self._next_batch(
            target_batch_candidates,
            max_wait_seconds,
        )
        return batch is not None, deadline

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
        before_batches: Callable[[], bool] | None = None,
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
                if before_batches is not None and not before_batches():
                    return AsyncUpdateWorkerResult(status="failed", failed=True)
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

                existing_operation_ids = {item.operation_id for item in batch if item.operation_id is not None}
                if len(existing_operation_ids) > 1:
                    raise RuntimeError("async recovery batch contains multiple operation ids")
                computed_batch_id = _batch_session_id(batch)
                batch_id = next(iter(existing_operation_ids), computed_batch_id)
                if existing_operation_ids and batch_id != computed_batch_id:
                    raise RuntimeError("async recovery batch no longer matches its operation id")
                reservation = self._reserve_cross_session_batch(batch, batch_id)
                if reservation is None:
                    # A pending candidate may be canceled after selection but before reservation.
                    continue
                batch = reservation.participants
                session_ids = [item.session_id for item in batch]
                with self._worker_locked():
                    self._write_worker_locked(
                        status="running",
                        pid=os.getpid(),
                        batch_id=batch_id,
                        session_ids=session_ids,
                        error=None,
                    )
                started = self._start_cross_session_batch(batch, batch_id)
                if not started:
                    self._clear_reservation_if_acknowledged(batch_id)
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

                session_ids = [item.session_id for item in batch]
                with self._worker_locked():
                    self._write_worker_locked(
                        status="running",
                        pid=os.getpid(),
                        batch_id=batch_id,
                        session_ids=session_ids,
                        error=None,
                    )

                terminal_output = self._terminal_operation_output(batch_id)
                if terminal_output is not None:
                    accepted_count = self._finish_cross_session_batch(batch, batch_id, terminal_output)
                    if accepted_count:
                        processed += accepted_count
                        if on_batch_success is not None:
                            on_batch_success(accepted_count)
                    continue

                try:
                    result = run_message(batch_id, _format_batch_message(batch))
                except Exception as exc:
                    terminal_output = self._terminal_operation_output(batch_id)
                    if terminal_output is not None:
                        accepted_count = self._finish_cross_session_batch(batch, batch_id, terminal_output)
                        if accepted_count:
                            processed += accepted_count
                            if on_batch_success is not None:
                                on_batch_success(accepted_count)
                        continue
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
                    if any(
                        state.status == "failed" and (state.current_batch or state.pending)
                        for state in failed_states
                    ):
                        continue
                    return AsyncUpdateWorkerResult(status="failed", processed=processed, failed=True)

                terminal_output = self._terminal_operation_output(batch_id)
                accepted_count = self._finish_cross_session_batch(
                    batch,
                    batch_id,
                    result if terminal_output is None else terminal_output,
                )
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
        reserved_batch, reserved_sessions, reservation_deadlines = self._next_reserved_batch(now)
        if reserved_batch is not None:
            return reserved_batch, None

        recovery: list[AsyncUpdateSessionBatch] = []
        operation_recovery: dict[str, list[AsyncUpdateSessionBatch]] = {}
        blocked_operation_ids: set[str] = set()
        eligible: list[AsyncUpdateSessionBatch] = []
        future_deadlines = list(reservation_deadlines)
        queue_store = UpdateQueueStore(self.memory_root)

        for path in self._session_state_paths():
            session_id = path.stem
            if session_id in reserved_sessions:
                continue
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                if state.role != self.role:
                    continue
                if state.current_batch:
                    operation_id = state.current_operation_id
                    if state.status == "failed":
                        ready_at = _required_time(state.next_retry_at, "next_retry_at")
                        item = AsyncUpdateSessionBatch(
                            state.session_id,
                            ready_at,
                            list(state.current_batch),
                            operation_id,
                        )
                        if operation_id is not None:
                            operation_recovery.setdefault(operation_id, []).append(item)
                        elif ready_at <= now:
                            recovery.append(item)
                        if ready_at > now:
                            future_deadlines.append(ready_at)
                    elif operation_id is not None:
                        # Every participant must resume the same reserved operation together.
                        blocked_operation_ids.add(operation_id)
                    continue
                if not state.pending:
                    continue
                if any(
                    queue_store.publication_state(job.candidate_uid) == "attempted"
                    for job in state.pending
                ):
                    # Once publication starts, only the Git lease may process this session.
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

        ready_operation_groups = [
            (max(item.ready_at for item in items), operation_id, items)
            for operation_id, items in operation_recovery.items()
            if operation_id not in blocked_operation_ids and all(item.ready_at <= now for item in items)
        ]
        if ready_operation_groups:
            ready_operation_groups.sort(key=lambda entry: (entry[0], entry[1]))
            return sorted(ready_operation_groups[0][2], key=lambda item: item.session_id), None

        recovery.sort(key=lambda item: (item.ready_at, item.session_id))
        if recovery:
            operation_id = recovery[0].operation_id
            return [item for item in recovery if item.operation_id == operation_id], None

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

    def _next_reserved_batch(
        self,
        now: datetime,
    ) -> tuple[list[AsyncUpdateSessionBatch] | None, set[str], list[datetime]]:
        reservations = self._read_reservations()
        reserved_sessions: set[str] = set()
        future_deadlines: list[datetime] = []
        ready: list[AsyncBatchReservation] = []

        for reservation in reservations:
            remaining = False
            blocked = False
            retry_deadlines: list[datetime] = []
            for item in reservation.participants:
                with self._locked(item.session_id):
                    state = self._read_checked_locked(item.session_id)
                if state.last_operation_id == reservation.operation_id and not state.current_batch:
                    continue

                remaining = True
                self._validate_reserved_participant(state, item, reservation.operation_id)
                if state.status == STATUS_MANUAL_RECOVERY:
                    blocked = True
                elif state.status == "failed":
                    retry_at = _required_time(state.next_retry_at, "next_retry_at")
                    if retry_at > now:
                        retry_deadlines.append(retry_at)

            if not remaining:
                self._clear_reservation_if_acknowledged(reservation.operation_id)
                continue

            participant_ids = {item.session_id for item in reservation.participants}
            overlap = reserved_sessions.intersection(participant_ids)
            if overlap:
                session_id = sorted(overlap)[0]
                raise RuntimeError(f"async session belongs to multiple reserved batches: {session_id}")
            reserved_sessions.update(participant_ids)
            if not blocked and not retry_deadlines:
                ready.append(reservation)
            elif not blocked and retry_deadlines:
                # A reserved operation resumes only when every participant is ready.
                future_deadlines.append(max(retry_deadlines))

        if ready:
            ready.sort(key=lambda item: (_required_time(item.created_at, "created_at"), item.operation_id))
            return ready[0].participants, reserved_sessions, future_deadlines
        return None, reserved_sessions, future_deadlines

    def _validate_reserved_participant(
        self,
        state: AsyncUpdateState,
        item: AsyncUpdateSessionBatch,
        operation_id: str,
    ) -> None:
        if state.session_id != item.session_id:
            raise RuntimeError(f"async reserved batch has the wrong session: {item.session_id}")
        if state.role != self.role:
            raise RuntimeError(f"async reserved batch has the wrong role: {item.session_id}")
        if state.last_operation_id == operation_id and not state.current_batch:
            return
        if state.current_batch == item.jobs:
            if state.current_operation_id not in {None, operation_id}:
                raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
            return
        if not state.current_batch and state.pending[: len(item.jobs)] == item.jobs:
            if state.current_operation_id is not None:
                raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
            return
        raise RuntimeError(f"async reserved batch no longer matches session state: {item.session_id}")

    def _reserve_cross_session_batch(
        self,
        batch: list[AsyncUpdateSessionBatch],
        operation_id: str,
    ) -> AsyncBatchReservation | None:
        if not batch:
            raise ValueError("async reserved batch must contain at least one participant")
        fresh_selection = all(item.operation_id is None for item in batch)
        participants = [
            replace(item, operation_id=operation_id)
            for item in sorted(batch, key=lambda item: item.session_id)
        ]
        session_ids = [item.session_id for item in participants]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("async reserved batch must contain unique sessions")
        if any(not item.jobs for item in participants):
            raise ValueError("async reserved batch participants must contain at least one candidate")
        if _batch_session_id(participants) != operation_id:
            raise RuntimeError("async reserved batch does not match its operation id")

        with self._reservations_locked():
            path = self._reservation_path(operation_id)
            if path.exists():
                existing = self._read_reservation_path(path)
                if existing.participants != participants:
                    raise RuntimeError("async operation id already belongs to a different reserved batch")
                return existing

            participant_ids = set(session_ids)
            for other_path in sorted(self.reservations_root.glob("*.json")):
                other = self._read_reservation_path(other_path)
                overlap = participant_ids.intersection(item.session_id for item in other.participants)
                if overlap:
                    session_id = sorted(overlap)[0]
                    raise RuntimeError(f"async session already belongs to a reserved batch: {session_id}")

            for item in participants:
                with self._locked(item.session_id):
                    state = self._read_raw(item.session_id)
                    if fresh_selection and any(
                        UpdateQueueStore(self.memory_root).publication_state(job.candidate_uid)
                        == "attempted"
                        for job in item.jobs
                    ):
                        return None
                if (
                    fresh_selection
                    and state.session_id == item.session_id
                    and state.role == self.role
                    and state.last_operation_id != operation_id
                    and not state.current_batch
                    and state.current_operation_id is None
                    and state.pending[: len(item.jobs)] != item.jobs
                ):
                    return None
                self._validate_reserved_participant(state, item, operation_id)

            reservation = AsyncBatchReservation(
                operation_id=operation_id,
                created_at=_now(),
                participants=participants,
            )
            self._write_reservation_locked(path, reservation)
            return reservation

    def _reserved_candidate_ids_locked(self, session_id: str) -> set[int]:
        reserved_ids: set[int] = set()
        for path in sorted(self.reservations_root.glob("*.json")):
            reservation = self._read_reservation_path(path)
            for item in reservation.participants:
                if item.session_id == session_id:
                    reserved_ids.update(job.id for job in item.jobs)
        return reserved_ids

    def _reserved_candidate_uids_locked(self, session_id: str) -> set[str]:
        reserved_uids: set[str] = set()
        for path in sorted(self.reservations_root.glob("*.json")):
            reservation = self._read_reservation_path(path)
            for item in reservation.participants:
                if item.session_id == session_id:
                    reserved_uids.update(job.candidate_uid for job in item.jobs)
        return reserved_uids

    def _read_reservation(self, operation_id: str) -> AsyncBatchReservation | None:
        if not self.reservations_root.exists():
            return None
        with self._reservations_locked():
            path = self._reservation_path(operation_id)
            return self._read_reservation_path(path) if path.exists() else None

    def operation_candidates(self, operation_id: str) -> tuple[UpdateCandidate, ...]:
        """Return the exact candidates in one durably reserved local operation."""
        reservation = self._read_reservation(operation_id)
        if reservation is None:
            raise RuntimeError("async update operation has no durable batch reservation")
        return tuple(
            self._candidate_from_job(item.session_id, job)
            for item in reservation.participants
            for job in item.jobs
        )

    def _read_reservations(self) -> list[AsyncBatchReservation]:
        if not self.reservations_root.exists():
            return []
        with self._reservations_locked():
            return [
                self._read_reservation_path(path)
                for path in sorted(self.reservations_root.glob("*.json"))
            ]

    def _read_reservation_path(self, path: Path) -> AsyncBatchReservation:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = data.get("version") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version != 1
        ):
            raise ValueError("async batch reservation must be a version 1 JSON object")
        operation_id = _required_state_str(data, "operation_id")
        created_at = _required_state_str(data, "created_at")
        _required_time(created_at, "created_at")
        raw_participants = data.get("participants")
        if not isinstance(raw_participants, list) or not raw_participants:
            raise ValueError("async batch reservation must contain participants")

        participants: list[AsyncUpdateSessionBatch] = []
        for raw_item in raw_participants:
            if not isinstance(raw_item, dict):
                raise ValueError("async batch reservation participants must be JSON objects")
            session_id = _required_state_str(raw_item, "session_id")
            _safe_session_id(session_id)
            ready_at_value = _required_state_str(raw_item, "ready_at")
            ready_at = _required_time(ready_at_value, "ready_at")
            jobs = _required_job_list(raw_item, "jobs")
            if not jobs:
                raise ValueError("async batch reservation participants must contain candidates")
            job_ids = [job.id for job in jobs]
            if any(first >= second for first, second in zip(job_ids, job_ids[1:])):
                raise ValueError("async batch reservation candidate ids must be strictly increasing")
            participants.append(AsyncUpdateSessionBatch(session_id, ready_at, jobs, operation_id))

        participants.sort(key=lambda item: item.session_id)
        session_ids = [item.session_id for item in participants]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("async batch reservation must contain unique sessions")
        if _batch_session_id(participants) != operation_id:
            raise ValueError("async batch reservation does not match its operation id")
        if path.name != self._reservation_path(operation_id).name:
            raise ValueError("async batch reservation filename does not match its operation id")
        return AsyncBatchReservation(operation_id, created_at, participants)

    def _write_reservation_locked(self, path: Path, reservation: AsyncBatchReservation) -> None:
        content = json.dumps(
            {
                "version": 1,
                "operation_id": reservation.operation_id,
                "created_at": reservation.created_at,
                "participants": [
                    {
                        "session_id": item.session_id,
                        "ready_at": _format_time(item.ready_at),
                        "jobs": [asdict(job) for job in item.jobs],
                    }
                    for item in reservation.participants
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)

    def _clear_reservation_if_acknowledged(self, operation_id: str) -> bool:
        if not self.reservations_root.exists():
            return False
        with self._reservations_locked():
            path = self._reservation_path(operation_id)
            if not path.exists():
                return False
            reservation = self._read_reservation_path(path)
            for item in reservation.participants:
                with self._locked(item.session_id):
                    state = self._read_raw(item.session_id)
                if state.last_operation_id != operation_id:
                    return False
            path.unlink()
            _fsync_directory(path.parent)
            return True

    def _reservation_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self.reservations_root / f"{digest}.json"

    @contextmanager
    def _reservations_locked(self):
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        created = not self.reservations_root.exists()
        self.reservations_root.mkdir(parents=True, exist_ok=True)
        if created:
            _fsync_directory(self.root)
        lock_path = self.reservations_root / "state.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _start_cross_session_batch(
        self,
        batch: list[AsyncUpdateSessionBatch],
        operation_id: str,
    ) -> list[AsyncUpdateSessionBatch]:
        reservation = self._read_reservation(operation_id)
        if reservation is None or reservation.participants != batch:
            raise RuntimeError("async batch must be durably reserved before it starts")
        if _batch_session_id(batch) != operation_id:
            raise RuntimeError("async reserved batch no longer matches its operation id")

        started: list[AsyncUpdateSessionBatch] = []
        for item in sorted(batch, key=lambda entry: entry.session_id):
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if state.last_operation_id == operation_id and not state.current_batch:
                    started.append(replace(item, operation_id=operation_id))
                    continue
                if state.current_batch == item.jobs:
                    if state.current_operation_id not in {None, operation_id}:
                        raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
                    next_state = replace(
                        state,
                        status="running",
                        phase="running",
                        started_at=_now(),
                        finished_at=None,
                        result=None,
                        error=None,
                        pid=os.getpid(),
                        current_operation_id=operation_id,
                    )
                    self._write(item.session_id, next_state)
                    started.append(
                        AsyncUpdateSessionBatch(
                            item.session_id,
                            item.ready_at,
                            list(next_state.current_batch),
                            operation_id,
                        )
                    )
                    continue
                if state.current_batch or state.pending[: len(item.jobs)] != item.jobs:
                    raise RuntimeError(f"async reserved batch changed before start: {item.session_id}")
                if state.current_operation_id is not None:
                    raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
                current_batch = state.pending[: len(item.jobs)]
                pending = state.pending[len(item.jobs):]
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
                    current_operation_id=operation_id,
                )
                self._write(item.session_id, next_state)
                started.append(
                    AsyncUpdateSessionBatch(item.session_id, item.ready_at, current_batch, operation_id)
                )
        return started

    def _finish_cross_session_batch(
        self,
        batch: list[AsyncUpdateSessionBatch],
        operation_id: str,
        result: str,
    ) -> int:
        accepted = 0
        for item in sorted(batch, key=lambda entry: entry.session_id):
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if state.last_operation_id == operation_id and not state.current_batch:
                    continue
                if state.current_batch != item.jobs:
                    raise RuntimeError(f"async reserved batch changed before finish: {item.session_id}")
                if state.current_operation_id != operation_id:
                    raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
                accepted += len(state.current_batch)
                queue_store = UpdateQueueStore(self.memory_root)
                for job in state.current_batch:
                    queue_store.remove_outbox(job.candidate_uid)
                    queue_store.clear_publication_marker(job.candidate_uid)
                next_state = self._finished_state(state, operation_id, result)
                self._write(item.session_id, next_state)
        self._clear_reservation_if_acknowledged(operation_id)
        return accepted

    def _finished_state(self, state: AsyncUpdateState, operation_id: str, result: str) -> AsyncUpdateState:
        common = {
            "pid": os.getpid(),
            "current_batch": [],
            "current_operation_id": None,
            "last_operation_id": operation_id,
            "result": result,
            "error": None,
            "attempts": 0,
            "next_retry_at": None,
            "last_error": None,
        }
        if state.pending:
            next_flush_at = state.next_flush_at or _format_time(
                _now_dt() + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS)
            )
            return replace(
                state,
                status="running",
                phase="waiting",
                started_at=_now(),
                finished_at=None,
                next_flush_at=next_flush_at,
                **common,
            )
        return replace(
            state,
            status="succeeded",
            phase=None,
            finished_at=_now(),
            pending=[],
            next_flush_at=None,
            **common,
        )

    def _terminal_operation_output(self, operation_id: str) -> str | None:
        record = SemanticOperationStore(self.memory_root).read(operation_id)
        if record is None or record.phase not in FINAL_PHASES or record.outcome is None:
            return None
        return record.outcome.output

    def _fail_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch], error: str) -> list[AsyncUpdateState]:
        failed_states: list[AsyncUpdateState] = []
        for item in sorted(batch, key=lambda entry: entry.session_id):
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if state.last_operation_id == item.operation_id and not state.current_batch:
                    continue
                if state.current_batch != item.jobs:
                    raise RuntimeError(f"async reserved batch changed before failure: {item.session_id}")
                if state.current_operation_id != item.operation_id:
                    raise RuntimeError(f"async batch has a different operation id: {item.session_id}")
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

    def _start_worker_if_needed(self, session_id: str | None) -> None:
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
                if session_id is not None:
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
        if state.current_batch and state.current_operation_id is not None:
            terminal_output = self._terminal_operation_output(state.current_operation_id)
            if terminal_output is not None:
                state = self._finished_state(state, state.current_operation_id, terminal_output)
                self._write(session_id, state)
                return state
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
                last_operation_id=state.last_operation_id,
                accepted_candidate_uids=state.accepted_candidate_uids,
                next_id=next_id,
            )
        if state.status in {"failed", STATUS_MANUAL_RECOVERY}:
            return replace(
                state,
                phase=None,
                pid=worker_pid,
                pending=[*state.pending, job],
                next_id=next_id,
            )
        if (
            state.status == "running"
            and state.phase == "running"
            and state.current_batch
        ):
            return replace(
                state,
                pid=worker_pid if worker_pid is not None else state.pid,
                pending=[*state.pending, job],
                next_id=next_id,
                next_flush_at=next_flush_at,
                error=None,
            )
        pending = [*state.pending, job]
        return AsyncUpdateState(
            status="running",
            session_id=state.session_id,
            role=self.role,
            phase="waiting",
            started_at=state.started_at or _format_time(now),
            pid=worker_pid,
            next_flush_at=next_flush_at,
            pending=pending,
            last_operation_id=state.last_operation_id,
            accepted_candidate_uids=state.accepted_candidate_uids,
            next_id=next_id,
        )

    def _reconcile_session_outbox_locked(
        self,
        state: AsyncUpdateState,
        *,
        candidates: Iterable[UpdateCandidate] | None = None,
    ) -> AsyncUpdateState:
        if self.role != "update":
            return state
        if state.role != self.role:
            raise ValueError(
                f"async update state role mismatch: expected {self.role}, got {state.role}"
            )

        queue_store = UpdateQueueStore(self.memory_root)
        for job in state.pending:
            candidate = self._candidate_from_job(state.session_id, job)
            stored = queue_store.read_outbox(job.candidate_uid)
            if stored is None:
                # Cancellation removes the outbox first. If its state write is
                # interrupted, restore the still-pending source of truth.
                queue_store.write_outbox(candidate)
            elif stored != candidate:
                raise RuntimeError("local outbox candidate conflicts with pending update state")
        if candidates is None:
            candidates = (
                candidate
                for candidate in queue_store.outbox_candidates()
                if candidate.session_id == state.session_id
            )
        session_candidates = sorted(
            candidates,
            key=lambda item: (item.display_id, item.submitted_at, item.uid),
        )
        live_jobs = [*state.current_batch, *state.pending]
        live_by_uid = {job.candidate_uid: job for job in live_jobs}

        changed = False
        for candidate in session_candidates:
            if candidate.session_id != state.session_id:
                raise ValueError("local outbox candidate belongs to a different update session")
            existing = live_by_uid.get(candidate.uid)
            if existing is not None:
                self._require_candidate_matches_job(candidate, state.session_id, existing)
                continue
            if candidate.uid in state.accepted_candidate_uids:
                continue
            if queue_store.publication_state(candidate.uid) == "attempted":
                # State acknowledgement may precede outbox cleanup. Git remains
                # authoritative, so an attempted orphan must not re-enter local work.
                continue
            if candidate.display_id != state.next_id:
                raise RuntimeError(
                    "local outbox candidate cannot be reconciled without changing its display id"
                )
            job = AsyncUpdateJob(
                id=candidate.display_id,
                candidate_uid=candidate.uid,
                message=candidate.message,
                submitted_at=candidate.submitted_at,
            )
            state = replace(
                state,
                accepted_candidate_uids=[
                    *state.accepted_candidate_uids,
                    candidate.uid,
                ],
            )
            state = self._enqueue_locked(
                state,
                job,
                now=_required_time(candidate.submitted_at, "candidate submitted_at"),
                worker_pid=state.pid,
            )
            live_by_uid[job.candidate_uid] = job
            changed = True

        if changed:
            self._write(state.session_id, state)
        return state

    @staticmethod
    def _candidate_from_job(session_id: str, job: AsyncUpdateJob) -> UpdateCandidate:
        return UpdateCandidate(
            uid=job.candidate_uid,
            session_id=session_id,
            display_id=job.id,
            message=job.message,
            submitted_at=job.submitted_at,
        )

    @staticmethod
    def _require_candidate_matches_job(
        candidate: UpdateCandidate,
        session_id: str,
        job: AsyncUpdateJob,
    ) -> None:
        if candidate != AsyncUpdateStore._candidate_from_job(session_id, job):
            raise RuntimeError("local outbox candidate conflicts with async update state")

    def _fail(self, session_id: str, error: str) -> AsyncUpdateState:
        with self._locked(session_id):
            return self._fail_locked(session_id, error)

    def _fail_locked(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
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
            next_flush_at=current.next_flush_at if current.current_batch and current.pending else None,
        )
        self._write(session_id, state)
        return state

    def _manual_recovery_locked(self, session_id: str, state: AsyncUpdateState) -> AsyncUpdateState:
        next_state = replace(
            state,
            status=STATUS_MANUAL_RECOVERY,
            phase=None,
            finished_at=state.finished_at or _now(),
            pid=os.getpid(),
            attempts=max(state.attempts, UPDATE_MAX_AUTOMATIC_ATTEMPTS),
            next_retry_at=None,
            last_error=state.last_error or state.error,
        )
        self._write(session_id, next_state)
        return next_state

    def _restore_manual_recovery(self, session_ids: list[str], error: str) -> None:
        for session_id in session_ids:
            with self._locked(session_id):
                state = self._read_raw(session_id)
                if not state.current_batch and not state.pending:
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
    candidate_uids = [job.candidate_uid for job in [*current_batch, *pending]]
    if any(first >= second for first, second in zip(job_ids, job_ids[1:])):
        raise ValueError("async update job ids must be unique and strictly increasing")
    if len(candidate_uids) != len(set(candidate_uids)):
        raise ValueError("async update candidate uids must be unique")
    raw_accepted = data.get("accepted_candidate_uids", [])
    if (
        not isinstance(raw_accepted, list)
        or any(
            not isinstance(uid, str) or CANDIDATE_UID_RE.fullmatch(uid) is None
            for uid in raw_accepted
        )
        or len(raw_accepted) != len(set(raw_accepted))
    ):
        raise ValueError("async update state must contain unique accepted candidate uids")
    accepted_candidate_uids = [*raw_accepted]
    accepted_candidate_uids.extend(
        uid for uid in candidate_uids if uid not in accepted_candidate_uids
    )
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
        current_operation_id=_optional_str(data.get("current_operation_id")),
        last_operation_id=_optional_str(data.get("last_operation_id")),
        accepted_candidate_uids=accepted_candidate_uids,
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
    candidate_uid = data.get("candidate_uid")
    message = data.get("message")
    submitted_at = data.get("submitted_at")
    if (
        isinstance(job_id, bool)
        or not isinstance(job_id, int)
        or job_id < 1
        or not isinstance(candidate_uid, str)
        or CANDIDATE_UID_RE.fullmatch(candidate_uid) is None
        or not isinstance(message, str)
        or not isinstance(submitted_at, str)
    ):
        return None
    return AsyncUpdateJob(
        id=job_id,
        candidate_uid=candidate_uid,
        message=message,
        submitted_at=submitted_at,
    )


def new_candidate_uid() -> str:
    return uuid.uuid4().hex


def normalize_candidate_uid(value: object) -> str:
    if not isinstance(value, str) or CANDIDATE_UID_RE.fullmatch(value) is None:
        raise ValueError("candidate uid must be 32 lowercase hexadecimal characters")
    return value


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
    if state.current_operation_id:
        lines.append(f"current_operation_id: {state.current_operation_id}")
    lines.append(f"pending: {len(state.pending)}")
    if state.pending:
        lines.append(f"pending_ids: {', '.join(str(job.id) for job in state.pending)}")
    if state.result:
        lines.append(f"result: {state.result}")
    if state.last_operation_id:
        lines.append(f"last_operation_id: {state.last_operation_id}")
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
    candidates = (
        AsyncUpdateStore._candidate_from_job(batch.session_id, job)
        for batch in batches
        for job in batch.jobs
    )
    return update_candidate_batch_id(candidates)


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
