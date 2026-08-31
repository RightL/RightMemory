"""Logical Pursuit editing over the canonical graph index.

This module has no Git, locking, UI, or agent responsibilities. Its writer is
intended for a transaction's disposable candidate root, never the active root.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from .graph import (
    ANCHOR_RE,
    FOCUS_HEADING_RE,
    BlockKey,
    DocumentBlock,
    GraphManifest,
    block_body_text,
    build_graph_manifest,
    heading_title,
    remove_edge_targets,
    render_heading_line,
    replace_heading_title,
    validate_heading_title,
)


class PursuitOperationError(ValueError):
    pass


def plain_title(title: str) -> str:
    """Strip balanced title marks, matching frontend title-format.ts, not Markdown.

    Other HTML and unmatched delimiters are visible literal text. This helper is
    confined to editing; it does not change the canonical graph's grammar.
    """
    output: list[str] = []
    stack: list[tuple[str, str, list[str], int]] = []

    def parts() -> list[str]:
        return stack[-1][2] if stack else output

    offset = 0
    for match in re.finditer(r"<[^>]*>|\*{2,}|~{2,}", title):
        run = match.group()
        token = "**" if run.startswith("**") else "~~" if run.startswith("~~") else run
        paired = token in {"**", "~~"}
        count = len(run) // 2 if paired else 1
        start, end = match.span()
        parts().append(title[offset:start])
        mark = "u" if token in {"<u>", "</u>"} else token if token in {"**", "~~"} else None
        previous = title[start - 1:start] if start else ""
        following = title[end:end + 1]
        closing_side = bool(previous and not previous.isspace()) or not following or following.isspace()
        for index in range(count):
            position = start + index * len(token)
            inner_start = stack[-1][3] + len(stack[-1][1]) if stack else position
            blank = position > inner_start and not title[inner_start:position].strip()
            closes = (mark and stack and stack[-1][0] == mark and token != "<u>"
                      and (token == "</u>" or closing_side or blank))
            if closes:
                _, _, children, _ = stack.pop()
                parts().extend(children)
            elif mark and token != "</u>":
                stack.append((mark, token, [], position))
            else:
                parts().append(token)
        if paired and len(run) % 2:
            parts().append(run[-1])
        offset = end
    parts().append(title[offset:])
    while stack:
        _, opening, children, _ = stack.pop()
        parts().append(opening)
        parts().extend(children)
    return "".join(output)


@dataclass(frozen=True, slots=True)
class PursuitItem:
    id: str
    title: str
    body: str
    parent_id: str | None
    child_ids: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    focused: bool
    source_path: str
    source_line: int
    anchor_kind: str
    editable: bool = True


@dataclass(frozen=True, slots=True)
class PursuitTree:
    items: dict[str, PursuitItem]
    root_ids: tuple[str, ...]
    focus_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        items = []
        for item in self.items.values():
            value = asdict(item)
            value["child_ids"] = list(item.child_ids)
            value["edges"] = [list(edge) for edge in item.edges]
            items.append(value)
        return {
            "items": items,
            "root_ids": list(self.root_ids),
            "focus_ids": list(self.focus_ids),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class PursuitEdit:
    changed_paths: tuple[str, ...]
    repaired_references: tuple[dict[str, object], ...]
    selected_id: str | None
    description: str


def _block_id(manifest: GraphManifest, block: DocumentBlock) -> str:
    if block.item_id is not None:
        return block.item_id
    relative = block.source_path.relative_to(manifest.root).as_posix()
    return f"plain:{relative}:{block.line_number}"


def _reserved(manifest: GraphManifest, block: DocumentBlock) -> bool:
    if block.kind != "heading":
        return False
    if FOCUS_HEADING_RE.match(block.line):
        return True
    return (
        block.source_path == manifest.root / "PURSUITS.md"
        and block.depth == 1
        and block.item_id is None
        and heading_title(block.line).casefold() == "pursuits"
    )


def load_pursuit_tree(root: Path) -> PursuitTree:
    return _project(build_graph_manifest(Path(root)))


def _project(manifest: GraphManifest) -> PursuitTree:
    focus_ids = tuple(item_id for item_id, _, _ in manifest.focus_ids)
    item_data: dict[str, dict[str, object]] = {}
    children: dict[str | None, list[str]] = {None: []}
    diagnostics = list(manifest.errors)
    seen: set[BlockKey] = set()

    def visit(key: BlockKey, parent_id: str | None) -> None:
        if key in seen:
            return
        seen.add(key)
        block = manifest.blocks[key]
        current_parent = parent_id
        if block.kind in {"heading", "node"} and not _reserved(manifest, block):
            item_id = _block_id(manifest, block)
            if item_id in item_data:
                return  # Duplicate ids are already diagnosed; the root is read-only.
            graph_item = manifest.items.get(block.item_id or "")
            legacy = block.kind == "node"
            item_data[item_id] = {
                "id": item_id,
                "title": block.title,
                "body": block_body_text(manifest, block).strip("\r\n"),
                "parent_id": parent_id,
                "edges": graph_item.edges if graph_item is not None else (),
                "focused": item_id in focus_ids,
                "source_path": block.source_path.relative_to(manifest.root).as_posix(),
                "source_line": block.line_number,
                "anchor_kind": "node" if legacy else block.anchor_kind or "plain",
                "editable": not legacy,
            }
            children.setdefault(parent_id, []).append(item_id)
            children[item_id] = []
            current_parent = item_id
            if legacy:
                diagnostics.append(
                    f"Legacy graph node `{item_id}` at "
                    f"{item_data[item_id]['source_path']}:{block.line_number} is read-only in the map."
                )
        for child in block.logical_children:
            visit(child, current_parent)

    for key in manifest.root_blocks:
        if manifest.blocks[key].family == "pursuit":
            visit(key, None)
    items = {
        item_id: PursuitItem(**value, child_ids=tuple(children[item_id]))
        for item_id, value in item_data.items()
    }
    return PursuitTree(items, tuple(children[None]), focus_ids, tuple(diagnostics))


@dataclass(eq=False)
class _Entry:
    kind: str
    id: str | None
    title: str
    anchor_kind: str | None
    depth: int
    header: str
    parts: list[str | _Entry]
    document: _Document
    parent: _Document | _Entry
    block: DocumentBlock | None = None


@dataclass(eq=False)
class _Document:
    path: Path
    newline: str
    parts: list[str | _Entry] = field(default_factory=list)


def _newline(text: str) -> str:
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        if line.endswith("\r"):
            return "\r"
    return "\n"


def _line_ending(text: str) -> str:
    return text[len(text.rstrip("\r\n")):]


def _with_newline(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _render(entry: _Entry | _Document) -> str:
    prefix = entry.header if isinstance(entry, _Entry) else ""
    return prefix + "".join(part if isinstance(part, str) else _render(part) for part in entry.parts)


class _Editor:
    def __init__(self, manifest: GraphManifest):
        self.manifest = manifest
        self.root = manifest.root
        self.tree = _project(manifest)
        self.documents: dict[Path, _Document] = {}
        self.entries: dict[BlockKey, _Entry] = {}
        self.items: dict[str, _Entry] = {}
        self.used_ids = {item_id.casefold() for item_id in manifest.items}
        self.used_paths = {path.name.casefold() for path in self.root.iterdir()}
        self.memory_changes: dict[Path, str] = {}
        self.repairs: list[dict[str, object]] = []
        for source in manifest.documents.values():
            if source.family != "pursuit":
                continue
            document = _Document(source.path, _newline(source.text))
            self.documents[source.path] = document
            lines = source.text.splitlines(keepends=True)

            def fill(block: DocumentBlock, owner: _Entry | _Document) -> None:
                cursor = block.line_number if block.kind != "root" else 0
                for child_key in block.physical_children:
                    child = manifest.blocks[child_key]
                    if child.line_number - 1 > cursor:
                        owner.parts.append("".join(lines[cursor:child.line_number - 1]))
                    entry = _Entry(
                        kind=child.kind,
                        id=_block_id(manifest, child) if child.kind in {"heading", "node"} else None,
                        title=heading_title(child.line) if child.kind == "heading" else "",
                        anchor_kind=child.anchor_kind,
                        depth=child.depth,
                        header=lines[child.line_number - 1],
                        parts=[],
                        document=document,
                        parent=owner,
                        block=child,
                    )
                    self.entries[child_key] = entry
                    if entry.id is not None:
                        self.items[entry.id] = entry
                    fill(child, entry)
                    owner.parts.append(entry)
                    cursor = child.end_line
                if cursor < block.end_line:
                    owner.parts.append("".join(lines[cursor:block.end_line]))

            fill(manifest.blocks[source.root_key], document)

    def item(self, value: object, *, editable: bool = True) -> _Entry:
        if not isinstance(value, str) or value not in self.tree.items:
            raise PursuitOperationError("unknown Pursuit item")
        entry = self.items[value]
        if editable and entry.kind != "heading":
            raise PursuitOperationError("legacy graph node leaves are read-only in the map")
        return entry

    def new_id(self, title: str) -> str:
        ascii_text = "".join(char for char in plain_title(title).lower() if ord(char) < 128)
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:64].rstrip("-")
        if not slug or slug == "rules":
            slug = "p-" + uuid.uuid4().hex[:12]
        candidate = slug
        suffix = 2
        while candidate.casefold() in self.used_ids or f"pursuit_{candidate}.md".casefold() in self.used_paths:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        self.used_ids.add(candidate.casefold())
        return candidate

    def address(self, entry: _Entry, *, title: str | None = None) -> None:
        if entry.anchor_kind is not None:
            return
        old_id = entry.id
        entry.id = self.new_id(title or entry.title)
        entry.anchor_kind = "#"
        entry.header = render_heading_line(
            entry.title, "#", entry.id, depth=entry.depth
        ) + _line_ending(entry.header)
        if old_id is not None:
            self.items.pop(old_id, None)
        self.items[entry.id] = entry

    def anchor(self, entry: _Entry, kind: str) -> None:
        self.address(entry)
        match = ANCHOR_RE.match(entry.header.rstrip("\r\n"))
        assert match is not None
        entry.header = entry.header[:match.start(2)] + kind + entry.header[match.end(2):]
        entry.anchor_kind = kind

    def root_container(self) -> _Entry | _Document:
        document = self.documents[self.root / "PURSUITS.md"]
        for part in document.parts:
            if isinstance(part, _Entry) and part.block is not None and _reserved(self.manifest, part.block):
                if not FOCUS_HEADING_RE.match(part.header):
                    return part
        return document

    def backing(self, entry: _Entry) -> _Document:
        assert entry.id is not None
        return self.documents[self.root / f"PURSUIT_{entry.id}.md"]

    def destination(self, parent: _Entry | None) -> _Entry | _Document:
        if parent is None:
            return self.root_container()
        if parent.anchor_kind != "F#" and parent.depth >= 3:
            self.split(parent)
        return self.backing(parent) if parent.anchor_kind == "F#" else parent

    def split(self, entry: _Entry) -> None:
        self.address(entry)
        path = self.root / f"PURSUIT_{entry.id}.md"
        if path.name.casefold() in self.used_paths:
            raise PursuitOperationError(f"cannot create backing file over existing path: {path.name}")
        document = _Document(path, entry.document.newline)
        self.documents[path] = document
        self.used_paths.add(path.name.casefold())
        # Strings in the indexed owner block are its own body, including prose
        # around legacy node lines. Keep that prose with the owner when its
        # children cross the physical backing boundary.
        document.parts = [part for part in entry.parts if isinstance(part, _Entry)]
        entry.parts = [part for part in entry.parts if isinstance(part, str)]
        self.anchor(entry, "F#")
        for part in document.parts:
            if isinstance(part, _Entry):
                part.parent = document
                self.normalize(part, 1, document)

    def normalize(self, entry: _Entry, depth: int, document: _Document) -> None:
        if entry.document.newline != document.newline:
            entry.header = _with_newline(entry.header, document.newline)
            entry.parts = [
                _with_newline(part, document.newline) if isinstance(part, str) else part
                for part in entry.parts
            ]
        entry.document = document
        if entry.kind != "heading":
            return
        if depth > 3:
            raise PursuitOperationError("a map heading needs a backing boundary before physical depth four")
        if depth != entry.depth:
            entry.header = "#" * depth + entry.header[entry.depth:]
            entry.depth = depth
        physical_children = [part for part in entry.parts if isinstance(part, _Entry)]
        if entry.anchor_kind != "F#" and depth == 3 and any(child.kind == "heading" for child in physical_children):
            self.split(entry)
            return
        for child in physical_children:
            self.normalize(child, depth + 1, document)

    def insert(self, entry: _Entry, container: _Entry | _Document, after: _Entry | None, *, first: bool) -> None:
        if after is not None:
            if after.parent is not container:
                raise PursuitOperationError("after_id must be a sibling at the destination")
            index = container.parts.index(after) + 1
        elif first:
            index = next((i for i, part in enumerate(container.parts) if isinstance(part, _Entry) and part.kind != "focus" and not (part.block and _reserved(self.manifest, part.block))), len(container.parts))
        else:
            index = len(container.parts)
        if after is not None and after.kind == "node":
            while index < len(container.parts) and isinstance(container.parts[index], str):
                index += 1
        if entry.kind == "heading" and any(
            isinstance(part, _Entry) and part.kind == "node" for part in container.parts[index:]
        ):
            raise PursuitOperationError(
                "legacy graph leaves must remain before heading children in their document; insert after them"
            )
        document = container if isinstance(container, _Document) else container.document
        depth = 1 if isinstance(container, _Document) and container.path.name != "PURSUITS.md" else (container.depth + 1 if isinstance(container, _Entry) else 2)
        entry.parent = container
        self.normalize(entry, depth, document)
        prefix = "".join(part if isinstance(part, str) else _render(part) for part in container.parts[:index])
        if isinstance(container, _Entry):
            prefix = container.header + prefix
        if prefix and not prefix.endswith(document.newline * 2):
            separator = document.newline if prefix.endswith(document.newline) else document.newline * 2
            container.parts.insert(index, separator)
            index += 1
        if index < len(container.parts) and not _render(entry).endswith(document.newline * 2):
            entry.parts.append(document.newline if _render(entry).endswith(document.newline) else document.newline * 2)
        container.parts.insert(index, entry)

    def cleanup_empty(self, container: _Entry | _Document) -> None:
        if not isinstance(container, _Document) or container.path.name == "PURSUITS.md":
            return
        if any(isinstance(part, _Entry) for part in container.parts) or _render(container).strip():
            return
        for entry in self.items.values():
            if entry.anchor_kind == "F#" and self.root / f"PURSUIT_{entry.id}.md" == container.path:
                self.anchor(entry, "#")
                self.documents.pop(container.path, None)
                return

    def descendants(self, item_id: str) -> set[str]:
        result: set[str] = set()
        stack = [item_id]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(self.tree.items[current].child_ids)
        return result

    def remove_focus(self, deleted: set[str], *, report: bool) -> None:
        for key in self.manifest.focus_blocks:
            block = self.manifest.blocks[key]
            if block.focus_target not in deleted:
                continue
            entry = self.entries[key]
            if entry in entry.parent.parts:
                entry.parent.parts.remove(entry)
            if report:
                self.repairs.append({
                    "kind": "focus", "source_id": None,
                    "source_path": block.source_path.relative_to(self.root).as_posix(),
                    "target_id": block.focus_target,
                })

    def repair_edges(self, deleted: set[str]) -> None:
        memory_lines: dict[Path, list[str]] = {}
        for item in self.manifest.items.values():
            if item.id in deleted:
                continue
            removed = [(kind, target) for kind, target in item.edges if target in deleted]
            if not removed:
                continue
            if item.family == "pursuit":
                entry = self.entries[item.block_key]
                entry.header = remove_edge_targets(entry.header, deleted)
            else:
                lines = memory_lines.setdefault(item.file, self.manifest.documents[item.file].text.splitlines(keepends=True))
                lines[item.line_number - 1] = remove_edge_targets(lines[item.line_number - 1], deleted)
            for kind, target in removed:
                self.repairs.append({
                    "kind": "edge", "source_id": item.id,
                    "source_path": item.file.relative_to(self.root).as_posix(),
                    "target_id": target, "edge_type": kind,
                })
        self.memory_changes.update({path: "".join(lines) for path, lines in memory_lines.items()})

    def reachable_documents(self) -> set[Path]:
        reachable: set[Path] = set()

        def visit(owner: _Entry | _Document) -> None:
            if isinstance(owner, _Document):
                if owner.path in reachable:
                    return
                reachable.add(owner.path)
            for part in owner.parts:
                if not isinstance(part, _Entry):
                    continue
                visit(part)
                if part.anchor_kind == "F#":
                    visit(self.backing(part))

        visit(self.documents[self.root / "PURSUITS.md"])
        return reachable

    def plan(self) -> dict[Path, str | None]:
        reachable = self.reachable_documents()
        before = {path for path, document in self.manifest.documents.items() if document.family == "pursuit"}
        changes: dict[Path, str | None] = {}
        for path in before | reachable:
            rendered = _render(self.documents[path]) if path in reachable else None
            original = self.manifest.documents[path].text if path in before else None
            if rendered != original:
                changes[path] = rendered
        changes.update(self.memory_changes)
        return changes


def _string(operation: Mapping[str, object], name: str) -> str:
    value = operation.get(name)
    if not isinstance(value, str):
        raise PursuitOperationError(f"{name} must be a string")
    return value


def _title(operation: Mapping[str, object]) -> str:
    try:
        title = validate_heading_title(_string(operation, "title"))
        if not plain_title(title).strip():
            raise ValueError("title must have nonempty visible text")
        return title
    except ValueError as exc:
        raise PursuitOperationError(str(exc)) from exc


def _apply(editor: _Editor, operation: Mapping[str, object]) -> tuple[str | None, str]:
    kind = operation.get("type")
    if kind == "create":
        title = _title(operation)
        parent = editor.item(operation["parent_id"]) if operation.get("parent_id") is not None else None
        after = editor.item(operation["after_id"], editable=False) if operation.get("after_id") is not None else None
        expected_parent = operation.get("parent_id")
        if after is not None and editor.tree.items[after.id].parent_id != expected_parent:
            raise PursuitOperationError("after_id must be a sibling at the destination")
        container = editor.destination(parent)
        document = container if isinstance(container, _Document) else container.document
        item_id = editor.new_id(title)
        entry = _Entry("heading", item_id, title, "#", 2,
                       render_heading_line(title, "#", item_id) + document.newline,
                       [document.newline], document, container)
        editor.items[item_id] = entry
        editor.insert(entry, container, after, first="after_id" in operation and after is None)
        return item_id, f"Create Pursuit: {title}"

    if kind not in {"rename", "move", "reorder", "delete", "edit_body", "set_focus"}:
        raise PursuitOperationError("unknown Pursuit operation")
    original_id = _string(operation, "id")
    entry = editor.item(original_id)
    if kind == "rename":
        title = _title(operation)
        editor.address(entry, title=title)
        entry.header = replace_heading_title(entry.header, title)
        entry.title = title
    elif kind in {"move", "reorder"}:
        parent_id = operation.get("parent_id", editor.tree.items[original_id].parent_id if kind == "reorder" else None)
        if parent_id is not None and parent_id in editor.descendants(original_id):
            raise PursuitOperationError("cannot move a direction under itself or its descendant")
        parent = editor.item(parent_id) if parent_id is not None else None
        after = editor.item(operation["after_id"], editable=False) if operation.get("after_id") is not None else None
        if after is entry or (after is not None and editor.tree.items[after.id].parent_id != parent_id):
            raise PursuitOperationError("after_id must be another sibling at the destination")
        editor.address(entry)
        container = editor.destination(parent)
        previous = entry.parent
        previous.parts.remove(entry)
        editor.insert(entry, container, after, first="after_id" in operation and after is None)
        editor.cleanup_empty(previous)
    elif kind == "delete":
        deleted = editor.descendants(original_id)
        previous = entry.parent
        previous.parts.remove(entry)
        editor.remove_focus(deleted, report=True)
        editor.repair_edges(deleted)
        editor.cleanup_empty(previous)
        parent_id = editor.tree.items[original_id].parent_id
        return parent_id, f"Delete Pursuit subtree: {entry.title}"
    elif kind == "edit_body":
        body = _string(operation, "body")
        newline = entry.document.newline
        body = _with_newline(body, newline).strip("\r\n")
        if body != _with_newline(editor.tree.items[original_id].body, newline):
            editor.address(entry)
            first_heading = next((i for i, part in enumerate(entry.parts) if isinstance(part, _Entry) and part.kind == "heading"), len(entry.parts))
            entry.parts[:first_heading] = [newline + body + newline * 2 if body else newline]
    elif kind == "set_focus":
        focused = operation.get("focused")
        if not isinstance(focused, bool):
            raise PursuitOperationError("focused must be true or false")
        editor.address(entry)
        if not focused:
            editor.remove_focus({original_id}, report=False)
        elif entry.id not in editor.tree.focus_ids:
            container = editor.root_container()
            document = container if isinstance(container, _Document) else container.document
            focus = next((part for part in container.parts if isinstance(part, _Entry) and FOCUS_HEADING_RE.match(part.header.rstrip("\r\n"))), None)
            if focus is None:
                focus = _Entry("heading", None, "Focus", None, 2,
                               "## Focus" + document.newline, [document.newline], document, container)
                legacy_after = next((part for part in reversed(container.parts) if isinstance(part, _Entry) and part.kind == "node"), None)
                editor.insert(focus, container, legacy_after, first=legacy_after is None)
            reference = _Entry("focus", None, "", None, 0,
                               f"- `{entry.id}`" + document.newline, [], document, focus)
            last = max((i for i, part in enumerate(focus.parts) if isinstance(part, _Entry) and part.kind == "focus"), default=-1)
            if last < 0:
                # Older starters used this sentence for an empty Focus block.
                # Remove only that entire placeholder, never user-authored notes.
                if all(isinstance(part, str) for part in focus.parts) and "".join(focus.parts).strip() == "No Pursuit is focused yet.":
                    focus.parts = [document.newline]
                if not _render(focus).endswith(document.newline):
                    focus.parts.append(document.newline)
                focus.parts.append(reference)
                focus.parts.append(document.newline)
            else:
                index = last + 1
                if not _render(focus.parts[last]).endswith(document.newline):
                    focus.parts.insert(index, document.newline)
                    index += 1
                focus.parts.insert(index, reference)
    return entry.id, f"{str(kind).replace('_', ' ').capitalize()} Pursuit: {entry.title}"


def _check_body_edit(before: GraphManifest, after: GraphManifest, original_id: str) -> None:
    """Notes may contain Markdown, but cannot silently replace graph structure."""
    old_item = before.items.get(original_id)
    # Addressing a pre-existing plain group is an allowed structural change.
    added = 0 if old_item is not None else 1
    before_headings = sum(block.kind == "heading" for block in before.blocks.values())
    after_headings = sum(block.kind == "heading" for block in after.blocks.values())
    if before_headings != after_headings or len(after.items) != len(before.items) + added:
        raise PursuitOperationError("note text cannot add or remove map headings or graph nodes; use the map editor")
    for item_id, item in before.items.items():
        other = after.items.get(item_id)
        if other is None:
            raise PursuitOperationError("note text cannot remove a graph item")
        if item.item_kind == "node":
            if before.blocks[item.block_key].line != after.blocks[other.block_key].line:
                raise PursuitOperationError("legacy graph node lines are read-only; preserve them when editing the note")


def apply_operation(root: Path, operation: Mapping[str, object]) -> PursuitEdit:
    """Apply one edit to an isolated candidate root, restoring it on failure.

    The store owns locking, revisions, complete root validation, and publication.
    This layer additionally checks the canonical graph before and after rendering.
    """
    if not isinstance(operation, Mapping):
        raise PursuitOperationError("operation must be an object")
    manifest = build_graph_manifest(Path(root))
    if manifest.errors:
        raise PursuitOperationError("Pursuit root is read-only until graph errors are repaired: " + "; ".join(manifest.errors))
    editor = _Editor(manifest)
    try:
        selected_id, description = _apply(editor, operation)
        changes = editor.plan()
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PursuitOperationError):
            raise
        raise PursuitOperationError(str(exc)) from exc
    previous: dict[Path, bytes | None] = {}
    try:
        for path, text in changes.items():
            previous[path] = path.read_bytes() if path.exists() else None
            if text is None:
                path.unlink()
            else:
                path.write_bytes(text.encode("utf-8"))
        candidate = build_graph_manifest(manifest.root)
        if candidate.errors:
            raise PursuitOperationError("edit would invalidate the graph: " + "; ".join(candidate.errors))
        if changes and operation.get("type") == "edit_body":
            _check_body_edit(manifest, candidate, _string(operation, "id"))
        if operation.get("type") == "set_focus":
            focused = any(item_id == selected_id for item_id, _, _ in candidate.focus_ids)
            if focused != operation["focused"]:
                raise PursuitOperationError("the edited document did not retain the requested Focus marker")
    except BaseException:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    return PursuitEdit(
        changed_paths=tuple(sorted(path.relative_to(manifest.root).as_posix() for path in changes)),
        repaired_references=tuple(editor.repairs),
        selected_id=selected_id,
        description=description,
    )
