from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NormalizedTurn:
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

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptFile:
    source: str
    path: Path

    @property
    def key(self) -> str:
        return f"{self.source}:{self.path.resolve(strict=False)}"
