from __future__ import annotations

from typing import Any


def text_from_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [text_from_content(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        block_type = str(value.get("type", ""))
        if block_type in {"tool_use", "tool_result", "thinking", "reasoning"}:
            return ""
        for key in ("text", "message", "content"):
            if key in value:
                return text_from_content(value.get(key))
    return ""


def append_text(existing: str, addition: str) -> str:
    addition = addition.strip()
    if not addition:
        return existing
    existing = existing.strip()
    if not existing:
        return addition
    if addition in existing:
        return existing
    return existing + "\n\n" + addition
