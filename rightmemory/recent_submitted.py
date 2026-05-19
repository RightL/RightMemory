from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore


RECENT_SUBMITTED_HEADER = "Recent submitted memory"
RECENT_SUBMITTED_INTRO = (
    "These are memory update submissions that have not been consolidated into MEMORY.md yet. "
    "Use them as short-term working memory when relevant."
)


@dataclass(frozen=True)
class RecentSubmittedMemoryEntry:
    update_session_id: str
    candidate_id: int
    submitted_at: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.update_session_id}:{self.candidate_id}:{self.submitted_at}"


def collect_recent_submitted_memory(memory_root: Path) -> list[RecentSubmittedMemoryEntry]:
    store = AsyncUpdateStore(memory_root, "update")
    if not store.root.exists():
        return []

    entries: list[RecentSubmittedMemoryEntry] = []
    for state_path in sorted(store.root.glob("*.json")):
        session_hint = state_path.stem
        with store._locked(session_hint):
            state = store._read_checked_locked(session_hint)
        if state.role != "update":
            raise ValueError(f"async update state role mismatch: expected update, got {state.role}")
        entries.extend(_entries_from_jobs(state, state.current_batch))
        entries.extend(_entries_from_jobs(state, state.pending))
    return sorted(
        entries,
        key=lambda entry: (entry.submitted_at, entry.update_session_id, entry.candidate_id),
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


def _entries_from_jobs(state: AsyncUpdateState, jobs: list[AsyncUpdateJob]) -> list[RecentSubmittedMemoryEntry]:
    return [
        RecentSubmittedMemoryEntry(
            update_session_id=state.session_id,
            candidate_id=job.id,
            submitted_at=job.submitted_at,
            message=job.message,
        )
        for job in jobs
    ]
