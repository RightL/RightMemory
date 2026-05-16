from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NormalizedTurn:
    i: int
    user: str
    assistant: str


@dataclass(frozen=True)
class NormalizedSession:
    source: str
    session_id: str
    project: str | None
    started_at: str | None
    ended_at: str | None
    turns: list[NormalizedTurn] = field(default_factory=list)
    already_reviewed_turns: int = 0

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def with_review_cursor(self, already_reviewed_turns: int) -> NormalizedSession:
        return NormalizedSession(
            source=self.source,
            session_id=self.session_id,
            project=self.project,
            started_at=self.started_at,
            ended_at=self.ended_at,
            turns=self.turns,
            already_reviewed_turns=already_reviewed_turns,
        )


@dataclass(frozen=True)
class TranscriptFile:
    source: str
    path: Path

    @property
    def key(self) -> str:
        return f"{self.source}:{self.path.resolve(strict=False)}"
