from __future__ import annotations

import copy
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from .graph import (
    BlockKey, DocumentBlock, GraphManifest, LIST_PREFIX_RE,
    block_content_hash, build_graph_manifest,
)
from .shared_view_models import load_connections
from .shared_view_package import FileViewPackageError, ValidatedFileViewPackage, validate_file_view_package


ACTIVE_RETRIEVE_VIEW: ContextVar[RetrieveView | None] = ContextVar("rightmemory_retrieve_view", default=None)


@dataclass
class RetrieveIndex:
    """Selection addresses over original indexed blocks, separate from graph IDs."""

    manifest: GraphManifest
    ids: dict[str, DocumentBlock] = field(default_factory=dict)
    block_ids: dict[BlockKey, str] = field(default_factory=dict)
    versions: dict[BlockKey, str] = field(default_factory=dict)
    coverage: dict[str, DocumentBlock] = field(default_factory=dict)

    def coverage_key(self, block: DocumentBlock) -> str:
        if block.item_id is not None:
            return block.item_id
        relative = block.source_path.relative_to(self.manifest.root).as_posix()
        # This is a location for comparing delivered content, never a saved identity.
        location = json.dumps([relative, block.line_number], separators=(",", ":"))
        return "@" + location

    def delivered_block(self, key: str, version: str, *, unchanged_only: bool) -> DocumentBlock | None:
        block = self.coverage.get(key)
        if block is None:
            return None
        # Without a stored ID, changed content at the same location is not the
        # previously delivered item. Require a fresh model selection.
        if (block.item_id is None or unchanged_only) and self.versions[block.key] != version:
            return None
        return block

    def document_text(self, path: Path) -> str:
        document = self.manifest.documents[path]
        lines = document.text.splitlines(keepends=True)
        for block in self.ids.values():
            if block.source_path != path or block.item_id is not None or block.kind == "root":
                continue
            number = block.line_number - 1
            raw = lines[number]
            line = raw.rstrip("\r\n")
            ending = raw[len(line):]
            selector = self.block_ids[block.key]
            if block.kind == "heading":
                # Insert before optional closing ATX markers.
                line = re.sub(r"[ \t]+#+[ \t]*$", "", line).rstrip()
                line += f" {{#{selector}}}"
            else:
                prefix = LIST_PREFIX_RE.match(line)
                assert prefix is not None
                marker = line[:prefix.end()]
                line = marker + ("" if marker[-1:].isspace() else " ") + f"`{selector}` " + line[prefix.end():] + " → []"
            lines[number] = line + ending
        root = self.manifest.blocks[document.root_key]
        root_id = self.block_ids[root.logical_parent if root.logical_parent is not None else root.key]
        return f"===== {document.relative_path} {{#{root_id}}} =====\n" + "".join(lines)


class RetrieveView:
    """One indexed read view and its session-local, never-reused selection IDs."""

    def __init__(self, memory_root: Path, state: dict | None = None):
        self.root = Path(memory_root).resolve()
        self.state = copy.deepcopy({"next_id": 1, "issued": [], "bindings": {}} if state is None or state == {} else state)
        self._validate_state()
        self.packages: dict[str, ValidatedFileViewPackage] = {}
        self.package_errors: dict[str, str] = {}
        local = build_graph_manifest(self.root)
        manifests = {"local": local}
        connections = load_connections(self.root)
        for item in local.headings.values():
            if item.anchor_kind != "MF#":
                continue
            connection = connections.get(item.id)
            expected = connection.target.view_id if connection and connection.target.view_id else item.id
            try:
                package = validate_file_view_package(
                    self.root / ".runtime/shared_views/imports" / item.id,
                    expected_view_id=expected, namespace_id=item.id,
                )
            except (OSError, FileViewPackageError) as exc:
                self.package_errors[item.id] = str(exc)
                continue
            self.packages[item.id] = package
            manifests[f"MF#{item.id}"] = package.manifest

        stored_ids = {item_id for manifest in manifests.values() for item_id in manifest.items}
        issued = set(self.state["issued"])
        if issued & stored_ids:
            raise ValueError("a stored ID now conflicts with this retrieval session's temporary IDs; start a new retrieval session")
        occupied = stored_ids | issued
        old_bindings = self.state["bindings"]
        bindings = {}
        self.indexes: dict[str, RetrieveIndex] = {}
        for namespace, manifest in manifests.items():
            index = RetrieveIndex(manifest)
            for block in sorted(manifest.blocks.values(), key=lambda b: (b.source_path.as_posix(), b.line_number)):
                if block.kind not in {"root", "heading", "node"}:
                    continue
                if block.kind == "root" and block.logical_parent is not None:
                    continue
                selector = block.item_id
                if selector is None:
                    identity = json.dumps([namespace, manifest.snapshot_hash,
                                           block.source_path.relative_to(manifest.root).as_posix(),
                                           block.line_number], separators=(",", ":"))
                    selector = old_bindings.get(identity)
                    if selector is None:
                        while (selector := f"r{self.state['next_id']}") in occupied:
                            self.state["next_id"] += 1
                        self.state["next_id"] += 1
                        occupied.add(selector)
                        issued.add(selector)
                    bindings[identity] = selector
                index.ids[selector] = block
                index.block_ids[block.key] = selector
                index.versions[block.key] = (
                    manifest.items[block.item_id].content_hash
                    if block.item_id is not None else block_content_hash(manifest, block)
                )
                index.coverage[index.coverage_key(block)] = block
            self.indexes[namespace] = index
        self.state["bindings"] = bindings
        self.state["issued"] = sorted(issued)

    @property
    def local(self) -> RetrieveIndex:
        return self.indexes["local"]

    def _validate_state(self) -> None:
        state = self.state
        if (not isinstance(state, dict) or set(state) != {"next_id", "issued", "bindings"}
                or type(state["next_id"]) is not int or state["next_id"] < 1
                or not isinstance(state["issued"], list)
                or any(not isinstance(value, str) or re.fullmatch(r"r[1-9][0-9]*", value) is None for value in state["issued"])
                or not isinstance(state["bindings"], dict)
                or any(not isinstance(key, str) or not isinstance(value, str) or value not in state["issued"]
                       for key, value in state["bindings"].items())
                or len(set(state["bindings"].values())) != len(state["bindings"])):
            raise ValueError("retrieve selection ID state is malformed")

    def assert_current(self) -> None:
        for index in self.indexes.values():
            for document in index.manifest.documents.values():
                try:
                    with document.path.open(encoding="utf-8", newline="") as source:
                        current = source.read()
                except OSError as exc:
                    raise ValueError("retrieval source changed; retrieve again from a refreshed view") from exc
                if current != document.text:
                    raise ValueError("retrieval source changed; retrieve again from a refreshed view")

    def write_files(self, session_id: str) -> Path:
        """Publish the same graph reading copies for shell-based CLI agents."""
        from .session import _ensure_runtime_gitignore, _safe_session_id

        _ensure_runtime_gitignore(self.root / ".runtime")
        directory = self.root / ".runtime/retrieve_context/views" / _safe_session_id(session_id)
        for namespace, index in self.indexes.items():
            base = directory / ("local" if namespace == "local" else "imports/" + namespace[3:])
            for document in index.manifest.documents.values():
                target = base / document.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(index.document_text(document.path), encoding="utf-8", newline="")
        return directory
