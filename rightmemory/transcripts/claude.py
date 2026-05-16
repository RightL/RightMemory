from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import NormalizedSession, NormalizedTurn, TranscriptFile
from .text import append_text, text_from_content


def discover(root: Path) -> list[TranscriptFile]:
    if not root.exists():
        return []
    return [TranscriptFile("claude", path) for path in sorted(root.glob("*/*.jsonl")) if path.is_file()]


def parse_session(path: Path) -> NormalizedSession | None:
    session_id = path.stem
    project = _project_from_path(path)
    started_at: str | None = None
    ended_at: str | None = None
    turns: list[tuple[str, str]] = []
    pending_user = ""
    pending_assistant = ""

    for obj in _iter_jsonl(path):
        if obj.get("isSidechain") is True or obj.get("isMeta") is True:
            continue

        timestamp = _string(obj.get("timestamp"))
        if timestamp:
            started_at = started_at or timestamp
            ended_at = timestamp
        session_id = _string(obj.get("sessionId")) or session_id
        project = _string(obj.get("cwd")) or project

        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role") or obj.get("type")
        text = text_from_content(message.get("content"))

        if role == "user":
            _finish_turn(turns, pending_user, pending_assistant)
            pending_user = text
            pending_assistant = ""
        elif role == "assistant":
            pending_assistant = append_text(pending_assistant, text)
            stop_reason = message.get("stop_reason")
            if stop_reason and pending_user and pending_assistant:
                _finish_turn(turns, pending_user, pending_assistant)
                pending_user = ""
                pending_assistant = ""

    _finish_turn(turns, pending_user, pending_assistant)
    normalized_turns = [
        NormalizedTurn(i=index, user=user, assistant=assistant)
        for index, (user, assistant) in enumerate(turns, start=1)
        if user.strip() and assistant.strip()
    ]
    if not normalized_turns:
        return None
    return NormalizedSession(
        source="claude",
        session_id=session_id,
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        turns=normalized_turns,
    )


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _finish_turn(turns: list[tuple[str, str]], user: str, assistant: str) -> None:
    user = user.strip()
    assistant = assistant.strip()
    if user and assistant:
        turns.append((user, assistant))


def _project_from_path(path: Path) -> str | None:
    parent = path.parent.name
    if not parent.startswith("-"):
        return None
    return "/" + parent.strip("-").replace("-", "/")


def _string(value: Any) -> str:
    return text_from_content(value)
