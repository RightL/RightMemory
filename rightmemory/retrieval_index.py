"""Searchable source units projected from the canonical RightMemory graph."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .corrections import AGENT_CORRECTION_SOURCE_PATHS, agent_correction_entries
from .graph import DocumentBlock, GraphManifest, build_graph_manifest
from .recent_submitted import RecentSubmittedMemoryEntry, collect_recent_submitted_memory
from .retrieve_selection import (
    LineRange, RetrieveSelectionError, RetrieveSelectionRenderer,
    _read_text, _resolve_line_ranges, _safe_source_file,
)


PASSAGE_CHARS = 2000


@dataclass(frozen=True)
class RetrievalEntry:
    key: str
    text: str
    source: str
    pending: bool = False
    display_parts: tuple[tuple[str, str], ...] = ()

    @property
    def version(self) -> str:
        return text_hash(self.source + "\n" + self.text)


@dataclass(frozen=True)
class RetrievalCorpus:
    entries: tuple[RetrievalEntry, ...]
    fingerprint: str


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def search_passages(text: str) -> list[str]:
    """Keep ordinary entries intact; cover long entries without dropping text."""
    if len(text) <= PASSAGE_CHARS:
        return [text]
    passages: list[str] = []
    offset = 0
    while offset < len(text):
        end = min(offset + PASSAGE_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind("\n", offset + PASSAGE_CHARS // 2, end)
            if boundary >= 0:
                end = boundary + 1
        passages.append(text[offset:end])
        offset = end
    return passages


def build_retrieval_corpus(memory_root: Path) -> RetrievalCorpus:
    root = Path(memory_root).resolve()
    manifest = build_graph_manifest(root)
    if manifest.errors:
        raise RetrieveSelectionError("cannot index invalid Memory: " + "; ".join(manifest.errors[:5]))
    entries: list[RetrievalEntry] = []
    renderer = RetrieveSelectionRenderer(root, max_output_chars=2**63 - 1)
    _graph_entries(manifest, entries)
    _backing_entries(manifest, entries)
    for item in sorted(manifest.items.values(), key=lambda item: item.traversal_rank):
        if item.anchor_kind != "MF#":
            continue
        owner = manifest.block_for_id(item.id)
        assert owner is not None
        package = renderer._validated_mf_package(item.id, f"MF#{item.id}")
        context = _entry_text(manifest, owner)
        display_context = _entry_display_parts(manifest, owner) + (
            (f"source:MF#{item.id}", f"Source: `MF#{item.id}`"),
        )
        _graph_entries(package.manifest, entries, namespace=f"MF#{item.id}", context=context,
                       display_context=display_context)
        _backing_entries(package.manifest, entries, namespace=f"MF#{item.id}", context=context,
                         display_context=display_context)
    for source, filename in AGENT_CORRECTION_SOURCE_PATHS.items():
        path = root / filename
        if not path.exists() and not path.is_symlink():
            continue
        _require_source(root, path)
        for entry in agent_correction_entries(_read_text(path)):
            entries.append(RetrievalEntry(
                key=f"{source}:{entry.position}",
                text=f"Agent Corrections ({source})\n{entry.text}",
                source=f"{source}:{entry.position} ({filename}:{entry.start_line})",
                display_parts=((f"source:{source}", f"Source: `{source}`"),
                               (f"{source}:{entry.position}", entry.text)),
            ))
    for entry in collect_recent_submitted_memory(root):
        entries.append(_pending_entry(entry))
    keys = [entry.key for entry in entries]
    if len(keys) != len(set(keys)):
        raise RetrieveSelectionError("duplicate retrieval source identity")
    fingerprint = text_hash(json.dumps(
        [(entry.key, entry.version) for entry in entries], ensure_ascii=False, separators=(",", ":"),
    ))
    return RetrievalCorpus(tuple(entries), fingerprint)


def _own_text(block: DocumentBlock) -> str:
    if block.kind == "node":
        return block.line
    return "\n".join([block.line, *(part.text for part in block.logical_text_parts)]).strip()


def _entry_chain(manifest: GraphManifest, block: DocumentBlock) -> list[DocumentBlock]:
    chain: list[DocumentBlock] = [block]
    parent = block.logical_parent
    while parent is not None:
        ancestor = manifest.blocks[parent]
        if ancestor.kind != "root":
            chain.append(ancestor)
        parent = ancestor.logical_parent
    return list(reversed(chain))


def _entry_text(manifest: GraphManifest, block: DocumentBlock) -> str:
    chain = _entry_chain(manifest, block)
    parts = [_own_text(ancestor) for ancestor in chain]
    # Focus records qualify the corresponding Pursuit; they are context, not children to expand.
    ids = {ancestor.item_id for ancestor in chain}
    for key in manifest.focus_blocks:
        focus = manifest.blocks[key]
        if focus.focus_target in ids:
            parts.append(_own_text(focus))
    return "\n\n".join(part for part in parts if part.strip())


def _entry_display_parts(manifest: GraphManifest, block: DocumentBlock) -> tuple[tuple[str, str], ...]:
    # Source identity, rather than equal wording, determines shared context.
    parts = []
    for ancestor in _entry_chain(manifest, block):
        text = [_own_text(ancestor)]
        text.extend(_own_text(manifest.blocks[key]) for key in manifest.focus_blocks
                    if manifest.blocks[key].focus_target == ancestor.item_id)
        parts.append((f"graph:{ancestor.key}", "\n\n".join(part for part in text if part.strip())))
    return tuple(parts)


def _graph_entries(
    manifest: GraphManifest, entries: list[RetrievalEntry], *, namespace: str = "", context: str = "",
    display_context: tuple[tuple[str, str], ...] = (),
) -> None:
    for item in sorted(manifest.items.values(), key=lambda item: item.traversal_rank):
        block = manifest.block_for_id(item.id)
        assert block is not None
        # Bare grouping headings provide context, rather than consuming a result slot.
        if block.kind == "heading" and not any(p.text.strip() for p in block.logical_text_parts):
            if item.anchor_kind != "MQ#":
                continue
        key = f"{namespace}:{item.id}" if namespace else item.id
        text = "\n\n".join(part for part in (context, _entry_text(manifest, block)) if part)
        display_parts = display_context + _entry_display_parts(manifest, block)
        if item.anchor_kind == "MQ#":
            notice = f"Provider question context is available for `MQ#{item.id}`."
            text += f"\n\n{notice}"
            display_parts += ((f"notice:{key}", notice),)
        location = item.file.relative_to(manifest.root).as_posix()
        entries.append(RetrievalEntry(key, text, f"{key} ({location}:{item.line_number})",
                                      display_parts=display_parts))


def _backing_entries(
    manifest: GraphManifest, entries: list[RetrievalEntry], *, namespace: str = "", context: str = "",
    display_context: tuple[tuple[str, str], ...] = (),
) -> None:
    for item in sorted(manifest.items.values(), key=lambda item: item.traversal_rank):
        if item.anchor_kind not in {"M#", "S#"}:
            continue
        reference = manifest.backing.get(item.id)
        if reference is None or reference.kind != item.anchor_kind:
            raise RetrieveSelectionError(f"missing backing source {item.anchor_kind}{item.id}")
        _require_source(manifest.root, reference.path)
        owner = manifest.block_for_id(item.id)
        assert owner is not None
        prefix = "\n\n".join(part for part in (context, _entry_text(manifest, owner)) if part)
        source = f"{item.anchor_kind}{item.id}"
        if namespace:
            source = f"{namespace}/{source}"
        display_parts = display_context + _entry_display_parts(manifest, owner) + (
            (f"source:{source}", f"Source: `{source}`"),
        )
        text = _read_text(reference.path)
        if item.anchor_kind == "S#":
            if text.strip():
                entries.append(RetrievalEntry(source, f"{prefix}\n\n{text.rstrip()}", source,
                                              display_parts=display_parts + ((source, text.rstrip()),)))
            continue
        seen: set[tuple[int, int]] = set()
        for start, end in _line_passages(text):
            resolved = _resolve_line_ranges(source, text, [LineRange(start=start, end=end)])
            # Canonical range resolution also preserves complete fenced blocks.
            first, last = resolved.delivered[0].start, resolved.delivered[-1].end
            if (first, last) in seen:
                continue
            seen.add((first, last))
            key = f"{source}:{first}-{last}"
            entries.append(RetrievalEntry(key, f"{prefix}\n\n{resolved.text}", key,
                                          display_parts=display_parts + ((key, resolved.text),)))


def _line_passages(text: str) -> list[tuple[int, int]]:
    lines = text.splitlines()
    intervals: list[tuple[int, int]] = []
    start, size = 1, 0
    for number, line in enumerate(lines, 1):
        if size and size + len(line) + 1 > PASSAGE_CHARS:
            intervals.append((start, number - 1))
            start, size = number, 0
        size += len(line) + 1
    if lines:
        intervals.append((start, len(lines)))
    return [(start, end) for start, end in intervals if any(line.strip() for line in lines[start - 1:end])]


def _pending_entry(entry: RecentSubmittedMemoryEntry) -> RetrievalEntry:
    return RetrievalEntry(
        key=f"pending:{entry.key}",
        text=f"Submitted at: {entry.submitted_at}\n\n{entry.message.rstrip()}",
        source=f"Pending submission {entry.update_session_id}:{entry.candidate_id} (not settled Memory)",
        pending=True,
    )


def _require_source(root: Path, path: Path) -> None:
    if not _safe_source_file(root, path):
        raise RetrieveSelectionError(f"missing or unsafe retrieval source: {path.name}")
