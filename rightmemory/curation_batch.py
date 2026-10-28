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
    admitted_candidate_ids: tuple[str, ...] = ()


def select_curation_batch(
    candidates: Iterable[LearningCandidate], config: CurationBatchConfig,
    *, admitted_candidate_ids: Iterable[str] = (), now: datetime | None = None,
) -> CurationBatchSelection:
    """Select one domain's pending evidence, without changing persistent state."""
    current = _utc_datetime(datetime.now(UTC) if now is None else now)
    pending = tuple(candidates)
    admitted = tuple(admitted_candidate_ids)
    admitted_set = set(admitted)
    continuing = bool(admitted_set.intersection(item.candidate_id for item in pending))
    sessions: dict[str, list[tuple[datetime, LearningCandidate]]] = {}
    count = 0
    for candidate in pending:
        if continuing and candidate.candidate_id not in admitted_set:
            continue
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
    triggered = continuing or count >= config.trigger_candidates
    deadline = ordered[0][0] + timedelta(seconds=config.max_wait_seconds)
    if not triggered and current < deadline:
        return CurationBatchSelection(next_ready_at=deadline)

    eligible = tuple(
        candidate
        for ready_at, _, items in ordered if triggered or ready_at <= current
        for _, candidate in sorted(items, key=lambda item: (item[0], item[1].candidate_id))
    )
    admitted = admitted if continuing else tuple(item.candidate_id for item in eligible)
    selected: list[LearningCandidate] = []
    evidence_chars = 0
    for candidate in eligible:
        if selected and evidence_chars + len(candidate.evidence) > MAX_BATCH_EVIDENCE_CHARS:
            break
        selected.append(candidate)
        evidence_chars += len(candidate.evidence)
        if len(selected) >= config.target_batch_candidates or evidence_chars >= MAX_BATCH_EVIDENCE_CHARS:
            break
    return CurationBatchSelection(tuple(selected), admitted_candidate_ids=admitted)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("curation batch timestamps must include a timezone")
    return value.astimezone(UTC)
