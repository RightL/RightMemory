"""Deterministic graph context for the first turn of a Pursuit conversation.

Controller source locations are always relative to the Controller's RightMemory
root.  They are intentionally kept separate from the execution host, project,
and working directory supplied by the conversation service.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from ..graph import (
    FOCUS_HEADING_RE,
    BlockKey,
    DocumentBlock,
    GraphItem,
    GraphManifest,
    SourceTextPart,
    build_graph_manifest,
)


class OpeningContextError(ValueError):
    """The selected Pursuit snapshot cannot be projected onto the current graph."""


@dataclass(frozen=True, slots=True)
class OpeningContextExecution:
    host_label: str
    project_label: str
    execution_cwd: str


@dataclass(frozen=True, slots=True)
class OpeningContextProseFragment:
    source_path: str
    start_line: int
    end_line: int
    markdown: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OpeningContextSection:
    selection_id: str
    item_id: str | None
    title: str
    block_kind: str
    family: str
    anchor_kind: str | None
    source_path: str
    source_line: int
    roles: tuple[str, ...]
    prose_fragments: tuple[OpeningContextProseFragment, ...]
    markdown: str

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["roles"] = list(self.roles)
        value["prose_fragments"] = [
            fragment.to_dict() for fragment in self.prose_fragments
        ]
        return value


@dataclass(frozen=True, slots=True)
class OpeningContext:
    controller_memory_root: str
    execution: OpeningContextExecution
    current: OpeningContextSection
    neighbors: tuple[OpeningContextSection, ...]
    ancestors: tuple[OpeningContextSection, ...]
    sections: tuple[OpeningContextSection, ...]
    edge_triples: tuple[tuple[str, str, str], ...]
    rendered_text: str

    @property
    def text(self) -> str:
        return self.rendered_text

    def to_dict(self) -> dict[str, object]:
        return {
            "controller_memory_root": self.controller_memory_root,
            "execution": asdict(self.execution),
            "current": self.current.to_dict(),
            "neighbors": [section.to_dict() for section in self.neighbors],
            "ancestors": [section.to_dict() for section in self.ancestors],
            "sections": [section.to_dict() for section in self.sections],
            "edge_triples": [list(edge) for edge in self.edge_triples],
            "rendered_text": self.rendered_text,
        }


def build_opening_context(
    memory_root: Path,
    pursuit_snapshot: Mapping[str, object],
    *,
    host_label: str,
    project_label: str,
    execution_cwd: str,
) -> OpeningContext:
    """Build opening context from the current canonical RightMemory graph."""
    manifest = build_graph_manifest(Path(memory_root))
    return project_opening_context(
        manifest,
        pursuit_snapshot,
        host_label=host_label,
        project_label=project_label,
        execution_cwd=execution_cwd,
    )


def project_opening_context(
    manifest: GraphManifest,
    pursuit_snapshot: Mapping[str, object],
    *,
    host_label: str,
    project_label: str,
    execution_cwd: str,
) -> OpeningContext:
    """Project one current Pursuit and its one-hop graph neighborhood."""
    controller_memory_root = str(manifest.root)
    execution = OpeningContextExecution(
        host_label=_required_line(host_label, "host_label"),
        project_label=_required_line(project_label, "project_label"),
        execution_cwd=_required_line(execution_cwd, "execution_cwd"),
    )
    current_block = _resolve_current_block(manifest, pursuit_snapshot)
    current_item = _graph_item_for_block(manifest, current_block)

    neighbor_keys: set[BlockKey] = set()
    edge_records: list[tuple[tuple[object, ...], tuple[str, str, str]]] = []
    if current_item is not None:
        for edge_index, (edge_type, target_id) in enumerate(current_item.edges):
            target = manifest.items.get(target_id)
            target_block = _block_for_item(manifest, target)
            if target_block is None or target_block.key == current_block.key or _is_focus(target_block):
                continue
            neighbor_keys.add(target_block.key)
            edge_records.append(
                (
                    (*_block_order(manifest, current_block), edge_index, target_id),
                    (current_item.id, edge_type, target_id),
                )
            )

        for source in sorted(manifest.items.values(), key=lambda item: _item_order(manifest, item)):
            source_block = _block_for_item(manifest, source)
            if source_block is None or source_block.key == current_block.key or _is_focus(source_block):
                continue
            for edge_index, (edge_type, target_id) in enumerate(source.edges):
                if target_id != current_item.id:
                    continue
                neighbor_keys.add(source_block.key)
                edge_records.append(
                    (
                        (*_block_order(manifest, source_block), edge_index, current_item.id),
                        (source.id, edge_type, current_item.id),
                    )
                )

    ordered_neighbor_keys = tuple(
        sorted(neighbor_keys, key=lambda key: _block_order(manifest, manifest.blocks[key]))
    )
    ancestor_keys: set[BlockKey] = set()
    for key in (current_block.key, *ordered_neighbor_keys):
        ancestor_keys.update(_heading_ancestors(manifest, key))

    roles_by_key: dict[BlockKey, set[str]] = {current_block.key: {"current"}}
    for key in ordered_neighbor_keys:
        roles_by_key.setdefault(key, set()).add("neighbor")
    for key in ancestor_keys:
        roles_by_key.setdefault(key, set()).add("ancestor")

    ordered_keys = tuple(
        sorted(roles_by_key, key=lambda key: _block_order(manifest, manifest.blocks[key]))
    )
    sections_by_key = {
        key: _section(manifest, manifest.blocks[key], roles_by_key[key])
        for key in ordered_keys
    }
    sections = tuple(sections_by_key[key] for key in ordered_keys)
    current = sections_by_key[current_block.key]
    neighbors = tuple(sections_by_key[key] for key in ordered_neighbor_keys)
    ancestors = tuple(
        sections_by_key[key]
        for key in sorted(
            ancestor_keys,
            key=lambda ancestor: _block_order(manifest, manifest.blocks[ancestor]),
        )
    )
    edge_triples = tuple(
        dict.fromkeys(
            edge for _order, edge in sorted(edge_records, key=lambda record: record[0])
        )
    )
    rendered = _render(
        controller_memory_root,
        execution,
        current,
        sections,
        edge_triples,
    )
    return OpeningContext(
        controller_memory_root=controller_memory_root,
        execution=execution,
        current=current,
        neighbors=neighbors,
        ancestors=ancestors,
        sections=sections,
        edge_triples=edge_triples,
        rendered_text=rendered,
    )


def _resolve_current_block(
    manifest: GraphManifest,
    pursuit_snapshot: Mapping[str, object],
) -> DocumentBlock:
    if not isinstance(pursuit_snapshot, Mapping):
        raise OpeningContextError("pursuit_snapshot must be a mapping")
    source_path = pursuit_snapshot.get("source_path")
    source_line = pursuit_snapshot.get("source_line")
    if not isinstance(source_path, str) or not source_path:
        raise OpeningContextError("the Pursuit snapshot needs a root-relative source_path")
    if "\\" in source_path:
        raise OpeningContextError("the Pursuit snapshot source_path must use POSIX separators")
    relative = PurePosixPath(source_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OpeningContextError("the Pursuit snapshot source_path must stay inside the RightMemory root")
    if not isinstance(source_line, int) or isinstance(source_line, bool) or source_line < 1:
        raise OpeningContextError("the Pursuit snapshot needs a positive source_line")

    candidate = (manifest.root.joinpath(*relative.parts)).resolve()
    try:
        candidate.relative_to(manifest.root)
    except ValueError as exc:
        raise OpeningContextError(
            "the Pursuit snapshot source_path must stay inside the RightMemory root"
        ) from exc
    block = manifest.blocks.get((candidate, source_line))
    if block is None or block.kind not in {"heading", "node"} or block.family != "pursuit":
        raise OpeningContextError("the Pursuit snapshot no longer identifies a Pursuit graph block")
    if _is_focus(block):
        raise OpeningContextError("Focus is not a selectable Pursuit graph block")

    expected_id = pursuit_snapshot.get("id")
    resolved_id = _selection_id(manifest, block)
    if not isinstance(expected_id, str) or expected_id != resolved_id:
        raise OpeningContextError("the Pursuit snapshot id no longer matches its source block")
    return block


def _graph_item_for_block(
    manifest: GraphManifest,
    block: DocumentBlock,
) -> GraphItem | None:
    if block.item_id is None:
        return None
    item = manifest.items.get(block.item_id)
    if item is None or item.block_key != block.key:
        raise OpeningContextError("the Pursuit snapshot identifies a duplicated graph id")
    return item


def _block_for_item(
    manifest: GraphManifest,
    item: GraphItem | None,
) -> DocumentBlock | None:
    if item is None or item.block_key is None:
        return None
    return manifest.blocks.get(item.block_key)


def _heading_ancestors(manifest: GraphManifest, key: BlockKey) -> set[BlockKey]:
    ancestors: set[BlockKey] = set()
    seen: set[BlockKey] = set()
    current = manifest.blocks[key].logical_parent
    while current is not None and current not in seen:
        seen.add(current)
        block = manifest.blocks[current]
        if block.kind == "heading" and not _is_focus(block):
            ancestors.add(current)
        current = block.logical_parent
    return ancestors


def _section(
    manifest: GraphManifest,
    block: DocumentBlock,
    roles: set[str],
) -> OpeningContextSection:
    role_order = ("current", "neighbor", "ancestor")
    prose_fragments = _owned_prose_fragments(manifest, block)
    return OpeningContextSection(
        selection_id=_selection_id(manifest, block),
        item_id=block.item_id,
        title=block.title,
        block_kind=block.kind,
        family=block.family,
        anchor_kind=block.anchor_kind,
        source_path=_source_path(manifest, block),
        source_line=block.line_number,
        roles=tuple(role for role in role_order if role in roles),
        prose_fragments=prose_fragments,
        markdown="\n\n".join(fragment.markdown for fragment in prose_fragments),
    )


def _owned_prose_fragments(
    manifest: GraphManifest,
    block: DocumentBlock,
) -> tuple[OpeningContextProseFragment, ...]:
    if block.kind == "node":
        if not block.prose:
            return ()
        return (
            OpeningContextProseFragment(
                source_path=_source_path(manifest, block),
                start_line=block.line_number,
                end_line=block.line_number,
                markdown=block.prose,
            ),
        )

    # F# is a file boundary, not a logical content boundary. Direct backing-root
    # text belongs to the owning heading, but retains its physical Controller
    # source. Descendant BlockKeys stay excluded.
    groups: list[list[SourceTextPart]] = []
    for part in block.logical_text_parts:
        if (
            groups
            and groups[-1][-1].source_path == part.source_path
            and groups[-1][-1].line_number + 1 == part.line_number
        ):
            groups[-1].append(part)
        else:
            groups.append([part])

    fragments: list[OpeningContextProseFragment] = []
    for group in groups:
        first_content = next(
            (index for index, part in enumerate(group) if part.text.strip()),
            None,
        )
        if first_content is None:
            continue
        last_content = next(
            index
            for index in range(len(group) - 1, first_content - 1, -1)
            if group[index].text.strip()
        )
        content = group[first_content:last_content + 1]
        fragments.append(
            OpeningContextProseFragment(
                source_path=_source_path_for_path(manifest, content[0].source_path),
                start_line=content[0].line_number,
                end_line=content[-1].line_number,
                markdown="\n".join(part.text for part in content),
            )
        )
    return tuple(fragments)


def _selection_id(manifest: GraphManifest, block: DocumentBlock) -> str:
    if block.item_id is not None:
        return block.item_id
    return f"plain:{_source_path(manifest, block)}:{block.line_number}"


def _source_path(manifest: GraphManifest, block: DocumentBlock) -> str:
    return _source_path_for_path(manifest, block.source_path)


def _source_path_for_path(manifest: GraphManifest, source_path: Path) -> str:
    return source_path.relative_to(manifest.root).as_posix()


def _item_order(manifest: GraphManifest, item: GraphItem) -> tuple[object, ...]:
    block = _block_for_item(manifest, item)
    if block is None:
        return (len(manifest.blocks), item.file.as_posix(), item.line_number, item.id)
    return _block_order(manifest, block)


def _block_order(manifest: GraphManifest, block: DocumentBlock) -> tuple[object, ...]:
    rank = block.traversal_rank if block.traversal_rank >= 0 else len(manifest.blocks)
    return (rank, _source_path(manifest, block), block.line_number, block.item_id or "")


def _is_focus(block: DocumentBlock) -> bool:
    return block.kind == "focus" or (
        block.kind == "heading" and FOCUS_HEADING_RE.match(block.line) is not None
    )


def _required_line(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise OpeningContextError(f"{name} must be text")
    clean = value.strip()
    if not clean or "\x00" in clean or "\n" in clean or "\r" in clean:
        raise OpeningContextError(f"{name} must be nonempty single-line text")
    return clean


def _render(
    controller_memory_root: str,
    execution: OpeningContextExecution,
    current: OpeningContextSection,
    sections: tuple[OpeningContextSection, ...],
    edge_triples: tuple[tuple[str, str, str], ...],
) -> str:
    parts = [
        "# RightMemory opening context",
        "",
        f"Execution host: {execution.host_label}",
        f"Execution project: {execution.project_label}",
        f"Execution working directory: `{execution.execution_cwd}`",
        "",
        (
            "Controller Memory root (Controller-only; not an execution working "
            f"directory): `{controller_memory_root}`"
        ),
        "",
        (
            "Controller sources below are relative to that root. They are not "
            "paths on the execution host."
        ),
        "",
        f"Current Pursuit: `{current.selection_id}`",
        "",
        "## Selected graph context",
    ]
    for section in sections:
        parts.extend(
            (
                "",
                f"### Block `{section.selection_id}`",
                f"Title: {section.title}",
                f"Role: {', '.join(section.roles)}",
                f"Controller anchor source: `{section.source_path}:{section.source_line}`",
            )
        )
        if section.prose_fragments:
            for fragment in section.prose_fragments:
                source = f"{fragment.source_path}:{fragment.start_line}"
                if fragment.end_line != fragment.start_line:
                    source += f"-{fragment.end_line}"
                parts.extend(
                    (
                        "",
                        f"Owned prose from Controller source `{source}`:",
                        "",
                        fragment.markdown,
                    )
                )
        else:
            parts.extend(("", "_(This block has no owned prose.)_"))
    parts.extend(("", "## Direct current-Pursuit connections"))
    if edge_triples:
        parts.extend(
            f"- `{source}` --`{edge_type}`--> `{target}`"
            for source, edge_type, target in edge_triples
        )
    else:
        parts.extend(("", "No direct graph connections were selected."))
    return "\n".join(parts).rstrip() + "\n"
