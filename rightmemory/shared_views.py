from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .session import _fsync_directory


REGISTRY_FILE = "shared_views.toml"
RUNTIME_DIR = ".runtime/shared_views"
CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}
TARGET_KINDS = {"none", "local_markdown", "revoked"}


@dataclass(frozen=True)
class SharedViewTarget:
    kind: str = "none"
    path: str | None = None


@dataclass(frozen=True)
class SharedViewConnection:
    heading_id: str
    ref: str
    relationship: str = "human"
    maintainer: str | None = None
    description: str | None = None
    accepted_from: str | None = None
    target: SharedViewTarget = SharedViewTarget()


def load_connections(memory_root: Path) -> dict[str, SharedViewConnection]:
    root = Path(memory_root).expanduser()
    registry = root / REGISTRY_FILE
    if not registry.exists():
        return {}
    with registry.open("rb") as handle:
        data = tomllib.load(handle)
    raw_connections = data.get("connections", {})
    if not isinstance(raw_connections, dict):
        raise ValueError(f"{REGISTRY_FILE} must contain a [connections] table")
    connections: dict[str, SharedViewConnection] = {}
    for raw_heading_id, raw_entry in raw_connections.items():
        heading_id = _validate_heading_id(str(raw_heading_id))
        if not isinstance(raw_entry, dict):
            raise ValueError(f"[connections.{heading_id}] must be a TOML table")
        ref = _required_string(raw_entry, "ref", heading_id)
        relationship = str(raw_entry.get("relationship", "human")).strip()
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"unknown shared view relationship `{relationship}` for {heading_id}")
        target = _load_target(root, heading_id, raw_entry.get("target", {}))
        connections[heading_id] = SharedViewConnection(
            heading_id=heading_id,
            ref=ref,
            relationship=relationship,
            maintainer=_optional_string(raw_entry.get("maintainer")),
            description=_optional_string(raw_entry.get("description")),
            accepted_from=_optional_string(raw_entry.get("accepted_from")),
            target=target,
        )
    return connections


def save_connections(memory_root: Path, connections: dict[str, SharedViewConnection]) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory shared view registry", ""]
    for heading_id in sorted(connections):
        connection = connections[heading_id]
        _validate_heading_id(connection.heading_id)
        if heading_id != connection.heading_id:
            raise ValueError(f"connection key `{heading_id}` does not match heading id `{connection.heading_id}`")
        table_key = _toml_key(heading_id)
        lines.append(f"[connections.{table_key}]")
        lines.append(f"ref = {_toml_string(connection.ref)}")
        lines.append(f"relationship = {_toml_string(connection.relationship)}")
        if connection.maintainer:
            lines.append(f"maintainer = {_toml_string(connection.maintainer)}")
        if connection.description:
            lines.append(f"description = {_toml_string(connection.description)}")
        if connection.accepted_from:
            lines.append(f"accepted_from = {_toml_string(connection.accepted_from)}")
        if connection.target.kind != "none" or connection.target.path:
            lines.append("")
            lines.append(f"[connections.{table_key}.target]")
            lines.append(f"kind = {_toml_string(connection.target.kind)}")
            if connection.target.path:
                lines.append(f"path = {_toml_string(connection.target.path)}")
        lines.append("")
    _atomic_write_text(root / REGISTRY_FILE, "\n".join(lines).rstrip() + "\n")


def _load_target(root: Path, heading_id: str, raw_target: object) -> SharedViewTarget:
    if raw_target in ({}, None):
        return SharedViewTarget()
    if not isinstance(raw_target, dict):
        raise ValueError(f"[connections.{heading_id}.target] must be a TOML table")
    kind = str(raw_target.get("kind", "none")).strip()
    if kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{kind}` for {heading_id}")
    path = _optional_string(raw_target.get("path"))
    if kind == "local_markdown" and not path:
        raise ValueError(f"local_markdown shared view target requires path for {heading_id}")
    if path:
        _resolve_under_root(root, path)
    return SharedViewTarget(kind=kind, path=path)


def _validate_heading_id(value: str) -> str:
    heading_id = value.strip()
    if not heading_id or CONNECTION_ID_RE.fullmatch(heading_id) is None:
        raise ValueError(f"shared view heading id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return heading_id


def _required_string(entry: dict[str, object], key: str, heading_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[connections.{heading_id}].{key} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional shared view string fields must be strings")
    stripped = value.strip()
    return stripped or None


def _resolve_under_root(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("shared view target path must stay under the memory root")
    resolved = (root / path).resolve()
    if root.resolve() not in (resolved, *resolved.parents):
        raise ValueError("shared view target path must stay under the memory root")
    return resolved


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_key(value: str) -> str:
    return _toml_string(value)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
