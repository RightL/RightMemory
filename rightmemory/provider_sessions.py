from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .session import (
    _ensure_durable_directory,
    _ensure_runtime_gitignore,
    _fsync_directory,
    _safe_session_id,
)


@dataclass(frozen=True)
class ProviderSessionRecord:
    provider: str
    provider_session_id: str
    role: str
    rightmemory_session_id: str
    created_at: str
    updated_at: str


class ProviderSessionStore:
    def __init__(self, memory_root: Path, role: str):
        self.runtime_root = memory_root / ".runtime"
        self.role = _safe_session_id(role)
        self.root = self.runtime_root / "agent_cli_sessions" / self.role

    def path(self, rightmemory_session_id: str) -> Path:
        safe_id = _safe_session_id(rightmemory_session_id)
        return self.root / f"{safe_id}.json"

    def load(self, rightmemory_session_id: str) -> ProviderSessionRecord | None:
        path = self.path(rightmemory_session_id)
        if not path.exists():
            return None
        return _record_from_json(path.read_text(encoding="utf-8"))

    def save(self, record: ProviderSessionRecord) -> None:
        if record.role != self.role:
            raise ValueError(f"provider session role does not match store role: {record.role}")
        _ensure_runtime_gitignore(self.runtime_root)
        _ensure_durable_directory(self.root)
        path = self.path(record.rightmemory_session_id)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        content = json.dumps(asdict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)

    def delete_if_matches(
        self,
        rightmemory_session_id: str,
        provider_session_id: str,
        *,
        provider: str | None = None,
    ) -> bool:
        path = self.path(rightmemory_session_id)
        try:
            record = _record_from_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        if record.provider_session_id != provider_session_id or (
            provider is not None and record.provider != provider
        ):
            return False
        path.unlink()
        _fsync_directory(path.parent)
        return True

    @staticmethod
    def is_internal_provider_session(memory_root: Path, provider: str, provider_session_id: str) -> bool:
        from .provider_threads import ProviderThreadStore

        if ProviderThreadStore(memory_root).is_owned(provider, provider_session_id):
            return True
        records_root = memory_root / ".runtime" / "agent_cli_sessions"
        if not records_root.exists():
            return False
        for path in records_root.glob("*/*.json"):
            try:
                record = _record_from_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if record.provider == provider and record.provider_session_id == provider_session_id:
                return True
        return False


def _record_from_json(content: str) -> ProviderSessionRecord:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("provider session record must be a JSON object")
    return _record_from_dict(data)


def _record_from_dict(data: dict[str, Any]) -> ProviderSessionRecord:
    fields = {
        "provider": data.get("provider"),
        "provider_session_id": data.get("provider_session_id"),
        "role": data.get("role"),
        "rightmemory_session_id": data.get("rightmemory_session_id"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise ValueError("provider session record fields must be non-empty strings")
    return ProviderSessionRecord(**fields)
