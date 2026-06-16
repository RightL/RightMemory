from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


REGISTRY_FILE = "shared_views.toml"
RUNTIME_DIR = ".runtime/shared_views"
PROVIDER_VIEWS_DIR = "shared_views"
CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VIEW_TYPES = {"file", "question"}
TARGET_KINDS = {"none", "http-file", "http-question", "revoked"}
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}


@dataclass(frozen=True)
class SharedViewTarget:
    kind: str = "none"
    path: str | None = None
    view_id: str | None = None
    base_url: str | None = None
    credential_id: str | None = None
    question_base_url: str | None = None
    question_credential_id: str | None = None
    version_id: str | None = None
    accepted_from_url: str | None = None


@dataclass(frozen=True)
class SharedViewConnection:
    heading_id: str
    view_type: str
    ref: str
    relationship: str = "human"
    maintainer: str | None = None
    description: str | None = None
    accepted_from: str | None = None
    target: SharedViewTarget = field(default_factory=SharedViewTarget)


def validate_heading_id(value: str) -> str:
    clean = str(value).strip()
    if not CONNECTION_ID_RE.fullmatch(clean):
        raise ValueError(f"shared view id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return clean


def validate_connection(root: Path, key: str, connection: SharedViewConnection) -> SharedViewConnection:
    heading_id = validate_heading_id(connection.heading_id)
    if key != heading_id:
        raise ValueError(f"connection key `{key}` does not match heading id `{heading_id}`")
    if connection.view_type not in VIEW_TYPES:
        raise ValueError(f"unknown shared view type `{connection.view_type}` for {heading_id}")
    if connection.relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{connection.relationship}` for {heading_id}")
    target = connection.target
    if target.kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{target.kind}` for {heading_id}")
    if target.path:
        raise ValueError("shared view target paths are no longer supported; use HTTP transport")
    if target.kind == "http-file" and connection.view_type != "file":
        raise ValueError(f"http-file target requires file view type for {heading_id}")
    if target.kind == "http-question" and connection.view_type != "question":
        raise ValueError(f"http-question target requires question view type for {heading_id}")
    if target.kind in {"http-file", "http-question"}:
        if not target.base_url or not target.credential_id:
            raise ValueError(f"{target.kind} target requires base_url and credential_id for {heading_id}")
        _validate_http_base_url(target.base_url)
    if target.kind == "http-question":
        if not target.question_base_url or not target.question_credential_id:
            raise ValueError(f"http-question target requires question_base_url and question_credential_id for {heading_id}")
        _validate_http_base_url(target.question_base_url)
    return connection


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
        heading_id = validate_heading_id(str(raw_heading_id))
        if not isinstance(raw_entry, dict):
            raise ValueError(f"[connections.{heading_id}] must be a TOML table")
        view_type = _required_string(raw_entry, "type", heading_id)
        connection = SharedViewConnection(
            heading_id=heading_id,
            view_type=view_type,
            ref=_required_string(raw_entry, "ref", heading_id),
            relationship=str(raw_entry.get("relationship", "human")).strip(),
            maintainer=_optional_string(raw_entry.get("maintainer")),
            description=_optional_string(raw_entry.get("description")),
            accepted_from=_optional_string(raw_entry.get("accepted_from")),
            target=_load_target(raw_entry.get("target", {})),
        )
        connections[heading_id] = validate_connection(root, heading_id, connection)
    return connections


def save_connections(memory_root: Path, connections: dict[str, SharedViewConnection]) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory shared view registry", ""]
    for heading_id in sorted(connections):
        connection = validate_connection(root, heading_id, connections[heading_id])
        table_key = _toml_key(heading_id)
        lines.append(f"[connections.{table_key}]")
        lines.append(f"type = {_toml_string(connection.view_type)}")
        lines.append(f"ref = {_toml_string(connection.ref)}")
        lines.append(f"relationship = {_toml_string(connection.relationship)}")
        if connection.maintainer:
            lines.append(f"maintainer = {_toml_string(connection.maintainer)}")
        if connection.description:
            lines.append(f"description = {_toml_string(connection.description)}")
        if connection.accepted_from:
            lines.append(f"accepted_from = {_toml_string(connection.accepted_from)}")
        if connection.target.kind != "none":
            lines.append("")
            lines.append(f"[connections.{table_key}.target]")
            lines.append(f"kind = {_toml_string(connection.target.kind)}")
            if connection.target.view_id:
                lines.append(f"view_id = {_toml_string(connection.target.view_id)}")
            if connection.target.base_url:
                lines.append(f"base_url = {_toml_string(connection.target.base_url)}")
            if connection.target.credential_id:
                lines.append(f"credential_id = {_toml_string(connection.target.credential_id)}")
            if connection.target.question_base_url:
                lines.append(f"question_base_url = {_toml_string(connection.target.question_base_url)}")
            if connection.target.question_credential_id:
                lines.append(f"question_credential_id = {_toml_string(connection.target.question_credential_id)}")
            if connection.target.version_id:
                lines.append(f"version_id = {_toml_string(connection.target.version_id)}")
            if connection.target.accepted_from_url:
                lines.append(f"accepted_from_url = {_toml_string(connection.target.accepted_from_url)}")
        lines.append("")
    _write_text(root / REGISTRY_FILE, "\n".join(lines).rstrip() + "\n")


def save_shared_view_credential(
    memory_root: Path,
    credential_id: str,
    *,
    kind: str,
    token: str,
    base_url: str | None = None,
    view_id: str | None = None,
    provider_id: str | None = None,
) -> None:
    root = Path(memory_root).expanduser()
    clean_id = validate_heading_id(credential_id)
    if not kind.strip():
        raise ValueError("shared view credential kind must be a non-empty string")
    if not token:
        raise ValueError("shared view credential token must be a non-empty string")
    clean_base_url = _optional_string(base_url)
    if clean_base_url:
        clean_base_url = clean_base_url.rstrip("/")
        _validate_http_base_url(clean_base_url)
    data = _load_credentials(root)
    credentials = data.setdefault("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("shared view credential store is invalid")
    entry: dict[str, str] = {
        "kind": kind.strip(),
        "token": token,
        "created_at": _now_iso(),
    }
    if clean_base_url:
        entry["base_url"] = clean_base_url
    if view_id:
        entry["view_id"] = validate_heading_id(view_id)
    if provider_id:
        entry["provider_id"] = validate_heading_id(provider_id)
    credentials[clean_id] = entry
    _write_credentials(root, data)


def load_shared_view_credential(memory_root: Path, credential_id: str) -> dict[str, str]:
    root = Path(memory_root).expanduser()
    clean_id = validate_heading_id(credential_id)
    credentials = _load_credentials(root).get("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("shared view credential store is invalid")
    raw = credentials.get(clean_id)
    if not isinstance(raw, dict):
        raise KeyError(f"shared view credential not found: {clean_id}")
    kind = raw.get("kind")
    token = raw.get("token")
    if not isinstance(kind, str) or not isinstance(token, str):
        raise ValueError(f"shared view credential is invalid: {clean_id}")
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def _load_target(raw_target: object) -> SharedViewTarget:
    if raw_target in (None, {}):
        return SharedViewTarget()
    if not isinstance(raw_target, dict):
        raise ValueError("shared view target must be a TOML table")
    return SharedViewTarget(
        kind=str(raw_target.get("kind", "none")).strip() or "none",
        path=_optional_string(raw_target.get("path")),
        view_id=_optional_heading_id(raw_target.get("view_id")),
        base_url=_optional_string(raw_target.get("base_url")),
        credential_id=_optional_heading_id(raw_target.get("credential_id")),
        question_base_url=_optional_string(raw_target.get("question_base_url")),
        question_credential_id=_optional_heading_id(raw_target.get("question_credential_id")),
        version_id=_optional_heading_id(raw_target.get("version_id")),
        accepted_from_url=_optional_string(raw_target.get("accepted_from_url")),
    )


def _load_credentials(root: Path) -> dict[str, object]:
    path = _credentials_path(root)
    if not path.exists():
        return {"credentials": {}}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError("shared view credential store is invalid")
    return data


def _write_credentials(root: Path, data: dict[str, object]) -> None:
    credentials = data.get("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("shared view credential store is invalid")
    lines = ["# RightMemory shared view credentials", ""]
    for credential_id in sorted(credentials):
        raw = credentials[credential_id]
        if not isinstance(raw, dict):
            continue
        table_key = _toml_key(str(credential_id))
        lines.append(f"[credentials.{table_key}]")
        for key in ("kind", "token", "base_url", "view_id", "provider_id", "created_at"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{key} = {_toml_string(value)}")
        lines.append("")
    path = _credentials_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, "\n".join(lines).rstrip() + "\n")


def _credentials_path(root: Path) -> Path:
    return root / RUNTIME_DIR / "credentials.toml"


def _optional_heading_id(value: object) -> str | None:
    text = _optional_string(value)
    return validate_heading_id(text) if text else None


def _required_string(data: dict[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires string field `{key}`")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _validate_http_base_url(value: str) -> None:
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("shared view HTTP base_url must start with http:// or https://")


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml_string(value)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
