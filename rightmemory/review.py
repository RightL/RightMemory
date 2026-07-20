from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .async_update import AsyncUpdateState, AsyncUpdateStore
from .config import ReviewConfig, ReviewSourceConfig
from .provider_sessions import ProviderSessionStore
from .session import _ensure_runtime_gitignore, _fsync_directory
from .transcripts import claude, codex
from .transcripts.model import NormalizedSession, TranscriptFile

SECONDS_PER_DAY = 24 * 60 * 60
REVIEW_MAX_RETRIES = 1
REVIEW_NO_CANDIDATE = "Nothing to save."


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


@dataclass(frozen=True)
class ReviewDeliveryReceipt:
    batch_id: str
    candidate: str
    candidate_id: int
    reviewed_at: str
    sessions: tuple[ReviewSessionState, ...]
    reviewed_count: int
    skipped_duplicate_count: int


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


class ReviewDeliveryStore:
    def __init__(self, memory_root: Path):
        self.root = memory_root / ".runtime" / "review" / "deliveries"
        self.runtime_root = memory_root / ".runtime"

    def oldest(self) -> ReviewDeliveryReceipt | None:
        if not self.root.exists():
            return None
        paths = list(self.root.glob("*.json"))
        if not paths:
            return None
        path = min(paths, key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name))
        return self._load(path)

    def save(self, receipt: ReviewDeliveryReceipt) -> None:
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(receipt.batch_id)
        if path.exists():
            if self._load(path) != receipt:
                raise RuntimeError(f"conflicting transcript-review delivery receipt: {receipt.batch_id}")
            return
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {
                "version": 1,
                "batch_id": receipt.batch_id,
                "candidate": receipt.candidate,
                "candidate_id": receipt.candidate_id,
                "reviewed_at": receipt.reviewed_at,
                "sessions": [asdict(session) for session in receipt.sessions],
                "reviewed_count": receipt.reviewed_count,
                "skipped_duplicate_count": receipt.skipped_duplicate_count,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(self.root)

    def delete(self, batch_id: str) -> None:
        path = self._path(batch_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self.root)

    def _load(self, path: Path) -> ReviewDeliveryReceipt:
        data = json.loads(path.read_text(encoding="utf-8"))
        receipt = _parse_delivery_receipt(data)
        if path != self._path(receipt.batch_id):
            raise ValueError("transcript-review delivery receipt filename does not match its batch id")
        return receipt

    def _path(self, batch_id: str) -> Path:
        digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"


class ReviewScanner:
    def __init__(
        self,
        config: ReviewConfig,
        run_reviewer: Callable[[str, str], str],
        *,
        submit_candidate: Callable[[str, str], object] | None = None,
    ):
        self.config = config
        self.run_reviewer = run_reviewer
        self.update_store = AsyncUpdateStore(config.memory_root, "update")
        self._uses_default_submit = submit_candidate is None
        self.submit_candidate = submit_candidate
        self.state_store = ReviewStateStore(config.memory_root)
        self.delivery_store = ReviewDeliveryStore(config.memory_root)

    def scan_once(self, *, now: float | None = None, require_full_batch: bool = False) -> ReviewScanResult:
        now = time.time() if now is None else now
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
        state = self.state_store.load()
        sessions = dict(state.sessions)
        if self._uses_default_submit:
            try:
                receipt = self.delivery_store.oldest()
            except Exception:
                counts["failed"] += 1
                return ReviewScanResult(**counts)
            if receipt is not None:
                return self._recover_delivery(receipt, sessions, counts)

        candidates: list[ReviewCandidate] = []

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
        reviewed_candidates = []
        for candidate in batch:
            reviewed_candidates.append(candidate)
            reviewed_candidates.extend(
                deduped.aliases_by_representative.get(_candidate_state_key(candidate), [])
            )

        reviewed_at = datetime.now(UTC).isoformat()
        reviewed_sessions = tuple(
            ReviewSessionState(
                session_id=candidate.normalized.session_id,
                source=candidate.normalized.source,
                last_reviewed_at=reviewed_at,
            )
            for candidate in reviewed_candidates
        )
        success, receipt_batch_id = self._review_with_retry(
            normalized_batch,
            reviewed_sessions,
            reviewed_at,
            len(reviewed_candidates) - len(normalized_batch),
            counts,
        )
        if not success:
            return ReviewScanResult(**counts)

        for session in reviewed_sessions:
            sessions[_state_key(session.source, session.session_id)] = session
        self.state_store.save(ReviewState(sessions=sessions))
        if receipt_batch_id is not None:
            self.delivery_store.delete(receipt_batch_id)
        counts["reviewed"] += len(normalized_batch)
        counts["skipped_duplicate"] += len(reviewed_candidates) - len(normalized_batch)
        return ReviewScanResult(**counts)

    def _review_with_retry(
        self,
        payload: list[NormalizedSession],
        reviewed_sessions: tuple[ReviewSessionState, ...],
        reviewed_at: str,
        skipped_duplicate_count: int,
        counts: dict[str, int],
    ) -> tuple[bool, str | None]:
        session_id = _review_batch_id(payload)
        message = _review_message(payload)
        for attempt in range(REVIEW_MAX_RETRIES + 1):
            try:
                candidate = normalize_review_candidate(self.run_reviewer(session_id, message))
            except Exception:
                if attempt < REVIEW_MAX_RETRIES:
                    counts["retried"] += 1
                    continue
                counts["failed"] += 1
                return False, None
            break
        if candidate is None:
            return True, None
        try:
            if not self._uses_default_submit:
                if self.submit_candidate is None:
                    raise RuntimeError("transcript-review candidate submitter is unavailable")
                self.submit_candidate(session_id, candidate)
                return True, None

            state = self.update_store.read(session_id)
            receipt = ReviewDeliveryReceipt(
                batch_id=session_id,
                candidate=candidate,
                candidate_id=state.next_id,
                reviewed_at=reviewed_at,
                sessions=reviewed_sessions,
                reviewed_count=len(payload),
                skipped_duplicate_count=skipped_duplicate_count,
            )
            self.delivery_store.save(receipt)
            self.update_store.submit(session_id, candidate)
        except Exception:
            counts["failed"] += 1
            return False, None
        return True, session_id

    def _recover_delivery(
        self,
        receipt: ReviewDeliveryReceipt,
        sessions: dict[str, ReviewSessionState],
        counts: dict[str, int],
    ) -> ReviewScanResult:
        try:
            self._resume_delivery(receipt)
        except Exception:
            counts["failed"] += 1
            return ReviewScanResult(**counts)

        for session in receipt.sessions:
            sessions[_state_key(session.source, session.session_id)] = session
        self.state_store.save(ReviewState(sessions=sessions))
        self.delivery_store.delete(receipt.batch_id)
        counts["reviewed"] += receipt.reviewed_count
        counts["skipped_duplicate"] += receipt.skipped_duplicate_count
        return ReviewScanResult(**counts)

    def _resume_delivery(self, receipt: ReviewDeliveryReceipt) -> None:
        state = self.update_store.read(receipt.batch_id)
        if state.next_id < receipt.candidate_id:
            raise RuntimeError("async updater state precedes transcript-review delivery receipt")
        if state.next_id == receipt.candidate_id:
            state = self.update_store.submit(receipt.batch_id, receipt.candidate)
            _validate_delivery_state(state, receipt)
            return

        _validate_delivery_state(state, receipt)
        if state.pending or state.current_batch:
            self.update_store.ensure_worker(receipt.batch_id)


def normalize_transcript(source: str, path: Path) -> NormalizedSession | None:
    transcript = TranscriptFile(source, path)
    return _parse(transcript)


def normalize_review_candidate(output: str) -> str | None:
    """Return one updater candidate, or ``None`` for the exact reviewer no-op."""
    clean = output.strip()
    if not clean:
        raise RuntimeError("transcript reviewer returned an empty response")
    if clean == REVIEW_NO_CANDIDATE:
        return None
    return clean


def _validate_delivery_state(state: AsyncUpdateState, receipt: ReviewDeliveryReceipt) -> None:
    if state.next_id <= receipt.candidate_id:
        raise RuntimeError("async updater did not accept transcript-review delivery receipt")
    matching_jobs = [
        job for job in [*state.current_batch, *state.pending] if job.id == receipt.candidate_id
    ]
    if matching_jobs and any(job.message != receipt.candidate for job in matching_jobs):
        raise RuntimeError("async updater candidate conflicts with transcript-review delivery receipt")


def _parse_delivery_receipt(data: object) -> ReviewDeliveryReceipt:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("unsupported transcript-review delivery receipt")
    batch_id = data.get("batch_id")
    candidate = data.get("candidate")
    candidate_id = data.get("candidate_id")
    reviewed_at = data.get("reviewed_at")
    reviewed_count = data.get("reviewed_count")
    skipped_duplicate_count = data.get("skipped_duplicate_count")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("transcript-review delivery receipt requires a batch id")
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("transcript-review delivery receipt requires a candidate")
    if not isinstance(candidate_id, int) or isinstance(candidate_id, bool) or candidate_id < 1:
        raise ValueError("transcript-review delivery receipt candidate id must be positive")
    if not isinstance(reviewed_at, str) or not reviewed_at:
        raise ValueError("transcript-review delivery receipt requires a review timestamp")
    if not isinstance(reviewed_count, int) or isinstance(reviewed_count, bool) or reviewed_count < 1:
        raise ValueError("transcript-review delivery receipt reviewed count must be positive")
    if (
        not isinstance(skipped_duplicate_count, int)
        or isinstance(skipped_duplicate_count, bool)
        or skipped_duplicate_count < 0
    ):
        raise ValueError("transcript-review delivery receipt duplicate count cannot be negative")

    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, list):
        raise ValueError("transcript-review delivery receipt requires sessions")
    sessions = []
    seen: set[str] = set()
    for raw_session in raw_sessions:
        if not isinstance(raw_session, dict):
            raise ValueError("invalid transcript-review delivery receipt session")
        session_id = raw_session.get("session_id")
        source = raw_session.get("source")
        last_reviewed_at = raw_session.get("last_reviewed_at")
        if not isinstance(session_id, str) or not session_id or not isinstance(source, str) or not source:
            raise ValueError("invalid transcript-review delivery receipt session identity")
        if last_reviewed_at != reviewed_at:
            raise ValueError("transcript-review delivery receipt session timestamp mismatch")
        key = _state_key(source, session_id)
        if key in seen:
            raise ValueError("duplicate transcript-review delivery receipt session")
        seen.add(key)
        sessions.append(
            ReviewSessionState(
                session_id=session_id,
                source=source,
                last_reviewed_at=reviewed_at,
            )
        )
    if len(sessions) != reviewed_count + skipped_duplicate_count:
        raise ValueError("transcript-review delivery receipt session counts do not match")
    return ReviewDeliveryReceipt(
        batch_id=batch_id,
        candidate=candidate,
        candidate_id=candidate_id,
        reviewed_at=reviewed_at,
        sessions=tuple(sessions),
        reviewed_count=reviewed_count,
        skipped_duplicate_count=skipped_duplicate_count,
    )


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
    digest_payload = json.dumps(
        [session.to_payload() for session in sessions],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()[:10]
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
        "Review this normalized provider transcript batch in read-only extraction mode.\n\n"
        "Return one provenance-preserving candidate bundle for the unified RightMemory updater. "
        "Do not edit or commit RightMemory files and do not decide which store should receive a signal. "
        f"If the batch contains no useful candidate, reply exactly: {REVIEW_NO_CANDIDATE}\n\n"
        "Normalized transcript batch JSON:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _state_key(source: str, session_id: str) -> str:
    return f"{source}:{session_id}"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
