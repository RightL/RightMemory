from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .session import _fsync_directory


REGISTRY_FILE = "shared_views.toml"
RUNTIME_DIR = ".runtime/shared_views"
CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|S#|M#|#)([A-Za-z0-9_.-]+)\}")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*$")
TITLE_MARKER_RE = re.compile(r"\{[^{}]*\}")
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}
TARGET_KINDS = {"none", "local_markdown", "revoked"}
QUERY_TERM_RE = re.compile(r"[A-Za-z0-9_]{3,}")
COMMON_QUERY_WORDS = {"the", "and", "for"}


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


class SharedViewTools:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser()

    def retrieve_shared_view(self, heading_id: str, query: str) -> str:
        """Retrieve context from a shared view by local M# heading id."""
        return retrieve_shared_view(self.memory_root, heading_id, query)


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
    _validate_connections_for_save(root, connections)

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


def accept_shared_view(
    memory_root: Path,
    *,
    heading_id: str,
    title: str,
    body: str,
    ref: str,
    relationship: str = "human",
    maintainer: str | None = None,
    description: str | None = None,
    accepted_from: str | None = None,
    target_path: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{relationship}`")
    target = SharedViewTarget("local_markdown", target_path) if target_path else SharedViewTarget()
    if target.path:
        _resolve_under_root(root, target.path)
    connection = SharedViewConnection(
        heading_id=heading_id,
        ref=ref.strip(),
        relationship=relationship,
        maintainer=maintainer.strip() if maintainer else None,
        description=description.strip() if description else None,
        accepted_from=accepted_from.strip() if accepted_from else None,
        target=target,
    )
    connections = load_connections(root)
    connections[heading_id] = connection
    _validate_connections_for_save(root, connections)
    _ensure_memory_heading(root, heading_id=heading_id, title=title, body=body)
    save_connections(root, connections)
    return f"accepted shared view {heading_id}"


def retrieve_shared_view(memory_root: Path, heading_id: str, query: str) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    stripped_query = query.strip()
    if not stripped_query:
        raise ValueError("shared view query must not be empty")

    connections = load_connections(root)
    connection = connections.get(heading_id)
    if connection is None:
        return _format_unavailable_shared_view(heading_id, "no shared view connection is registered")
    if connection.target.kind == "revoked":
        return _format_unavailable_shared_view(connection.heading_id, "access revoked")

    if connection.target.kind == "local_markdown" and connection.target.path:
        target = _resolve_under_root(root, connection.target.path)
        if target.exists():
            matches = _retrieve_local_markdown_matches(target, stripped_query)
            result = _format_shared_view_result(connection, matches)
            _write_shared_view_cache(root, heading_id, result)
            return result

    cached = _read_shared_view_cache(root, heading_id)
    if cached is not None:
        return cached.replace("Status: fresh", "Status: cached", 1)

    return _format_unavailable_shared_view(connection.heading_id, "no shared view content is available")


def _ensure_memory_heading(root: Path, *, heading_id: str, title: str, body: str) -> None:
    memory = root / "MEMORY.md"
    if _has_existing_shared_view_heading(root, heading_id):
        return
    if not memory.exists():
        memory.write_text("# Shared Views\n", encoding="utf-8")
    text = memory.read_text(encoding="utf-8")
    title_text = _normalize_heading_title(title, heading_id)
    body_text = body.strip()
    section = "# Shared Views"
    addition = f"\n\n### {title_text} {{M#{heading_id}}}\n"
    if body_text:
        addition += f"\n{body_text}\n"
    if section not in text:
        addition = f"\n\n{section}{addition}"
    memory.write_text(text.rstrip() + addition, encoding="utf-8")


def _validate_connections_for_save(root: Path, connections: dict[str, SharedViewConnection]) -> None:
    for heading_id in sorted(connections):
        _validate_connection_for_save(root, heading_id, connections[heading_id])


def _has_existing_shared_view_heading(root: Path, heading_id: str) -> bool:
    found_shared_view = False
    for memory_file in _active_memory_files(root):
        relative_path = memory_file.relative_to(root)
        for line_number, line in enumerate(memory_file.read_text(encoding="utf-8").splitlines(), start=1):
            anchor = ANCHOR_KIND_RE.match(line)
            if anchor is not None and anchor.group(3) == heading_id:
                kind = anchor.group(2)
                if kind == "M#":
                    found_shared_view = True
                    continue
                raise ValueError(
                    f"shared view graph id `{heading_id}` already exists as "
                    f"`{{{kind}{heading_id}}}` in {relative_path}:{line_number}"
                )
            node = NODE_RE.match(line)
            if node is not None and node.group(1) == heading_id:
                raise ValueError(
                    f"shared view graph id `{heading_id}` already exists as "
                    f"bullet node `{heading_id}` in {relative_path}:{line_number}"
                )
    return found_shared_view


def _active_memory_files(root: Path) -> list[Path]:
    files = []
    memory = root / "MEMORY.md"
    if memory.is_file():
        files.append(memory)
    files.extend(
        path
        for path in sorted(root.glob("MEMORY_*.md"))
        if path.is_file() and not path.name.startswith("MEMORY_SKILL_")
    )
    return files


def _retrieve_local_markdown_matches(target: Path, query: str) -> list[str]:
    terms = _query_terms(query)
    matches: list[str] = []
    if terms:
        for memory_file in sorted(target.glob("MEMORY*.md")):
            if not memory_file.is_file():
                continue
            relative = memory_file.relative_to(target).as_posix()
            for line_number, line in enumerate(memory_file.read_text(encoding="utf-8").splitlines(), start=1):
                lowered = line.lower()
                if any(term in lowered for term in terms):
                    matches.append(f"- {relative}:{line_number}: {line}")
                    if len(matches) >= 12:
                        return matches
    return matches or ["- no strong match in published shared memory"]


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in QUERY_TERM_RE.findall(query) if term.lower() not in COMMON_QUERY_WORDS]


def _format_shared_view_result(connection: SharedViewConnection, matches: list[str]) -> str:
    lines = [
        f"Shared view: {connection.heading_id}",
        "Status: fresh",
        f"Ref: {connection.ref}",
    ]
    if connection.maintainer:
        lines.append(f"Maintainer: {connection.maintainer}")
    if connection.description:
        lines.append(f"Description: {connection.description}")
    lines.extend(
        [
            f"Freshness: {datetime.now(UTC).replace(microsecond=0).isoformat()}",
            "Matches:",
            *matches,
        ]
    )
    return "\n".join(lines) + "\n"


def _format_unavailable_shared_view(heading_id: str, reason: str) -> str:
    return "\n".join(
        [
            f"Shared view: {heading_id}",
            "Status: unavailable",
            f"Reason: {reason}",
        ]
    ) + "\n"


def _shared_view_cache_path(root: Path, heading_id: str) -> Path:
    return root / RUNTIME_DIR / "cache" / f"{heading_id}.txt"


def _read_shared_view_cache(root: Path, heading_id: str) -> str | None:
    cache_path = _shared_view_cache_path(root, heading_id)
    if not cache_path.is_file():
        return None
    return cache_path.read_text(encoding="utf-8")


def _write_shared_view_cache(root: Path, heading_id: str, text: str) -> None:
    _ensure_runtime_gitignore(root)
    _atomic_write_text(_shared_view_cache_path(root, heading_id), text)


def _ensure_runtime_gitignore(root: Path) -> None:
    gitignore = root / ".runtime" / ".gitignore"
    desired = ["*", "!.gitignore"]
    if not gitignore.exists():
        _atomic_write_text(gitignore, "\n".join(desired) + "\n")
        return
    text = gitignore.read_text(encoding="utf-8")
    existing = set(text.splitlines())
    additions = [line for line in desired if line not in existing]
    if additions:
        _atomic_write_text(gitignore, text.rstrip() + "\n" + "\n".join(additions) + "\n")


def _normalize_heading_title(title: str, heading_id: str) -> str:
    title_text = TITLE_MARKER_RE.sub(" ", title)
    title_text = " ".join(title_text.split())
    return title_text or heading_id


def _load_target(root: Path, heading_id: str, raw_target: object) -> SharedViewTarget:
    if raw_target in ({}, None):
        return SharedViewTarget()
    if not isinstance(raw_target, dict):
        raise ValueError(f"[connections.{heading_id}.target] must be a TOML table")
    return _validate_target(root, heading_id, raw_target.get("kind", "none"), raw_target.get("path"))


def _validate_connection_for_save(root: Path, heading_id: str, connection: SharedViewConnection) -> None:
    validated_heading_id = _validate_heading_id(connection.heading_id)
    if heading_id != validated_heading_id:
        raise ValueError(f"connection key `{heading_id}` does not match heading id `{connection.heading_id}`")
    _required_string({"ref": connection.ref}, "ref", validated_heading_id)
    relationship = str(connection.relationship).strip()
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{relationship}` for {validated_heading_id}")
    _optional_string(connection.maintainer)
    _optional_string(connection.description)
    _optional_string(connection.accepted_from)
    _validate_target(root, validated_heading_id, connection.target.kind, connection.target.path)


def _validate_target(root: Path, heading_id: str, raw_kind: object, raw_path: object) -> SharedViewTarget:
    kind = str(raw_kind).strip()
    if kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{kind}` for {heading_id}")
    path = _optional_string(raw_path)
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
