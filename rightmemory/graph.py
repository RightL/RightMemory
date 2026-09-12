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
    rf"^ {{0,3}}(#{{1,}})[ \t]+.*?\{{(F#|M#|S#|MF#|MQ#|#)({ITEM_ID_PATTERN})\}}"
    r"(?:\s*(?:\u2192|->)\s*\[(.*?)\])?(?:[ \t]+#+)?[ \t]*$"
)
ANCHOR_CANDIDATE_RE = re.compile(
    r"^ {0,3}(#{1,})[ \t]+.*?\{(F#|M#|S#|MF#|MQ#|#)([^}]*)\}(?=\s*(?:(?:→|->)\s*\[.*|#+)?$)"
)
UNSUPPORTED_ANCHOR_RE = re.compile(r"^ {0,3}(#{1,})[ \t]+.*?\{([A-Za-z]+#)([^}]*)\}(?=\s*(?:(?:→|->)\s*\[.*|#+)?$)")
NODE_RE = re.compile(r"^\s*(?:[-+*]|[0-9]{1,9}[.)])\s+`([^`]+)`.*(?:\s(?:\u2192|->)\s*\[(.*?)\])\s*$")
NODE_CANDIDATE_RE = re.compile(r"^\s*(?:[-+*]|[0-9]{1,9}[.)])\s+`(?!`)([^`]*)`(?!`)(?:\s|$)")
EDGE_RE = re.compile(rf"^\s*([A-Za-z][A-Za-z0-9_-]*):\s*({ITEM_ID_PATTERN})\s*$")
FOCUS_HEADING_RE = re.compile(r"^ {0,3}##[ \t]+Focus(?:[ \t]+#+)?[ \t]*$", re.IGNORECASE)
FOCUS_REFERENCE_RE = re.compile(rf"^\s*-\s+`({ITEM_ID_PATTERN})`(?:\s|$)")
FOCUS_CANDIDATE_RE = re.compile(r"^\s*-\s+`([^`]+)`(?:\s|$)")
HEADING_RE = re.compile(r"^ {0,3}(#+)(?:[ \t]+|$)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
BODY_OPEN_RE = re.compile(r"^(:{3,})body[ \t]*$")
BODY_CLOSE_RE = re.compile(r"^:{3,}[ \t]*$")
LIST_PREFIX_RE = re.compile(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]+|$)")

MEMORY_DETAIL_FILE_RE = re.compile(rf"^MEMORY_{ITEM_ID_PATTERN}\.md$")
MEMORY_SKILL_FILE_RE = re.compile(rf"^MEMORY_SKILL_{ITEM_ID_PATTERN}\.md$")
PURSUIT_DETAIL_FILE_RE = re.compile(
    rf"^PURSUIT_(?!(?i:RULES)\.md$){ITEM_ID_PATTERN}\.md$"
)

ROOT_FILES = (("MEMORY.md", "memory"), ("PURSUITS.md", "pursuit"))
MEMORY_ONLY_ANCHOR_KINDS = {"M#", "S#", "MF#", "MQ#"}
TERMINAL_HEADING_KINDS = {"F#", "M#", "S#", "MF#", "MQ#"}

BlockKey = tuple[Path, int]


class BodyFenceDelimiter(str):
    """A source delimiter retained for editing and omitted from rendered content."""


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


def _literal_mask(line: str) -> str:
    """Mask escaped characters and complete inline code spans, preserving offsets."""
    chars = list(line)
    index = 0
    while index < len(line):
        if line[index] == "\\" and index + 1 < len(line):
            chars[index:index + 2] = "  "
            index += 2
        elif line[index] == "`":
            run = re.match(r"`+", line[index:]).group()
            close = re.search(r"(?<!`)" + run + r"(?!`)", line[index + len(run):])
            if close is None:
                index += len(run)
            else:
                end = index + len(run) + close.end()
                chars[index:end] = " " * (end - index)
                index = end
        else:
            index += 1
    return "".join(chars)


def _heading_declarations(line: str):
    mask = _literal_mask(line)
    matches = []
    for regex in (ANCHOR_RE, ANCHOR_CANDIDATE_RE, UNSUPPORTED_ANCHOR_RE):
        match = regex.match(line)
        if match is not None and mask[match.start(2) - 1:match.start(2)] != "{":
            match = None
        if match is not None and regex is ANCHOR_RE and match.group(4) is not None:
            if mask[match.start(4) - 1] != "[":
                match = None
        matches.append(match)
    return tuple(matches)


def _node_declaration(line: str):
    candidate = NODE_CANDIDATE_RE.match(line)
    if candidate is None:
        return None, None
    mask = _literal_mask(line)
    markers = list(re.finditer(r"(?:→|->)\s*\[", mask[candidate.end():]))
    if not markers:
        return None, None
    node = NODE_RE.match(line)
    if node is not None and node.start(2) - 1 != candidate.end() + markers[-1].end() - 1:
        node = None
    return candidate, node


def parse_addressable_heading(line: str) -> AddressableHeading | None:
    """Read a heading using the same grammar as the canonical document index."""
    match, _, _ = _heading_declarations(line.rstrip("\r\n"))
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
    return re.sub(r"[ \t]+#+[ \t]*$", "", line[heading.end():]).strip()


def validate_heading_title(title: str) -> str:
    """Reject titles that would change the structural meaning of a heading."""
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    title = title.strip()
    if "\n" in title or "\r" in title or "\x00" in title:
        raise ValueError("title must fit on one line")
    probe = f"## {title}"
    if any(_heading_declarations(probe)):
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
    """Patch a heading's title, retaining any existing address and edge suffix."""
    title = validate_heading_title(title)
    anchor, _, _ = _heading_declarations(line.rstrip("\r\n"))
    heading = HEADING_RE.match(line)
    if heading is None:
        raise ValueError("expected a Markdown heading")
    if anchor is None:
        ending = line[len(line.rstrip("\r\n")):]
        return line[:heading.end()].rstrip(" \t") + (" " + title if title else "") + ending
    title_end = anchor.start(2) - 1
    old_title = line[heading.end():title_end]
    spacing = old_title[len(old_title.rstrip()):]
    return line[:heading.end()] + title + spacing + line[title_end:]


def remove_edge_targets(line: str, deleted_ids: set[str] | frozenset[str]) -> str:
    """Remove selected typed edges without changing prose, other edges, or newlines."""
    source = line.rstrip("\r\n")
    match, _, _ = _heading_declarations(source)
    group = 4
    if match is None:
        _, match = _node_declaration(source)
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
    body_delimiters: set[int] = field(default_factory=set)
    reference_spans: list[SourceSpan] = field(default_factory=list)
    references: dict = field(default_factory=dict)


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
    snapshot_hash: str = ""

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
    """Source-editable own body, with separation across backing boundaries."""
    if isinstance(block, GraphItem):
        block = manifest.blocks[block.block_key]
    pieces = []
    for part in block.logical_text_parts:
        if part.line_number == 0:
            pieces.append("\n" if not pieces or pieces[-1].endswith(("\n", "\r")) else "\n\n")
        else:
            pieces.append(span_text(manifest, SourceSpan(part.source_path, part.line_number, part.line_number)))
    return "".join(pieces)


def snapshot_block_id(manifest: GraphManifest, block: DocumentBlock) -> str:
    """Anonymous handles identify one exact snapshot, never persistent identity."""
    if block.item_id is not None:
        return block.item_id
    relative = block.source_path.relative_to(manifest.root).as_posix()
    return f"plain:{relative}:{block.line_number}:{manifest.snapshot_hash}"


def _reference_parser(*, body_fences: bool = True):
    from markdown_it import MarkdownIt
    from markdown_it.helpers import parseLinkLabel
    from markdown_it.rules_inline import image, link

    parser = MarkdownIt("commonmark", {"store_labels": True}).enable("table")

    def trace(rule, image_rule=False):
        def wrapped(state, silent):
            if state.src[state.pos] != ("!" if image_rule else "["):
                return False
            start, first = state.pos, len(state.tokens)
            label_end = parseLinkLabel(state, start + int(image_rule), not image_rule)
            accepted = rule(state, silent)
            if accepted and not silent and first < len(state.tokens):
                expected = "image" if image_rule else "link_open"
                token = next((item for item in state.tokens[first:] if item.type == expected), None)
                if token is not None and token.meta.get("label") is not None:
                    token.meta["reference_suffix"] = (label_end + 1, state.pos)
            return accepted
        return wrapped

    parser.inline.ruler.at("link", trace(link))
    parser.inline.ruler.at("image", trace(image, True))
    if body_fences:
        parser.block.ruler.before("fence", "rightmemory_body", _body_fence_rule,
                                 {"alt": ["paragraph", "reference", "blockquote", "list"]})
    return parser


def resolve_markdown_references(text: str, document: ParsedDocument, *, _body_fences: bool = True) -> str:
    """Make extracted Markdown independent of document-local link definitions.

    Only references recognized by CommonMark are rewritten, as inline links.
    Code, HTML, escaping, visible link text, and all other source text survive.
    Graph files and their backings are siblings, so relative resource bases stay
    the same across their physical boundaries.
    """
    if not document.references:
        return text
    parser = _reference_parser(body_fences=_body_fences)
    environment = {"references": dict(document.references)}
    tokens = parser.parse(text, environment)
    lines = text.splitlines(keepends=True)
    starts, offset = [], 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    replacements = []
    in_cell = False
    cell_offsets: dict[int, int] = {}
    for token in tokens:
        if token.type in {"th_open", "td_open"}:
            in_cell = True
        elif token.type in {"th_close", "td_close"}:
            in_cell = False
        if token.type == "rightmemory_body" and token.map and not token.meta["error"]:
            start, end = token.map
            payload_start, payload_end = starts[start + 1], starts[end - 1]
            replacements.append((payload_start, payload_end,
                                 resolve_markdown_references(text[payload_start:payload_end], document, _body_fences=False)))
            continue
        if token.type != "inline" or token.map is None:
            continue
        if not in_cell and not any("reference_suffix" in child.meta for child in token.children or []):
            continue
        fragments = token.content.split("\n")
        locations = []
        content_offset = 0
        for number, fragment in enumerate(fragments, token.map[0]):
            if number >= len(lines):
                break
            raw = lines[number]
            positions = [index for index in range(len(raw))
                         if not (in_cell and raw[index:index + 2] == "\\|")]
            normalized = "".join(raw[index] for index in positions)
            column = normalized.find(fragment, cell_offsets.get(number, 0) if in_cell else 0)
            if column < 0:
                raise ValueError("cannot preserve a Markdown reference source span")
            if in_cell:
                cell_offsets[number] = column + len(fragment)
            mapped = positions[column:column + len(fragment)]
            mapped.append(mapped[-1] + 1 if mapped else (positions[column] if column < len(positions) else len(raw)))
            locations.append((content_offset, [starts[number] + position for position in mapped]))
            content_offset += len(fragment) + 1

        def source_offset(position):
            begin, source_positions = next(pair for pair in reversed(locations) if pair[0] <= position)
            return source_positions[position - begin]

        for child in token.children or []:
            suffix = child.meta.get("reference_suffix")
            if suffix is None:
                continue
            href = child.attrGet("src" if child.type == "image" else "href") or ""
            href = href.replace("<", "%3C").replace(">", "%3E")
            title = child.attrGet("title")
            destination = "(<" + href + ">"
            if title:
                destination += ' "' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
            if in_cell:
                destination = destination.replace("|", "\\|")
            replacements.append((source_offset(suffix[0]), source_offset(suffix[1]), destination + ")"))
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def rendered_block_parts(manifest: GraphManifest, block: DocumentBlock, *, physical: bool = False,
                         retain_fences: bool = False) -> Iterable[str | BlockKey]:
    """Project source-aware owned Markdown while preserving child positions."""
    text_parts = iter(part for part in block.logical_text_parts
                      if not physical or part.source_path == block.source_path)
    pending: list[str] = []
    source_path = block.source_path

    def flush():
        text = resolve_markdown_references("\n".join(pending), manifest.documents[source_path])
        pending.clear()
        return text

    for part in block.physical_parts if physical else block.logical_parts:
        if isinstance(part, tuple):
            if pending:
                yield flush()
            yield part
            continue
        source = next(text_parts)
        if source.source_path != source_path or isinstance(part, BodyFenceDelimiter):
            if pending:
                yield flush()
            source_path = source.source_path
        if isinstance(part, BodyFenceDelimiter):
            if retain_fences:
                yield str(part)
        else:
            pending.append(part)
    if pending:
        yield flush()


@dataclass(frozen=True)
class _ParseProfile:
    name: str
    namespace: str
    roots: tuple[tuple[str, str], ...]
    allowed_memory_kinds: frozenset[str]
    allow_focus: bool


def build_graph_manifest(memory_root: Path) -> GraphManifest:
    root = Path(memory_root).resolve()
    profile = _ParseProfile(
        name="local",
        namespace="local",
        roots=ROOT_FILES,
        allowed_memory_kinds=frozenset({"#", "F#", "M#", "S#", "MF#", "MQ#"}),
        allow_focus=True,
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
    _assign_logical_metadata(manifest)
    digest = hashlib.sha256()
    for document in sorted(manifest.documents.values(), key=lambda item: item.relative_path):
        digest.update(document.relative_path.encode("utf-8") + b"\0" + document.text.encode("utf-8") + b"\0")
    manifest.snapshot_hash = digest.hexdigest()
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


def _body_fence_rule(state, start_line: int, end_line: int, silent: bool) -> bool:
    # The Markdown parser owns container boundaries. A marker inside a list,
    # quote, HTML block, or code block never reaches this rule at level zero.
    if state.level != 0 or state.tShift[start_line] != 0:
        return False
    line = state.src[state.bMarks[start_line]:state.eMarks[start_line]]
    opener = BODY_OPEN_RE.fullmatch(line)
    if opener is None and BODY_CLOSE_RE.fullmatch(line) is None:
        return False
    if silent:
        return True
    last = start_line
    error = "unmatched closing body fence" if opener is None else "unclosed body fence"
    if opener is not None:
        for last in range(start_line + 1, end_line):
            candidate = state.src[state.bMarks[last]:state.eMarks[last]]
            if candidate.rstrip(" \t") == opener.group(1):
                error = ""
                break
        else:
            last = end_line - 1
    state.line = last + 1
    token = state.push("rightmemory_body", "", 0)
    token.map = [start_line, state.line]
    token.meta = {"error": error}
    return True


def _document_structure(manifest: GraphManifest, document: ParsedDocument) -> dict[int, tuple[str, int]]:
    # Keep this import lazy: the installer bootstrap itself is stdlib-only.
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark").enable("table")
    parser.block.ruler.before("fence", "rightmemory_body", _body_fence_rule,
                             {"alt": ["paragraph", "reference", "blockquote", "list"]})
    environment: dict = {}
    tokens = parser.parse(document.text, environment)
    structure = {}
    for token in tokens:
        if token.map is None:
            continue
        start, end = token.map
        if token.type == "heading_open" and token.level == 0 and token.markup.startswith("#"):
            structure[start + 1] = ("heading", end)
        elif token.type == "list_item_open" and token.level == 1:
            structure[start + 1] = ("node", end)
        elif token.type == "rightmemory_body":
            document.body_delimiters.add(start + 1)
            if token.meta["error"]:
                _add_error(manifest, f"{token.meta['error']} at {document.relative_path}:{start + 1}",
                           document.path, start + 1)
            else:
                document.body_delimiters.add(end)
                payload_environment: dict = {}
                MarkdownIt("commonmark").enable("table").parse(
                    "\n".join(document.lines[start + 1:end - 1]), payload_environment)
                for label, reference in payload_environment.get("references", {}).items():
                    reference["map"] = [number + start + 1 for number in reference["map"]]
                    existing = environment.setdefault("references", {}).get(label)
                    if existing is None or existing["map"][0] > reference["map"][0]:
                        environment["references"][label] = reference
    document.references.update(environment.get("references", {}))
    for reference in environment.get("references", {}).values():
        if "map" in reference:
            start, end = reference["map"]
            document.reference_spans.append(SourceSpan(document.path, start + 1, end))
    return structure


def _parse_document(
    manifest: GraphManifest,
    profile: _ParseProfile,
    document: ParsedDocument,
) -> list[tuple[BlockKey, BackingReference]]:
    stack: list[BlockKey] = []
    in_focus = False
    f_references: list[tuple[BlockKey, BackingReference]] = []
    structure = _document_structure(manifest, document)
    consumed_until = 0

    for line_number, line in enumerate(document.lines, start=1):
        if line_number <= consumed_until:
            continue
        if line_number in document.body_delimiters:
            line = BodyFenceDelimiter(line)
        parent_key = stack[-1] if stack else document.root_key
        kind, end_line = structure.get(line_number, ("body", line_number))
        heading_match = HEADING_RE.match(line) if kind == "heading" else None
        if heading_match is not None:
            depth = len(heading_match.group(1))
            while stack and manifest.blocks[stack[-1]].depth >= depth:
                manifest.blocks[stack.pop()].end_line = line_number - 1
            parent_key = stack[-1] if stack else document.root_key
            parent = manifest.blocks[parent_key]
            anchor, candidate, unsupported = _heading_declarations(line)
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
            indexed_title = line[heading_match.end():title_end].strip() if addressed else heading_title(line)

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
        if in_focus and kind == "node":
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
                    end_line=end_line,
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
                for number in range(line_number + 1, end_line + 1):
                    _append_text(block, document.lines[number - 1], number)
                consumed_until = end_line
                continue

        if kind == "node":
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
            node_candidate, node = _node_declaration(line)
            node_id = node_candidate.group(1) if node_candidate else None
            if node_id is not None and not is_valid_item_id(node_id):
                _add_error(
                    manifest,
                    f"invalid node id `{node_id}` at {document.relative_path}:{line_number}; "
                    "use only letters, numbers, dot, underscore, or dash",
                    document.path,
                    line_number,
                )
                node_id = None
            elif node_candidate is not None and node is None:
                _add_error(
                    manifest,
                    f"node `{node_id}` has a malformed edge list at "
                    f"{document.relative_path}:{line_number}",
                    document.path,
                    line_number,
                )
                node_id = None
            edges, malformed = _parse_edges(node.group(2) or "") if node and node_id else ([], [])
            prose = _node_prose(line, node) if node and node_id else LIST_PREFIX_RE.sub("", line, count=1)
            key = (document.path, line_number)
            block = DocumentBlock(
                key=key,
                kind="node",
                line=line,
                title=prose or node_id or "",
                prose=prose,
                line_number=line_number,
                end_line=end_line,
                item_id=node_id,
                item_kind="node",
                family=document.family,
                source_path=document.path,
                physical_parent=parent_key,
                logical_parent=parent_key,
            )
            manifest.blocks[key] = block
            _append_child(parent, key)
            for number in range(line_number + 1, end_line + 1):
                _append_text(block, document.lines[number - 1], number)
            consumed_until = end_line
            if node_id is not None:
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
                        end_line=end_line,
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
    first_root_child = next(
        (
            manifest.blocks[child].line_number
            for child in root.physical_children
        ),
        None,
    )
    root_body_end = (first_root_child - 1) if first_root_child is not None else len(document.lines)
    if root_body_end >= 1:
        root.body_span = SourceSpan(document.path, 1, root_body_end)
    for key, block in manifest.blocks.items():
        if key[0] != document.path or block.kind == "root":
            continue
        if block.kind == "heading":
            first_child = next(
                (
                    manifest.blocks[child].line_number
                    for child in block.physical_children
                ),
                None,
            )
            body_start = block.line_number + 1
            body_end = (first_child - 1) if first_child is not None else block.end_line
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
    detail_root.logical_parent = owner_key
    detail_parts = list(detail_root.logical_parts)
    if not detail_parts:
        return
    if not owner.logical_parts or not isinstance(owner.logical_parts[-1], str) or owner.logical_parts[-1].strip():
        owner.logical_parts.append("")
        owner.logical_text_parts.append(SourceTextPart(owner.source_path, 0, ""))
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
            item.content_hash = block_content_hash(manifest, manifest.blocks[item.block_key])


def block_content_hash(manifest: GraphManifest, block: DocumentBlock) -> str:
    """Version original content and its logical ancestor context, without selectors."""
    context = []
    current = block.logical_parent
    while current is not None:
        ancestor = manifest.blocks[current]
        context.append(resolve_markdown_references(ancestor.line, manifest.documents[ancestor.source_path]))
        context.extend(part for part in rendered_block_parts(manifest, ancestor) if isinstance(part, str))
        current = ancestor.logical_parent
    return hashlib.sha256(
        ("\n".join(context) + "\n" + _flatten_logical_block(manifest, block.key)).rstrip("\r\n").encode("utf-8")
    ).hexdigest()


def _flatten_logical_block(manifest: GraphManifest, key: BlockKey) -> str:
    block = manifest.blocks[key]
    if block.kind in {"node", "focus"}:
        return resolve_markdown_references("\n".join([block.line, *block.logical_parts]),
                                           manifest.documents[block.source_path])
    lines = [resolve_markdown_references(block.line, manifest.documents[block.source_path])] if block.kind != "root" else []
    for part in rendered_block_parts(manifest, block):
        if isinstance(part, tuple):
            lines.append(_flatten_logical_block(manifest, part))
        else:
            lines.append(part)
    return "\n".join(lines)


def _parse_edges(edge_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    edges: list[tuple[str, str]] = []
    malformed: list[str] = []
    if not edge_text.strip():
        return edges, malformed
    for raw in edge_text.split(","):
        value = raw.strip()
        if not value:
            malformed.append("(empty edge)")
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
