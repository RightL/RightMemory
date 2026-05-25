from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


MAX_DEBUG_TRACE_FIELD_CHARS = 200_000
MAX_DEBUG_TRACE_COLLECTION_ITEMS = 100


class DebugTrace:
    def __init__(self, memory_root: Path, role: str, session_id: str):
        safe_id = _safe_session_id(session_id)
        self.path = memory_root / ".runtime" / "debug" / role / f"{safe_id}.jsonl"
        self.role = role
        self.session_id = session_id

    def append(self, event: str, **fields: Any) -> None:
        _ensure_runtime_gitignore(self.path.parents[2])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "role": self.role,
            "session_id": self.session_id,
            "pid": os.getpid(),
            **{key: _bounded_trace_value(value) for key, value in fields.items()},
        }
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.path.parent)


def _bounded_trace_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= MAX_DEBUG_TRACE_FIELD_CHARS:
            return value
        return (
            value[:MAX_DEBUG_TRACE_FIELD_CHARS]
            + f"\n...[debug trace field truncated; original_chars={len(value)}]"
        )
    if isinstance(value, list):
        items = [_bounded_trace_value(item) for item in value[:MAX_DEBUG_TRACE_COLLECTION_ITEMS]]
        if len(value) > MAX_DEBUG_TRACE_COLLECTION_ITEMS:
            items.append(f"...[debug trace list truncated; original_items={len(value)}]")
        return items
    if isinstance(value, tuple):
        items = [_bounded_trace_value(item) for item in value[:MAX_DEBUG_TRACE_COLLECTION_ITEMS]]
        if len(value) > MAX_DEBUG_TRACE_COLLECTION_ITEMS:
            items.append(f"...[debug trace tuple truncated; original_items={len(value)}]")
        return items
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DEBUG_TRACE_COLLECTION_ITEMS:
                bounded["...[debug trace dict truncated]"] = f"original_items={len(value)}"
                break
            bounded[str(key)] = _bounded_trace_value(item)
        return bounded
    return value
