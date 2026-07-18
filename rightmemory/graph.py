from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


KNOWN_EDGE_TYPES = {
    "dep",
    "emb",
    "bak",
    "agg",
    "ver",
    "ext",
    "up",
    "rel",
    "loc",
    "run",
    "cfg",
    "out",
    "in",
    "doc",
    "todo",
}

ANCHOR_RE = re.compile(
    r"^(#{1,4})\s+.*?\{(F#|M#|S#|MF#|MQ#|#)([A-Za-z0-9_.-]+)\}"
    r"(?:\s*(?:\u2192|->)\s*\[(.*?)\])?"
)
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*?(?:\s*(?:\u2192|->)\s*\[(.*?)\])?\s*$")
PURSUIT_ACTION_RE = re.compile(r"^\s*-\s+`([^`]+)`(?:\s|$)")
PURSUIT_FIELD_RE = re.compile(r"^\s*\*\*(State|Next|Done when|Status):\*\*", re.IGNORECASE)
EDGE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*):\s*([A-Za-z0-9_.-]+)\s*$")
FOCUS_HEADING_RE = re.compile(r"^##\s+Focus\s*$", re.IGNORECASE)
FOCUS_REFERENCE_RE = re.compile(r"^\s*-\s+`([A-Za-z0-9_.-]+)`(?:\s|$)")

MEMORY_DETAIL_FILE_RE = re.compile(r"^MEMORY_[A-Za-z0-9_.-]+\.md$")
MEMORY_SKILL_FILE_RE = re.compile(r"^MEMORY_SKILL_[A-Za-z0-9_.-]+\.md$")
PURSUIT_DETAIL_FILE_RE = re.compile(r"^PURSUIT_(?!(?i:RULES)\.md$)[A-Za-z0-9_.-]+\.md$")

ROOT_FILES = (("MEMORY.md", "memory"), ("PURSUITS.md", "pursuit"))
MEMORY_ONLY_ANCHOR_KINDS = {"M#", "S#", "MF#", "MQ#"}


@dataclass(frozen=True)
class GraphItem:
    id: str
    file: Path
    line_number: int
    family: str
    item_kind: str
    anchor_kind: str | None
    edges: tuple[tuple[str, str], ...]
    malformed_edges: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackingReference:
    id: str
    kind: str
    family: str
    source_file: Path
    line_number: int
    path: Path


@dataclass
class GraphManifest:
    root: Path
    graph_files: list[Path] = field(default_factory=list)
    non_graph_files: list[Path] = field(default_factory=list)
    items: dict[str, GraphItem] = field(default_factory=dict)
    headings: dict[str, GraphItem] = field(default_factory=dict)
    backing: dict[str, BackingReference] = field(default_factory=dict)
    backing_paths: dict[str, BackingReference] = field(default_factory=dict)
    focus_ids: list[tuple[str, Path, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return sorted(set((*self.graph_files, *self.non_graph_files)))


def build_graph_manifest(memory_root: Path) -> GraphManifest:
    root = Path(memory_root).resolve()
    manifest = GraphManifest(root=root)
    pending: list[tuple[Path, str]] = []
    for name, family in ROOT_FILES:
        path = root / name
        if path.exists():
            pending.append((path, family))
        else:
            manifest.errors.append(f"missing canonical RightMemory root `{name}`")

    visited: set[Path] = set()
    while pending:
        file_path, family = pending.pop(0)
        relative = _relative(root, file_path)
        if file_path in visited:
            continue
        visited.add(file_path)
        if not _is_regular_file_under_root(root, file_path):
            manifest.errors.append(f"graph file `{relative}` must be a regular file inside the RightMemory root")
            continue
        manifest.graph_files.append(file_path)
        _scan_graph_file(manifest, file_path, family, pending)

    _validate_items(manifest)
    _validate_focus(manifest)
    manifest.graph_files.sort()
    manifest.non_graph_files = sorted(set(manifest.non_graph_files))
    return manifest


def resolve_backing_reference(memory_root: Path, item_id: str, kind: str) -> BackingReference | None:
    manifest = build_graph_manifest(memory_root)
    reference = manifest.backing.get(item_id)
    if reference is None or reference.kind != kind:
        return None
    return reference


def _scan_graph_file(
    manifest: GraphManifest,
    file_path: Path,
    family: str,
    pending: list[tuple[Path, str]],
) -> None:
    in_focus = False
    in_pursuit_next = False
    file_backed_heading: GraphItem | None = None
    file_backed_depth: int | None = None
    fence_char: str | None = None
    fence_length = 0
    for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            marker = fence.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is not None:
            continue
        heading_text = re.match(r"^(#+)\s+", line)
        if heading_text:
            depth = len(heading_text.group(1))
            if file_backed_depth is not None:
                if depth > file_backed_depth:
                    manifest.errors.append(
                        f"F# heading cannot retain child headings at "
                        f"{_relative(manifest.root, file_path)}:{line_number}; "
                        f"move them to the backing file for `{file_backed_heading.id}`"
                    )
                else:
                    file_backed_heading = None
                    file_backed_depth = None
            if depth <= 2:
                in_focus = bool(family == "pursuit" and FOCUS_HEADING_RE.match(line))
            if family == "pursuit":
                in_pursuit_next = False
        if family == "pursuit":
            field = PURSUIT_FIELD_RE.match(line)
            if field:
                in_pursuit_next = field.group(1).casefold() == "next"
                continue
        if in_focus:
            focus = FOCUS_REFERENCE_RE.match(line)
            if focus:
                manifest.focus_ids.append((focus.group(1), file_path, line_number))
                continue

        anchor = ANCHOR_RE.match(line)
        if anchor:
            anchor_kind = anchor.group(2)
            item_id = anchor.group(3)
            edges, malformed = _parse_edges(anchor.group(4) or "")
            item = GraphItem(
                id=item_id,
                file=file_path,
                line_number=line_number,
                family=family,
                item_kind="heading",
                anchor_kind=anchor_kind,
                edges=tuple(edges),
                malformed_edges=tuple(malformed),
            )
            _record_item(manifest, item)
            if anchor_kind == "F#":
                file_backed_heading = item
                file_backed_depth = len(anchor.group(1))
            if anchor_kind in MEMORY_ONLY_ANCHOR_KINDS and family != "memory":
                manifest.errors.append(
                    f"{anchor_kind} heading `{item_id}` is only valid in Memory at {_loc(manifest.root, item)}"
                )
            if anchor_kind == "F#" and family == "pursuit" and item_id.casefold() == "rules":
                manifest.errors.append(
                    f"Pursuit F# id `{item_id}` is reserved for PURSUIT_RULES.md at {_loc(manifest.root, item)}"
                )
                continue
            reference = _backing_reference(manifest.root, item)
            if reference is not None:
                manifest.backing.setdefault(item_id, reference)
                backing_path_key = _backing_path_key(manifest.root, reference.path)
                previous_reference = manifest.backing_paths.get(backing_path_key)
                if previous_reference is not None and (
                    previous_reference.id != reference.id or previous_reference.kind != reference.kind
                ):
                    manifest.errors.append(
                        f"backing file `{_relative(manifest.root, reference.path)}` is claimed by "
                        f"{previous_reference.kind} heading `{previous_reference.id}` at "
                        f"{_relative(manifest.root, previous_reference.source_file)}:"
                        f"{previous_reference.line_number} and {reference.kind} heading `{reference.id}` at "
                        f"{_relative(manifest.root, reference.source_file)}:{reference.line_number}"
                    )
                else:
                    manifest.backing_paths[backing_path_key] = reference
                if not _is_regular_file_under_root(manifest.root, reference.path):
                    relative = _relative(manifest.root, reference.path)
                    if anchor_kind == "S#":
                        message = f"missing skill file `{relative}`"
                    elif anchor_kind == "M#":
                        message = f"missing Markdown backing file `{relative}`"
                    else:
                        message = f"missing F# backing file `{relative}`"
                    manifest.errors.append(f"{message} for heading at {_loc(manifest.root, item)}")
                elif anchor_kind == "F#":
                    pending.append((reference.path, family))
                else:
                    manifest.non_graph_files.append(reference.path)
            continue

        if family == "pursuit" and in_pursuit_next:
            action = PURSUIT_ACTION_RE.match(line)
            if action:
                if action.group(1) not in {"do", "ask", "wait"}:
                    relative = _relative(manifest.root, file_path)
                    manifest.errors.append(
                        f"invalid Pursuit Next action `{action.group(1)}` at {relative}:{line_number}; "
                        "use `do`, `ask`, or `wait`"
                    )
                continue

        node = NODE_RE.match(line)
        if node:
            if file_backed_heading is not None:
                manifest.errors.append(
                    f"F# heading cannot retain child node lines at "
                    f"{_relative(manifest.root, file_path)}:{line_number}; "
                    f"move them to the backing file for `{file_backed_heading.id}`"
                )
            edges, malformed = _parse_edges(node.group(2) or "")
            _record_item(
                manifest,
                GraphItem(
                    id=node.group(1),
                    file=file_path,
                    line_number=line_number,
                    family=family,
                    item_kind="node",
                    anchor_kind=None,
                    edges=tuple(edges),
                    malformed_edges=tuple(malformed),
                ),
            )


def _record_item(manifest: GraphManifest, item: GraphItem) -> None:
    previous = manifest.items.get(item.id)
    if previous is not None:
        manifest.errors.append(
            f"duplicate id `{item.id}` at {_loc(manifest.root, item)}; first seen at {_loc(manifest.root, previous)}"
        )
        return
    manifest.items[item.id] = item
    if item.item_kind == "heading":
        manifest.headings[item.id] = item


def _backing_reference(root: Path, item: GraphItem) -> BackingReference | None:
    if item.anchor_kind == "F#":
        prefix = "MEMORY" if item.family == "memory" else "PURSUIT"
        path = root / f"{prefix}_{item.id}.md"
    elif item.anchor_kind == "M#":
        path = root / f"MEMORY_{item.id}.md"
    elif item.anchor_kind == "S#":
        path = root / f"MEMORY_SKILL_{item.id}.md"
    else:
        return None
    return BackingReference(
        id=item.id,
        kind=item.anchor_kind,
        family=item.family,
        source_file=item.file,
        line_number=item.line_number,
        path=path,
    )


def _validate_items(manifest: GraphManifest) -> None:
    for item in manifest.items.values():
        seen_edges: set[tuple[str, str]] = set()
        for malformed_edge in item.malformed_edges:
            manifest.errors.append(f"malformed edge `{malformed_edge}` at {_loc(manifest.root, item)}")
        for edge_type, target in item.edges:
            edge = (edge_type, target)
            if edge in seen_edges:
                manifest.errors.append(f"duplicate edge `{edge_type}:{target}` at {_loc(manifest.root, item)}")
                continue
            seen_edges.add(edge)
            if target == item.id:
                manifest.errors.append(f"self-edge `{edge_type}:{target}` at {_loc(manifest.root, item)}")
            elif edge_type not in KNOWN_EDGE_TYPES:
                manifest.errors.append(f"unknown edge type `{edge_type}` at {_loc(manifest.root, item)}")
            elif target not in manifest.items:
                manifest.errors.append(f"dangling edge `{edge_type}:{target}` at {_loc(manifest.root, item)}")


def _validate_focus(manifest: GraphManifest) -> None:
    seen: set[str] = set()
    for item_id, path, line_number in manifest.focus_ids:
        relative = _relative(manifest.root, path)
        if item_id in seen:
            manifest.errors.append(f"duplicate Focus reference `{item_id}` at {relative}:{line_number}")
            continue
        seen.add(item_id)
        item = manifest.items.get(item_id)
        if item is None:
            manifest.errors.append(f"dangling Focus reference `{item_id}` at {relative}:{line_number}")
        elif item.item_kind != "heading" or item.family != "pursuit":
            manifest.errors.append(
                f"Focus reference `{item_id}` must target a Pursuit heading at {relative}:{line_number}"
            )


def _parse_edges(edge_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    edges: list[tuple[str, str]] = []
    malformed: list[str] = []
    for raw in edge_text.split(","):
        value = raw.strip()
        if not value:
            continue
        match = EDGE_RE.match(value)
        if match is None:
            malformed.append(value)
        else:
            edges.append((match.group(1), match.group(2)))
    return edges, malformed


def _loc(root: Path, item: GraphItem) -> str:
    return f"{_relative(root, item.file)}:{item.line_number}"


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _backing_path_key(root: Path, path: Path) -> str:
    """Return a stable key with Windows' case-insensitive path semantics."""
    return _relative(root, path).casefold()


def _is_regular_file_under_root(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    return True
