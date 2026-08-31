from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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

ITEM_ID_PATTERN = r"[A-Za-z0-9_.-]+"
ITEM_ID_RE = re.compile(rf"^{ITEM_ID_PATTERN}$")
ANCHOR_RE = re.compile(
    rf"^(#{{1,}})\s+.*?\{{(F#|M#|S#|MF#|MQ#|#)({ITEM_ID_PATTERN})\}}"
    r"(?:\s*(?:\u2192|->)\s*\[(.*?)\])?\s*$"
)
ANCHOR_CANDIDATE_RE = re.compile(
    r"^(#{1,})\s+.*?\{(F#|M#|S#|MF#|MQ#|#)([^}]*)\}"
)
UNSUPPORTED_ANCHOR_RE = re.compile(r"^(#{1,})\s+.*?\{([A-Za-z]+#)([^}]*)\}")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*?(?:\s(?:\u2192|->)\s*\[(.*?)\])\s*$")
NODE_CANDIDATE_RE = re.compile(r"^\s*-\s+`([^`]+)`(?:\s|$)")
# Reading compatibility for existing user data, not the current Pursuit schema.
LEGACY_PURSUIT_FIELD_RE = re.compile(r"^\s*\*\*(State|Next|Done when|Status):\*\*", re.IGNORECASE)
EDGE_RE = re.compile(rf"^\s*([A-Za-z][A-Za-z0-9_-]*):\s*({ITEM_ID_PATTERN})\s*$")
FOCUS_HEADING_RE = re.compile(r"^##\s+Focus\s*$", re.IGNORECASE)
FOCUS_REFERENCE_RE = re.compile(rf"^\s*-\s+`({ITEM_ID_PATTERN})`(?:\s|$)")
FOCUS_CANDIDATE_RE = re.compile(r"^\s*-\s+`([^`]+)`(?:\s|$)")
HEADING_RE = re.compile(r"^(#+)\s+")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

MEMORY_DETAIL_FILE_RE = re.compile(rf"^MEMORY_{ITEM_ID_PATTERN}\.md$")
MEMORY_SKILL_FILE_RE = re.compile(rf"^MEMORY_SKILL_{ITEM_ID_PATTERN}\.md$")
PURSUIT_DETAIL_FILE_RE = re.compile(
    rf"^PURSUIT_(?!(?i:RULES)\.md$){ITEM_ID_PATTERN}\.md$"
)

ROOT_FILES = (("MEMORY.md", "memory"), ("PURSUITS.md", "pursuit"))
MEMORY_ONLY_ANCHOR_KINDS = {"M#", "S#", "MF#", "MQ#"}
TERMINAL_HEADING_KINDS = {"F#", "M#", "S#", "MF#", "MQ#"}

BlockKey = tuple[Path, int]


def is_valid_item_id(value: str) -> bool:
    """Return whether value is a canonical RightMemory short slug."""
    return ITEM_ID_RE.fullmatch(value) is not None


def validate_item_id(value: str) -> str:
    """Validate a caller-supplied item id without changing its identity."""
    if not is_valid_item_id(value):
        raise ValueError("id must contain only letters, numbers, dot, underscore, or dash")
    return value


@dataclass(frozen=True, slots=True)
class AddressableHeading:
    depth: int
    title: str
    anchor_kind: str
    id: str
    edges: tuple[tuple[str, str], ...]
    malformed_edges: tuple[str, ...] = ()


def parse_addressable_heading(line: str) -> AddressableHeading | None:
    """Read a heading using the same grammar as the canonical document index."""
    match = ANCHOR_RE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    heading = HEADING_RE.match(line)
    assert heading is not None
    edges, malformed = _parse_edges(match.group(4) or "")
    return AddressableHeading(
        depth=len(match.group(1)),
        title=line[heading.end():match.start(2) - 1].strip(),
        anchor_kind=match.group(2),
        id=match.group(3),
        edges=tuple(edges),
        malformed_edges=tuple(malformed),
    )


def heading_title(line: str) -> str:
    """Return the title of an addressed or plain heading, without its edges."""
    addressed = parse_addressable_heading(line)
    if addressed is not None:
        return addressed.title
    heading = HEADING_RE.match(line)
    if heading is None:
        raise ValueError("expected a Markdown heading")
    return line[heading.end():].strip()


def validate_heading_title(title: str) -> str:
    """Reject titles that would change the structural meaning of a heading."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a nonempty string")
    title = title.strip()
    if len(title.splitlines()) != 1 or "\x00" in title:
        raise ValueError("title must fit on one line")
    probe = f"## {title}"
    if ANCHOR_CANDIDATE_RE.match(probe) or UNSUPPORTED_ANCHOR_RE.match(probe):
        raise ValueError("title must not contain a RightMemory heading anchor")
    return title


def render_heading_line(
    title: str,
    anchor_kind: str,
    item_id: str,
    edges: Iterable[tuple[str, str]] = (),
    *,
    depth: int = 2,
) -> str:
    """Render a new addressable heading; callers choose the document newline."""
    title = validate_heading_title(title)
    validate_item_id(item_id)
    if anchor_kind not in {"#", *TERMINAL_HEADING_KINDS}:
        raise ValueError("unsupported heading anchor kind")
    if not 1 <= depth <= 4 or (depth == 4 and anchor_kind not in TERMINAL_HEADING_KINDS):
        raise ValueError("ordinary headings must have depth one through three")
    edge_list = tuple(edges)
    for edge_type, target in edge_list:
        if edge_type not in KNOWN_EDGE_TYPES:
            raise ValueError(f"unknown edge type: {edge_type}")
        validate_item_id(target)
    result = f"{'#' * depth} {title} {{{anchor_kind}{item_id}}}"
    if edge_list:
        result += " \u2192 [" + ", ".join(f"{kind}:{target}" for kind, target in edge_list) + "]"
    return result


def replace_heading_title(line: str, title: str) -> str:
    """Patch only an addressed heading's title, retaining its anchor and suffix."""
    title = validate_heading_title(title)
    anchor = ANCHOR_RE.match(line.rstrip("\r\n"))
    heading = HEADING_RE.match(line)
    if anchor is None or heading is None:
        raise ValueError("expected an addressable heading")
    title_end = anchor.start(2) - 1
    old_title = line[heading.end():title_end]
    spacing = old_title[len(old_title.rstrip()):]
    return line[:heading.end()] + title + spacing + line[title_end:]


def remove_edge_targets(line: str, deleted_ids: set[str] | frozenset[str]) -> str:
    """Remove selected typed edges without changing prose, other edges, or newlines."""
    source = line.rstrip("\r\n")
    match = ANCHOR_RE.match(source)
    group = 4
    if match is None:
        match = NODE_RE.match(source)
        group = 2
    if match is None or match.group(group) is None:
        return line
    tokens = match.group(group).split(",")
    kept = []
    for token in tokens:
        edge = EDGE_RE.match(token)
        if edge is None or edge.group(2) not in deleted_ids:
            kept.append(token)
    if len(kept) == len(tokens):
        return line
    return line[:match.start(group)] + ",".join(kept) + line[match.end(group):]


@dataclass(frozen=True)
class SourceSpan:
    path: Path
    start_line: int
    end_line: int


@dataclass(frozen=True)
class SourceTextPart:
    """One physical source line retained in a block's owned-text projection."""

    source_path: Path
    line_number: int
    text: str


@dataclass
class DocumentBlock:
    key: BlockKey
    kind: str
    source_path: Path
    family: str
    line: str = ""
    title: str = ""
    prose: str = ""
    depth: int = 0
    line_number: int = 0
    end_line: int = 0
    body_span: SourceSpan | None = None
    item_id: str | None = None
    item_kind: str | None = None
    anchor_kind: str | None = None
    focus_target: str | None = None
    physical_parent: BlockKey | None = None
    logical_parent: BlockKey | None = None
    physical_children: list[BlockKey] = field(default_factory=list)
    logical_children: list[BlockKey] = field(default_factory=list)
    physical_parts: list[str | BlockKey] = field(default_factory=list)
    logical_parts: list[str | BlockKey] = field(default_factory=list)
    logical_text_parts: list[SourceTextPart] = field(default_factory=list)
    traversal_rank: int = -1

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.source_path, self.line_number, self.end_line)


@dataclass(frozen=True)
class ParsedDocument:
    path: Path
    relative_path: str
    text: str
    lines: tuple[str, ...]
    family: str
    source_order: int
    root_key: BlockKey


@dataclass
class GraphItem:
    id: str
    file: Path
    line_number: int
    family: str
    item_kind: str
    anchor_kind: str | None
    edges: tuple[tuple[str, str], ...]
    title: str = ""
    prose: str = ""
    malformed_edges: tuple[str, ...] = ()
    block_key: BlockKey | None = None
    end_line: int = 0
    body_span: SourceSpan | None = None
    physical_parent: BlockKey | None = None
    logical_parent: BlockKey | None = None
    traversal_rank: int = -1
    content_hash: str = ""

    @property
    def span(self) -> SourceSpan:
        return SourceSpan(self.file, self.line_number, self.end_line or self.line_number)


@dataclass(frozen=True)
class BackingReference:
    id: str
    kind: str
    family: str
    source_file: Path
    line_number: int
    path: Path


@dataclass(frozen=True)
class GraphDiagnostic:
    message: str
    namespace: str
    path: str | None = None
    line_number: int | None = None


@dataclass
class GraphManifest:
    root: Path
    namespace: str = "local"
    profile: str = "local"
    graph_files: list[Path] = field(default_factory=list)
    non_graph_files: list[Path] = field(default_factory=list)
    documents: dict[Path, ParsedDocument] = field(default_factory=dict)
    blocks: dict[BlockKey, DocumentBlock] = field(default_factory=dict)
    root_blocks: list[BlockKey] = field(default_factory=list)
    document_roots: list[BlockKey] = field(default_factory=list)
    items: dict[str, GraphItem] = field(default_factory=dict)
    headings: dict[str, GraphItem] = field(default_factory=dict)
    backing: dict[str, BackingReference] = field(default_factory=dict)
    backing_paths: dict[str, BackingReference] = field(default_factory=dict)
    focus_ids: list[tuple[str, Path, int]] = field(default_factory=list)
    focus_blocks: list[BlockKey] = field(default_factory=list)
    duplicates: set[str] = field(default_factory=set)
    diagnostics: list[GraphDiagnostic] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return sorted(set((*self.graph_files, *self.non_graph_files)))

    def block_for_id(self, item_id: str) -> DocumentBlock | None:
        item = self.items.get(item_id)
        if item is None or item.block_key is None:
            return None
        return self.blocks[item.block_key]

    def walk_logical(self, key: BlockKey, *, include_self: bool = False) -> Iterable[DocumentBlock]:
        block = self.blocks[key]
        if include_self and block.kind != "root":
            yield block
        for part in block.logical_parts:
            if isinstance(part, tuple):
                child = self.blocks[part]
                yield child
                yield from self.walk_logical(part)


def span_text(manifest: GraphManifest, span: SourceSpan | None) -> str:
    """Return an indexed source span with its exact original line endings."""
    if span is None:
        return ""
    document = manifest.documents[span.path]
    return "".join(document.text.splitlines(keepends=True)[span.start_line - 1:span.end_line])


def block_body_text(manifest: GraphManifest, block: DocumentBlock | GraphItem) -> str:
    return span_text(manifest, block.body_span)


@dataclass(frozen=True)
class _ParseProfile:
    name: str
    namespace: str
    roots: tuple[tuple[str, str], ...]
    allowed_memory_kinds: frozenset[str]
    allow_focus: bool
    require_addressed_body: bool


def build_graph_manifest(memory_root: Path) -> GraphManifest:
    root = Path(memory_root).resolve()
    profile = _ParseProfile(
        name="local",
        namespace="local",
        roots=ROOT_FILES,
        allowed_memory_kinds=frozenset({"#", "F#", "M#", "S#", "MF#", "MQ#"}),
        allow_focus=True,
        require_addressed_body=False,
    )
    return _build_manifest(root, profile)


def build_mf_manifest(package_root: Path, view_id: str) -> GraphManifest:
    """Build an MF-local graph from a package's dist directory."""
    validate_item_id(view_id)
    root = Path(package_root).resolve()
    profile = _ParseProfile(
        name="mf",
        namespace=f"MF#{view_id}",
        roots=(("MEMORY.md", "memory"),),
        allowed_memory_kinds=frozenset({"#", "F#", "M#", "S#"}),
        allow_focus=False,
        require_addressed_body=True,
    )
    return _build_manifest(root, profile)


def resolve_backing_reference(memory_root: Path, item_id: str, kind: str) -> BackingReference | None:
    manifest = build_graph_manifest(memory_root)
    reference = manifest.backing.get(item_id)
    if reference is None or reference.kind != kind:
        return None
    return reference


def _build_manifest(root: Path, profile: _ParseProfile) -> GraphManifest:
    manifest = GraphManifest(root=root, namespace=profile.namespace, profile=profile.name)
    for name, family in profile.roots:
        path = root / name
        if not path.exists():
            if profile.name == "local":
                _add_error(manifest, f"missing canonical RightMemory root `{name}`")
            else:
                _add_error(manifest, "missing canonical MF Memory document `MEMORY.md`")
            continue
        root_key = _load_document(manifest, profile, path, family, path_stack=())
        if root_key is not None:
            manifest.root_blocks.append(root_key)

    _validate_items(manifest)
    if profile.allow_focus:
        _validate_focus(manifest)
    if profile.require_addressed_body:
        _validate_addressed_body(manifest)
    _assign_logical_metadata(manifest)
    manifest.graph_files.sort()
    manifest.non_graph_files = sorted(set(manifest.non_graph_files))
    return manifest


def _load_document(
    manifest: GraphManifest,
    profile: _ParseProfile,
    file_path: Path,
    family: str,
    *,
    path_stack: tuple[Path, ...],
) -> BlockKey | None:
    relative = _relative(manifest.root, file_path)
    if not _is_regular_file_under_root(manifest.root, file_path):
        _add_error(
            manifest,
            f"graph file `{relative}` must be a regular file inside the RightMemory root",
            file_path,
        )
        return None
    resolved = file_path.resolve(strict=True)
    existing = manifest.documents.get(resolved)
    if existing is not None:
        return existing.root_key

    text = _read_text(resolved)
    lines = tuple(text.splitlines())
    root_key = (resolved, 0)
    root_block = DocumentBlock(
        key=root_key,
        kind="root",
        source_path=resolved,
        family=family,
        end_line=len(lines),
    )
    manifest.blocks[root_key] = root_block
    document = ParsedDocument(
        path=resolved,
        relative_path=relative,
        text=text,
        lines=lines,
        family=family,
        source_order=len(manifest.documents),
        root_key=root_key,
    )
    manifest.documents[resolved] = document
    manifest.document_roots.append(root_key)
    manifest.graph_files.append(resolved)

    f_references = _parse_document(manifest, profile, document)
    _finalize_document(manifest, document)
    next_stack = (*path_stack, resolved)
    for owner_key, reference in f_references:
        if not _is_regular_file_under_root(manifest.root, reference.path):
            continue
        target = reference.path.resolve(strict=True)
        if target in next_stack:
            owner = manifest.blocks[owner_key]
            _add_error(
                manifest,
                f"cyclic F# backing path `{_relative(manifest.root, reference.path)}` "
                f"for heading `{reference.id}` at {_block_loc(manifest, owner)}",
                owner.source_path,
                owner.line_number,
            )
            continue
        detail_root = _load_document(
            manifest,
            profile,
            reference.path,
            reference.family,
            path_stack=next_stack,
        )
        if detail_root is not None:
            _attach_detail_document(manifest, owner_key, detail_root)
    return root_key


def _parse_document(
    manifest: GraphManifest,
    profile: _ParseProfile,
    document: ParsedDocument,
) -> list[tuple[BlockKey, BackingReference]]:
    stack: list[BlockKey] = []
    in_focus = False
    in_pursuit_next = False
    fence_char: str | None = None
    fence_length = 0
    f_references: list[tuple[BlockKey, BackingReference]] = []

    for line_number, line in enumerate(document.lines, start=1):
        parent_key = stack[-1] if stack else document.root_key
        fence = FENCE_RE.match(line)
        if fence is not None:
            _append_text(manifest.blocks[parent_key], line, line_number)
            marker = fence.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is not None:
            _append_text(manifest.blocks[parent_key], line, line_number)
            continue

        heading_match = HEADING_RE.match(line)
        if heading_match is not None:
            depth = len(heading_match.group(1))
            while stack and manifest.blocks[stack[-1]].depth >= depth:
                manifest.blocks[stack.pop()].end_line = line_number - 1
            parent_key = stack[-1] if stack else document.root_key
            parent = manifest.blocks[parent_key]
            anchor = ANCHOR_RE.match(line)
            candidate = ANCHOR_CANDIDATE_RE.match(line)
            unsupported = UNSUPPORTED_ANCHOR_RE.match(line)
            anchor_kind: str | None = None
            item_id: str | None = None
            edge_text = ""
            if anchor is not None:
                anchor_kind = anchor.group(2)
                item_id = anchor.group(3)
                edge_text = anchor.group(4) or ""
            elif candidate is not None:
                anchor_kind = candidate.group(2)
                candidate_id = candidate.group(3)
                if is_valid_item_id(candidate_id):
                    item_id = candidate_id
                    _add_error(
                        manifest,
                        f"malformed heading edge list at {document.relative_path}:{line_number}",
                        document.path,
                        line_number,
                    )
                else:
                    _add_error(
                        manifest,
                        f"invalid heading id `{candidate_id}` at {document.relative_path}:{line_number}; "
                        "use only letters, numbers, dot, underscore, or dash",
                        document.path,
                        line_number,
                    )
            elif unsupported is not None:
                _add_error(
                    manifest,
                    f"unsupported heading marker `{unsupported.group(2)}` at "
                    f"{document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )

            addressed = anchor or candidate or unsupported
            title_end = addressed.start(2) - 1 if addressed is not None else len(line)
            indexed_title = line[heading_match.end():title_end].strip()

            key = (document.path, line_number)
            block = DocumentBlock(
                key=key,
                kind="heading",
                line=line,
                title=indexed_title,
                depth=depth,
                line_number=line_number,
                end_line=line_number,
                item_id=item_id,
                item_kind="heading" if item_id is not None else None,
                anchor_kind=anchor_kind,
                family=document.family,
                source_path=document.path,
                physical_parent=parent_key,
                logical_parent=parent_key,
            )
            manifest.blocks[key] = block
            _append_child(parent, key)

            f_ancestor = _nearest_ancestor(manifest, stack, lambda item: item.anchor_kind == "F#")
            if f_ancestor is not None:
                _add_error(
                    manifest,
                    f"F# heading cannot retain child headings at {document.relative_path}:{line_number}; "
                    f"move them to the backing file for `{f_ancestor.item_id}`",
                    document.path,
                    line_number,
                )
            terminal_ancestor = _nearest_ancestor(
                manifest,
                stack,
                lambda item: item.depth == 4 and item.anchor_kind in TERMINAL_HEADING_KINDS,
            )
            if terminal_ancestor is not None:
                _add_error(
                    manifest,
                    f"terminal `####` heading cannot contain child headings at "
                    f"{document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )

            if depth > 4:
                _add_error(
                    manifest,
                    f"headings deeper than `####` are not allowed at {document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )
            elif depth == 4:
                if anchor_kind not in TERMINAL_HEADING_KINDS or item_id is None:
                    _add_error(
                        manifest,
                        "`####` terminal reference must use `{F#slug}`, `{M#slug}`, `{S#slug}`, "
                        f"`{{MF#slug}}`, or `{{MQ#slug}}` at {document.relative_path}:{line_number}",
                        document.path,
                        line_number,
                    )
                if parent.depth != 3:
                    _add_error(
                        manifest,
                        f"`####` terminal reference must be under a `###` heading at "
                        f"{document.relative_path}:{line_number}",
                        document.path,
                        line_number,
                    )

            if document.family == "pursuit" and depth <= 2:
                in_focus = bool(profile.allow_focus and FOCUS_HEADING_RE.match(line))
            if document.family == "pursuit":
                in_pursuit_next = False

            if item_id is not None:
                edges, malformed = _parse_edges(edge_text)
                item = GraphItem(
                    id=item_id,
                    file=document.path,
                    line_number=line_number,
                    family=document.family,
                    item_kind="heading",
                    anchor_kind=anchor_kind,
                    edges=tuple(edges),
                    title=block.title,
                    malformed_edges=tuple(malformed),
                    block_key=key,
                    physical_parent=parent_key,
                    logical_parent=parent_key,
                )
                _record_item(manifest, item)
                if anchor_kind not in profile.allowed_memory_kinds and document.family == "memory":
                    _add_error(
                        manifest,
                        f"{anchor_kind} heading `{item_id}` is not valid in {profile.namespace} at "
                        f"{_loc(manifest.root, item)}",
                        document.path,
                        line_number,
                    )
                if anchor_kind in MEMORY_ONLY_ANCHOR_KINDS and document.family != "memory":
                    _add_error(
                        manifest,
                        f"{anchor_kind} heading `{item_id}` is only valid in Memory at {_loc(manifest.root, item)}",
                        document.path,
                        line_number,
                    )
                if anchor_kind == "F#" and document.family == "pursuit" and item_id.casefold() == "rules":
                    _add_error(
                        manifest,
                        f"Pursuit F# id `{item_id}` remains reserved for the legacy root reference path at "
                        f"{_loc(manifest.root, item)}",
                        document.path,
                        line_number,
                    )
                else:
                    reference = _backing_reference(manifest.root, item)
                    if reference is not None:
                        _record_backing(manifest, reference, item)
                        if reference.kind == "F#":
                            f_references.append((key, reference))

            stack.append(key)
            continue

        parent_key = stack[-1] if stack else document.root_key
        parent = manifest.blocks[parent_key]
        if document.family == "pursuit":
            field_match = LEGACY_PURSUIT_FIELD_RE.match(line)
            if field_match:
                in_pursuit_next = field_match.group(1).casefold() == "next"
                _append_text(parent, line, line_number)
                continue
        if in_focus:
            focus_candidate = FOCUS_CANDIDATE_RE.match(line)
            if focus_candidate is not None:
                focus_id = focus_candidate.group(1)
                if not is_valid_item_id(focus_id):
                    _add_error(
                        manifest,
                        f"invalid Focus reference id `{focus_id}` at {document.relative_path}:{line_number}",
                        document.path,
                        line_number,
                    )
                    _append_text(parent, line, line_number)
                    continue
                key = (document.path, line_number)
                block = DocumentBlock(
                    key=key,
                    kind="focus",
                    line=line,
                    line_number=line_number,
                    end_line=line_number,
                    family=document.family,
                    source_path=document.path,
                    focus_target=focus_id,
                    physical_parent=parent_key,
                    logical_parent=parent_key,
                )
                manifest.blocks[key] = block
                _append_child(parent, key)
                manifest.focus_ids.append((focus_id, document.path, line_number))
                manifest.focus_blocks.append(key)
                continue

        if document.family == "pursuit" and in_pursuit_next:
            # Older roots used Next lists. Retain every line as unstructured body
            # until another legacy field or heading, including unknown actions.
            _append_text(parent, line, line_number)
            continue

        node_candidate = NODE_CANDIDATE_RE.match(line)
        if node_candidate is not None:
            terminal_ancestor = _nearest_ancestor(
                manifest,
                stack,
                lambda item: item.depth == 4 and item.anchor_kind in TERMINAL_HEADING_KINDS,
            )
            if terminal_ancestor is not None:
                _add_error(
                    manifest,
                    f"terminal `####` heading cannot contain node lines at "
                    f"{document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )
            f_ancestor = _nearest_ancestor(manifest, stack, lambda item: item.anchor_kind == "F#")
            if f_ancestor is not None:
                _add_error(
                    manifest,
                    f"F# heading cannot retain child node lines at {document.relative_path}:{line_number}; "
                    f"move them to the backing file for `{f_ancestor.item_id}`",
                    document.path,
                    line_number,
                )
            node = NODE_RE.match(line)
            node_id = node_candidate.group(1)
            if not is_valid_item_id(node_id):
                _add_error(
                    manifest,
                    f"invalid node id `{node_id}` at {document.relative_path}:{line_number}; "
                    "use only letters, numbers, dot, underscore, or dash",
                    document.path,
                    line_number,
                )
                _append_text(parent, line, line_number)
                continue
            if node is None:
                _add_error(
                    manifest,
                    f"node `{node_id}` must include an edge list such as `\u2192 []` at "
                    f"{document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )
                _append_text(parent, line, line_number)
                continue
            edges, malformed = _parse_edges(node.group(2) or "")
            prose = _node_prose(line, node)
            key = (document.path, line_number)
            block = DocumentBlock(
                key=key,
                kind="node",
                line=line,
                title=prose or node_id,
                prose=prose,
                line_number=line_number,
                end_line=line_number,
                item_id=node_id,
                item_kind="node",
                family=document.family,
                source_path=document.path,
                physical_parent=parent_key,
                logical_parent=parent_key,
            )
            manifest.blocks[key] = block
            _append_child(parent, key)
            _record_item(
                manifest,
                GraphItem(
                    id=node_id,
                    file=document.path,
                    line_number=line_number,
                    family=document.family,
                    item_kind="node",
                    anchor_kind=None,
                    edges=tuple(edges),
                    title=block.title,
                    prose=block.prose,
                    malformed_edges=tuple(malformed),
                    block_key=key,
                    end_line=line_number,
                    physical_parent=parent_key,
                    logical_parent=parent_key,
                ),
            )
            continue

        _append_text(parent, line, line_number)

    for key in stack:
        manifest.blocks[key].end_line = len(document.lines)
    return f_references


def _append_text(block: DocumentBlock, line: str, line_number: int) -> None:
    source_part = SourceTextPart(block.source_path, line_number, line)
    block.physical_parts.append(line)
    block.logical_parts.append(line)
    block.logical_text_parts.append(source_part)


def _node_prose(line: str, match: re.Match[str]) -> str:
    """Return the prose owned by a parsed graph-node line."""
    first_tick = line.index("`")
    prose_start = line.index("`", first_tick + 1) + 1
    prose_end = match.start(2) - 1
    prose = line[prose_start:prose_end].rstrip()
    for arrow in ("\u2192", "->"):
        if prose.endswith(arrow):
            prose = prose[:-len(arrow)].rstrip()
            break
    return prose.strip()


def _append_child(parent: DocumentBlock, key: BlockKey) -> None:
    parent.physical_children.append(key)
    parent.logical_children.append(key)
    parent.physical_parts.append(key)
    parent.logical_parts.append(key)


def _nearest_ancestor(
    manifest: GraphManifest,
    stack: list[BlockKey],
    predicate,
) -> DocumentBlock | None:
    for key in reversed(stack):
        block = manifest.blocks[key]
        if predicate(block):
            return block
    return None


def _finalize_document(manifest: GraphManifest, document: ParsedDocument) -> None:
    root = manifest.blocks[document.root_key]
    first_root_heading = next(
        (
            manifest.blocks[child].line_number
            for child in root.physical_children
            if manifest.blocks[child].kind == "heading"
        ),
        None,
    )
    root_body_end = (first_root_heading - 1) if first_root_heading is not None else len(document.lines)
    if root_body_end >= 1:
        root.body_span = SourceSpan(document.path, 1, root_body_end)
    for key, block in manifest.blocks.items():
        if key[0] != document.path or block.kind == "root":
            continue
        if block.kind == "heading":
            first_child_heading = next(
                (
                    manifest.blocks[child].line_number
                    for child in block.physical_children
                    if manifest.blocks[child].kind == "heading"
                ),
                None,
            )
            body_start = block.line_number + 1
            body_end = (first_child_heading - 1) if first_child_heading is not None else block.end_line
            if body_start <= body_end:
                block.body_span = SourceSpan(document.path, body_start, body_end)
        item = manifest.items.get(block.item_id or "")
        if item is not None and item.block_key == key:
            item.end_line = block.end_line
            item.body_span = block.body_span


def _record_item(manifest: GraphManifest, item: GraphItem) -> None:
    previous = manifest.items.get(item.id)
    if previous is not None:
        manifest.duplicates.add(item.id)
        _add_error(
            manifest,
            f"duplicate id `{item.id}` at {_loc(manifest.root, item)}; "
            f"first seen at {_loc(manifest.root, previous)}",
            item.file,
            item.line_number,
        )
        return
    manifest.items[item.id] = item
    if item.item_kind == "heading":
        manifest.headings[item.id] = item


def _record_backing(manifest: GraphManifest, reference: BackingReference, item: GraphItem) -> None:
    manifest.backing.setdefault(item.id, reference)
    backing_path_key = _backing_path_key(manifest.root, reference.path)
    previous = manifest.backing_paths.get(backing_path_key)
    if previous is not None and (previous.id != reference.id or previous.kind != reference.kind):
        _add_error(
            manifest,
            f"backing file `{_relative(manifest.root, reference.path)}` is claimed by "
            f"{previous.kind} heading `{previous.id}` at "
            f"{_relative(manifest.root, previous.source_file)}:{previous.line_number} and "
            f"{reference.kind} heading `{reference.id}` at "
            f"{_relative(manifest.root, reference.source_file)}:{reference.line_number}",
            reference.source_file,
            reference.line_number,
        )
    else:
        manifest.backing_paths[backing_path_key] = reference

    if not _is_regular_file_under_root(manifest.root, reference.path):
        relative = _relative(manifest.root, reference.path)
        if reference.kind == "S#":
            message = f"missing skill file `{relative}`"
        elif reference.kind == "M#":
            message = f"missing Markdown backing file `{relative}`"
        else:
            message = f"missing F# backing file `{relative}`"
        _add_error(
            manifest,
            f"{message} for heading at {_loc(manifest.root, item)}",
            item.file,
            item.line_number,
        )
    elif reference.kind != "F#":
        manifest.non_graph_files.append(reference.path.resolve(strict=True))


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


def _attach_detail_document(manifest: GraphManifest, owner_key: BlockKey, detail_root_key: BlockKey) -> None:
    owner = manifest.blocks[owner_key]
    detail_root = manifest.blocks[detail_root_key]
    detail_parts = list(detail_root.logical_parts)
    if not detail_parts:
        return
    if not owner.logical_parts or not isinstance(owner.logical_parts[-1], str) or owner.logical_parts[-1].strip():
        owner.logical_parts.append("")
    for part in detail_parts:
        if isinstance(part, tuple):
            child = manifest.blocks[part]
            if child.logical_parent not in {detail_root_key, owner_key}:
                _add_error(
                    manifest,
                    f"graph file `{_relative(manifest.root, child.source_path)}` is attached by more than one F# heading",
                    owner.source_path,
                    owner.line_number,
                )
                continue
            child.logical_parent = owner_key
            owner.logical_children.append(part)
            _update_item_logical_parent(manifest, child)
        owner.logical_parts.append(part)
    owner.logical_text_parts.extend(detail_root.logical_text_parts)


def _update_item_logical_parent(manifest: GraphManifest, block: DocumentBlock) -> None:
    if block.item_id is None:
        return
    item = manifest.items.get(block.item_id)
    if item is not None and item.block_key == block.key:
        item.logical_parent = block.logical_parent


def _validate_items(manifest: GraphManifest) -> None:
    for item in manifest.items.values():
        seen_edges: set[tuple[str, str]] = set()
        for malformed_edge in item.malformed_edges:
            _add_error(
                manifest,
                f"malformed edge `{malformed_edge}` at {_loc(manifest.root, item)}",
                item.file,
                item.line_number,
            )
        for edge_type, target in item.edges:
            edge = (edge_type, target)
            if edge in seen_edges:
                _add_error(
                    manifest,
                    f"duplicate edge `{edge_type}:{target}` at {_loc(manifest.root, item)}",
                    item.file,
                    item.line_number,
                )
                continue
            seen_edges.add(edge)
            if target == item.id:
                _add_error(
                    manifest,
                    f"self-edge `{edge_type}:{target}` at {_loc(manifest.root, item)}",
                    item.file,
                    item.line_number,
                )
            elif edge_type not in KNOWN_EDGE_TYPES:
                _add_error(
                    manifest,
                    f"unknown edge type `{edge_type}` at {_loc(manifest.root, item)}",
                    item.file,
                    item.line_number,
                )
            elif target not in manifest.items:
                _add_error(
                    manifest,
                    f"dangling edge `{edge_type}:{target}` at {_loc(manifest.root, item)}",
                    item.file,
                    item.line_number,
                )
            elif edge_type == "rel" and _has_logical_ancestor_heading(manifest, item, target):
                _add_error(
                    manifest,
                    f"containment-only `rel:` edge from source item `{item.id}` to ancestor heading "
                    f"`{target}` at {_loc(manifest.root, item)}; remove the edge because logical "
                    "heading nesting already expresses this relationship",
                    item.file,
                    item.line_number,
                )


def _has_logical_ancestor_heading(
    manifest: GraphManifest,
    item: GraphItem,
    target_id: str,
) -> bool:
    current = item.logical_parent
    while current is not None:
        block = manifest.blocks[current]
        if block.kind == "heading" and block.item_id == target_id:
            return True
        current = block.logical_parent
    return False


def _validate_focus(manifest: GraphManifest) -> None:
    seen: set[str] = set()
    for item_id, path, line_number in manifest.focus_ids:
        relative = _relative(manifest.root, path)
        if item_id in seen:
            _add_error(
                manifest,
                f"duplicate Focus reference `{item_id}` at {relative}:{line_number}",
                path,
                line_number,
            )
            continue
        seen.add(item_id)
        item = manifest.items.get(item_id)
        if item is None:
            _add_error(
                manifest,
                f"dangling Focus reference `{item_id}` at {relative}:{line_number}",
                path,
                line_number,
            )
        elif item.item_kind != "heading" or item.family != "pursuit":
            _add_error(
                manifest,
                f"Focus reference `{item_id}` must target a Pursuit heading at {relative}:{line_number}",
                path,
                line_number,
            )


def _validate_addressed_body(manifest: GraphManifest) -> None:
    for document in manifest.documents.values():
        structured_lines = {
            block.line_number
            for key, block in manifest.blocks.items()
            if key[0] == document.path and block.kind in {"node", "focus"}
        }
        for key, block in manifest.blocks.items():
            if key[0] != document.path or block.kind not in {"root", "heading"}:
                continue
            if block.kind == "heading" and block.item_id is not None:
                continue
            offending_line = _first_nonblank_body_line(block, document, structured_lines)
            if offending_line is not None:
                _add_error(
                    manifest,
                    f"MF document prose must belong to an addressable heading at "
                    f"{document.relative_path}:{offending_line}",
                    document.path,
                    offending_line,
                )


def _first_nonblank_body_line(
    block: DocumentBlock,
    document: ParsedDocument,
    structured_lines: set[int],
) -> int | None:
    if block.body_span is None:
        return None
    for line_number in range(block.body_span.start_line, block.body_span.end_line + 1):
        if line_number in structured_lines:
            continue
        if document.lines[line_number - 1].strip():
            return line_number
    return None


def _assign_logical_metadata(manifest: GraphManifest) -> None:
    rank = 0
    seen: set[BlockKey] = set()
    for root_key in manifest.root_blocks:
        for block in manifest.walk_logical(root_key):
            if block.key in seen:
                continue
            seen.add(block.key)
            block.traversal_rank = rank
            rank += 1
            if block.item_id is not None:
                item = manifest.items.get(block.item_id)
                if item is not None and item.block_key == block.key:
                    item.traversal_rank = block.traversal_rank
                    item.logical_parent = block.logical_parent
    for item in manifest.items.values():
        if item.block_key is not None:
            item.content_hash = hashlib.sha256(
                _flatten_logical_block(manifest, item.block_key).rstrip("\r\n").encode("utf-8")
            ).hexdigest()


def _flatten_logical_block(manifest: GraphManifest, key: BlockKey) -> str:
    block = manifest.blocks[key]
    lines = [block.line] if block.kind != "root" else []
    for part in block.logical_parts:
        if isinstance(part, tuple):
            lines.extend(_flatten_logical_block(manifest, part).splitlines())
        else:
            lines.append(part)
    return "\n".join(lines)


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


def _add_error(
    manifest: GraphManifest,
    message: str,
    path: Path | None = None,
    line_number: int | None = None,
) -> None:
    manifest.errors.append(message)
    manifest.diagnostics.append(
        GraphDiagnostic(
            message=message,
            namespace=manifest.namespace,
            path=_relative(manifest.root, path) if path is not None else None,
            line_number=line_number,
        )
    )


def _block_loc(manifest: GraphManifest, block: DocumentBlock) -> str:
    return f"{_relative(manifest.root, block.source_path)}:{block.line_number}"


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


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()
