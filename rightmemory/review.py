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
from .session import _ensure_runtime_gitignore, _fsync_directory
from .transcripts import claude, codex
from .transcripts.model import NormalizedSession, TranscriptFile

SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class ReviewSessionState:
    session_id: str
    source: str
    last_reviewed_turn: int = 0
    reviewed_turns_hash: str | None = None
    last_seen_mtime: float | None = None
    last_seen_size: int | None = None
    last_reviewed_at: str | None = None


@dataclass(frozen=True)
class ReviewState:
    sessions: dict[str, ReviewSessionState] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewScanResult:
    reviewed: int = 0
    skipped_idle: int = 0
    skipped_old: int = 0
    skipped_unchanged: int = 0
    skipped_empty: int = 0
    reset_changed: int = 0
    failed: int = 0

    def format(self) -> str:
        return (
            f"reviewed: {self.reviewed}\n"
            f"skipped_idle: {self.skipped_idle}\n"
            f"skipped_old: {self.skipped_old}\n"
            f"skipped_unchanged: {self.skipped_unchanged}\n"
            f"skipped_empty: {self.skipped_empty}\n"
            f"reset_changed: {self.reset_changed}\n"
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
                sessions[key] = ReviewSessionState(
                    session_id=str(value.get("session_id", "")),
                    source=str(value.get("source", "")),
                    last_reviewed_turn=_int(value.get("last_reviewed_turn")),
                    reviewed_turns_hash=_str_or_none(value.get("reviewed_turns_hash")),
                    last_seen_mtime=_float(value.get("last_seen_mtime")),
                    last_seen_size=_int_or_none(value.get("last_seen_size")),
                    last_reviewed_at=_str_or_none(value.get("last_reviewed_at")),
                )
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
            "skipped_unchanged": 0,
            "skipped_empty": 0,
            "reset_changed": 0,
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

                prior = sessions.get(transcript.key)
                last_reviewed_turn = prior.last_reviewed_turn if prior else 0
                if prior and prior.reviewed_turns_hash and last_reviewed_turn > 0:
                    reviewed_prefix_hash = _turns_hash(normalized, last_reviewed_turn)
                    if len(normalized.turns) < last_reviewed_turn or reviewed_prefix_hash != prior.reviewed_turns_hash:
                        last_reviewed_turn = 0
                        counts["reset_changed"] += 1

                if len(normalized.turns) <= last_reviewed_turn:
                    counts["skipped_unchanged"] += 1
                    continue

                payload = normalized.with_review_cursor(last_reviewed_turn)
                try:
                    self.run_reviewer(_review_session_id(payload), _review_message(payload))
                except Exception:
                    counts["failed"] += 1
                    continue

                sessions[transcript.key] = ReviewSessionState(
                    session_id=normalized.session_id,
                    source=normalized.source,
                    last_reviewed_turn=len(normalized.turns),
                    reviewed_turns_hash=_turns_hash(normalized, len(normalized.turns)),
                    last_seen_mtime=stat.st_mtime,
                    last_seen_size=stat.st_size,
                    last_reviewed_at=datetime.now(UTC).isoformat(),
                )
                self.state_store.save(ReviewState(sessions=sessions))
                counts["reviewed"] += 1

        return ReviewScanResult(**counts)


def normalize_transcript(source: str, path: Path, already_reviewed_turns: int = 0) -> NormalizedSession | None:
    transcript = TranscriptFile(source, path)
    normalized = _parse(transcript)
    if normalized is None:
        return None
    return normalized.with_review_cursor(already_reviewed_turns)


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
        "Use the whole session for context, but only extract durable memory "
        "from turns where i > already_reviewed_turns. If nothing is worth "
        "saving, reply exactly: Nothing to save.\n\n"
        "Normalized session JSON:\n"
        + json.dumps(session.to_payload(), ensure_ascii=False, indent=2)
    )


def _turns_hash(session: NormalizedSession, count: int) -> str:
    payload = [
        {"i": turn.i, "user": turn.user, "assistant": turn.assistant}
        for turn in session.turns[:count]
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
