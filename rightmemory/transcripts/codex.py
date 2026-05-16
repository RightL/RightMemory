from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import NormalizedSession, NormalizedTurn, TranscriptFile
from .text import append_text, text_from_content


def discover(root: Path) -> list[TranscriptFile]:
    if not root.exists():
        return []
    return [TranscriptFile("codex", path) for path in sorted(root.rglob("*.jsonl")) if path.is_file()]


def parse_session(path: Path) -> NormalizedSession | None:
    session_id = path.stem
    project: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    turns: list[tuple[str, str]] = []
    pending_user = ""
    pending_assistant = ""

    for obj in _iter_jsonl(path):
        timestamp = _string(obj.get("timestamp"))
        if timestamp:
            started_at = started_at or timestamp
            ended_at = timestamp

        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue

        if obj.get("type") == "session_meta":
            session_id = _string(payload.get("id")) or session_id
            project = _string(payload.get("cwd")) or project
            started_at = _string(payload.get("timestamp")) or started_at
            continue

        if obj.get("type") == "turn_context":
            project = _string(payload.get("cwd")) or project
            continue

        payload_type = payload.get("type")
        if obj.get("type") == "event_msg" and payload_type == "user_message":
            _finish_turn(turns, pending_user, pending_assistant)
            pending_user = _string(payload.get("message")) or _string(payload.get("text_elements")) or ""
            pending_assistant = ""
            continue

        if obj.get("type") == "event_msg" and payload_type == "agent_message":
            pending_assistant = append_text(pending_assistant, _string(payload.get("message")))
            continue

        if obj.get("type") == "event_msg" and payload_type == "task_complete":
            pending_assistant = append_text(pending_assistant, _string(payload.get("last_agent_message")))
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
        source="codex",
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


def _string(value: Any) -> str:
    return text_from_content(value)
