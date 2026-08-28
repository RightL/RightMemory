from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path

from .async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore
from .platform import lock_file, unlock_file
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id
from .update_queue import UpdateQueueStore


RECENT_SUBMITTED_HEADER = "Recent submitted RightMemory candidates"
RECENT_SUBMITTED_INTRO = (
    "These are pending updater submissions, not settled Memory or Agent Corrections. "
    "Use relevant entries as short-term continuity while preserving their candidate status."
)


@dataclass(frozen=True)
class RecentSubmittedMemoryEntry:
    update_session_id: str
    candidate_id: int
    submitted_at: str
    message: str
    candidate_uid: str | None = None

    @property
    def key(self) -> str:
        if self.candidate_uid is not None:
            return self.candidate_uid
        return f"{self.update_session_id}:{self.candidate_id}:{self.submitted_at}"


class RecentSubmittedMemoryDeliveryStore:
    def __init__(self, memory_root: Path):
        self.root = memory_root / ".runtime" / "recent_submitted" / "retrieve"

    def new_entries(
        self,
        retrieve_session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> list[RecentSubmittedMemoryEntry]:
        with self._locked(retrieve_session_id):
            delivered = self._read_delivered_locked(retrieve_session_id)
        return [entry for entry in entries if entry.key not in delivered]

    def record_delivered(
        self,
        retrieve_session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> None:
        if not entries:
            return
        with self._locked(retrieve_session_id):
            delivered = self._read_delivered_locked(retrieve_session_id)
            delivered.update(entry.key for entry in entries)
            self._write_delivered_locked(retrieve_session_id, delivered)

    def reset(self, retrieve_session_id: str) -> bool:
        with self._locked(retrieve_session_id):
            state_path = self._state_path(retrieve_session_id)
            try:
                state_path.unlink()
            except FileNotFoundError:
                return False
            _fsync_directory(state_path.parent)
            return True

    def _state_path(self, retrieve_session_id: str) -> Path:
        safe_id = _safe_session_id(retrieve_session_id)
        return self.root / f"{safe_id}.json"

    def _lock_path(self, retrieve_session_id: str) -> Path:
        safe_id = _safe_session_id(retrieve_session_id)
        return self.root / f"{safe_id}.lock"

    @contextmanager
    def _locked(self, retrieve_session_id: str):
        runtime_root = self.root.parent.parent
        _ensure_runtime_gitignore(runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(retrieve_session_id)
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _read_delivered_locked(self, retrieve_session_id: str) -> set[str]:
        state_path = self._state_path(retrieve_session_id)
        if not state_path.exists():
            return set()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("recent submitted delivery state must be an object")
        if data.get("session_id") != retrieve_session_id:
            raise ValueError("recent submitted delivery state session_id mismatch")
        delivered = data.get("delivered")
        if not isinstance(delivered, list) or any(not isinstance(key, str) for key in delivered):
            raise ValueError("recent submitted delivery state must contain string delivered keys")
        return set(delivered)

    def _write_delivered_locked(self, retrieve_session_id: str, delivered: set[str]) -> None:
        state_path = self._state_path(retrieve_session_id)
        tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {"session_id": retrieve_session_id, "delivered": sorted(delivered)},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        _fsync_directory(state_path.parent)


def collect_recent_submitted_memory(memory_root: Path) -> list[RecentSubmittedMemoryEntry]:
    store = AsyncUpdateStore(memory_root, "update")
    entries: list[RecentSubmittedMemoryEntry] = []
    if store.root.exists():
        for state_path in store._session_state_paths():
            session_hint = state_path.stem
            with store._locked(session_hint):
                state = store._read_checked_locked(session_hint)
            if state.role != "update":
                raise ValueError(f"async update state role mismatch: expected update, got {state.role}")
            entries.extend(_entries_from_jobs(state, state.current_batch))
            entries.extend(_entries_from_jobs(state, state.pending))

    queue_store = UpdateQueueStore(memory_root)
    candidates = [*queue_store.outbox_candidates(), *queue_store.snapshot().candidates]
    for candidate in candidates:
        entries.append(
            RecentSubmittedMemoryEntry(
                update_session_id=candidate.session_id,
                candidate_id=candidate.display_id,
                submitted_at=candidate.submitted_at,
                message=candidate.message,
                candidate_uid=candidate.uid,
            )
        )
    # A publishing device may temporarily see the same immutable candidate in both lanes.
    entries = list({entry.key: entry for entry in entries}.values())
    return sorted(
        entries,
        key=lambda entry: (
            entry.submitted_at,
            entry.update_session_id,
            entry.candidate_uid or "",
            entry.candidate_id,
        ),
    )


def format_recent_submitted_block(entries: list[RecentSubmittedMemoryEntry]) -> str:
    if not entries:
        return ""

    lines = [RECENT_SUBMITTED_HEADER, "", RECENT_SUBMITTED_INTRO, ""]
    for entry in entries:
        lines.append(
            f"[update session: {entry.update_session_id} | "
            f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
        )
        lines.extend(entry.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


def append_recent_submitted_memory(message: str, entries: list[RecentSubmittedMemoryEntry]) -> str:
    block = format_recent_submitted_block(entries)
    if not block:
        return message
    return f"{message.rstrip()}\n\n{block}"


def _entries_from_jobs(state: AsyncUpdateState, jobs: list[AsyncUpdateJob]) -> list[RecentSubmittedMemoryEntry]:
    return [
        RecentSubmittedMemoryEntry(
            update_session_id=state.session_id,
            candidate_id=job.id,
            submitted_at=job.submitted_at,
            message=job.message,
            candidate_uid=job.candidate_uid,
        )
        for job in jobs
    ]
