from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROLES
from .session import (
    _ensure_durable_directory,
    _ensure_runtime_gitignore,
    _fsync_directory,
    _safe_session_id,
)


PROVIDER_THREAD_SCHEMA_VERSION = 1
THREAD_POLICIES = {"persistent", "one-shot", "process-local"}
THREAD_STATUSES = {"active", "delete-pending"}


@dataclass(frozen=True)
class ProviderThreadRecord:
    provider: str
    provider_session_id: str
    role: str
    rightmemory_session_id: str
    policy: str
    created_at: str
    last_successful_activity_at: str | None = None
    status: str = "active"
    last_delete_attempt_at: str | None = None
    last_delete_error: str | None = None
    schema_version: int = PROVIDER_THREAD_SCHEMA_VERSION

    @property
    def activity_at(self) -> str:
        return self.last_successful_activity_at or self.created_at


@dataclass(frozen=True)
class MalformedProviderThreadRecord:
    path: Path
    error: str


@dataclass(frozen=True)
class ProviderThreadScan:
    records: tuple[ProviderThreadRecord, ...]
    malformed: tuple[MalformedProviderThreadRecord, ...]


class ProviderThreadStore:
    def __init__(self, memory_root: Path):
        self.runtime_root = Path(memory_root) / ".runtime"
        self.root = self.runtime_root / "agent_cli_threads"

    def provider_root(self, provider: str) -> Path:
        return self.root / _provider_name(provider)

    def path(self, provider: str, provider_session_id: str) -> Path:
        provider = _provider_name(provider)
        thread_id = _required_string(provider_session_id, "provider_session_id")
        key = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self.provider_root(provider) / f"{key}.json"

    def load(self, provider: str, provider_session_id: str) -> ProviderThreadRecord | None:
        path = self.path(provider, provider_session_id)
        if not path.exists():
            return None
        record = _record_from_json(path.read_text(encoding="utf-8"))
        if record.provider != provider or record.provider_session_id != provider_session_id:
            raise ValueError(f"provider thread record identity mismatch: {path}")
        return record

    def save(self, record: ProviderThreadRecord) -> None:
        _validate_record(record)
        _ensure_runtime_gitignore(self.runtime_root)
        path = self.path(record.provider, record.provider_session_id)
        _ensure_durable_directory(path.parent)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        content = json.dumps(asdict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    def record_created(
        self,
        *,
        provider: str,
        provider_session_id: str,
        role: str,
        rightmemory_session_id: str,
        policy: str,
        created_at: str,
    ) -> ProviderThreadRecord:
        existing = self.load(provider, provider_session_id)
        if existing is not None:
            if (
                existing.role != role
                or existing.rightmemory_session_id != rightmemory_session_id
                or existing.policy != policy
            ):
                raise ValueError("provider thread id is already owned by a different RightMemory session")
            return existing
        record = ProviderThreadRecord(
            provider=_provider_name(provider),
            provider_session_id=_required_string(provider_session_id, "provider_session_id"),
            role=_role(role),
            rightmemory_session_id=_rightmemory_session_id(rightmemory_session_id),
            policy=_thread_policy(policy),
            created_at=_required_string(created_at, "created_at"),
        )
        self.save(record)
        return record

    def record_success(self, provider: str, provider_session_id: str, *, activity_at: str) -> None:
        record = self.load(provider, provider_session_id)
        if record is None:
            return
        self.save(
            replace(
                record,
                last_successful_activity_at=_required_string(activity_at, "activity_at"),
                status="active",
                last_delete_attempt_at=None,
                last_delete_error=None,
            )
        )

    def mark_delete_pending(
        self,
        record: ProviderThreadRecord,
        *,
        attempted_at: str | None = None,
        error: str | None = None,
    ) -> ProviderThreadRecord:
        current = self.load(record.provider, record.provider_session_id)
        if current is None:
            return record
        updated = replace(
            current,
            status="delete-pending",
            last_delete_attempt_at=attempted_at,
            last_delete_error=_optional_bounded_string(error),
        )
        self.save(updated)
        return updated

    def delete(self, provider: str, provider_session_id: str) -> bool:
        path = self.path(provider, provider_session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(path.parent)
        return True

    def scan(self, provider: str | None = None) -> ProviderThreadScan:
        roots = [self.provider_root(provider)] if provider is not None else sorted(self.root.glob("*"))
        records: list[ProviderThreadRecord] = []
        malformed: list[MalformedProviderThreadRecord] = []
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in sorted(root.glob("*.json")):
                try:
                    record = _record_from_json(path.read_text(encoding="utf-8"))
                    if path != self.path(record.provider, record.provider_session_id):
                        raise ValueError("record path does not match provider thread id")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    malformed.append(MalformedProviderThreadRecord(path=path, error=str(exc)))
                    continue
                records.append(record)
        return ProviderThreadScan(records=tuple(records), malformed=tuple(malformed))

    def is_owned(self, provider: str, provider_session_id: str) -> bool:
        try:
            return self.load(provider, provider_session_id) is not None
        except (OSError, ValueError, json.JSONDecodeError):
            return False


def _record_from_json(content: str) -> ProviderThreadRecord:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("provider thread record must be a JSON object")
    return _record_from_dict(data)


def _record_from_dict(data: dict[str, Any]) -> ProviderThreadRecord:
    schema_version = data.get("schema_version")
    if schema_version != PROVIDER_THREAD_SCHEMA_VERSION:
        raise ValueError(f"unsupported provider thread schema version: {schema_version}")
    record = ProviderThreadRecord(
        provider=_required_string(data.get("provider"), "provider"),
        provider_session_id=_required_string(data.get("provider_session_id"), "provider_session_id"),
        role=_role(data.get("role")),
        rightmemory_session_id=_rightmemory_session_id(data.get("rightmemory_session_id")),
        policy=_required_string(data.get("policy"), "policy"),
        created_at=_required_string(data.get("created_at"), "created_at"),
        last_successful_activity_at=_optional_string(
            data.get("last_successful_activity_at"), "last_successful_activity_at"
        ),
        status=_required_string(data.get("status"), "status"),
        last_delete_attempt_at=_optional_string(data.get("last_delete_attempt_at"), "last_delete_attempt_at"),
        last_delete_error=_optional_string(data.get("last_delete_error"), "last_delete_error"),
        schema_version=schema_version,
    )
    _validate_record(record)
    return record


def _validate_record(record: ProviderThreadRecord) -> None:
    _provider_name(record.provider)
    _required_string(record.provider_session_id, "provider_session_id")
    _role(record.role)
    _rightmemory_session_id(record.rightmemory_session_id)
    _thread_policy(record.policy)
    _timestamp(record.created_at, "created_at")
    if record.last_successful_activity_at is not None:
        _timestamp(record.last_successful_activity_at, "last_successful_activity_at")
    if record.last_delete_attempt_at is not None:
        _timestamp(record.last_delete_attempt_at, "last_delete_attempt_at")
    if record.status not in THREAD_STATUSES:
        raise ValueError(f"provider thread status must be one of: {', '.join(sorted(THREAD_STATUSES))}")
    if record.schema_version != PROVIDER_THREAD_SCHEMA_VERSION:
        raise ValueError(f"unsupported provider thread schema version: {record.schema_version}")


def _provider_name(value: object) -> str:
    provider = _required_string(value, "provider")
    if any(character in provider for character in "/\\") or provider in {".", ".."}:
        raise ValueError("provider must be a safe path segment")
    return provider


def _thread_policy(value: object) -> str:
    policy = _required_string(value, "policy")
    if policy not in THREAD_POLICIES:
        raise ValueError(f"provider thread policy must be one of: {', '.join(sorted(THREAD_POLICIES))}")
    return policy


def _role(value: object) -> str:
    role = _required_string(value, "role")
    if role not in ROLES:
        raise ValueError(f"provider thread role must be one of: {', '.join(sorted(ROLES))}")
    return role


def _rightmemory_session_id(value: object) -> str:
    session_id = _required_string(value, "rightmemory_session_id")
    return _safe_session_id(session_id)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider thread {field} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field)


def _optional_bounded_string(value: str | None, limit: int = 2000) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean:
        return None
    return clean if len(clean) <= limit else clean[:limit] + "...[truncated]"


def _timestamp(value: object, field: str) -> str:
    text = _required_string(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"provider thread {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"provider thread {field} must include a timezone")
    return text
