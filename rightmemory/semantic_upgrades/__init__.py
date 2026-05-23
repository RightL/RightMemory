from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Iterable, Sequence

from rightmemory.session import _ensure_runtime_gitignore, _fsync_directory


STATE_RELATIVE_PATH = Path(".runtime") / "semantic-upgrades.json"


@dataclass(frozen=True)
class SemanticUpgradeNote:
    id: str
    introduced_at: date
    title: str
    body: str
    source: str


@dataclass(frozen=True)
class SemanticUpgradeLoadResult:
    notes: list[SemanticUpgradeNote]
    warnings: list[str]


@dataclass(frozen=True)
class SemanticUpgradeContext:
    notes: list[SemanticUpgradeNote]
    warnings: list[str]

    @property
    def ids(self) -> list[str]:
        return [note.id for note in self.notes]


def parse_note_text(source: str, text: str) -> SemanticUpgradeNote:
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: missing front matter")
    end_marker = "\n---\n"
    end = text.find(end_marker, 4)
    if end == -1:
        raise ValueError(f"{source}: unterminated front matter")

    metadata = _parse_front_matter(source, text[4:end])
    body = text[end + len(end_marker) :].strip()
    note_id = _required_metadata(source, metadata, "id")
    introduced_at = _parse_date(source, _required_metadata(source, metadata, "introduced_at"))
    title = _extract_title(source, body)
    return SemanticUpgradeNote(
        id=note_id,
        introduced_at=introduced_at,
        title=title,
        body=body,
        source=source,
    )


def load_packaged_notes() -> SemanticUpgradeLoadResult:
    root = resources.files(__package__)
    entries = [entry for entry in root.iterdir() if entry.name.endswith(".md")]
    return _load_note_entries(entries)


def load_notes_from_directory(directory: Path) -> SemanticUpgradeLoadResult:
    entries = sorted(directory.glob("*.md"))
    return _load_note_entries(entries)


def pending_context(memory_root: Path, state_root: Path | None = None) -> SemanticUpgradeContext:
    state_root = state_root if state_root is not None else memory_root
    loaded = load_packaged_notes()
    absorbed, state_warnings = _read_absorbed_ids(state_root)
    notes = [note for note in loaded.notes if note.id not in absorbed]
    return SemanticUpgradeContext(notes=notes, warnings=[*loaded.warnings, *state_warnings])


def render_prompt_context(context: SemanticUpgradeContext) -> str:
    if not context.notes and not context.warnings:
        return ""
    lines = [
        "Pending semantic upgrade notes:",
        "",
        "Use these notes to reconsider how existing memory should be organized and interpreted under the current RightMemory model. Process them in chronological order. If later notes refine, narrow, or contradict earlier notes, treat the later note as the current guidance. Do not copy these notes into memory as maintenance text. Apply them when they help make existing memory clearer, less stale, or better aligned with the current schema and role prompts.",
        "",
    ]
    if context.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in context.warnings)
        lines.append("")
    for note in context.notes:
        lines.extend(
            [
                f"## {note.id}",
                f"Introduced: {note.introduced_at.isoformat()}",
                f"Source: {note.source}",
                "",
                note.body.strip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def mark_absorbed(memory_root: Path, ids: Iterable[str], now: datetime | None = None) -> None:
    ids = sorted(set(ids))
    if not ids:
        return
    state_path = _state_path(memory_root)
    absorbed, _warnings = _read_absorbed_ids(memory_root)
    timestamp = (now or datetime.now(UTC)).isoformat()
    data = {
        "absorbed": {
            note_id: {"absorbed_at": timestamp}
            for note_id in sorted(absorbed.union(ids))
        }
    }
    _ensure_runtime_gitignore(memory_root / ".runtime")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, state_path)
    _fsync_directory(state_path.parent)


def format_refresh_summary(context: SemanticUpgradeContext) -> str:
    lines: list[str] = []
    for warning in context.warnings:
        lines.append(f"  [warning] semantic upgrade note skipped: {warning}")
    if context.notes:
        lines.append(
            f"  [notice]  {len(context.notes)} semantic upgrade note(s) pending for the next dreamer cycle:"
        )
        lines.extend(f"            {note.id}" for note in context.notes)
    else:
        lines.append("  [keep]    no semantic upgrade notes pending")
    return "\n".join(lines)


def baseline_packaged_notes(memory_root: Path) -> SemanticUpgradeContext:
    loaded = load_packaged_notes()
    note_ids = [note.id for note in loaded.notes]
    if note_ids:
        mark_absorbed(memory_root, note_ids)
    else:
        _write_state_if_missing(memory_root)
    return SemanticUpgradeContext(notes=loaded.notes, warnings=loaded.warnings)


def format_baseline_summary(context: SemanticUpgradeContext) -> str:
    lines: list[str] = []
    for warning in context.warnings:
        lines.append(f"  [warning] semantic upgrade note skipped: {warning}")
    if context.notes:
        lines.append(
            f"  [keep]    semantic upgrade baseline recorded for {len(context.notes)} current note(s):"
        )
        lines.extend(f"            {note.id}" for note in context.notes)
    else:
        lines.append("  [keep]    semantic upgrade baseline has no current notes")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rightmemory.semantic_upgrades")
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh = subparsers.add_parser("refresh", help="refresh semantic upgrade pending state")
    refresh.add_argument("--memory-root", required=True, help="RightMemory memory root")
    baseline = subparsers.add_parser("baseline", help="mark current packaged semantic upgrades as absorbed")
    baseline.add_argument("--memory-root", required=True, help="RightMemory memory root")
    args = parser.parse_args(argv)

    if args.command == "refresh":
        memory_root = Path(args.memory_root).expanduser().resolve()
        _ensure_runtime_gitignore(memory_root / ".runtime")
        memory_root.joinpath(".runtime").mkdir(parents=True, exist_ok=True)
        context = pending_context(memory_root)
        _write_state_if_missing(memory_root)
        print(format_refresh_summary(context))
        return 0
    if args.command == "baseline":
        memory_root = Path(args.memory_root).expanduser().resolve()
        _ensure_runtime_gitignore(memory_root / ".runtime")
        memory_root.joinpath(".runtime").mkdir(parents=True, exist_ok=True)
        context = baseline_packaged_notes(memory_root)
        print(format_baseline_summary(context))
        return 0
    raise ValueError(f"unknown semantic upgrade command: {args.command}")


def _load_note_entries(entries: Sequence[object]) -> SemanticUpgradeLoadResult:
    notes: list[SemanticUpgradeNote] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for entry in sorted(entries, key=lambda item: getattr(item, "name", str(item))):
        source = getattr(entry, "name", str(entry))
        try:
            text = entry.read_text(encoding="utf-8")
            note = parse_note_text(source, text)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        if note.id in seen:
            warnings.append(f"{source}: duplicate semantic upgrade id: {note.id}")
            continue
        seen.add(note.id)
        notes.append(note)
    notes.sort(key=lambda note: (note.introduced_at, note.id))
    return SemanticUpgradeLoadResult(notes=notes, warnings=warnings)


def _parse_front_matter(source: str, text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{source}: invalid front matter line: {line}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def _required_metadata(source: str, metadata: dict[str, str], key: str) -> str:
    value = metadata.get(key)
    if not value:
        raise ValueError(f"{source}: missing required front matter key: {key}")
    return value


def _parse_date(source: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{source}: introduced_at must be an ISO date") from exc


def _extract_title(source: str, body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip()
    raise ValueError(f"{source}: missing top-level title")


def _state_path(memory_root: Path) -> Path:
    return memory_root / STATE_RELATIVE_PATH


def _read_absorbed_ids(memory_root: Path) -> tuple[set[str], list[str]]:
    path = _state_path(memory_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set(), []
    except json.JSONDecodeError as exc:
        return set(), [f"could not read semantic upgrade state {path}: {exc}"]
    absorbed = data.get("absorbed")
    if not isinstance(absorbed, dict):
        return set(), [f"could not read semantic upgrade state {path}: absorbed must be an object"]
    return {key for key in absorbed if isinstance(key, str)}, []


def _write_state_if_missing(memory_root: Path) -> None:
    path = _state_path(memory_root)
    if path.exists():
        return
    _ensure_runtime_gitignore(memory_root / ".runtime")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump({"absorbed": {}}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
