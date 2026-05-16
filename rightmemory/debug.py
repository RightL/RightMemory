from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


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
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with self.path.open("ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.path.parent)
