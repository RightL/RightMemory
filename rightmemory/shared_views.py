from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory


REGISTRY_FILE = "shared_views.toml"
RUNTIME_DIR = ".runtime/shared_views"
PROVIDER_VIEWS_DIR = "shared_views"
INVITATION_FILE = "rightmemory-shared-view.toml"
VIEW_METADATA_FILE = "export.toml"
VIEW_MANIFEST_FILE = "manifest.toml"
CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|S#|M#|#)([A-Za-z0-9_.-]+)\}")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*$")
TITLE_MARKER_RE = re.compile(r"\{[^{}]*\}")
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}
TARGET_KINDS = {"none", "local_markdown", "package", "local", "hub", "revoked"}
QUERY_TERM_RE = re.compile(r"[A-Za-z0-9_]{3,}")
COMMON_QUERY_WORDS = {"the", "and", "for"}
CACHE_VERSION = 1
DEFAULT_SOURCE_GLOBS = ("MEMORY.md", "MEMORY_*.md")


@dataclass(frozen=True)
class SharedViewTarget:
    kind: str = "none"
    path: str | None = None
    view_id: str | None = None


@dataclass(frozen=True)
class SharedViewConnection:
    heading_id: str
    ref: str
    relationship: str = "human"
    maintainer: str | None = None
    description: str | None = None
    accepted_from: str | None = None
    target: SharedViewTarget = SharedViewTarget()


@dataclass(frozen=True)
class SharedViewDefinition:
    view_id: str
    title: str
    ref: str
    description: str | None = None
    audience: str | None = None
    maintainer: str | None = None
    source_globs: tuple[str, ...] = DEFAULT_SOURCE_GLOBS
    filter_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SharedViewSourceLine:
    relative: str
    line_number: int
    line: str


@dataclass(frozen=True)
class _SharedViewCache:
    freshness: str
    source_lines: list[_SharedViewSourceLine]
    provenance: str | None = None
    backing: str | None = None


class SharedViewTools:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser()

    def retrieve_shared_view(self, heading_id: str, query: str) -> str:
        """Retrieve context from a shared view by local M# heading id."""
        return retrieve_shared_view(self.memory_root, heading_id, query)


def define_shared_view(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    description: str | None = None,
    audience: str | None = None,
    maintainer: str | None = None,
    retriever_instructions: str | None = None,
    source_globs: list[str] | tuple[str, ...] | None = None,
    filter_terms: list[str] | tuple[str, ...] | None = None,
    ref: str | None = None,
) -> str:
    """Create or update provider-owned shared-view source files."""
    root = Path(memory_root).expanduser()
    definition = SharedViewDefinition(
        view_id=_validate_heading_id(view_id),
        title=_required_plain_string(title, "shared view title"),
        ref=(ref or f"rightmemory://view/{_validate_heading_id(view_id)}").strip(),
        description=_optional_string(description),
        audience=_optional_string(audience),
        maintainer=_optional_string(maintainer),
        source_globs=_normalize_source_globs(source_globs),
        filter_terms=tuple(_query_terms(" ".join(filter_terms or ()))) if filter_terms else (),
    )
    _required_plain_string(definition.ref, "shared view ref")

    view_dir = _provider_view_dir(root, definition.view_id)
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_provider_view_gitignore(view_dir)
    _atomic_write_text(view_dir / "view.md", _render_view_markdown(definition))
    if retriever_instructions is not None:
        instructions = retriever_instructions.strip()
        if instructions:
            _atomic_write_text(view_dir / "retriever.md", instructions.rstrip() + "\n")
    _write_definition_metadata(view_dir / VIEW_METADATA_FILE, definition)
    return f"defined shared view {definition.view_id} in {PROVIDER_VIEWS_DIR}/{definition.view_id}"


def build_shared_view(
    memory_root: Path,
    view_id: str,
    *,
    query: str | None = None,
    context_lines: int = 0,
    limit: int = 200,
) -> str:
    """Materialize a filtered Markdown preview/export surface for a provider view."""
    root = Path(memory_root).expanduser()
    definition = load_shared_view_definition(root, view_id)
    if context_lines < 0:
        raise ValueError("context_lines must be >= 0")
    if isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    terms = list(definition.filter_terms)
    if query:
        terms.extend(_query_terms(query))
    source_lines = _filtered_provider_source_lines(root, definition, terms, context_lines, limit)
    rendered = _render_filtered_memory(definition, source_lines)
    dist = _provider_view_dir(root, definition.view_id) / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(dist / "MEMORY.md", rendered)
    _atomic_write_text(
        dist / VIEW_MANIFEST_FILE,
        _render_view_manifest(definition, rendered, len(source_lines)),
    )
    return f"built shared view {definition.view_id}: {len(source_lines)} exported lines"


def export_shared_view(
    memory_root: Path,
    view_id: str,
    target_path: Path,
    *,
    replace: bool = False,
) -> str:
    """Export a provider shared view as a shareable package with an invitation."""
    root = Path(memory_root).expanduser()
    definition = load_shared_view_definition(root, view_id)
    view_dir = _provider_view_dir(root, definition.view_id)
    if not (view_dir / "dist" / "MEMORY.md").exists():
        build_shared_view(root, definition.view_id)

    target = Path(target_path).expanduser()
    _prepare_output_directory(target, replace=replace)
    _copy_if_exists(view_dir / "view.md", target / "view.md")
    _copy_if_exists(view_dir / "retriever.md", target / "retriever.md")
    _copy_if_exists(view_dir / VIEW_METADATA_FILE, target / VIEW_METADATA_FILE)
    if (view_dir / "dist").is_dir():
        shutil.copytree(view_dir / "dist", target / "dist", dirs_exist_ok=True)
    _atomic_write_text(
        target / INVITATION_FILE,
        _render_invitation(definition, transport_kind="package", transport_path="."),
    )
    return f"exported shared view {definition.view_id} to {target}"


def publish_shared_view(
    memory_root: Path,
    view_id: str,
    hub_path: Path,
    *,
    replace: bool = False,
) -> str:
    """Publish a shared view package into a small local hub directory."""
    root = Path(memory_root).expanduser()
    definition = load_shared_view_definition(root, view_id)
    hub = Path(hub_path).expanduser()
    package_path = hub / "views" / definition.view_id
    export_shared_view(root, definition.view_id, package_path, replace=replace)
    registry = _load_hub_registry(hub)
    registry[definition.view_id] = {
        "ref": definition.ref,
        "maintainer": definition.maintainer or "",
        "description": definition.description or "",
        "package_path": f"views/{definition.view_id}",
        "updated_at": _now_iso(),
    }
    _save_hub_registry(hub, registry)
    invitation_dir = hub / "invitations"
    invitation_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        invitation_dir / f"{definition.view_id}.toml",
        _render_invitation(
            definition,
            transport_kind="hub",
            transport_path=str(hub),
        ),
    )
    return f"published shared view {definition.view_id} to hub {hub}"


def accept_shared_view_invitation(
    memory_root: Path,
    invitation_path: Path,
    *,
    heading_id: str | None = None,
    title: str | None = None,
    body: str | None = None,
    relationship: str | None = None,
    copy_package: bool = True,
) -> str:
    """Accept a shared-view invitation and create a local M# relationship."""
    root = Path(memory_root).expanduser()
    invitation_file = _resolve_invitation_file(Path(invitation_path).expanduser())
    invitation = _load_invitation(invitation_file)
    view_id = _required_string(invitation, "view_id", "invitation")
    local_heading_id = _validate_heading_id(heading_id or view_id)
    ref = _required_string(invitation, "ref", "invitation")
    local_title = title or _required_string(invitation, "title", "invitation")
    local_body = body if body is not None else _default_invitation_body(invitation)
    local_relationship = relationship or str(invitation.get("relationship", "human"))
    transport = invitation.get("transport", {})
    if not isinstance(transport, dict):
        raise ValueError("[transport] in shared-view invitation must be a TOML table")
    target = _target_from_invitation(root, local_heading_id, invitation_file, transport, view_id, copy_package)
    return accept_shared_view(
        root,
        heading_id=local_heading_id,
        title=local_title,
        body=local_body,
        ref=ref,
        relationship=local_relationship,
        maintainer=_optional_string(invitation.get("maintainer")),
        description=_optional_string(invitation.get("description")),
        accepted_from=str(invitation_file),
        target=target,
    )


def load_shared_view_definition(memory_root: Path, view_id: str) -> SharedViewDefinition:
    root = Path(memory_root).expanduser()
    clean_view_id = _validate_heading_id(view_id)
    metadata = _provider_view_dir(root, clean_view_id) / VIEW_METADATA_FILE
    if not metadata.exists():
        raise FileNotFoundError(f"shared view definition not found: {PROVIDER_VIEWS_DIR}/{clean_view_id}")
    return _load_definition_metadata(metadata)


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
            if connection.target.view_id:
                lines.append(f"view_id = {_toml_string(connection.target.view_id)}")
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
    target: SharedViewTarget | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{relationship}`")
    if target is not None and target_path is not None:
        raise ValueError("provide either target or target_path, not both")
    resolved_target = target or (SharedViewTarget("local_markdown", target_path) if target_path else SharedViewTarget())
    resolved_target = _validate_target(root, heading_id, resolved_target.kind, resolved_target.path, resolved_target.view_id)
    connection = SharedViewConnection(
        heading_id=heading_id,
        ref=ref.strip(),
        relationship=relationship,
        maintainer=maintainer.strip() if maintainer else None,
        description=description.strip() if description else None,
        accepted_from=accepted_from.strip() if accepted_from else None,
        target=resolved_target,
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

    fresh_cache = _retrieve_fresh_shared_view(root, connection, stripped_query)
    if fresh_cache is not None:
        matches = _match_local_markdown_lines(fresh_cache.source_lines, stripped_query)
        result = _format_shared_view_result(
            connection,
            "fresh",
            fresh_cache.freshness,
            matches,
            provenance=fresh_cache.provenance,
            backing=fresh_cache.backing,
        )
        try:
            _write_shared_view_cache(root, heading_id, fresh_cache)
        except OSError:
            pass
        return result

    cached = _read_shared_view_cache(root, heading_id)
    if cached is not None:
        matches = _match_local_markdown_lines(cached.source_lines, stripped_query)
        return _format_shared_view_result(
            connection,
            "cached",
            cached.freshness,
            matches,
            provenance=cached.provenance,
            backing=cached.backing,
        )

    return _format_unavailable_shared_view(connection.heading_id, "no shared view content is available")


def record_shared_view_note(
    memory_root: Path,
    heading_id: str,
    message: str,
    *,
    confirmed: bool = False,
    actor: str = "user",
    task_context: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    message = message.strip()
    if not message:
        raise ValueError("shared view note message must not be empty")
    connections = load_connections(root)
    connection = connections.get(heading_id)
    if connection is None:
        return f"shared view {heading_id} is not registered"
    if connection.relationship in {"human", "external"} and not confirmed:
        maintainer = f" for {connection.maintainer}" if connection.maintainer else ""
        return f"confirmation required before sending note{maintainer}: {message}"
    record: dict[str, object] = {
        "created_at": _now_iso(),
        "heading_id": heading_id,
        "ref": connection.ref,
        "relationship": connection.relationship,
        "maintainer": connection.maintainer,
        "actor": actor,
        "message": message,
    }
    task_context_text = _optional_string(task_context)
    if task_context_text:
        record["task_context"] = task_context_text
    status = _deliver_interaction_record(root, connection, record)
    record["status"] = status
    _append_interaction_record(root, heading_id, record)
    if status == "queued":
        return f"queued shared view note for {heading_id}"
    return f"recorded shared view note for {heading_id}"


def list_shared_view_inbox(memory_root: Path, view_id: str | None = None) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    inbox_dir = root / RUNTIME_DIR / "inbox"
    if view_id is not None:
        paths = [inbox_dir / f"{_validate_heading_id(view_id)}.jsonl"]
    else:
        paths = sorted(inbox_dir.glob("*.jsonl"))
    records: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def list_shared_view_notes(memory_root: Path, heading_id: str | None = None) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    interactions_dir = root / RUNTIME_DIR / "interactions"
    if heading_id is not None:
        paths = [interactions_dir / f"{_validate_heading_id(heading_id)}.jsonl"]
    else:
        paths = sorted(interactions_dir.glob("*.jsonl"))
    records: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _provider_view_dir(root: Path, view_id: str) -> Path:
    return root / PROVIDER_VIEWS_DIR / _validate_heading_id(view_id)


def _write_provider_view_gitignore(view_dir: Path) -> None:
    _atomic_write_text(view_dir / ".gitignore", "dist/\n")


def _render_view_markdown(definition: SharedViewDefinition) -> str:
    lines = [
        f"# {definition.title}",
        "",
        f"View id: `{definition.view_id}`",
        f"Reference: `{definition.ref}`",
    ]
    if definition.maintainer:
        lines.append(f"Maintainer: {definition.maintainer}")
    if definition.audience:
        lines.extend(["", "## Audience", "", definition.audience])
    if definition.description:
        lines.extend(["", "## Collaboration Meaning", "", definition.description])
    lines.extend(
        [
            "",
            "## Builder Scope",
            "",
            "Source globs:",
            *[f"- `{source_glob}`" for source_glob in definition.source_globs],
        ]
    )
    if definition.filter_terms:
        lines.extend(["", "Filter terms:", *[f"- `{term}`" for term in definition.filter_terms]])
    return "\n".join(lines).rstrip() + "\n"


def _write_definition_metadata(path: Path, definition: SharedViewDefinition) -> None:
    lines = [
        "version = 1",
        f"view_id = {_toml_string(definition.view_id)}",
        f"ref = {_toml_string(definition.ref)}",
        f"title = {_toml_string(definition.title)}",
    ]
    if definition.description:
        lines.append(f"description = {_toml_string(definition.description)}")
    if definition.audience:
        lines.append(f"audience = {_toml_string(definition.audience)}")
    if definition.maintainer:
        lines.append(f"maintainer = {_toml_string(definition.maintainer)}")
    lines.append(f"source_globs = {_toml_array(definition.source_globs)}")
    if definition.filter_terms:
        lines.append(f"filter_terms = {_toml_array(definition.filter_terms)}")
    _atomic_write_text(path, "\n".join(lines) + "\n")


def _load_definition_metadata(path: Path) -> SharedViewDefinition:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    view_id = _validate_heading_id(_required_string(data, "view_id", "shared-view"))
    title = _required_string(data, "title", view_id)
    ref = _required_string(data, "ref", view_id)
    return SharedViewDefinition(
        view_id=view_id,
        title=title,
        ref=ref,
        description=_optional_string(data.get("description")),
        audience=_optional_string(data.get("audience")),
        maintainer=_optional_string(data.get("maintainer")),
        source_globs=_normalize_source_globs(data.get("source_globs")),
        filter_terms=_normalize_terms(data.get("filter_terms")),
    )


def _normalize_source_globs(raw_globs: object) -> tuple[str, ...]:
    if raw_globs is None:
        return DEFAULT_SOURCE_GLOBS
    if not isinstance(raw_globs, (list, tuple)):
        raise ValueError("shared view source_globs must be a list")
    globs: list[str] = []
    for raw_glob in raw_globs:
        if not isinstance(raw_glob, str) or not raw_glob.strip():
            raise ValueError("shared view source globs must be non-empty strings")
        source_glob = raw_glob.strip()
        raw_path = Path(source_glob)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise ValueError("shared view source globs must be relative and must not contain '..'")
        globs.append(source_glob)
    return tuple(globs) or DEFAULT_SOURCE_GLOBS


def _normalize_terms(raw_terms: object) -> tuple[str, ...]:
    if raw_terms is None:
        return ()
    if not isinstance(raw_terms, (list, tuple)):
        raise ValueError("shared view filter_terms must be a list")
    terms: list[str] = []
    for raw_term in raw_terms:
        if not isinstance(raw_term, str):
            raise ValueError("shared view filter terms must be strings")
        terms.extend(_query_terms(raw_term))
    return tuple(dict.fromkeys(terms))


def _provider_source_files(root: Path, definition: SharedViewDefinition) -> list[Path]:
    files: dict[str, Path] = {}
    for source_glob in definition.source_globs:
        explicit_skill_glob = source_glob.startswith("MEMORY_SKILL_")
        for path in root.glob(source_glob):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.startswith("MEMORY_SKILL_") and not explicit_skill_glob:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root.resolve() not in (resolved, *resolved.parents):
                continue
            if path.name.startswith("MEMORY") and path.suffix == ".md":
                files[path.relative_to(root).as_posix()] = path
    return [files[key] for key in sorted(files)]


def _filtered_provider_source_lines(
    root: Path,
    definition: SharedViewDefinition,
    terms: list[str],
    context_lines: int,
    limit: int,
    required_terms: tuple[str, ...] = (),
) -> list[_SharedViewSourceLine]:
    unique_terms = list(dict.fromkeys(term.lower() for term in terms))
    required = tuple(dict.fromkeys(term.lower() for term in required_terms))
    selected: list[_SharedViewSourceLine] = []
    for source_file in _provider_source_files(root, definition):
        relative = source_file.relative_to(root).as_posix()
        lines = source_file.read_text(encoding="utf-8").splitlines()
        indexes = _matching_line_indexes(lines, unique_terms, context_lines, required)
        for index in indexes:
            selected.append(_SharedViewSourceLine(relative, index + 1, lines[index]))
            if len(selected) >= limit:
                return selected
    return selected


def _matching_line_indexes(
    lines: list[str],
    terms: list[str],
    context_lines: int,
    required_terms: tuple[str, ...] = (),
) -> list[int]:
    if not terms:
        return list(range(len(lines)))
    indexes: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(term in lowered for term in terms) and (
            not required_terms or any(term in lowered for term in required_terms)
        ):
            for context_index in range(max(0, index - context_lines), min(len(lines), index + context_lines + 1)):
                indexes.add(context_index)
    return sorted(indexes)


def _render_filtered_memory(definition: SharedViewDefinition, source_lines: list[_SharedViewSourceLine]) -> str:
    lines = [
        f"# {definition.title} Shared View",
        "",
        f"Shared view reference: `{definition.ref}`",
    ]
    if definition.description:
        lines.extend(["", definition.description])
    lines.extend(["", "## Published Context", ""])
    if source_lines:
        current_file = None
        for source_line in source_lines:
            if source_line.relative != current_file:
                current_file = source_line.relative
                lines.extend(["", f"### {current_file}", ""])
            lines.append(f"- `{source_line.relative}:{source_line.line_number}` {source_line.line}")
    else:
        lines.append("No source lines matched the shared view filter.")
    return "\n".join(lines).rstrip() + "\n"


def _render_view_manifest(definition: SharedViewDefinition, rendered_memory: str, exported_line_count: int) -> str:
    checksum = sha256(rendered_memory.encode("utf-8")).hexdigest()
    lines = [
        "version = 1",
        f"view_id = {_toml_string(definition.view_id)}",
        f"ref = {_toml_string(definition.ref)}",
        f"exported_at = {_toml_string(_now_iso())}",
        f"exported_line_count = {exported_line_count}",
        f"memory_sha256 = {_toml_string(checksum)}",
    ]
    return "\n".join(lines) + "\n"


def _prepare_output_directory(path: Path, *, replace: bool) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"shared view output target is not a directory: {path}")
        if any(path.iterdir()):
            if not replace:
                raise ValueError(f"shared view output target is not empty: {path}")
            shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _render_invitation(definition: SharedViewDefinition, *, transport_kind: str, transport_path: str) -> str:
    lines = [
        "version = 1",
        f"view_id = {_toml_string(definition.view_id)}",
        f"ref = {_toml_string(definition.ref)}",
        f"title = {_toml_string(definition.title)}",
        "relationship = \"human\"",
    ]
    if definition.description:
        lines.append(f"description = {_toml_string(definition.description)}")
    if definition.maintainer:
        lines.append(f"maintainer = {_toml_string(definition.maintainer)}")
    lines.extend(
        [
            "",
            "[transport]",
            f"kind = {_toml_string(transport_kind)}",
            f"path = {_toml_string(transport_path)}",
            f"view_id = {_toml_string(definition.view_id)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_hub_registry(hub: Path) -> dict[str, dict[str, str]]:
    registry_path = hub / "registry.toml"
    if not registry_path.exists():
        return {}
    with registry_path.open("rb") as handle:
        data = tomllib.load(handle)
    raw_views = data.get("views", {})
    if not isinstance(raw_views, dict):
        raise ValueError("hub registry must contain a [views] table")
    registry: dict[str, dict[str, str]] = {}
    for view_id, raw_entry in raw_views.items():
        if not isinstance(raw_entry, dict):
            raise ValueError(f"[views.{view_id}] must be a TOML table")
        registry[_validate_heading_id(str(view_id))] = {
            key: str(value)
            for key, value in raw_entry.items()
            if isinstance(value, str)
        }
    return registry


def _save_hub_registry(hub: Path, registry: dict[str, dict[str, str]]) -> None:
    lines = ["# RightMemory minimal shared-view hub registry", ""]
    for view_id in sorted(registry):
        lines.append(f"[views.{_toml_key(view_id)}]")
        for key in sorted(registry[view_id]):
            lines.append(f"{key} = {_toml_string(registry[view_id][key])}")
        lines.append("")
    _atomic_write_text(hub / "registry.toml", "\n".join(lines).rstrip() + "\n")


def _resolve_invitation_file(path: Path) -> Path:
    if path.is_dir():
        path = path / INVITATION_FILE
    if not path.is_file():
        raise FileNotFoundError(f"shared-view invitation not found: {path}")
    return path


def _load_invitation(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError("shared-view invitation must be a TOML document")
    return data


def _target_from_invitation(
    root: Path,
    heading_id: str,
    invitation_file: Path,
    transport: dict[str, object],
    view_id: str,
    copy_package: bool,
) -> SharedViewTarget:
    kind = _required_string(transport, "kind", "transport")
    path = _optional_string(transport.get("path"))
    transport_view_id = _optional_string(transport.get("view_id")) or view_id
    if kind == "package":
        package_dir = invitation_file.parent if path in (None, ".") else (invitation_file.parent / path)
        package_dir = package_dir.resolve()
        if copy_package:
            target_dir = root / RUNTIME_DIR / "imports" / heading_id
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.copytree(package_dir, target_dir)
            return SharedViewTarget(kind="package", path=target_dir.relative_to(root).as_posix(), view_id=transport_view_id)
        return SharedViewTarget(kind="package", path=str(package_dir), view_id=transport_view_id)
    if kind == "local":
        if not path:
            raise ValueError("local shared-view invitations require transport.path")
        return SharedViewTarget(kind="local", path=path, view_id=transport_view_id)
    if kind == "hub":
        if not path:
            raise ValueError("hub shared-view invitations require transport.path")
        return SharedViewTarget(kind="hub", path=path, view_id=transport_view_id)
    raise ValueError(f"unknown shared-view invitation transport `{kind}`")


def _default_invitation_body(invitation: dict[str, object]) -> str:
    description = _optional_string(invitation.get("description"))
    maintainer = _optional_string(invitation.get("maintainer"))
    if description and maintainer:
        return f"{description} Maintained by {maintainer}."
    if description:
        return description
    if maintainer:
        return f"Shared view maintained by {maintainer}."
    return "Accepted shared view relationship."


def _retrieve_fresh_shared_view(
    root: Path,
    connection: SharedViewConnection,
    query: str,
) -> _SharedViewCache | None:
    target = connection.target
    if target.kind in {"local_markdown", "package"} and target.path:
        package_root = _resolve_package_path(root, target.path, require_under_root=target.kind == "local_markdown")
        if package_root.exists():
            return _collect_package_cache(package_root, connection, target.view_id)
    if target.kind == "local" and target.path:
        provider_root = _resolve_external_path(root, target.path)
        view_id = target.view_id or _view_id_from_ref(connection.ref) or connection.heading_id
        if provider_root.exists():
            return _retrieve_provider_view(provider_root, view_id, query)
    if target.kind == "hub" and target.path:
        hub = _resolve_external_path(root, target.path)
        view_id = target.view_id or _view_id_from_ref(connection.ref) or connection.heading_id
        if hub.exists():
            return _retrieve_hub_view(hub, view_id, query)
    return None


def _collect_package_cache(
    package_root: Path,
    connection: SharedViewConnection,
    view_id: str | None = None,
) -> _SharedViewCache:
    memory_root = package_root / "dist" if (package_root / "dist").is_dir() else package_root
    cache = _collect_local_markdown_cache(memory_root)
    metadata = _read_package_metadata(package_root)
    provenance = metadata.get("title") or connection.description or connection.heading_id
    backing_parts = []
    if (memory_root / "MEMORY.md").exists() or list(memory_root.glob("MEMORY_*.md")):
        backing_parts.append("filtered Markdown")
    if (package_root / "retriever.md").exists():
        backing_parts.append("retriever prompt")
    if view_id and not provenance:
        provenance = view_id
    return _SharedViewCache(
        freshness=cache.freshness,
        source_lines=cache.source_lines,
        provenance=provenance,
        backing=", ".join(backing_parts) if backing_parts else "package",
    )


def _read_package_metadata(package_root: Path) -> dict[str, str]:
    metadata_path = package_root / VIEW_METADATA_FILE
    if not metadata_path.exists():
        return {}
    with metadata_path.open("rb") as handle:
        data = tomllib.load(handle)
    return {key: value for key, value in data.items() if isinstance(value, str)}


def _retrieve_provider_view(provider_root: Path, view_id: str, query: str) -> _SharedViewCache | None:
    definition = load_shared_view_definition(provider_root, view_id)
    view_dir = _provider_view_dir(provider_root, definition.view_id)
    source_lines: list[_SharedViewSourceLine] = []
    backing_parts: list[str] = []
    dist = view_dir / "dist"
    if dist.is_dir():
        package_cache = _collect_local_markdown_cache(dist)
        source_lines.extend(
            _SharedViewSourceLine(f"shared_views/{definition.view_id}/dist/{line.relative}", line.line_number, line.line)
            for line in package_cache.source_lines
        )
        backing_parts.append("filtered Markdown")
    retriever = view_dir / "retriever.md"
    if retriever.exists():
        terms = list(definition.filter_terms)
        terms.extend(_query_terms(query))
        source_lines.extend(
            _filtered_provider_source_lines(
                provider_root,
                definition,
                terms,
                0,
                200,
                required_terms=definition.filter_terms,
            )
        )
        backing_parts.append("retriever prompt")
    if not source_lines:
        return None
    return _SharedViewCache(
        freshness=_now_iso(),
        source_lines=source_lines,
        provenance=f"{definition.title} shared view",
        backing=", ".join(dict.fromkeys(backing_parts)),
    )


def _retrieve_hub_view(hub: Path, view_id: str, query: str) -> _SharedViewCache | None:
    registry = _load_hub_registry(hub)
    entry = registry.get(_validate_heading_id(view_id))
    if entry is None:
        return None
    package_path = entry.get("package_path")
    if package_path:
        package_root = _resolve_external_path(hub, package_path)
        if package_root.exists():
            connection = SharedViewConnection(
                heading_id=view_id,
                ref=entry.get("ref", f"rightmemory://view/{view_id}"),
                maintainer=entry.get("maintainer") or None,
                description=entry.get("description") or None,
            )
            cache = _collect_package_cache(package_root, connection, view_id)
            return _SharedViewCache(
                freshness=cache.freshness,
                source_lines=cache.source_lines,
                provenance=f"hub hosted {cache.provenance or view_id}",
                backing=cache.backing,
            )
    provider_root = entry.get("provider_root")
    if provider_root:
        return _retrieve_provider_view(_resolve_external_path(hub, provider_root), view_id, query)
    return None


def _deliver_interaction_record(
    root: Path,
    connection: SharedViewConnection,
    record: dict[str, object],
) -> str:
    target = connection.target
    view_id = target.view_id or _view_id_from_ref(connection.ref) or connection.heading_id
    if target.kind == "local" and target.path:
        provider_root = _resolve_external_path(root, target.path)
        if provider_root.exists():
            _append_provider_inbox_record(provider_root, view_id, record)
            return "sent"
    if target.kind == "hub" and target.path:
        hub = _resolve_external_path(root, target.path)
        if hub.exists():
            _append_hub_interaction_record(hub, view_id, record)
            return "sent"
    if target.kind in {"package", "local_markdown"}:
        return "queued"
    return "sent"


def _append_provider_inbox_record(provider_root: Path, view_id: str, record: dict[str, object]) -> None:
    _ensure_runtime_gitignore(provider_root / ".runtime")
    path = provider_root / RUNTIME_DIR / "inbox" / f"{_validate_heading_id(view_id)}.jsonl"
    _append_jsonl(path, record)


def _append_hub_interaction_record(hub: Path, view_id: str, record: dict[str, object]) -> None:
    path = hub / "interactions" / f"{_validate_heading_id(view_id)}.jsonl"
    _append_jsonl(path, record)


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
    entry = f"### {title_text} {{M#{heading_id}}}\n"
    if body_text:
        entry += f"\n{body_text}\n"
    memory.write_text(_insert_shared_view_heading(text, section=section, entry=entry), encoding="utf-8")


def _insert_shared_view_heading(text: str, *, section: str, entry: str) -> str:
    lines = text.splitlines(keepends=True)
    section_index = next(
        (index for index, line in enumerate(lines) if line.startswith("# ") and line.strip() == section),
        None,
    )
    entry_text = entry.rstrip()

    if section_index is None:
        base = text.rstrip()
        addition = f"{section}\n\n{entry_text}\n"
        return f"{base}\n\n{addition}" if base else addition

    insert_index = len(lines)
    for index in range(section_index + 1, len(lines)):
        if lines[index].startswith("# "):
            insert_index = index
            break

    before = "".join(lines[:insert_index]).rstrip()
    after = "".join(lines[insert_index:]).lstrip("\n")
    updated = f"{before}\n\n{entry_text}\n"
    if after:
        updated += f"\n{after}"
    return updated


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


def _collect_local_markdown_cache(target: Path) -> _SharedViewCache:
    source_lines: list[_SharedViewSourceLine] = []
    target_root = target.resolve()
    for memory_file in sorted(target.glob("MEMORY*.md")):
        if memory_file.is_symlink():
            continue
        try:
            resolved = memory_file.resolve()
        except OSError:
            continue
        if target_root not in (resolved, *resolved.parents) or not memory_file.is_file():
            continue
        relative = memory_file.relative_to(target).as_posix()
        for line_number, line in enumerate(memory_file.read_text(encoding="utf-8").splitlines(), start=1):
            source_lines.append(_SharedViewSourceLine(relative, line_number, line))
    return _SharedViewCache(
        freshness=datetime.now(UTC).replace(microsecond=0).isoformat(),
        source_lines=source_lines,
    )


def _match_local_markdown_lines(source_lines: list[_SharedViewSourceLine], query: str) -> list[str]:
    terms = _query_terms(query)
    matches: list[str] = []
    for source_line in source_lines:
        lowered = source_line.line.lower()
        if any(term in lowered for term in terms):
            matches.append(f"- {source_line.relative}:{source_line.line_number}: {source_line.line}")
            if len(matches) >= 12:
                return matches
    return matches or ["- no strong match in published shared memory"]


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in QUERY_TERM_RE.findall(query) if term.lower() not in COMMON_QUERY_WORDS]


def _format_shared_view_result(
    connection: SharedViewConnection,
    status: str,
    freshness: str,
    matches: list[str],
    *,
    provenance: str | None = None,
    backing: str | None = None,
) -> str:
    lines = [
        f"Shared view: {connection.heading_id}",
        f"Status: {status}",
        f"Ref: {connection.ref}",
    ]
    if provenance:
        lines.append(f"Provenance: {provenance}")
    if backing:
        lines.append(f"Backing: {backing}")
    if connection.maintainer:
        lines.append(f"Maintainer: {connection.maintainer}")
    if connection.description:
        lines.append(f"Description: {connection.description}")
    lines.extend(
        [
            f"Freshness: {freshness}",
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


def _read_shared_view_cache(root: Path, heading_id: str) -> _SharedViewCache | None:
    cache_path = _shared_view_cache_path(root, heading_id)
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    freshness = data.get("freshness")
    provenance = data.get("provenance")
    backing = data.get("backing")
    raw_source_lines = data.get("source_lines")
    if not isinstance(freshness, str) or not isinstance(raw_source_lines, list):
        return None
    if provenance is not None and not isinstance(provenance, str):
        return None
    if backing is not None and not isinstance(backing, str):
        return None
    source_lines: list[_SharedViewSourceLine] = []
    for raw_source_line in raw_source_lines:
        if not isinstance(raw_source_line, dict):
            return None
        relative = raw_source_line.get("relative")
        line_number = raw_source_line.get("line_number")
        line = raw_source_line.get("line")
        if not isinstance(relative, str) or not isinstance(line_number, int) or not isinstance(line, str):
            return None
        source_lines.append(_SharedViewSourceLine(relative, line_number, line))
    return _SharedViewCache(
        freshness=freshness,
        source_lines=source_lines,
        provenance=provenance,
        backing=backing,
    )


def _write_shared_view_cache(root: Path, heading_id: str, cache: _SharedViewCache) -> None:
    _ensure_runtime_gitignore(root / ".runtime")
    data = {
        "version": CACHE_VERSION,
        "freshness": cache.freshness,
        "provenance": cache.provenance,
        "backing": cache.backing,
        "source_lines": [
            {
                "relative": source_line.relative,
                "line_number": source_line.line_number,
                "line": source_line.line,
            }
            for source_line in cache.source_lines
        ],
    }
    _atomic_write_text(
        _shared_view_cache_path(root, heading_id),
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


def _append_interaction_record(root: Path, heading_id: str, record: dict[str, object]) -> None:
    _ensure_runtime_gitignore(root / ".runtime")
    path = root / RUNTIME_DIR / "interactions" / f"{heading_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _normalize_heading_title(title: str, heading_id: str) -> str:
    title_text = TITLE_MARKER_RE.sub(" ", title)
    title_text = " ".join(title_text.split())
    return title_text or heading_id


def _load_target(root: Path, heading_id: str, raw_target: object) -> SharedViewTarget:
    if raw_target in ({}, None):
        return SharedViewTarget()
    if not isinstance(raw_target, dict):
        raise ValueError(f"[connections.{heading_id}.target] must be a TOML table")
    return _validate_target(
        root,
        heading_id,
        raw_target.get("kind", "none"),
        raw_target.get("path"),
        raw_target.get("view_id"),
    )


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
    _validate_target(
        root,
        validated_heading_id,
        connection.target.kind,
        connection.target.path,
        connection.target.view_id,
    )


def _validate_target(
    root: Path,
    heading_id: str,
    raw_kind: object,
    raw_path: object,
    raw_view_id: object = None,
) -> SharedViewTarget:
    kind = str(raw_kind).strip()
    if kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{kind}` for {heading_id}")
    path = _optional_string(raw_path)
    view_id = _optional_string(raw_view_id)
    if view_id:
        view_id = _validate_heading_id(view_id)
    if kind in {"local_markdown", "package", "local", "hub"} and not path:
        raise ValueError(f"{kind} shared view target requires path for {heading_id}")
    if kind == "local_markdown" and path:
        _resolve_under_root(root, path)
    elif kind in {"package", "local", "hub"} and path:
        _resolve_external_path(root, path)
    if kind in {"none", "revoked"} and path:
        raise ValueError(f"{kind} shared view target must not set path for {heading_id}")
    return SharedViewTarget(kind=kind, path=path, view_id=view_id)


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


def _required_plain_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
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


def _resolve_package_path(root: Path, raw_path: str, *, require_under_root: bool) -> Path:
    if require_under_root:
        return _resolve_under_root(root, raw_path)
    return _resolve_external_path(root, raw_path)


def _resolve_external_path(base: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    if ".." in path.parts:
        raise ValueError("shared view external paths must not contain '..'")
    return (base / path).resolve()


def _view_id_from_ref(ref: str) -> str | None:
    candidate = ref.rstrip("/").rsplit("/", 1)[-1]
    try:
        return _validate_heading_id(candidate)
    except ValueError:
        return None


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_key(value: str) -> str:
    return _toml_string(value)


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
