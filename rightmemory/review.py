from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .config import ReviewConfig, ReviewSourceConfig
from .provider_sessions import ProviderSessionStore
from .session import _ensure_runtime_gitignore, _fsync_directory
from .transcripts import claude, codex
from .transcripts.model import NormalizedSession, TranscriptFile

SECONDS_PER_DAY = 24 * 60 * 60
REVIEW_MAX_RETRIES = 1


@dataclass(frozen=True)
class ReviewSessionState:
    session_id: str
    source: str
    last_reviewed_at: str | None = None


@dataclass(frozen=True)
class ReviewState:
    sessions: dict[str, ReviewSessionState] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewScanResult:
    reviewed: int = 0
    skipped_duplicate: int = 0
    waiting_for_batch: int = 0
    skipped_idle: int = 0
    skipped_old: int = 0
    skipped_reviewed: int = 0
    skipped_internal: int = 0
    skipped_empty: int = 0
    retried: int = 0
    failed: int = 0

    def format(self) -> str:
        return (
            f"reviewed: {self.reviewed}\n"
            f"skipped_duplicate: {self.skipped_duplicate}\n"
            f"waiting_for_batch: {self.waiting_for_batch}\n"
            f"skipped_idle: {self.skipped_idle}\n"
            f"skipped_old: {self.skipped_old}\n"
            f"skipped_reviewed: {self.skipped_reviewed}\n"
            f"skipped_internal: {self.skipped_internal}\n"
            f"skipped_empty: {self.skipped_empty}\n"
            f"retried: {self.retried}\n"
            f"failed: {self.failed}"
        )


@dataclass(frozen=True)
class ReviewCandidate:
    transcript: TranscriptFile
    normalized: NormalizedSession
    mtime: float


@dataclass(frozen=True)
class ReviewCandidateDedupeResult:
    representatives: list[ReviewCandidate]
    aliases_by_representative: dict[str, list[ReviewCandidate]] = field(default_factory=dict)


class ReviewStateStore:
    def __init__(self, memory_root: Path):
        self.path = memory_root / ".runtime" / "review" / "state.json"
        self.runtime_root = memory_root / ".runtime"

    def load(self) -> ReviewState:
        if not self.path.exists():
            return ReviewState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        sessions = {}
        for key, value in data.get("sessions", {}).items():
            if isinstance(value, dict):
                session = ReviewSessionState(
                    session_id=str(value.get("session_id", "")),
                    source=str(value.get("source", "")),
                    last_reviewed_at=_str_or_none(value.get("last_reviewed_at")),
                )
                if session.source and session.session_id:
                    sessions[_state_key(session.source, session.session_id)] = session
        return ReviewState(sessions=sessions)

    def save(self, state: ReviewState) -> None:
        _ensure_runtime_gitignore(self.runtime_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {"sessions": {key: asdict(value) for key, value in state.sessions.items()}},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)
        _fsync_directory(self.path.parent)


class ReviewScanner:
    def __init__(
        self,
        config: ReviewConfig,
        run_reviewer: Callable[[str, str], str],
        *,
        on_review_success: Callable[[int], None] | None = None,
    ):
        self.config = config
        self.run_reviewer = run_reviewer
        self.on_review_success = on_review_success
        self.state_store = ReviewStateStore(config.memory_root)

    def scan_once(self, *, now: float | None = None, require_full_batch: bool = False) -> ReviewScanResult:
        now = time.time() if now is None else now
        state = self.state_store.load()
        sessions = dict(state.sessions)
        candidates: list[ReviewCandidate] = []
        counts = {
            "reviewed": 0,
            "skipped_duplicate": 0,
            "waiting_for_batch": 0,
            "skipped_idle": 0,
            "skipped_old": 0,
            "skipped_reviewed": 0,
            "skipped_internal": 0,
            "skipped_empty": 0,
            "retried": 0,
            "failed": 0,
        }

        for source in self.config.sources:
            for transcript in _discover(source):
                try:
                    stat = transcript.path.stat()
                except OSError:
                    counts["skipped_empty"] += 1
                    continue
                if now - stat.st_mtime > self.config.since_days * SECONDS_PER_DAY:
                    counts["skipped_old"] += 1
                    continue
                if now - stat.st_mtime < self.config.idle_seconds:
                    counts["skipped_idle"] += 1
                    continue

                normalized = _parse(transcript)
                if normalized is None or not normalized.turns:
                    counts["skipped_empty"] += 1
                    continue

                if ProviderSessionStore.is_internal_provider_session(
                    self.config.memory_root,
                    normalized.source,
                    normalized.session_id,
                ):
                    counts["skipped_internal"] += 1
                    continue

                state_key = _state_key(normalized.source, normalized.session_id)
                if state_key in sessions:
                    counts["skipped_reviewed"] += 1
                    continue

                candidates.append(
                    ReviewCandidate(
                        transcript=transcript,
                        normalized=normalized,
                        mtime=stat.st_mtime,
                    )
                )

        sorted_candidates = sorted(candidates, key=_scan_order_key)
        unique_candidates = []
        seen_candidate_keys: set[str] = set()
        for candidate in sorted_candidates:
            state_key = _state_key(candidate.normalized.source, candidate.normalized.session_id)
            if state_key in seen_candidate_keys:
                counts["skipped_reviewed"] += 1
                continue
            seen_candidate_keys.add(state_key)
            unique_candidates.append(candidate)

        deduped = _dedupe_prefix_candidates(unique_candidates)
        representatives = deduped.representatives

        if require_full_batch and len(representatives) < self.config.batch_size:
            counts["waiting_for_batch"] += len(representatives)
            return ReviewScanResult(**counts)

        batch = representatives[: self.config.batch_size]
        if not batch:
            return ReviewScanResult(**counts)

        normalized_batch = [candidate.normalized for candidate in batch]
        if not self._review_with_retry(normalized_batch, counts):
            return ReviewScanResult(**counts)

        reviewed_candidates = []
        for candidate in batch:
            reviewed_candidates.append(candidate)
            reviewed_candidates.extend(
                deduped.aliases_by_representative.get(_candidate_state_key(candidate), [])
            )

        reviewed_at = datetime.now(UTC).isoformat()
        for candidate in reviewed_candidates:
            session = candidate.normalized
            sessions[_state_key(session.source, session.session_id)] = ReviewSessionState(
                session_id=session.session_id,
                source=session.source,
                last_reviewed_at=reviewed_at,
            )
        self.state_store.save(ReviewState(sessions=sessions))
        counts["reviewed"] += len(normalized_batch)
        counts["skipped_duplicate"] += len(reviewed_candidates) - len(normalized_batch)
        if self.on_review_success is not None:
            self.on_review_success(len(normalized_batch))
        return ReviewScanResult(**counts)

    def _review_with_retry(self, payload: list[NormalizedSession], counts: dict[str, int]) -> bool:
        session_id = _review_batch_id(payload)
        message = _review_message(payload)
        for attempt in range(REVIEW_MAX_RETRIES + 1):
            try:
                self.run_reviewer(session_id, message)
                return True
            except Exception:
                if attempt < REVIEW_MAX_RETRIES:
                    counts["retried"] += 1
                    continue
                counts["failed"] += 1
                return False
        return False


def normalize_transcript(source: str, path: Path) -> NormalizedSession | None:
    transcript = TranscriptFile(source, path)
    return _parse(transcript)


def _discover(source: ReviewSourceConfig) -> list[TranscriptFile]:
    if source.kind == "codex":
        return codex.discover(source.path)
    if source.kind == "claude":
        return claude.discover(source.path)
    return []


def _parse(transcript: TranscriptFile) -> NormalizedSession | None:
    if transcript.source == "codex":
        return codex.parse_session(transcript.path)
    if transcript.source == "claude":
        return claude.parse_session(transcript.path)
    return None


def _dedupe_prefix_candidates(candidates: list[ReviewCandidate]) -> ReviewCandidateDedupeResult:
    kept: list[tuple[ReviewCandidate, tuple[str, ...]]] = []
    aliases_by_representative: dict[str, list[ReviewCandidate]] = {}

    for candidate in sorted(candidates, key=_prefix_dedupe_order_key):
        candidate_hashes = _turn_hashes(candidate.normalized)
        representative: ReviewCandidate | None = None
        for kept_candidate, kept_hashes in kept:
            if candidate.normalized.source != kept_candidate.normalized.source:
                continue
            if _is_hash_prefix(candidate_hashes, kept_hashes):
                representative = kept_candidate
                break
        if representative is None:
            kept.append((candidate, candidate_hashes))
            continue
        aliases_by_representative.setdefault(_candidate_state_key(representative), []).append(candidate)

    representatives = sorted((candidate for candidate, _hashes in kept), key=_scan_order_key)
    return ReviewCandidateDedupeResult(
        representatives=representatives,
        aliases_by_representative=aliases_by_representative,
    )


def _scan_order_key(candidate: ReviewCandidate) -> tuple[float, str, str, str]:
    return (
        candidate.mtime,
        candidate.transcript.path.as_posix(),
        candidate.normalized.source,
        candidate.normalized.session_id,
    )


def _prefix_dedupe_order_key(candidate: ReviewCandidate) -> tuple[int, float, str, str, str]:
    return (
        -len(candidate.normalized.turns),
        -candidate.mtime,
        candidate.transcript.path.as_posix(),
        candidate.normalized.source,
        candidate.normalized.session_id,
    )


def _candidate_state_key(candidate: ReviewCandidate) -> str:
    return _state_key(candidate.normalized.source, candidate.normalized.session_id)


def _turn_hashes(session: NormalizedSession) -> tuple[str, ...]:
    hashes = []
    for turn in session.turns:
        payload = json.dumps(
            {"user": turn.user, "assistant": turn.assistant},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        hashes.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return tuple(hashes)


def _is_hash_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    return len(shorter) <= len(longer) and longer[: len(shorter)] == shorter


def _review_batch_id(sessions: list[NormalizedSession]) -> str:
    identifiers = [f"{session.source}-{session.session_id}" for session in sessions]
    digest = hashlib.sha1("\n".join(identifiers).encode("utf-8")).hexdigest()[:10]
    joined = "-".join(_safe_batch_id_part(identifier) for identifier in identifiers)
    if len(joined) > 80:
        joined = joined[:80].rstrip("._-")
    if joined:
        return f"review-batch-{joined}-{digest}"
    return f"review-batch-{digest}"


def _safe_batch_id_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._-")


def _review_message(sessions: list[NormalizedSession]) -> str:
    batch_id = _review_batch_id(sessions)
    payload = {
        "batch_id": batch_id,
        "sessions": [session.to_payload() for session in sessions],
    }
    return (
        "Review this normalized provider transcript batch.\n\n"
        "Review the ordered sessions together for durable memory. If nothing is worth saving, "
        "reply exactly: Nothing to save.\n\n"
        "Normalized transcript batch JSON:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _state_key(source: str, session_id: str) -> str:
    return f"{source}:{session_id}"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
