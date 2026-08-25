from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .graph import KNOWN_EDGE_TYPES, GraphManifest, build_graph_manifest, validate_item_id
from .session import MemoryWriteLock


_HEADING_TITLE_RE = re.compile(r"^(#{1,4})\s+(.*?)\s+\{(?:F#|#)[A-Za-z0-9_.-]+\}(?:\s*(?:→|->)\s*\[(.*?)\])?\s*$")
_PLAIN_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*$")
_FIELD_RE = re.compile(r"^\s*\*\*(State|Next|Done when|Status):\*\*\s*(.*)$", re.IGNORECASE)
_NEXT_RE = re.compile(r"^\s*-\s+`(do|ask|wait)`\s+(.+?)\s*$", re.IGNORECASE)
_HISTORY_LIMIT = 20
_HISTORY_PATH = Path(".runtime") / "pursuit-studio" / "history.json"


class PursuitWorkspaceError(ValueError):
    pass


class PursuitRevisionConflict(PursuitWorkspaceError):
    pass


@dataclass(slots=True)
class PursuitNext:
    kind: str
    text: str

    def __post_init__(self) -> None:
        self.kind = self.kind.strip().lower()
        self.text = self.text.strip()
        if "\n" in self.text or "\r" in self.text or "\x00" in self.text:
            raise PursuitWorkspaceError("Next text must be one line")
        if self.kind not in {"do", "ask", "wait"}:
            raise PursuitWorkspaceError("Next kind must be do, ask, or wait")
        if not self.text:
            raise PursuitWorkspaceError("Next text must not be empty")


@dataclass(slots=True)
class PursuitBody:
    objective: str = ""
    state: str = ""
    next: list[PursuitNext] = field(default_factory=list)
    done_when: str = ""
    status: str = "active"
    extra: str = ""


@dataclass(slots=True)
class _Node:
    key: str
    item_id: str | None
    title: str
    anchor_kind: str | None
    edges: list[tuple[str, str]]
    depth: int
    document: str
    parent_key: str | None
    children: list[str]
    raw_body: str
    body: PursuitBody
    traversal_rank: int
    source_line: int
    dirty_body: bool = False
    removed: bool = False
    is_focus: bool = False

    @property
    def editable(self) -> bool:
        return self.item_id is not None and not self.is_focus

    @property
    def parked(self) -> bool:
        return self.body.status.casefold() == "parked"


@dataclass(frozen=True, slots=True)
class PursuitPreview:
    revision: str
    candidate_revision: str
    diff: str
    changed_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    files: dict[str, str | None]
    snapshot: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "candidate_revision": self.candidate_revision,
            "diff": self.diff,
            "changed_files": list(self.changed_files),
            "removed_files": list(self.removed_files),
            "files": dict(self.files),
            "snapshot": self.snapshot,
        }


@dataclass(frozen=True, slots=True)
class PursuitApplyResult:
    revision: str
    changed_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    commit: str | None
    snapshot: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "changed_files": list(self.changed_files),
            "removed_files": list(self.removed_files),
            "commit": self.commit,
            "snapshot": self.snapshot,
        }


class PursuitEditor:
    """Mutable view over canonical Pursuit Markdown backed by graph.py's index."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        self.manifest = build_graph_manifest(self.memory_root)
        if self.manifest.errors:
            joined = "\n- ".join(self.manifest.errors)
            raise PursuitWorkspaceError(f"RightMemory graph is invalid:\n- {joined}")
        self.nodes: dict[str, _Node] = {}
        self.block_to_key: dict[tuple[Path, int], str] = {}
        self.document_preambles: dict[str, str] = {}
        self.document_newlines: dict[str, str] = {}
        self.document_order: list[str] = []
        self.root_order: list[str] = []
        self.focus_ids: list[str] = [item_id for item_id, _path, _line in self.manifest.focus_ids]
        self._known_item_ids = set(self.manifest.items)
        self._top_container_key: str | None = None
        self._load()

    @classmethod
    def snapshot_from_root(cls, memory_root: Path) -> dict[str, Any]:
        return cls(memory_root).snapshot()

    def _load(self) -> None:
        pursuit_documents = [
            document
            for document in self.manifest.documents.values()
            if document.family == "pursuit"
        ]
        pursuit_documents.sort(key=lambda document: document.source_order)
        for document in pursuit_documents:
            relative = document.relative_path
            self.document_order.append(relative)
            self.document_newlines[relative] = "\r\n" if "\r\n" in document.text else "\n"
            first_heading = min(
                (
                    block.line_number
                    for block in self.manifest.blocks.values()
                    if block.source_path == document.path and block.kind == "heading"
                ),
                default=len(document.lines) + 1,
            )
            self.document_preambles[relative] = "\n".join(document.lines[: max(0, first_heading - 1)])

        heading_blocks = [
            block
            for block in self.manifest.blocks.values()
            if block.kind == "heading" and block.family == "pursuit"
        ]
        heading_blocks.sort(key=lambda block: (block.traversal_rank if block.traversal_rank >= 0 else 10**9, block.line_number))

        for block in heading_blocks:
            document = self.manifest.documents[block.source_path]
            key = block.item_id or f"@{document.relative_path}:{block.line_number}"
            if key in self.nodes:
                raise PursuitWorkspaceError(f"duplicate Pursuit editor key: {key}")
            title = _heading_title(block.line)
            raw_body = _body_text(document.lines, block.body_span)
            is_focus = block.depth == 2 and title.casefold() == "focus" and block.item_id is None
            node = _Node(
                key=key,
                item_id=block.item_id,
                title=title,
                anchor_kind=block.anchor_kind,
                edges=list(self.manifest.items[block.item_id].edges) if block.item_id else [],
                depth=block.depth,
                document=document.relative_path,
                parent_key=None,
                children=[],
                raw_body=raw_body,
                body=_parse_body(raw_body),
                traversal_rank=block.traversal_rank,
                source_line=block.line_number,
                is_focus=is_focus,
            )
            self.nodes[key] = node
            self.block_to_key[block.key] = key
            if (
                document.relative_path == "PURSUITS.md"
                and block.depth == 1
                and title.casefold() == "pursuits"
                and block.item_id is None
            ):
                self._top_container_key = key

        for block_key, key in self.block_to_key.items():
            block = self.manifest.blocks[block_key]
            parent = block.logical_parent
            while parent is not None and parent not in self.block_to_key:
                parent = self.manifest.blocks[parent].logical_parent
            parent_key = self.block_to_key.get(parent) if parent is not None else None
            self.nodes[key].parent_key = parent_key

        for node in self.nodes.values():
            if node.parent_key is None:
                self.root_order.append(node.key)
            else:
                self.nodes[node.parent_key].children.append(node.key)

        for node in self.nodes.values():
            node.children.sort(key=lambda key: self.nodes[key].traversal_rank)
        self.root_order.sort(key=lambda key: self.nodes[key].traversal_rank)

        if self._top_container_key is None:
            self._top_container_key = self._create_plain_top_container()
        self._ensure_focus_node()

    def _create_plain_top_container(self) -> str:
        key = "@PURSUITS.md:virtual-root"
        node = _Node(
            key=key,
            item_id=None,
            title="Pursuits",
            anchor_kind=None,
            edges=[],
            depth=1,
            document="PURSUITS.md",
            parent_key=None,
            children=[],
            raw_body="",
            body=PursuitBody(),
            traversal_rank=-1,
            source_line=0,
        )
        self.nodes[key] = node
        self.root_order.insert(0, key)
        return key


    def _ensure_focus_node(self) -> None:
        if any(node.is_focus and not node.removed for node in self.nodes.values()):
            return
        assert self._top_container_key is not None
        parent = self.nodes[self._top_container_key]
        key = "@PURSUITS.md:virtual-focus"
        node = _Node(
            key=key,
            item_id=None,
            title="Focus",
            anchor_kind=None,
            edges=[],
            depth=parent.depth + 1,
            document=parent.document,
            parent_key=parent.key,
            children=[],
            raw_body="",
            body=PursuitBody(),
            traversal_rank=-1,
            source_line=0,
            is_focus=True,
        )
        self.nodes[key] = node
        parent.children.insert(0, key)

    def snapshot(self) -> dict[str, Any]:
        editable_nodes = [node for node in self.nodes.values() if node.editable and not node.removed]
        editable_nodes.sort(key=lambda node: (node.traversal_rank if node.traversal_rank >= 0 else 10**9, node.item_id or ""))
        tasks_by_pursuit: dict[str, list[dict[str, Any]]] = {}
        try:
            from .pursuit_tasks import list_tasks

            for task in list_tasks(self.memory_root):
                for pursuit_id in task.pursuit_ids:
                    tasks_by_pursuit.setdefault(pursuit_id, []).append(task.to_json())
        except (FileNotFoundError, OSError, UnicodeError):
            tasks_by_pursuit = {}

        return {
            "revision": self.revision(),
            "focus_ids": list(self.focus_ids),
            "nodes": [self._node_json(node, tasks_by_pursuit.get(node.item_id or "", [])) for node in editable_nodes],
            "roots": [
                node.item_id
                for node in editable_nodes
                if self._nearest_editable_parent(node.key) is None
            ],
            "documents": sorted(self._current_pursuit_files()),
            "edge_types": sorted(KNOWN_EDGE_TYPES),
        }

    def _node_json(self, node: _Node, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        assert node.item_id is not None
        parent_id = self._nearest_editable_parent(node.key)
        children = [self.nodes[key].item_id for key in self._editable_children(node.key)]
        return {
            "id": node.item_id,
            "title": node.title,
            "parent_id": parent_id,
            "children": children,
            "objective": node.body.objective,
            "state": node.body.state,
            "next": [asdict(item) for item in node.body.next],
            "done_when": node.body.done_when,
            "status": node.body.status,
            "parked": node.parked,
            "focused": node.item_id in self.focus_ids,
            "edges": [{"type": edge_type, "target": target} for edge_type, target in node.edges],
            "backing": node.anchor_kind == "F#",
            "document": node.document,
            "depth": node.depth,
            "tasks": tasks,
        }

    def _editable_children(self, key: str) -> list[str]:
        result: list[str] = []
        for child_key in self.nodes[key].children:
            child = self.nodes[child_key]
            if child.removed:
                continue
            if child.editable:
                result.append(child_key)
            else:
                result.extend(self._editable_children(child_key))
        return result

    def _nearest_editable_parent(self, key: str) -> str | None:
        parent_key = self.nodes[key].parent_key
        while parent_key is not None:
            parent = self.nodes[parent_key]
            if parent.editable and not parent.removed:
                return parent.item_id
            parent_key = parent.parent_key
        return None

    def revision(self) -> str:
        return _files_revision(self._current_pursuit_files())

    def _current_pursuit_files(self) -> dict[str, str | None]:
        paths: set[Path] = {self.memory_root / "PURSUITS.md"}
        for document in self.manifest.documents.values():
            if document.family == "pursuit":
                paths.add(document.path)
        return {
            _relative(self.memory_root, path): _read_text_exact(path) if path.is_file() else None
            for path in sorted(paths)
        }

    def get_node(self, item_id: str) -> _Node:
        node = self.nodes.get(item_id)
        if node is None or not node.editable or node.removed:
            raise PursuitWorkspaceError(f"unknown Pursuit id: {item_id}")
        return node

    def ancestor_ids(self, item_id: str) -> list[str]:
        node = self.get_node(item_id)
        result: list[str] = []
        parent_key = node.parent_key
        while parent_key is not None:
            parent = self.nodes[parent_key]
            if parent.editable and parent.item_id and not parent.removed:
                result.append(parent.item_id)
            parent_key = parent.parent_key
        result.reverse()
        return result

    def apply_operations(self, operations: Iterable[dict[str, Any]]) -> None:
        for raw_operation in operations:
            if not isinstance(raw_operation, dict):
                raise PursuitWorkspaceError("each operation must be an object")
            operation = str(raw_operation.get("op", "")).strip()
            if operation == "create":
                self._op_create(raw_operation)
            elif operation == "update":
                self._op_update(raw_operation)
            elif operation == "move":
                self._op_move(raw_operation)
            elif operation == "reorder":
                self._op_reorder(raw_operation)
            elif operation == "delete":
                self._op_delete(raw_operation)
            elif operation == "set_focus":
                self._op_set_focus(raw_operation)
            elif operation == "park":
                self._op_park(raw_operation, parked=True)
            elif operation == "unpark":
                self._op_park(raw_operation, parked=False)
            elif operation == "split_file":
                self._op_split_file(raw_operation)
            elif operation == "inline_file":
                self._op_inline_file(raw_operation)
            else:
                raise PursuitWorkspaceError(f"unknown Pursuit operation: {operation or '<empty>'}")
        self._validate_model()

    def _op_create(self, operation: dict[str, Any]) -> None:
        item_id = validate_item_id(_required_str(operation, "id"))
        if item_id in self._known_item_ids or item_id in self.nodes:
            raise PursuitWorkspaceError(f"Pursuit id already exists: {item_id}")
        title = _title_text(_required_str(operation, "title"))
        parent_key = self._resolve_parent_key(operation.get("parent_id"))
        parent = self.nodes[parent_key]
        depth = parent.depth + 1
        if depth > 4:
            raise PursuitWorkspaceError("Pursuit headings cannot be deeper than ####")
        document = _child_document(parent)
        body = _body_from_operation(operation, base=PursuitBody())
        edges = _edges_from_value(operation.get("edges", []))
        node = _Node(
            key=item_id,
            item_id=item_id,
            title=title,
            anchor_kind="#",
            edges=edges,
            depth=depth,
            document=document,
            parent_key=parent_key,
            children=[],
            raw_body="",
            body=body,
            traversal_rank=10**9 + len(self.nodes),
            source_line=0,
            dirty_body=True,
        )
        self.nodes[item_id] = node
        self._known_item_ids.add(item_id)
        index = _optional_index(operation.get("index"), len(parent.children))
        parent.children.insert(index, item_id)

    def _op_update(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        if "title" in operation:
            node.title = _title_text(_non_empty_str(operation["title"], "title"))
        if "edges" in operation:
            node.edges = _edges_from_value(operation["edges"])
        new_body = _body_from_operation(operation, base=node.body)
        if new_body != node.body:
            node.body = new_body
            node.dirty_body = True
        if node.parked and node.item_id in self.focus_ids:
            self.focus_ids = [item_id for item_id in self.focus_ids if item_id != node.item_id]

    def _op_move(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        new_parent_key = self._resolve_parent_key(operation.get("parent_id"))
        if new_parent_key == node.key or self._is_descendant(new_parent_key, node.key):
            raise PursuitWorkspaceError("cannot move a Pursuit inside its own subtree")
        new_parent = self.nodes[new_parent_key]
        new_depth = new_parent.depth + 1
        delta = new_depth - node.depth
        if max(self.nodes[key].depth for key in self._subtree_keys(node.key)) + delta > 4:
            raise PursuitWorkspaceError("move would make a Pursuit heading deeper than ####")
        self._remove_from_parent(node)
        node.parent_key = new_parent_key
        index = _optional_index(operation.get("index"), len(new_parent.children))
        new_parent.children.insert(index, node.key)
        self._shift_depth(node.key, delta)
        self._assign_subtree_document(node.key, _child_document(new_parent))

    def _op_reorder(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        parent = self.nodes[node.parent_key] if node.parent_key else None
        siblings = parent.children if parent is not None else self.root_order
        if node.key not in siblings:
            raise PursuitWorkspaceError("Pursuit is not present in its parent order")
        siblings.remove(node.key)
        siblings.insert(_optional_index(operation.get("index"), len(siblings)), node.key)

    def _op_delete(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        descendants = [key for key in self._subtree_keys(node.key) if key != node.key and self.nodes[key].editable]
        if descendants and not bool(operation.get("cascade", False)):
            raise PursuitWorkspaceError("deleting a Pursuit with children requires cascade=true")
        self._remove_from_parent(node)
        removed_ids: set[str] = set()
        for key in self._subtree_keys(node.key):
            target = self.nodes[key]
            target.removed = True
            if target.item_id:
                removed_ids.add(target.item_id)
        self.focus_ids = [item_id for item_id in self.focus_ids if item_id not in removed_ids]

    def _op_set_focus(self, operation: dict[str, Any]) -> None:
        value = operation.get("ids")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PursuitWorkspaceError("set_focus ids must be a list of Pursuit ids")
        clean: list[str] = []
        for item_id in value:
            node = self.get_node(item_id.strip())
            if node.parked:
                raise PursuitWorkspaceError(f"parked Pursuit cannot be focused: {item_id}")
            if node.item_id not in clean:
                clean.append(node.item_id or "")
        self.focus_ids = clean

    def _op_park(self, operation: dict[str, Any], *, parked: bool) -> None:
        node = self.get_node(_required_str(operation, "id"))
        node.body = PursuitBody(
            objective=node.body.objective,
            state=node.body.state,
            next=list(node.body.next),
            done_when=node.body.done_when,
            status="parked" if parked else "active",
            extra=node.body.extra,
        )
        node.dirty_body = True
        if parked and node.item_id in self.focus_ids:
            self.focus_ids = [item_id for item_id in self.focus_ids if item_id != node.item_id]

    def _op_split_file(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        if node.anchor_kind == "F#":
            return
        assert node.item_id is not None
        backing = f"PURSUIT_{node.item_id}.md"
        if (self.memory_root / backing).exists() and backing not in self.document_preambles:
            raise PursuitWorkspaceError(f"backing file already exists: {backing}")
        node.anchor_kind = "F#"
        self.document_preambles.setdefault(backing, "")
        self.document_newlines.setdefault(backing, self.document_newlines.get(node.document, "\n"))
        if backing not in self.document_order:
            self.document_order.append(backing)
        for child_key in node.children:
            self._assign_subtree_document(child_key, backing)

    def _op_inline_file(self, operation: dict[str, Any]) -> None:
        node = self.get_node(_required_str(operation, "id"))
        if node.anchor_kind != "F#":
            return
        assert node.item_id is not None
        backing = f"PURSUIT_{node.item_id}.md"
        if self.document_preambles.get(backing, "").strip():
            raise PursuitWorkspaceError(
                f"cannot inline {backing} because it has document-level prose outside child Pursuits"
            )
        node.anchor_kind = "#"
        for child_key in node.children:
            self._assign_subtree_document(child_key, node.document)

    def _resolve_parent_key(self, parent_id: object) -> str:
        if parent_id is None or (isinstance(parent_id, str) and not parent_id.strip()):
            assert self._top_container_key is not None
            return self._top_container_key
        if not isinstance(parent_id, str):
            raise PursuitWorkspaceError("parent_id must be a Pursuit id or null")
        return self.get_node(parent_id.strip()).key

    def _remove_from_parent(self, node: _Node) -> None:
        if node.parent_key is None:
            if node.key in self.root_order:
                self.root_order.remove(node.key)
        else:
            children = self.nodes[node.parent_key].children
            if node.key in children:
                children.remove(node.key)

    def _is_descendant(self, candidate_key: str, ancestor_key: str) -> bool:
        current: str | None = candidate_key
        while current is not None:
            if current == ancestor_key:
                return True
            current = self.nodes[current].parent_key
        return False

    def _subtree_keys(self, key: str) -> list[str]:
        result: list[str] = []

        def visit(current: str) -> None:
            result.append(current)
            for child in self.nodes[current].children:
                visit(child)

        visit(key)
        return result

    def _shift_depth(self, key: str, delta: int) -> None:
        for target_key in self._subtree_keys(key):
            self.nodes[target_key].depth += delta

    def _assign_subtree_document(self, key: str, document: str) -> None:
        node = self.nodes[key]
        node.document = document
        if node.anchor_kind == "F#" and node.item_id:
            child_document = f"PURSUIT_{node.item_id}.md"
            self.document_preambles.setdefault(child_document, "")
            self.document_newlines.setdefault(child_document, self.document_newlines.get(document, "\n"))
            if child_document not in self.document_order:
                self.document_order.append(child_document)
        else:
            child_document = document
        for child_key in node.children:
            self._assign_subtree_document(child_key, child_document)

    def _validate_model(self) -> None:
        active_ids = {node.item_id for node in self.nodes.values() if node.editable and not node.removed}
        if None in active_ids:
            active_ids.remove(None)
        if len(active_ids) != len([node for node in self.nodes.values() if node.editable and not node.removed]):
            raise PursuitWorkspaceError("Pursuit ids must remain unique")
        if len(self.focus_ids) != len(set(self.focus_ids)):
            raise PursuitWorkspaceError("Focus ids must remain unique")
        for item_id in self.focus_ids:
            node = self.get_node(item_id)
            if node.parked:
                raise PursuitWorkspaceError(f"parked Pursuit cannot be focused: {item_id}")
        for node in self.nodes.values():
            if node.removed:
                continue
            if node.depth < 1 or node.depth > 4:
                raise PursuitWorkspaceError(f"invalid heading depth for {node.key}: {node.depth}")
            if node.editable:
                if not node.title.strip():
                    raise PursuitWorkspaceError(f"Pursuit title must not be empty: {node.item_id}")
                seen_edges: set[tuple[str, str]] = set()
                for edge_type, target in node.edges:
                    if edge_type not in KNOWN_EDGE_TYPES:
                        raise PursuitWorkspaceError(f"unknown edge type: {edge_type}")
                    if target == node.item_id:
                        raise PursuitWorkspaceError(f"self edge is not allowed: {edge_type}:{target}")
                    if (edge_type, target) in seen_edges:
                        raise PursuitWorkspaceError(f"duplicate edge: {edge_type}:{target}")
                    seen_edges.add((edge_type, target))
                    if target not in self._known_item_ids:
                        raise PursuitWorkspaceError(f"edge target does not exist: {target}")
                if node.anchor_kind == "F#" and node.item_id:
                    expected = f"PURSUIT_{node.item_id}.md"
                    for child_key in node.children:
                        if self.nodes[child_key].document != expected:
                            raise PursuitWorkspaceError(
                                f"children of F# Pursuit {node.item_id} must live in {expected}"
                            )

    def render_files(self) -> dict[str, str | None]:
        self._validate_model()
        files: dict[str, str | None] = {}
        active_documents = {node.document for node in self.nodes.values() if not node.removed}
        active_documents.add("PURSUITS.md")
        for document in sorted(active_documents, key=self._document_sort_key):
            roots = self._document_roots(document)
            preamble = self.document_preambles.get(document, "")
            chunks: list[str] = []
            if preamble.strip():
                chunks.append(preamble.strip("\n"))
            for key in roots:
                chunks.append(self._render_node(key, document).strip("\n"))
            text = "\n\n".join(chunk for chunk in chunks if chunk).rstrip() + "\n"
            newline = self.document_newlines.get(document, self.document_newlines.get("PURSUITS.md", "\n"))
            files[document] = text if newline == "\n" else text.replace("\n", newline)

        current_files = self._current_pursuit_files()
        for document in current_files:
            if document != "PURSUITS.md" and document not in active_documents:
                files[document] = None
        return files

    def _document_sort_key(self, document: str) -> tuple[int, str]:
        try:
            return (self.document_order.index(document), document)
        except ValueError:
            return (10**9, document)

    def _document_roots(self, document: str) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def visit(key: str) -> None:
            node = self.nodes[key]
            if node.removed:
                return
            parent = self.nodes[node.parent_key] if node.parent_key else None
            if node.document == document and (parent is None or parent.document != document):
                if key not in seen:
                    ordered.append(key)
                    seen.add(key)
            for child in node.children:
                visit(child)

        for key in self.root_order:
            visit(key)
        return ordered

    def _render_node(self, key: str, document: str) -> str:
        node = self.nodes[key]
        if node.removed or node.document != document:
            return ""
        lines = [self._heading_line(node)]
        body = self._body_for_render(node)
        if body.strip():
            lines.extend(["", body.strip("\n")])
        for child_key in node.children:
            child = self.nodes[child_key]
            if child.removed or child.document != document:
                continue
            rendered = self._render_node(child_key, document)
            if rendered:
                lines.extend(["", rendered.strip("\n")])
        return "\n".join(lines)

    def _heading_line(self, node: _Node) -> str:
        prefix = "#" * node.depth
        if node.item_id is None:
            return f"{prefix} {node.title}"
        marker = "F#" if node.anchor_kind == "F#" else "#"
        line = f"{prefix} {node.title} {{{marker}{node.item_id}}}"
        if node.edges:
            line += " → [" + ", ".join(f"{edge_type}:{target}" for edge_type, target in node.edges) + "]"
        return line

    def _body_for_render(self, node: _Node) -> str:
        if node.is_focus:
            if not self.focus_ids:
                return "No Pursuit is focused yet."
            return "\n".join(f"- `{item_id}`" for item_id in self.focus_ids)
        if not node.dirty_body:
            return node.raw_body
        return _render_body(node.body)


def preview_operations(
    memory_root: Path,
    operations: Iterable[dict[str, Any]],
    *,
    expected_revision: str | None = None,
) -> PursuitPreview:
    editor = PursuitEditor(memory_root)
    revision = editor.revision()
    if expected_revision is not None and expected_revision != revision:
        raise PursuitRevisionConflict("Pursuit files changed since the workspace was loaded")
    before = editor._current_pursuit_files()
    editor.apply_operations(operations)
    after = editor.render_files()
    _validate_candidate(editor.memory_root, editor.manifest, after)
    changed = tuple(sorted(path for path, text in after.items() if before.get(path) != text and text is not None))
    removed = tuple(sorted(path for path, text in after.items() if text is None and before.get(path) is not None))
    diff = _files_diff(before, after)
    candidate_revision = _files_revision(after)
    return PursuitPreview(
        revision=revision,
        candidate_revision=candidate_revision,
        diff=diff,
        changed_files=changed,
        removed_files=removed,
        files=after,
        snapshot=editor.snapshot() | {"revision": candidate_revision},
    )


def apply_operations(
    memory_root: Path,
    operations: Iterable[dict[str, Any]],
    *,
    expected_revision: str | None = None,
    commit: bool = False,
    commit_message: str = "pursuit: edit via Pursuit Studio",
) -> PursuitApplyResult:
    root = Path(memory_root).expanduser().resolve()
    operation_list = [dict(operation) for operation in operations]
    with MemoryWriteLock(root):
        preview = preview_operations(root, operation_list, expected_revision=expected_revision)
        before = PursuitEditor(root)._current_pursuit_files()
        if commit:
            _require_clean_git_paths(root, (*preview.changed_files, *preview.removed_files))
        _write_file_transaction(root, preview.files)
        actual = PursuitEditor(root)
        if actual.revision() != preview.candidate_revision:
            _write_file_transaction(root, before)
            raise PursuitWorkspaceError("written Pursuit files did not match the validated candidate")
        _record_history(root, before, preview.files, preview.revision, preview.candidate_revision)
        commit_sha = _commit_files(root, (*preview.changed_files, *preview.removed_files), commit_message) if commit else None
        return PursuitApplyResult(
            revision=actual.revision(),
            changed_files=preview.changed_files,
            removed_files=preview.removed_files,
            commit=commit_sha,
            snapshot=actual.snapshot(),
        )


def undo(memory_root: Path, *, commit: bool = False) -> PursuitApplyResult:
    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        history = _load_history(root)
        cursor = int(history.get("cursor", 0))
        entries = history.get("entries", [])
        if cursor <= 0 or not isinstance(entries, list):
            raise PursuitWorkspaceError("nothing to undo")
        entry = entries[cursor - 1]
        current = PursuitEditor(root)
        if current.revision() != entry["after_revision"]:
            raise PursuitRevisionConflict("Pursuit files changed after the recorded edit; undo refused")
        before = _decode_history_files(entry["before"])
        _validate_candidate(root, current.manifest, before)
        current_files = current._current_pursuit_files()
        changed, removed = _changed_paths(current_files, before)
        if commit:
            _require_clean_git_paths(root, (*changed, *removed))
        _write_file_transaction(root, before)
        history["cursor"] = cursor - 1
        _save_history(root, history)
        restored = PursuitEditor(root)
        commit_sha = _commit_files(root, (*changed, *removed), "pursuit: undo Pursuit Studio edit") if commit else None
        return PursuitApplyResult(restored.revision(), changed, removed, commit_sha, restored.snapshot())


def redo(memory_root: Path, *, commit: bool = False) -> PursuitApplyResult:
    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        history = _load_history(root)
        cursor = int(history.get("cursor", 0))
        entries = history.get("entries", [])
        if not isinstance(entries, list) or cursor >= len(entries):
            raise PursuitWorkspaceError("nothing to redo")
        entry = entries[cursor]
        current = PursuitEditor(root)
        if current.revision() != entry["before_revision"]:
            raise PursuitRevisionConflict("Pursuit files changed after undo; redo refused")
        after = _decode_history_files(entry["after"])
        _validate_candidate(root, current.manifest, after)
        current_files = current._current_pursuit_files()
        changed, removed = _changed_paths(current_files, after)
        if commit:
            _require_clean_git_paths(root, (*changed, *removed))
        _write_file_transaction(root, after)
        history["cursor"] = cursor + 1
        _save_history(root, history)
        restored = PursuitEditor(root)
        commit_sha = _commit_files(root, (*changed, *removed), "pursuit: redo Pursuit Studio edit") if commit else None
        return PursuitApplyResult(restored.revision(), changed, removed, commit_sha, restored.snapshot())


def _heading_title(line: str) -> str:
    match = _HEADING_TITLE_RE.match(line)
    if match is not None:
        return match.group(2).strip()
    plain = _PLAIN_HEADING_RE.match(line)
    if plain is not None:
        return plain.group(2).strip()
    raise PursuitWorkspaceError(f"could not parse Pursuit heading: {line}")


def _body_text(lines: tuple[str, ...], span: Any | None) -> str:
    if span is None:
        return ""
    return "\n".join(lines[span.start_line - 1 : span.end_line])


def _parse_body(raw: str) -> PursuitBody:
    lines = raw.splitlines()
    fields: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = _FIELD_RE.match(line)
        if match is not None:
            fields.append((index, match.group(1).casefold(), match.group(2)))
    first_field = fields[0][0] if fields else len(lines)
    objective = "\n".join(lines[:first_field]).strip()
    state = ""
    next_items: list[PursuitNext] = []
    done_when = ""
    status = "active"
    extra_chunks: list[str] = []
    for position, (index, name, inline) in enumerate(fields):
        end = fields[position + 1][0] if position + 1 < len(fields) else len(lines)
        section_lines = ([inline] if inline else []) + lines[index + 1 : end]
        if name == "next":
            leftovers: list[str] = []
            for line in section_lines:
                if not line.strip():
                    continue
                match = _NEXT_RE.match(line)
                if match is not None:
                    next_items.append(PursuitNext(match.group(1), match.group(2)))
                elif next_items and (line.startswith("  ") or line.startswith("\t")):
                    next_items[-1].text = f"{next_items[-1].text} {line.strip()}"
                else:
                    leftovers.append(line)
            if leftovers:
                extra_chunks.append("\n".join(leftovers).strip())
        else:
            value = "\n".join(section_lines).strip()
            if name == "state":
                state = value
            elif name == "done when":
                done_when = value
            elif name == "status":
                status = value.casefold() or "active"
    if status not in {"active", "parked"}:
        status = status.strip() or "active"
    return PursuitBody(objective, state, next_items, done_when, status, "\n\n".join(chunk for chunk in extra_chunks if chunk))


def _render_body(body: PursuitBody) -> str:
    chunks: list[str] = []
    if body.objective.strip():
        chunks.append(body.objective.strip())
    if body.state.strip():
        chunks.append(f"**State:** {body.state.strip()}")
    if body.next:
        chunks.append("**Next:**\n" + "\n".join(f"- `{item.kind}` {item.text}" for item in body.next))
    if body.done_when.strip():
        chunks.append(f"**Done when:** {body.done_when.strip()}")
    if body.status.casefold() == "parked":
        chunks.append("**Status:** parked")
    if body.extra.strip():
        chunks.append(body.extra.strip())
    return "\n\n".join(chunks)


def _body_from_operation(operation: dict[str, Any], *, base: PursuitBody) -> PursuitBody:
    objective = _optional_text(operation, "objective", base.objective)
    state = _optional_text(operation, "state", base.state)
    done_when = _optional_text(operation, "done_when", base.done_when)
    status = _optional_text(operation, "status", base.status).casefold() or "active"
    if status not in {"active", "parked"}:
        raise PursuitWorkspaceError("Pursuit status must be active or parked")
    extra = _optional_text(operation, "extra", base.extra)
    if "next" in operation:
        next_value = operation["next"]
        if not isinstance(next_value, list):
            raise PursuitWorkspaceError("next must be a list")
        next_items: list[PursuitNext] = []
        for item in next_value:
            if isinstance(item, dict):
                next_items.append(PursuitNext(_required_str(item, "kind"), _required_str(item, "text")))
            elif isinstance(item, str):
                kind, separator, text = item.partition(":")
                if not separator:
                    raise PursuitWorkspaceError("string Next entries must use kind:text")
                next_items.append(PursuitNext(kind, text))
            else:
                raise PursuitWorkspaceError("Next entries must be objects or kind:text strings")
    else:
        next_items = list(base.next)
    for label, text in (("objective", objective), ("state", state), ("done_when", done_when), ("extra", extra)):
        _validate_structured_body_text(text, label)
    return PursuitBody(objective, state, next_items, done_when, status, extra)


def _edges_from_value(value: object) -> list[tuple[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PursuitWorkspaceError("edges must be a list")
    result: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            edge_type = _required_str(item, "type")
            target = validate_item_id(_required_str(item, "target"))
        elif isinstance(item, str):
            edge_type, separator, target = item.partition(":")
            if not separator:
                raise PursuitWorkspaceError("string edges must use type:target")
            edge_type = edge_type.strip()
            target = validate_item_id(target.strip())
        else:
            raise PursuitWorkspaceError("edge entries must be objects or type:target strings")
        result.append((edge_type, target))
    return result



def _title_text(value: str) -> str:
    value = value.strip()
    if any(character in value for character in "\r\n\x00{}"):
        raise PursuitWorkspaceError("Pursuit title must be one line and must not contain braces")
    return value


def _validate_structured_body_text(value: str, label: str) -> None:
    if "\x00" in value:
        raise PursuitWorkspaceError(f"{label} must not contain NUL")
    for line in value.splitlines():
        if re.match(r"^ {0,3}#{1,4}(?:\s|$)", line):
            raise PursuitWorkspaceError(f"{label} must not introduce Markdown headings")
        if re.match(r"^ {0,3}(?:`{3,}|~{3,})", line):
            raise PursuitWorkspaceError(f"{label} must not introduce fenced blocks")
        if _FIELD_RE.match(line):
            raise PursuitWorkspaceError(f"{label} must not introduce Pursuit field markers")
        if re.match(r"^\s*-\s+`[^`]+`.*(?:→|->)\s*\[", line):
            raise PursuitWorkspaceError(f"{label} must not introduce graph nodes")


def _required_str(payload: dict[str, Any], key: str) -> str:
    if key not in payload:
        raise PursuitWorkspaceError(f"missing required field: {key}")
    return _non_empty_str(payload[key], key)


def _non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PursuitWorkspaceError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(payload: dict[str, Any], key: str, default: str) -> str:
    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PursuitWorkspaceError(f"{key} must be a string")
    return value.strip()


def _optional_index(value: object, length: int) -> int:
    if value is None:
        return length
    if isinstance(value, bool) or not isinstance(value, int):
        raise PursuitWorkspaceError("index must be an integer")
    return max(0, min(value, length))


def _child_document(parent: _Node) -> str:
    if parent.anchor_kind == "F#" and parent.item_id:
        return f"PURSUIT_{parent.item_id}.md"
    return parent.document


def _validate_candidate(root: Path, manifest: GraphManifest, pursuit_files: dict[str, str | None]) -> None:
    runtime = root / ".runtime" / "pursuit-studio"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=runtime) as tempdir:
        candidate = Path(tempdir)
        for path in manifest.files:
            if not path.is_file() or path.is_symlink():
                continue
            relative = _relative(root, path)
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        for relative, text in pursuit_files.items():
            target = candidate / relative
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="")
        candidate_manifest = build_graph_manifest(candidate)
        if candidate_manifest.errors:
            joined = "\n- ".join(candidate_manifest.errors)
            raise PursuitWorkspaceError(f"candidate Pursuit graph is invalid:\n- {joined}")
        _validate_candidate_task_links(root, candidate_manifest)



def _validate_candidate_task_links(root: Path, candidate_manifest: GraphManifest) -> None:
    registry_path = root / "pursuit_tasks.toml"
    if not registry_path.exists():
        return
    from .pursuit_tasks import load_registry

    registry = load_registry(root)
    candidate_ids = {
        item.id
        for item in candidate_manifest.items.values()
        if item.family == "pursuit" and item.item_kind == "heading"
    }
    for task in registry.tasks:
        missing = [item_id for item_id in task.pursuit_ids if item_id not in candidate_ids]
        if missing:
            joined = ", ".join(missing)
            raise PursuitWorkspaceError(
                f"unlink Pursuit {joined} from task {task.task_id} before removing it"
            )


def _read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _files_revision(files: dict[str, str | None]) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path, value in files.items() if value is not None):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        value = files[path]
        assert value is not None
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _files_diff(before: dict[str, str | None], after: dict[str, str | None]) -> str:
    chunks: list[str] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        old_lines = [] if old is None else old.splitlines(keepends=True)
        new_lines = [] if new is None else new.splitlines(keepends=True)
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}" if new is not None else "/dev/null",
            )
        )
    return "".join(chunks)


def _write_file_transaction(root: Path, files: dict[str, str | None]) -> None:
    originals: dict[str, bytes | None] = {}
    for relative in files:
        path = _safe_root_path(root, relative)
        originals[relative] = path.read_bytes() if path.is_file() else None
    try:
        for relative, text in files.items():
            path = _safe_root_path(root, relative)
            if text is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_text(path, text)
    except Exception:
        for relative, data in originals.items():
            path = _safe_root_path(root, relative)
            if data is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, data)
        raise


def _safe_root_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PursuitWorkspaceError(f"path escapes RightMemory root: {relative}") from exc
    if path.exists() and path.is_symlink():
        raise PursuitWorkspaceError(f"refusing to write symlink: {relative}")
    return path


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _record_history(
    root: Path,
    before: dict[str, str | None],
    after: dict[str, str | None],
    before_revision: str,
    after_revision: str,
) -> None:
    history = _load_history(root)
    entries = history.get("entries", [])
    cursor = int(history.get("cursor", 0))
    if not isinstance(entries, list):
        entries = []
    entries = entries[:cursor]
    entries.append(
        {
            "created_at": datetime.now(UTC).isoformat(),
            "before_revision": before_revision,
            "after_revision": after_revision,
            "before": before,
            "after": after,
        }
    )
    if len(entries) > _HISTORY_LIMIT:
        entries = entries[-_HISTORY_LIMIT:]
    history = {"version": 1, "cursor": len(entries), "entries": entries}
    _save_history(root, history)


def _load_history(root: Path) -> dict[str, Any]:
    path = root / _HISTORY_PATH
    if not path.is_file():
        return {"version": 1, "cursor": 0, "entries": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PursuitWorkspaceError(f"invalid Pursuit Studio history: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise PursuitWorkspaceError("invalid Pursuit Studio history version")
    return value


def _save_history(root: Path, history: dict[str, Any]) -> None:
    path = root / _HISTORY_PATH
    _atomic_write_text(path, json.dumps(history, ensure_ascii=False, indent=2) + "\n")


def _decode_history_files(value: object) -> dict[str, str | None]:
    if not isinstance(value, dict):
        raise PursuitWorkspaceError("invalid Pursuit Studio history file map")
    result: dict[str, str | None] = {}
    for path, text in value.items():
        if not isinstance(path, str) or (text is not None and not isinstance(text, str)):
            raise PursuitWorkspaceError("invalid Pursuit Studio history file entry")
        result[path] = text
    return result


def _changed_paths(before: dict[str, str | None], after: dict[str, str | None]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed = tuple(sorted(path for path, value in after.items() if value is not None and before.get(path) != value))
    removed = tuple(sorted(path for path, value in after.items() if value is None and before.get(path) is not None))
    return changed, removed



def _require_clean_git_paths(root: Path, paths: Iterable[str]) -> None:
    unique = sorted(set(paths))
    if not unique:
        return
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--", *unique],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if status.returncode != 0:
        raise PursuitWorkspaceError(status.stderr.strip() or "could not inspect Memory Git state")
    if status.stdout.strip():
        raise PursuitWorkspaceError(
            "cannot commit Pursuit edit because affected files already have uncommitted changes"
        )


def _commit_files(root: Path, paths: Iterable[str], message: str) -> str | None:
    unique = sorted(set(paths))
    if not unique:
        return None
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--", *unique],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if status.returncode != 0:
        raise PursuitWorkspaceError(status.stderr.strip() or "could not inspect Memory Git state")
    if not status.stdout.strip():
        return None
    add = subprocess.run(
        ["git", "-C", str(root), "add", "--", *unique],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if add.returncode != 0:
        raise PursuitWorkspaceError(add.stderr.strip() or "could not stage Pursuit files")
    commit = subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message, "--", *unique],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if commit.returncode != 0:
        raise PursuitWorkspaceError(commit.stderr.strip() or commit.stdout.strip() or "could not commit Pursuit files")
    rev = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return rev.stdout.strip()


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()
