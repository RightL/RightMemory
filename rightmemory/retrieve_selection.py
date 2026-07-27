from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .graph import (
    ITEM_ID_PATTERN,
    BlockKey,
    DocumentBlock,
    GraphManifest,
    build_graph_manifest,
    is_valid_item_id,
)
from .recent_submitted import RecentSubmittedMemoryEntry
from .shared_view_models import load_connections
from .shared_view_package import FileViewPackageError, ValidatedFileViewPackage, validate_file_view_package


NO_STRONG_MATCH = "no strong match"
SOURCE_ID_RE = re.compile(
    rf"^(?:(M#|S#|MF#)({ITEM_ID_PATTERN})|MF#({ITEM_ID_PATTERN})/(M#|S#)({ITEM_ID_PATTERN}))$"
)
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
            raise ValueError(
                "source_id must be an M#, S#, MF#, or qualified MF#/M# or MF#/S# id"
            )
        return value

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if not is_valid_item_id(value):
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
            if not is_valid_item_id(value):
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
    recent_candidates: dict[str, str] = field(default_factory=dict)

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

        recent = {**self.recent_candidates, **newer.recent_candidates}
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
        recent_entries: list[RecentSubmittedMemoryEntry] | None = None,
    ) -> RenderedRetrieveSelection:
        recent_entries = recent_entries or []
        manifest = build_graph_manifest(self.memory_root)

        local_text, local_delivery = self._render_local(selection.ids, manifest)
        source_sections = self._render_sources(
            selection.sources,
            manifest,
        )
        recent_text, selected_recent = self._render_recent(
            selection.recent_candidates,
            recent_entries,
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
            recent_candidates={
                _candidate_selection_id(entry): _hash_text(_rstrip_newlines(entry.message))
                for entry in selected_recent
            },
        )
        return RenderedRetrieveSelection(output, delivery, selected_recent)

    def current_delivery_selection(
        self,
        delivered: RetrieveDeliveryCoverage,
        *,
        recent_entries: list[RecentSubmittedMemoryEntry] | None = None,
        unchanged_only: bool = False,
    ) -> RetrieveSelection:
        """Return the currently selectable form of previously delivered coverage."""
        manifest = build_graph_manifest(self.memory_root)
        local_ids = [
            item_id
            for item_id, version in delivered.local_items.items()
            if (
                item_id not in manifest.duplicates
                and (item := manifest.items.get(item_id)) is not None
                and (not unchanged_only or item.content_hash == version)
            )
        ]
        local_ids.sort(
            key=lambda item_id: (
                manifest.items[item_id].traversal_rank,
                item_id,
            )
        )

        source_parts: dict[str, tuple[list[str], list[LineRange]]] = {}
        for key, version in delivered.source_items.items():
            try:
                source_id, item_id = key.rsplit(":", 1)
            except ValueError:
                continue
            match = SOURCE_ID_RE.fullmatch(source_id)
            if match is None:
                continue
            marker, owner_id, qualified_owner_id, _nested_marker, _nested_id = match.groups()
            if marker != "MF#" or owner_id is None or qualified_owner_id is not None:
                continue
            try:
                self._require_local_source_owner(
                    manifest,
                    owner_id,
                    "MF#",
                    source_id,
                )
                package = self._validated_mf_package(owner_id, source_id)
            except (OSError, ValueError):
                continue
            item = package.manifest.items.get(item_id)
            if item is None or (unchanged_only and item.content_hash != version):
                continue
            ids, _ranges = source_parts.setdefault(source_id, ([], []))
            ids.append(item_id)

        for source_id, version in delivered.complete_sources.items():
            text = self._current_linked_source_text(
                source_id,
                manifest,
                expected_marker="S#",
            )
            if text is None or (
                unchanged_only and _hash_text(text.rstrip("\r\n")) != version
            ):
                continue
            source_parts.setdefault(source_id, ([], []))

        current_range_texts: dict[str, str | None] = {}
        ranges_by_source: dict[str, list[tuple[int, int]]] = {}
        for delivered_range in delivered.ranges:
            source_id = delivered_range.source_id
            if source_id not in current_range_texts:
                current_range_texts[source_id] = self._current_linked_source_text(
                    source_id,
                    manifest,
                    expected_marker="M#",
                )
            text = current_range_texts[source_id]
            if text is None or (
                unchanged_only and _hash_text(text) != delivered_range.source_hash
            ):
                continue
            if delivered_range.end > len(text.splitlines()):
                continue
            ranges_by_source.setdefault(source_id, []).append(
                (delivered_range.start, delivered_range.end)
            )
        for source_id, intervals in ranges_by_source.items():
            _ids, ranges = source_parts.setdefault(source_id, ([], []))
            ranges.extend(
                LineRange(start=start, end=end)
                for start, end in _merge_intervals(intervals)
            )

        recent_candidates = [
            selection_id
            for entry in (recent_entries or [])
            if (
                (selection_id := _candidate_selection_id(entry))
                in delivered.recent_candidates
                and (
                    not unchanged_only
                    or delivered.recent_candidates[selection_id]
                    == _hash_text(_rstrip_newlines(entry.message))
                )
            )
        ]
        sources = [
            SourceSelection(
                source_id=source_id,
                ids=sorted(set(ids)),
                ranges=ranges,
            )
            for source_id, (ids, ranges) in sorted(source_parts.items())
        ]
        return RetrieveSelection(
            ids=local_ids,
            sources=sources,
            recent_candidates=recent_candidates,
        )

    def changed_delivery_labels(
        self,
        delivered: RetrieveDeliveryCoverage,
        *,
        recent_entries: list[RecentSubmittedMemoryEntry] | None = None,
    ) -> list[str]:
        """Describe selectable delivered material whose current content changed."""
        current = self.current_delivery_selection(
            delivered,
            recent_entries=recent_entries,
        )
        unchanged = self.current_delivery_selection(
            delivered,
            recent_entries=recent_entries,
            unchanged_only=True,
        )
        manifest = build_graph_manifest(self.memory_root)
        labels = [
            f"local item `{item_id}`"
            for item_id in current.ids
            if item_id not in set(unchanged.ids)
            and (block := manifest.block_for_id(item_id)) is not None
            and block.source_path.name not in {"MEMORY.md", "PURSUITS.md"}
        ]
        unchanged_sources = {source.source_id: source for source in unchanged.sources}
        for source in current.sources:
            old = unchanged_sources.get(source.source_id)
            old_ids = set(old.ids if old is not None else [])
            labels.extend(
                f"item `{item_id}` in `{source.source_id}`"
                for item_id in source.ids
                if item_id not in old_ids
            )
            current_ranges = {(item.start, item.end) for item in source.ranges}
            old_ranges = {
                (item.start, item.end)
                for item in (old.ranges if old is not None else [])
            }
            if current_ranges and current_ranges != old_ranges:
                labels.append(f"source `{source.source_id}`")
            if not source.ids and not source.ranges and old is None:
                labels.append(f"source `{source.source_id}`")
        delivered_range_hashes: dict[str, set[str]] = {}
        for delivered_range in delivered.ranges:
            delivered_range_hashes.setdefault(delivered_range.source_id, set()).add(
                delivered_range.source_hash
            )
        for source_id, old_hashes in delivered_range_hashes.items():
            text = self._current_linked_source_text(
                source_id,
                manifest,
                expected_marker="M#",
            )
            if (
                text is not None
                and _hash_text(text) not in old_hashes
            ):
                labels.append(f"source `{source_id}`")
        old_recent = set(unchanged.recent_candidates)
        labels.extend(
            f"recent candidate `{candidate_id}`"
            for candidate_id in current.recent_candidates
            if candidate_id not in old_recent
        )
        return list(dict.fromkeys(labels))

    def _current_linked_source_text(
        self,
        source_id: str,
        manifest: GraphManifest,
        *,
        expected_marker: str,
    ) -> str | None:
        match = SOURCE_ID_RE.fullmatch(source_id)
        if match is None:
            return None
        marker, owner_id, qualified_owner_id, nested_marker, nested_id = match.groups()
        try:
            if qualified_owner_id is None:
                if marker != expected_marker or owner_id is None:
                    return None
                owner = self._require_local_source_owner(
                    manifest,
                    owner_id,
                    expected_marker,
                    source_id,
                )
                reference = manifest.backing.get(owner.item_id or "")
                source_root = self.memory_root
            else:
                if nested_marker != expected_marker or nested_id is None:
                    return None
                self._require_local_source_owner(
                    manifest,
                    qualified_owner_id,
                    "MF#",
                    source_id,
                )
                package = self._validated_mf_package(qualified_owner_id, source_id)
                nested = package.manifest.block_for_id(nested_id)
                if (
                    nested is None
                    or nested.kind != "heading"
                    or nested.anchor_kind != expected_marker
                ):
                    return None
                reference = package.manifest.backing.get(nested_id)
                source_root = package.root
            if (
                reference is None
                or reference.kind != expected_marker
                or not _safe_source_file(source_root, reference.path)
            ):
                return None
            return _read_text(reference.path)
        except (OSError, UnicodeError, ValueError):
            return None

    def _render_local(
        self,
        requested_ids: list[str],
        manifest: GraphManifest,
    ) -> tuple[str, dict[str, str]]:
        full_entries: set[BlockKey] = set()
        exact_entries: set[BlockKey] = set()
        selected_pursuits: set[str] = set()
        delivery: dict[str, str] = {}
        for item_id in _unique(requested_ids):
            if item_id in manifest.duplicates:
                raise RetrieveSelectionError(f"local graph id `{item_id}` is duplicated")
            item = manifest.items.get(item_id)
            entry = manifest.block_for_id(item_id)
            if item is None or entry is None:
                raise RetrieveSelectionError(f"unknown local graph id `{item_id}`")
            version = item.content_hash
            if entry.kind == "heading":
                full_entries.add(entry.key)
                if entry.family == "pursuit":
                    selected_pursuits.add(item_id)
                for descendant in manifest.walk_logical(entry.key, include_self=True):
                    if descendant.item_id is not None:
                        descendant_item = manifest.items.get(descendant.item_id)
                        if descendant_item is not None:
                            delivery[descendant.item_id] = descendant_item.content_hash
            else:
                exact_entries.add(entry.key)
                delivery[item_id] = version

        focus_entries: set[BlockKey] = set()
        if selected_pursuits:
            for key in manifest.focus_blocks:
                entry = manifest.blocks[key]
                if entry.focus_target in selected_pursuits:
                    focus_entries.add(key)
        text = "\n\n".join(
            part
            for part in (
                _render_selected_tree(
                    manifest,
                    root,
                    full_entries,
                    exact_entries | focus_entries,
                    mq_notice=True,
                )
                for root in manifest.root_blocks
            )
            if part
        )
        return text, delivery

    def _render_sources(
        self,
        requested_sources: list[SourceSelection],
        manifest: GraphManifest,
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
            marker, owner_id, qualified_owner_id, nested_marker, nested_id = match.groups()
            if qualified_owner_id is not None:
                owner = self._require_local_source_owner(
                    manifest,
                    qualified_owner_id,
                    "MF#",
                    source_id,
                )
                rendered.append(
                    self._render_mf_linked_source(
                        source_id,
                        owner,
                        nested_marker or "",
                        nested_id or "",
                        ids,
                        ranges,
                    )
                )
                continue
            assert marker is not None and owner_id is not None
            owner = self._require_local_source_owner(
                manifest,
                owner_id,
                marker,
                source_id,
            )
            if marker == "M#":
                rendered.append(
                    self._render_markdown_source(
                        source_id,
                        owner,
                        ids,
                        ranges,
                        manifest,
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
                    )
                )
            else:
                rendered.append(
                    self._render_mf_source(
                        source_id,
                        owner,
                        ids,
                        ranges,
                    )
                )
        return rendered

    def _require_local_source_owner(
        self,
        manifest: GraphManifest,
        owner_id: str,
        marker: str,
        source_id: str,
    ) -> DocumentBlock:
        if owner_id in manifest.duplicates:
            raise RetrieveSelectionError(f"linked source owner id `{owner_id}` is duplicated")
        owner = manifest.block_for_id(owner_id)
        if owner is None or owner.kind != "heading":
            raise RetrieveSelectionError(f"unknown linked source `{source_id}`")
        if owner.anchor_kind != marker:
            actual = owner.anchor_kind or "#"
            raise RetrieveSelectionError(
                f"source marker mismatch for `{source_id}`; local heading uses `{actual}{owner_id}`"
            )
        return owner

    def _render_markdown_source(
        self,
        source_id: str,
        owner: DocumentBlock,
        ids: list[str],
        ranges: list[LineRange],
        manifest: GraphManifest,
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
        )
        return _RenderedSource(owner.traversal_rank, source_id, resolved.text, ranges=resolved.delivered)

    def _render_skill_source(
        self,
        source_id: str,
        owner: DocumentBlock,
        ids: list[str],
        ranges: list[LineRange],
        manifest: GraphManifest,
    ) -> _RenderedSource:
        if ids or ranges:
            raise RetrieveSelectionError(f"`{source_id}` is selected as one complete skill; ids and ranges are invalid")
        reference = manifest.backing.get(owner.item_id or "")
        if reference is None or reference.kind != "S#" or not _safe_source_file(self.memory_root, reference.path):
            raise RetrieveSelectionError(f"missing skill source `{source_id}`")
        text = _read_text(reference.path).rstrip("\r\n")
        version = _hash_text(text)
        return _RenderedSource(
            owner.traversal_rank,
            source_id,
            text,
            complete_sources={source_id: version},
        )

    def _render_mf_source(
        self,
        source_id: str,
        owner: DocumentBlock,
        ids: list[str],
        ranges: list[LineRange],
    ) -> _RenderedSource:
        if ranges:
            raise RetrieveSelectionError(
                f"`{source_id}` is a schema-valid MF graph; select source-scoped ids, not line ranges"
            )
        if not ids:
            raise RetrieveSelectionError(f"`{source_id}` requires at least one source-scoped id")
        owner_id = owner.item_id or ""
        validated = self._validated_mf_package(owner_id, source_id)
        mf_manifest = validated.manifest

        full_entries: set[BlockKey] = set()
        exact_entries: set[BlockKey] = set()
        source_delivery: dict[str, str] = {}
        for item_id in _unique(ids):
            if item_id in mf_manifest.duplicates:
                raise RetrieveSelectionError(f"source-scoped id `{item_id}` is duplicated in `{source_id}`")
            item = mf_manifest.items.get(item_id)
            entry = mf_manifest.block_for_id(item_id)
            if item is None or entry is None:
                raise RetrieveSelectionError(f"unknown source-scoped id `{item_id}` in `{source_id}`")
            key = f"{source_id}:{item_id}"
            version = item.content_hash
            if entry.kind == "heading":
                full_entries.add(entry.key)
                for descendant in mf_manifest.walk_logical(entry.key, include_self=True):
                    if descendant.item_id is not None:
                        descendant_key = f"{source_id}:{descendant.item_id}"
                        descendant_item = mf_manifest.items.get(descendant.item_id)
                        if descendant_item is not None:
                            source_delivery[descendant_key] = descendant_item.content_hash
            else:
                exact_entries.add(entry.key)
                source_delivery[key] = version
        tree_text = "\n\n".join(
            rendered
            for root in mf_manifest.root_blocks
            if (
                rendered := _render_selected_tree(
                    mf_manifest,
                    root,
                    full_entries,
                    exact_entries,
                    mq_notice=False,
                )
            )
        )
        return _RenderedSource(
            owner.traversal_rank,
            source_id,
            tree_text,
            source_items=source_delivery,
        )

    def _render_mf_linked_source(
        self,
        source_id: str,
        owner: DocumentBlock,
        marker: str,
        nested_id: str,
        ids: list[str],
        ranges: list[LineRange],
    ) -> _RenderedSource:
        owner_id = owner.item_id or ""
        validated = self._validated_mf_package(owner_id, source_id)
        nested = validated.manifest.block_for_id(nested_id)
        reference = validated.manifest.backing.get(nested_id)
        if nested is None or nested.kind != "heading" or nested.anchor_kind != marker:
            raise RetrieveSelectionError(f"unknown or mismatched qualified MF source `{source_id}`")
        if reference is None or reference.kind != marker:
            raise RetrieveSelectionError(f"missing qualified MF backing source `{source_id}`")
        if not _safe_source_file(validated.root, reference.path):
            raise RetrieveSelectionError(f"unsafe qualified MF backing source `{source_id}`")

        if marker == "M#":
            if ids:
                raise RetrieveSelectionError(
                    f"`{source_id}` is free-form Markdown; select line ranges, not ids"
                )
            if not ranges:
                raise RetrieveSelectionError(f"`{source_id}` requires at least one line range")
            resolved = _resolve_line_ranges(
                source_id,
                _read_text(reference.path),
                ranges,
            )
            return _RenderedSource(
                owner.traversal_rank,
                source_id,
                resolved.text,
                ranges=resolved.delivered,
            )

        if marker != "S#":
            raise RetrieveSelectionError(f"unsupported qualified MF source `{source_id}`")
        if ids or ranges:
            raise RetrieveSelectionError(
                f"`{source_id}` is selected as one complete skill; ids and ranges are invalid"
            )
        text = _read_text(reference.path).rstrip("\r\n")
        version = _hash_text(text)
        return _RenderedSource(
            owner.traversal_rank,
            source_id,
            text,
            complete_sources={source_id: version},
        )

    def _validated_mf_package(
        self,
        owner_id: str,
        source_id: str,
    ) -> ValidatedFileViewPackage:
        package_root = self.memory_root / MF_CANONICAL_PATH / owner_id
        connection = load_connections(self.memory_root).get(owner_id)
        expected_view_id = (
            connection.target.view_id
            if connection is not None and connection.target.view_id
            else owner_id
        )
        try:
            return validate_file_view_package(
                package_root,
                expected_view_id=expected_view_id,
                namespace_id=owner_id,
            )
        except (FileNotFoundError, OSError, FileViewPackageError) as exc:
            raise RetrieveSelectionError(
                f"missing or invalid canonical mirrored view `{source_id}`: {exc}"
            ) from exc

    def _render_recent(
        self,
        requested_ids: list[str],
        entries: list[RecentSubmittedMemoryEntry],
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
    manifest: GraphManifest,
    root: BlockKey,
    full_entries: set[BlockKey],
    exact_entries: set[BlockKey],
    *,
    mq_notice: bool,
) -> str:
    memo: dict[BlockKey, bool] = {}

    def relevant(key: BlockKey) -> bool:
        if key in full_entries or key in exact_entries:
            return True
        if key in memo:
            return memo[key]
        block = manifest.blocks[key]
        value = any(relevant(part) for part in block.logical_parts if isinstance(part, tuple))
        memo[key] = value
        return value

    def render_entry(key: BlockKey) -> str:
        block = manifest.blocks[key]
        if key in full_entries:
            return _flatten_block(manifest, key, mq_notice=mq_notice).rstrip("\r\n")
        if key in exact_entries and block.kind in {"node", "focus"}:
            return block.line
        children = [
            render_entry(part)
            for part in block.logical_parts
            if isinstance(part, tuple) and relevant(part)
        ]
        children = [child for child in children if child]
        if block.kind == "root":
            return "\n\n".join(children)
        if not children:
            return block.line if key in exact_entries else ""
        body = "\n".join(
            part for part in block.logical_parts if isinstance(part, str)
        ).strip("\r\n")
        return "\n\n".join(part for part in (block.line, body, *children) if part)

    return render_entry(root).strip("\r\n")


def _flatten_block(manifest: GraphManifest, key: BlockKey, *, mq_notice: bool) -> str:
    block = manifest.blocks[key]
    lines = [block.line] if block.kind != "root" else []
    for part in block.logical_parts:
        if isinstance(part, tuple):
            lines.extend(_flatten_block(manifest, part, mq_notice=mq_notice).splitlines())
        else:
            lines.append(part)
    if mq_notice and block.anchor_kind == "MQ#" and block.item_id is not None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"Provider question context is available for `MQ#{block.item_id}`.")
    return "\n".join(lines)


def _resolve_line_ranges(
    source_id: str,
    text: str,
    requested: list[LineRange],
    *,
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
