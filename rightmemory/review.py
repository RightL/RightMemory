from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .config import ReviewConfig, ReviewSourceConfig
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
    skipped_idle: int = 0
    skipped_old: int = 0
    skipped_reviewed: int = 0
    skipped_empty: int = 0
    retried: int = 0
    failed: int = 0

    def format(self) -> str:
        return (
            f"reviewed: {self.reviewed}\n"
            f"skipped_idle: {self.skipped_idle}\n"
            f"skipped_old: {self.skipped_old}\n"
            f"skipped_reviewed: {self.skipped_reviewed}\n"
            f"skipped_empty: {self.skipped_empty}\n"
            f"retried: {self.retried}\n"
            f"failed: {self.failed}"
        )


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
    def __init__(self, config: ReviewConfig, run_reviewer: Callable[[str, str], str]):
        self.config = config
        self.run_reviewer = run_reviewer
        self.state_store = ReviewStateStore(config.memory_root)

    def scan_once(self, *, now: float | None = None) -> ReviewScanResult:
        now = time.time() if now is None else now
        state = self.state_store.load()
        sessions = dict(state.sessions)
        counts = {
            "reviewed": 0,
            "skipped_idle": 0,
            "skipped_old": 0,
            "skipped_reviewed": 0,
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

                state_key = _state_key(normalized.source, normalized.session_id)
                if state_key in sessions:
                    counts["skipped_reviewed"] += 1
                    continue

                if not self._review_with_retry(normalized, counts):
                    return ReviewScanResult(**counts)

                sessions[state_key] = ReviewSessionState(
                    session_id=normalized.session_id,
                    source=normalized.source,
                    last_reviewed_at=datetime.now(UTC).isoformat(),
                )
                self.state_store.save(ReviewState(sessions=sessions))
                counts["reviewed"] += 1
                return ReviewScanResult(**counts)

        return ReviewScanResult(**counts)

    def _review_with_retry(self, payload: NormalizedSession, counts: dict[str, int]) -> bool:
        session_id = _review_session_id(payload)
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


def _review_session_id(session: NormalizedSession) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in session.session_id)
    return f"review-{session.source}-{safe}"


def _review_message(session: NormalizedSession) -> str:
    return (
        "Review this normalized provider transcript session.\n\n"
        "Review the whole session for durable memory and memory-backed skill knowledge. "
        "Choose the coherent shape for any durable signal: ordinary memory, refinement of an "
        "existing memory-backed skill, a new memory-backed skill topic, or purpose-driven support "
        "files under `skill_artifacts/<slug>/...`. If nothing is worth saving, reply exactly: "
        "Nothing to save.\n\n"
        "Normalized session JSON:\n"
        + json.dumps(session.to_payload(), ensure_ascii=False, indent=2)
    )


def _state_key(source: str, session_id: str) -> str:
    return f"{source}:{session_id}"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
