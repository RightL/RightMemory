from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator
from uuid import uuid4

from .platform import lock_file, unlock_file
from .session import _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory


PROVIDER_PREFIX_SCHEMA_VERSION = 1
_PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_PREFIX_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_FIELDS = frozenset(
    {
        "provider",
        "prefix_key",
        "provider_session_id",
        "created_at",
        "updated_at",
        "schema_version",
    }
)


@dataclass(frozen=True)
class ProviderPrefixRecord:
    provider: str
    prefix_key: str
    provider_session_id: str
    created_at: str
    updated_at: str
    schema_version: int = PROVIDER_PREFIX_SCHEMA_VERSION


class ProviderPrefixStore:
    def __init__(self, memory_root: Path):
        self.runtime_root = Path(memory_root) / ".runtime"
        self.root = self.runtime_root / "agent_cli_prefixes"

    def provider_root(self, provider: str) -> Path:
        return self.root / _provider_name(provider)

    def path(self, provider: str, prefix_key: str) -> Path:
        provider = _provider_name(provider)
        prefix_key = _prefix_key(prefix_key)
        return self.provider_root(provider) / f"{prefix_key}.json"

    def lock_path(self, provider: str, prefix_key: str) -> Path:
        provider = _provider_name(provider)
        prefix_key = _prefix_key(prefix_key)
        return self.provider_root(provider) / f"{prefix_key}.lock"

    def load(self, provider: str, prefix_key: str) -> ProviderPrefixRecord | None:
        provider = _provider_name(provider)
        prefix_key = _prefix_key(prefix_key)
        path = self.path(provider, prefix_key)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        record = _record_from_json(content)
        if record.provider != provider or record.prefix_key != prefix_key:
            raise ValueError(f"provider prefix record identity mismatch: {path}")
        return record

    def save(self, record: ProviderPrefixRecord) -> None:
        _validate_record(record)
        _ensure_runtime_gitignore(self.runtime_root)
        path = self.path(record.provider, record.prefix_key)
        _ensure_durable_directory(path.parent)
        tmp_path = path.parent / f".{os.getpid()}.{uuid4().hex}.tmp"
        content = json.dumps(
            {
                "provider": record.provider,
                "prefix_key": record.prefix_key,
                "provider_session_id": record.provider_session_id,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "schema_version": record.schema_version,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    def delete_if_matches(
        self,
        provider: str,
        prefix_key: str,
        provider_session_id: str,
    ) -> bool:
        provider = _provider_name(provider)
        prefix_key = _prefix_key(prefix_key)
        provider_session_id = _required_string(provider_session_id, "provider_session_id")
        record = self.load(provider, prefix_key)
        if record is None or record.provider_session_id != provider_session_id:
            return False
        path = self.path(provider, prefix_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(path.parent)
        return True

    @contextmanager
    def locked(self, provider: str, prefix_key: str) -> Iterator[None]:
        provider = _provider_name(provider)
        prefix_key = _prefix_key(prefix_key)
        _ensure_runtime_gitignore(self.runtime_root)
        lock_path = self.lock_path(provider, prefix_key)
        _ensure_durable_directory(lock_path.parent)
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)


def _record_from_json(content: str) -> ProviderPrefixRecord:
    data = json.loads(content, object_pairs_hook=_unique_object)
    if not isinstance(data, dict):
        raise ValueError("provider prefix record must be a JSON object")
    return _record_from_dict(data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"provider prefix record contains duplicate field: {key}")
        result[key] = value
    return result


def _record_from_dict(data: dict[str, Any]) -> ProviderPrefixRecord:
    if set(data) != _RECORD_FIELDS:
        missing = sorted(_RECORD_FIELDS - set(data))
        unknown = sorted(set(data) - _RECORD_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValueError(f"invalid provider prefix record fields: {'; '.join(details)}")
    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != PROVIDER_PREFIX_SCHEMA_VERSION:
        raise ValueError(f"unsupported provider prefix schema version: {schema_version}")
    record = ProviderPrefixRecord(
        provider=_provider_name(data["provider"]),
        prefix_key=_prefix_key(data["prefix_key"]),
        provider_session_id=_required_string(data["provider_session_id"], "provider_session_id"),
        created_at=_timestamp(data["created_at"], "created_at"),
        updated_at=_timestamp(data["updated_at"], "updated_at"),
        schema_version=schema_version,
    )
    _validate_record(record)
    return record


def _validate_record(record: ProviderPrefixRecord) -> None:
    _provider_name(record.provider)
    _prefix_key(record.prefix_key)
    _required_string(record.provider_session_id, "provider_session_id")
    _timestamp(record.created_at, "created_at")
    _timestamp(record.updated_at, "updated_at")
    if type(record.schema_version) is not int or record.schema_version != PROVIDER_PREFIX_SCHEMA_VERSION:
        raise ValueError(f"unsupported provider prefix schema version: {record.schema_version}")


def _provider_name(value: object) -> str:
    provider = _required_string(value, "provider")
    if _PROVIDER_PATTERN.fullmatch(provider) is None:
        raise ValueError("provider must be a lowercase, filename-safe identifier")
    return provider


def _prefix_key(value: object) -> str:
    prefix_key = _required_string(value, "prefix_key")
    if _PREFIX_KEY_PATTERN.fullmatch(prefix_key) is None:
        raise ValueError("prefix_key must be exactly 64 lowercase SHA-256 hexadecimal characters")
    return prefix_key


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider prefix {field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _required_string(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"provider prefix {field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"provider prefix {field} must include a timezone")
    return text
