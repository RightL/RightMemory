from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .graph import ANCHOR_RE, NODE_RE, GraphManifest, build_graph_manifest
from .recent_submitted import RecentSubmittedMemoryEntry


NO_STRONG_MATCH = "no strong match"
SOURCE_ID_RE = re.compile(r"^(M#|S#|MF#)([A-Za-z0-9_.-]+)$")
PLAIN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
HEADING_RE = re.compile(r"^(#{1,})\s+")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
MF_CANONICAL_PATH = Path(".runtime/shared_views/imports")


class RetrieveSelectionError(ValueError):
    """A terminal retrieve selection could not be resolved safely."""


class LineRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> LineRange:
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class SourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    ids: list[str] = Field(default_factory=list)
    ranges: list[LineRange] = Field(default_factory=list)

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if SOURCE_ID_RE.fullmatch(value) is None:
            raise ValueError("source_id must be an M#, S#, or MF# id")
        return value

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if PLAIN_ID_RE.fullmatch(value) is None:
                raise ValueError(f"invalid source-scoped id: {value!r}")
        return values


class RetrieveSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ids: list[str] = Field(default_factory=list)
    sources: list[SourceSelection] = Field(default_factory=list)
    recent_candidates: list[str] = Field(default_factory=list)

    @field_validator("ids")
    @classmethod
    def validate_local_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if PLAIN_ID_RE.fullmatch(value) is None:
                raise ValueError(f"invalid local graph id: {value!r}")
        return values

    @field_validator("recent_candidates")
    @classmethod
    def validate_recent_candidates(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or value != value.strip():
                raise ValueError("recent candidate ids must be non-empty and must not contain outer whitespace")
        return values


@dataclass(frozen=True)
class DeliveredRange:
    source_id: str
    start: int
    end: int
    source_hash: str


@dataclass(frozen=True)
class RetrieveDeliveryCoverage:
    local_items: dict[str, str] = field(default_factory=dict)
    source_items: dict[str, str] = field(default_factory=dict)
    complete_sources: dict[str, str] = field(default_factory=dict)
    ranges: list[DeliveredRange] = field(default_factory=list)
    recent_candidates: list[str] = field(default_factory=list)

    def merged(self, newer: RetrieveDeliveryCoverage) -> RetrieveDeliveryCoverage:
        local_items = {**self.local_items, **newer.local_items}
        source_items = {**self.source_items, **newer.source_items}
        complete_sources = {**self.complete_sources, **newer.complete_sources}

        changed_range_sources = {
            item.source_id
            for item in newer.ranges
            if any(
                old.source_id == item.source_id and old.source_hash != item.source_hash
                for old in self.ranges
            )
        }
        ranges = [item for item in self.ranges if item.source_id not in changed_range_sources]
        known_ranges = {(item.source_id, item.start, item.end, item.source_hash) for item in ranges}
        for item in newer.ranges:
            key = (item.source_id, item.start, item.end, item.source_hash)
            if key not in known_ranges:
                ranges.append(item)
                known_ranges.add(key)

        recent = list(dict.fromkeys((*self.recent_candidates, *newer.recent_candidates)))
        return RetrieveDeliveryCoverage(
            local_items=local_items,
            source_items=source_items,
            complete_sources=complete_sources,
            ranges=ranges,
            recent_candidates=recent,
        )


@dataclass(frozen=True)
class RenderedRetrieveSelection:
    text: str
    delivery: RetrieveDeliveryCoverage
    recent_entries: list[RecentSubmittedMemoryEntry]


@dataclass(eq=False)
class _Entry:
    kind: str
    line: str = ""
    depth: int = 0
    line_number: int = 0
    end_line: int = 0
    item_id: str | None = None
    item_kind: str | None = None
    anchor_kind: str | None = None
    family: str = ""
    source_path: Path | None = None
    focus_target: str | None = None
    parent: _Entry | None = None
    parts: list[str | _Entry] = field(default_factory=list)
    rank: int = -1


class _TreeParser:
    def __init__(self, manifest: GraphManifest | None = None):
        self.manifest = manifest
        self.by_id: dict[str, _Entry] = {}
        self.duplicates: set[str] = set()
        self._items_by_location: dict[tuple[Path, int], object] = {}
        self._focus_by_location: dict[tuple[Path, int], str] = {}
        if manifest is not None:
            self._items_by_location = {
                (item.file.resolve(), item.line_number): item for item in manifest.items.values()
            }
            self._focus_by_location = {
                (path.resolve(), line_number): item_id
                for item_id, path, line_number in manifest.focus_ids
            }

    def parse(self, path: Path, family: str, *, scoped: bool = False) -> _Entry:
        root = _Entry(kind="root", family=family, source_path=path)
        stack: list[_Entry] = []
        fence_char: str | None = None
        fence_length = 0
        text = _read_text(path)
        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            parent = stack[-1] if stack else root
            fence = FENCE_RE.match(line)
            if fence is not None:
                parent.parts.append(line)
                marker = fence.group(1)
                if fence_char is None:
                    fence_char = marker[0]
                    fence_length = len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = None
                    fence_length = 0
                continue
            if fence_char is not None:
                parent.parts.append(line)
                continue

            heading = HEADING_RE.match(line)
            if heading is not None:
                depth = len(heading.group(1))
                while stack and stack[-1].depth >= depth:
                    stack.pop().end_line = line_number - 1
                parent = stack[-1] if stack else root
                graph_item = self._items_by_location.get((path.resolve(), line_number))
                item_id = getattr(graph_item, "id", None)
                anchor_kind = getattr(graph_item, "anchor_kind", None)
                item_kind = getattr(graph_item, "item_kind", None)
                if scoped:
                    anchor = ANCHOR_RE.match(line)
                    if anchor is not None:
                        anchor_kind = anchor.group(2)
                        item_id = anchor.group(3)
                        item_kind = "heading"
                entry = _Entry(
                    kind="heading",
                    line=line,
                    depth=depth,
                    line_number=line_number,
                    end_line=line_number,
                    item_id=item_id,
                    item_kind=item_kind,
                    anchor_kind=anchor_kind,
                    family=family,
                    source_path=path,
                    parent=parent,
                )
                parent.parts.append(entry)
                stack.append(entry)
                self._register(entry)
                continue

            focus_target = self._focus_by_location.get((path.resolve(), line_number))
            if focus_target is not None:
                entry = _Entry(
                    kind="focus",
                    line=line,
                    line_number=line_number,
                    end_line=line_number,
                    family=family,
                    source_path=path,
                    focus_target=focus_target,
                    parent=parent,
                )
                parent.parts.append(entry)
                continue

            graph_item = self._items_by_location.get((path.resolve(), line_number))
            item_id = getattr(graph_item, "id", None)
            item_kind = getattr(graph_item, "item_kind", None)
            if scoped:
                node = NODE_RE.match(line)
                if node is not None:
                    item_id = node.group(1)
                    item_kind = "node"
            if item_id is not None and item_kind == "node":
                entry = _Entry(
                    kind="node",
                    line=line,
                    line_number=line_number,
                    end_line=line_number,
                    item_id=item_id,
                    item_kind="node",
                    family=family,
                    source_path=path,
                    parent=parent,
                )
                parent.parts.append(entry)
                self._register(entry)
            else:
                parent.parts.append(line)
        for entry in stack:
            entry.end_line = len(lines)
        root.end_line = len(lines)
        return root

    def _register(self, entry: _Entry) -> None:
        if entry.item_id is None:
            return
        if entry.item_id in self.by_id:
            self.duplicates.add(entry.item_id)
            return
        self.by_id[entry.item_id] = entry


class _LogicalGraph:
    def __init__(self, memory_root: Path, manifest: GraphManifest):
        self.memory_root = memory_root
        self.manifest = manifest
        self.parser = _TreeParser(manifest)
        self.roots: list[_Entry] = []
        for name, family in (("MEMORY.md", "memory"), ("PURSUITS.md", "pursuit")):
            path = memory_root / name
            if _safe_source_file(memory_root, path):
                root = self.parser.parse(path, family)
                self._expand_f_backing(root, set())
                self.roots.append(root)
        rank = 0
        for root in self.roots:
            for entry in _walk_entries(root):
                entry.rank = rank
                rank += 1

    @property
    def by_id(self) -> dict[str, _Entry]:
        return self.parser.by_id

    def _expand_f_backing(self, root: _Entry, path_stack: set[Path]) -> None:
        for part in list(root.parts):
            if not isinstance(part, _Entry):
                continue
            self._expand_f_backing(part, path_stack)
            if part.anchor_kind != "F#" or part.item_id is None:
                continue
            reference = self.manifest.backing.get(part.item_id)
            if (
                reference is None
                or reference.kind != "F#"
                or not _safe_source_file(self.memory_root, reference.path)
            ):
                continue
            resolved = reference.path.resolve()
            if resolved in path_stack:
                raise RetrieveSelectionError(f"cyclic F# backing path for local id `{part.item_id}`")
            detail_root = self.parser.parse(reference.path, part.family)
            self._expand_f_backing(detail_root, {*path_stack, resolved})
            detail_parts = list(detail_root.parts)
            if detail_parts:
                if not part.parts or not isinstance(part.parts[-1], str) or part.parts[-1].strip():
                    part.parts.append("")
                for child in detail_parts:
                    if isinstance(child, _Entry):
                        child.parent = part
                    part.parts.append(child)


@dataclass(frozen=True)
class _ResolvedRanges:
    text: str
    delivered: list[DeliveredRange]


@dataclass(frozen=True)
class _RenderedSource:
    rank: int
    source_id: str
    text: str
    source_items: dict[str, str] = field(default_factory=dict)
    complete_sources: dict[str, str] = field(default_factory=dict)
    ranges: list[DeliveredRange] = field(default_factory=list)


class RetrieveSelectionRenderer:
    def __init__(self, memory_root: Path, *, max_output_chars: int):
        self.memory_root = Path(memory_root).resolve()
        self.max_output_chars = max_output_chars

    def render(
        self,
        selection: RetrieveSelection,
        *,
        delivered: RetrieveDeliveryCoverage | None = None,
        recent_entries: list[RecentSubmittedMemoryEntry] | None = None,
        include_returned: bool = False,
    ) -> RenderedRetrieveSelection:
        delivered = delivered or RetrieveDeliveryCoverage()
        recent_entries = recent_entries or []
        manifest = build_graph_manifest(self.memory_root)
        graph = _LogicalGraph(self.memory_root, manifest)

        local_text, local_delivery = self._render_local(
            selection.ids,
            graph,
            delivered,
            include_returned=include_returned,
        )
        source_sections = self._render_sources(
            selection.sources,
            graph,
            manifest,
            delivered,
            include_returned=include_returned,
        )
        recent_text, selected_recent = self._render_recent(
            selection.recent_candidates,
            recent_entries,
            delivered,
            include_returned=include_returned,
        )

        sections: list[str] = []
        if local_text:
            sections.append(local_text)
        for source in sorted(source_sections, key=lambda item: (item.rank, item.source_id)):
            if source.text:
                sections.append(f"Source: `{source.source_id}`\n\n{source.text}")
        if recent_text:
            sections.append(recent_text)
        output = "\n\n".join(
            section.rstrip("\r\n") for section in sections if section.strip()
        ).strip("\r\n")
        if not output:
            output = NO_STRONG_MATCH
        if len(output) > self.max_output_chars:
            raise RetrieveSelectionError(
                f"resolved retrieve output is {len(output)} characters; "
                f"select less content to stay within {self.max_output_chars}"
            )

        source_items: dict[str, str] = {}
        complete_sources: dict[str, str] = {}
        delivered_ranges: list[DeliveredRange] = []
        for source in source_sections:
            source_items.update(source.source_items)
            complete_sources.update(source.complete_sources)
            delivered_ranges.extend(source.ranges)
        delivery = RetrieveDeliveryCoverage(
            local_items=local_delivery,
            source_items=source_items,
            complete_sources=complete_sources,
            ranges=delivered_ranges,
            recent_candidates=[_candidate_selection_id(entry) for entry in selected_recent],
        )
        return RenderedRetrieveSelection(output, delivery, selected_recent)

    def _render_local(
        self,
        requested_ids: list[str],
        graph: _LogicalGraph,
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> tuple[str, dict[str, str]]:
        full_entries: set[_Entry] = set()
        exact_entries: set[_Entry] = set()
        selected_pursuits: set[str] = set()
        delivery: dict[str, str] = {}
        for item_id in _unique(requested_ids):
            duplicate_prefix = f"duplicate id `{item_id}` "
            if item_id in graph.parser.duplicates or any(
                error.startswith(duplicate_prefix) for error in graph.manifest.errors
            ):
                raise RetrieveSelectionError(f"local graph id `{item_id}` is duplicated")
            entry = graph.by_id.get(item_id)
            if entry is None:
                raise RetrieveSelectionError(f"unknown local graph id `{item_id}`")
            version = _entry_hash(entry, mq_notice=True)
            if not include_returned and delivered.local_items.get(item_id) == version:
                continue
            if entry.kind == "heading":
                full_entries.add(entry)
                if entry.family == "pursuit":
                    selected_pursuits.add(item_id)
                for descendant in _walk_entries(entry, include_self=True):
                    if descendant.item_id is not None:
                        delivery[descendant.item_id] = _entry_hash(descendant, mq_notice=True)
            else:
                exact_entries.add(entry)
                delivery[item_id] = version

        focus_entries: set[_Entry] = set()
        if selected_pursuits:
            for root in graph.roots:
                for entry in _walk_entries(root):
                    if entry.kind == "focus" and entry.focus_target in selected_pursuits:
                        focus_entries.add(entry)
        text = "\n\n".join(
            part
            for part in (
                _render_selected_tree(root, full_entries, exact_entries | focus_entries, mq_notice=True)
                for root in graph.roots
            )
            if part
        )
        return text, delivery

    def _render_sources(
        self,
        requested_sources: list[SourceSelection],
        graph: _LogicalGraph,
        manifest: GraphManifest,
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> list[_RenderedSource]:
        merged: dict[str, tuple[list[str], list[LineRange]]] = {}
        for source in requested_sources:
            ids, ranges = merged.setdefault(source.source_id, ([], []))
            ids.extend(source.ids)
            ranges.extend(source.ranges)

        rendered: list[_RenderedSource] = []
        for source_id, (ids, ranges) in merged.items():
            match = SOURCE_ID_RE.fullmatch(source_id)
            assert match is not None
            marker, owner_id = match.groups()
            duplicate_prefix = f"duplicate id `{owner_id}` "
            if owner_id in graph.parser.duplicates or any(
                error.startswith(duplicate_prefix) for error in manifest.errors
            ):
                raise RetrieveSelectionError(f"linked source owner id `{owner_id}` is duplicated")
            owner = graph.by_id.get(owner_id)
            if owner is None or owner.kind != "heading":
                raise RetrieveSelectionError(f"unknown linked source `{source_id}`")
            if owner.anchor_kind != marker:
                actual = owner.anchor_kind or "#"
                raise RetrieveSelectionError(
                    f"source marker mismatch for `{source_id}`; local heading uses `{actual}{owner_id}`"
                )
            if marker == "M#":
                rendered.append(
                    self._render_markdown_source(
                        source_id,
                        owner,
                        ids,
                        ranges,
                        manifest,
                        delivered,
                        include_returned=include_returned,
                    )
                )
            elif marker == "S#":
                rendered.append(
                    self._render_skill_source(
                        source_id,
                        owner,
                        ids,
                        ranges,
                        manifest,
                        delivered,
                        include_returned=include_returned,
                    )
                )
            else:
                rendered.append(
                    self._render_mf_source(
                        source_id,
                        owner,
                        ids,
                        ranges,
                        delivered,
                        include_returned=include_returned,
                    )
                )
        return rendered

    def _render_markdown_source(
        self,
        source_id: str,
        owner: _Entry,
        ids: list[str],
        ranges: list[LineRange],
        manifest: GraphManifest,
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> _RenderedSource:
        if ids:
            raise RetrieveSelectionError(f"`{source_id}` is free-form Markdown; select line ranges, not ids")
        if not ranges:
            raise RetrieveSelectionError(f"`{source_id}` requires at least one line range")
        reference = manifest.backing.get(owner.item_id or "")
        if reference is None or reference.kind != "M#" or not _safe_source_file(self.memory_root, reference.path):
            raise RetrieveSelectionError(f"missing Markdown source `{source_id}`")
        text = _read_text(reference.path)
        resolved = _resolve_line_ranges(
            source_id,
            text,
            ranges,
            delivered.ranges,
            include_returned=include_returned,
        )
        return _RenderedSource(owner.rank, source_id, resolved.text, ranges=resolved.delivered)

    def _render_skill_source(
        self,
        source_id: str,
        owner: _Entry,
        ids: list[str],
        ranges: list[LineRange],
        manifest: GraphManifest,
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> _RenderedSource:
        if ids or ranges:
            raise RetrieveSelectionError(f"`{source_id}` is selected as one complete skill; ids and ranges are invalid")
        reference = manifest.backing.get(owner.item_id or "")
        if reference is None or reference.kind != "S#" or not _safe_source_file(self.memory_root, reference.path):
            raise RetrieveSelectionError(f"missing skill source `{source_id}`")
        text = _read_text(reference.path).rstrip("\r\n")
        version = _hash_text(text)
        if not include_returned and delivered.complete_sources.get(source_id) == version:
            return _RenderedSource(owner.rank, source_id, "")
        return _RenderedSource(
            owner.rank,
            source_id,
            text,
            complete_sources={source_id: version},
        )

    def _render_mf_source(
        self,
        source_id: str,
        owner: _Entry,
        ids: list[str],
        ranges: list[LineRange],
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> _RenderedSource:
        if not ids and not ranges:
            raise RetrieveSelectionError(f"`{source_id}` requires a source-scoped id or line range")
        owner_id = owner.item_id or ""
        path = self.memory_root / MF_CANONICAL_PATH / owner_id / "dist" / "MEMORY.md"
        if not _safe_source_file(self.memory_root, path):
            raise RetrieveSelectionError(f"missing canonical mirrored view `{source_id}`")
        text = _read_text(path)
        parser = _TreeParser()
        root = parser.parse(path, "external", scoped=True)
        rank = 0
        for entry in _walk_entries(root):
            entry.rank = rank
            rank += 1

        full_entries: set[_Entry] = set()
        exact_entries: set[_Entry] = set()
        requested_item_ids: set[str] = set()
        requested_item_intervals: list[tuple[int, int]] = []
        source_delivery: dict[str, str] = {}
        for item_id in _unique(ids):
            if item_id in parser.duplicates:
                raise RetrieveSelectionError(f"source-scoped id `{item_id}` is duplicated in `{source_id}`")
            entry = parser.by_id.get(item_id)
            if entry is None:
                raise RetrieveSelectionError(f"unknown source-scoped id `{item_id}` in `{source_id}`")
            key = f"{source_id}:{item_id}"
            version = _entry_hash(entry, mq_notice=False)
            selected_entries = (
                list(_walk_entries(entry, include_self=True))
                if entry.kind == "heading"
                else [entry]
            )
            for selected_entry in selected_entries:
                if selected_entry.item_id is not None:
                    requested_item_ids.add(selected_entry.item_id)
            requested_item_intervals.append((entry.line_number, entry.end_line))
            if not include_returned and delivered.source_items.get(key) == version:
                continue
            if entry.kind == "heading":
                full_entries.add(entry)
                for descendant in _walk_entries(entry, include_self=True):
                    if descendant.item_id is not None:
                        descendant_key = f"{source_id}:{descendant.item_id}"
                        source_delivery[descendant_key] = _entry_hash(descendant, mq_notice=False)
            else:
                exact_entries.add(entry)
                source_delivery[key] = version
        tree_text = _render_selected_tree(root, full_entries, exact_entries, mq_notice=False)

        addressable_by_line = {
            entry.line_number: entry.item_id
            for entry in _walk_entries(root)
            if entry.item_id is not None
        }
        for selected_range in ranges:
            invalid_lines = [
                line
                for line, item_id in addressable_by_line.items()
                if selected_range.start <= line <= selected_range.end
                and item_id not in requested_item_ids
            ]
            if invalid_lines:
                raise RetrieveSelectionError(
                    f"line range {selected_range.start}-{selected_range.end} in `{source_id}` contains an "
                    "addressable item; select its source-scoped id"
                )
        previously_delivered_item_intervals = []
        if not include_returned:
            for entry in _walk_entries(root):
                if entry.item_id is None:
                    continue
                key = f"{source_id}:{entry.item_id}"
                if delivered.source_items.get(key) == _entry_hash(entry, mq_notice=False):
                    previously_delivered_item_intervals.append((entry.line_number, entry.end_line))
        resolved = _resolve_line_ranges(
            source_id,
            text,
            ranges,
            delivered.ranges,
            include_returned=include_returned,
            omit_intervals=[*requested_item_intervals, *previously_delivered_item_intervals],
        ) if ranges else _ResolvedRanges("", [])
        source_text = "\n\n".join(part for part in (tree_text, resolved.text) if part)
        return _RenderedSource(
            owner.rank,
            source_id,
            source_text,
            source_items=source_delivery,
            ranges=resolved.delivered,
        )

    def _render_recent(
        self,
        requested_ids: list[str],
        entries: list[RecentSubmittedMemoryEntry],
        delivered: RetrieveDeliveryCoverage,
        *,
        include_returned: bool,
    ) -> tuple[str, list[RecentSubmittedMemoryEntry]]:
        by_id = {_candidate_selection_id(entry): entry for entry in entries}
        requested = set(_unique(requested_ids))
        unknown = sorted(requested - set(by_id))
        if unknown:
            raise RetrieveSelectionError(f"unknown recent candidate id `{unknown[0]}`")
        selected = [
            entry
            for entry in entries
            if _candidate_selection_id(entry) in requested
            and (include_returned or _candidate_selection_id(entry) not in delivered.recent_candidates)
        ]
        blocks = []
        for entry in selected:
            selection_id = _candidate_selection_id(entry)
            blocks.append(
                f"Recent submitted evidence: `{selection_id}`\n"
                f"Submitted at: {entry.submitted_at}\n\n"
                f"{_rstrip_newlines(entry.message)}"
            )
        return "\n\n".join(blocks), selected


def parse_retrieve_selection_json(value: str) -> RetrieveSelection:
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or set(decoded) != {"ids", "sources", "recent_candidates"}:
            raise ValueError("top-level JSON must contain exactly ids, sources, and recent_candidates")
        sources = decoded.get("sources")
        if not isinstance(sources, list):
            raise ValueError("sources must be a list")
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"source_id", "ids", "ranges"}:
                raise ValueError("each source must contain exactly source_id, ids, and ranges")
        return RetrieveSelection.model_validate(decoded)
    except Exception as exc:
        raise RetrieveSelectionError(f"final response must be strict retrieve-selection JSON: {exc}") from exc


def _render_selected_tree(
    root: _Entry,
    full_entries: set[_Entry],
    exact_entries: set[_Entry],
    *,
    mq_notice: bool,
) -> str:
    memo: dict[_Entry, bool] = {}

    def relevant(entry: _Entry) -> bool:
        if entry in full_entries or entry in exact_entries:
            return True
        if entry in memo:
            return memo[entry]
        value = any(relevant(part) for part in entry.parts if isinstance(part, _Entry))
        memo[entry] = value
        return value

    def render_entry(entry: _Entry) -> str:
        if entry in full_entries:
            return _flatten_entry(entry, mq_notice=mq_notice).rstrip("\r\n")
        if entry in exact_entries and entry.kind in {"node", "focus"}:
            return entry.line
        children = [
            render_entry(part)
            for part in entry.parts
            if isinstance(part, _Entry) and relevant(part)
        ]
        children = [child for child in children if child]
        if entry.kind == "root":
            return "\n\n".join(children)
        if not children:
            return entry.line if entry in exact_entries else ""
        return "\n\n".join((entry.line, *children))

    return render_entry(root).strip("\r\n")


def _flatten_entry(entry: _Entry, *, mq_notice: bool) -> str:
    lines = [entry.line] if entry.kind != "root" else []
    for part in entry.parts:
        if isinstance(part, _Entry):
            lines.extend(_flatten_entry(part, mq_notice=mq_notice).splitlines())
        else:
            lines.append(part)
    if mq_notice and entry.anchor_kind == "MQ#" and entry.item_id is not None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"Provider question context is available for `MQ#{entry.item_id}`.")
    return "\n".join(lines)


def _entry_hash(entry: _Entry, *, mq_notice: bool) -> str:
    return _hash_text(_flatten_entry(entry, mq_notice=mq_notice).rstrip("\r\n"))


def _walk_entries(root: _Entry, *, include_self: bool = False) -> Iterable[_Entry]:
    if include_self and root.kind != "root":
        yield root
    for part in root.parts:
        if isinstance(part, _Entry):
            yield part
            yield from _walk_entries(part)


def _resolve_line_ranges(
    source_id: str,
    text: str,
    requested: list[LineRange],
    delivered: list[DeliveredRange],
    *,
    include_returned: bool,
    omit_intervals: list[tuple[int, int]] | None = None,
) -> _ResolvedRanges:
    lines = text.splitlines()
    if not lines:
        raise RetrieveSelectionError(f"source `{source_id}` is empty")
    source_hash = _hash_text(text)
    spans = _fence_spans(lines)
    heading_lines = _heading_lines(lines, spans)
    intervals: list[tuple[int, int]] = []
    for item in requested:
        if item.end > len(lines):
            raise RetrieveSelectionError(
                f"line range {item.start}-{item.end} exceeds `{source_id}` length {len(lines)}"
            )
        start, end = _expand_fence_boundaries(item.start, item.end, spans)
        intervals.append((start, end))
    intervals = _merge_source_intervals(intervals, heading_lines)
    if omit_intervals:
        intervals = _subtract_intervals(intervals, _merge_intervals(omit_intervals))
    if not include_returned:
        old = [
            (item.start, item.end)
            for item in delivered
            if item.source_id == source_id and item.source_hash == source_hash
        ]
        intervals = _subtract_intervals(intervals, _merge_intervals(old))
    if not intervals:
        return _ResolvedRanges("", [])

    contextual: list[tuple[int | None, int, int]] = []
    for start, end in intervals:
        context = max((line for line in heading_lines if line <= start), default=None)
        if contextual and contextual[-1][0] == context and start <= contextual[-1][2] + 1:
            previous_context, previous_start, previous_end = contextual[-1]
            contextual[-1] = (previous_context, previous_start, max(previous_end, end))
        else:
            contextual.append((context, start, end))

    parts: list[str] = []
    shown_context: set[int] = set()
    for context, start, end in contextual:
        chunk: list[str] = []
        if context is not None and context < start and context not in shown_context:
            chunk.append(lines[context - 1])
            shown_context.add(context)
        chunk.extend(lines[start - 1 : end])
        parts.append("\n".join(chunk).rstrip("\r\n"))
    coverage = [DeliveredRange(source_id, start, end, source_hash) for start, end in intervals]
    return _ResolvedRanges("\n\n".join(part for part in parts if part), coverage)


def _fence_spans(lines: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    open_line: int | None = None
    fence_char: str | None = None
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        match = FENCE_RE.match(line)
        if match is None:
            continue
        marker = match.group(1)
        if open_line is None:
            open_line = line_number
            fence_char = marker[0]
            fence_length = len(marker)
        elif marker[0] == fence_char and len(marker) >= fence_length:
            spans.append((open_line, line_number))
            open_line = None
            fence_char = None
            fence_length = 0
    if open_line is not None:
        spans.append((open_line, len(lines)))
    return spans


def _expand_fence_boundaries(start: int, end: int, spans: list[tuple[int, int]]) -> tuple[int, int]:
    changed = True
    while changed:
        changed = False
        for fence_start, fence_end in spans:
            if end < fence_start or start > fence_end:
                continue
            expanded_start = min(start, fence_start)
            expanded_end = max(end, fence_end)
            if (expanded_start, expanded_end) != (start, end):
                start, end = expanded_start, expanded_end
                changed = True
    return start, end


def _heading_lines(lines: list[str], fence_spans: list[tuple[int, int]]) -> list[int]:
    inside_fence = {
        line_number
        for start, end in fence_spans
        for line_number in range(start, end + 1)
    }
    return [
        line_number
        for line_number, line in enumerate(lines, start=1)
        if line_number not in inside_fence and HEADING_RE.match(line) is not None
    ]


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(intervals)):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _merge_source_intervals(
    intervals: list[tuple[int, int]],
    heading_lines: list[int],
) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    contexts: list[int | None] = []
    for start, end in sorted(set(intervals)):
        context = max((line for line in heading_lines if line <= start), default=None)
        overlaps = bool(merged and start <= merged[-1][1])
        adjacent_same_context = bool(
            merged and start == merged[-1][1] + 1 and contexts[-1] == context
        )
        if overlaps or adjacent_same_context:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
            contexts.append(context)
    return merged


def _subtract_intervals(
    intervals: list[tuple[int, int]],
    delivered: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    remaining: list[tuple[int, int]] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for old_start, old_end in delivered:
            next_pieces: list[tuple[int, int]] = []
            for piece_start, piece_end in pieces:
                if old_end < piece_start or old_start > piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < old_start:
                    next_pieces.append((piece_start, old_start - 1))
                if old_end < piece_end:
                    next_pieces.append((old_end + 1, piece_end))
            pieces = next_pieces
        remaining.extend(pieces)
    return remaining


def _candidate_selection_id(entry: RecentSubmittedMemoryEntry) -> str:
    return f"{entry.update_session_id}:{entry.candidate_id}"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rstrip_newlines(text: str) -> str:
    return text.rstrip("\r\n")


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _safe_source_file(root: Path, path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        return False
    return True
