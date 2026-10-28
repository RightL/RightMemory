from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import CurationBatchConfig
from .learning import LearningCandidate


SESSION_QUIET_SECONDS = 3_600
MAX_BATCH_EVIDENCE_CHARS = 24_000


@dataclass(frozen=True)
class CurationBatchSelection:
    candidates: tuple[LearningCandidate, ...] = ()
    next_ready_at: datetime | None = None


def select_curation_batch(
    candidates: Iterable[LearningCandidate], config: CurationBatchConfig,
    *, now: datetime | None = None,
) -> CurationBatchSelection:
    """Select one domain's pending evidence, without changing persistent state."""
    current = _utc_datetime(datetime.now(UTC) if now is None else now)
    sessions: dict[str, list[tuple[datetime, LearningCandidate]]] = {}
    count = 0
    for candidate in candidates:
        submitted = _utc_datetime(datetime.fromisoformat(candidate.submitted_at))
        sessions.setdefault(candidate.session_id, []).append((submitted, candidate))
        count += 1
    if not sessions:
        return CurationBatchSelection()

    quiet_period = timedelta(seconds=SESSION_QUIET_SECONDS)
    ordered = sorted(
        (max(timestamp for timestamp, _ in items) + quiet_period, session_id, items)
        for session_id, items in sessions.items()
    )
    triggered = count >= config.trigger_candidates
    deadline = ordered[0][0] + timedelta(seconds=config.max_wait_seconds)
    if not triggered and current < deadline:
        return CurationBatchSelection(next_ready_at=deadline)

    selected: list[LearningCandidate] = []
    evidence_chars = 0
    for ready_at, _, items in ordered:
        if not triggered and ready_at > current:
            continue
        for _, candidate in sorted(items, key=lambda item: (item[0], item[1].candidate_id)):
            if selected and evidence_chars + len(candidate.evidence) > MAX_BATCH_EVIDENCE_CHARS:
                return CurationBatchSelection(tuple(selected))
            selected.append(candidate)
            evidence_chars += len(candidate.evidence)
            if len(selected) >= config.target_batch_candidates or evidence_chars >= MAX_BATCH_EVIDENCE_CHARS:
                return CurationBatchSelection(tuple(selected))
    return CurationBatchSelection(tuple(selected))


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("curation batch timestamps must include a timezone")
    return value.astimezone(UTC)
