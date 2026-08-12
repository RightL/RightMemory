from __future__ import annotations

import re
from dataclasses import dataclass


_SECTIONS = ("Candidate", "Proposed edit", "Accepted edit")
CORRECTION_COLLECTION_MAX_ENTRIES = 10
AGENT_CORRECTION_MAX_ENTRY_LINES = 16
AGENT_CORRECTION_MAX_COLLECTION_LINES = 180
AGENT_CORRECTION_MAX_LINE_LENGTH = 200
AGENT_CORRECTION_SOURCE_PATHS = {
    "AC#writing": "MEMORY_agent-corrections-writing.md",
    "AC#design": "MEMORY_agent-corrections-design.md",
}


@dataclass(frozen=True)
class AgentCorrectionEntry:
    """One position-addressed entry in a fixed Agent Correction collection."""

    position: int
    title: str
    start_line: int
    text: str


def _iter_unfenced_lines(lines: list[str]):
    fence_char: str | None = None
    fence_length = 0
    for line_number, line in enumerate(lines, start=1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            marker = fence.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is None:
            yield line_number, line


def agent_correction_entries(text: str) -> list[AgentCorrectionEntry]:
    """Return complete ``###`` entries with stable one-based positions."""
    lines = text.splitlines()
    headings: list[tuple[str, int]] = []
    for line_number, line in _iter_unfenced_lines(lines):
        heading = re.match(r"^ {0,3}###[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", line)
        if heading is not None:
            headings.append((heading.group(1).strip(), line_number))

    entries: list[AgentCorrectionEntry] = []
    for index, (title, line_number) in enumerate(headings):
        end_line = headings[index + 1][1] - 1 if index + 1 < len(headings) else len(lines)
        entry_text = "\n".join(lines[line_number - 1 : end_line]).rstrip("\r\n")
        entries.append(
            AgentCorrectionEntry(
                position=index + 1,
                title=title,
                start_line=line_number,
                text=entry_text,
            )
        )
    return entries


def annotate_agent_correction_entries(text: str, source_id: str) -> str:
    """Expose each entry's retrieval id immediately before its heading."""
    if source_id not in AGENT_CORRECTION_SOURCE_PATHS:
        raise ValueError(f"unknown Agent Correction source: {source_id}")
    entries = agent_correction_entries(text)
    positions = {entry.start_line: entry.position for entry in entries}
    rendered: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        position = positions.get(line_number)
        if position is not None:
            rendered.append(f"[source_id: {source_id} | entry_position: {position}]")
        rendered.append(line)
    return "\n".join(rendered)


def validate_agent_correction_markdown(text: str, path: str) -> list[str]:
    """Validate one fixed Agent Corrections collection."""
    errors: list[str] = []
    lines = text.splitlines()
    entries = agent_correction_entries(text)

    if len(entries) > CORRECTION_COLLECTION_MAX_ENTRIES:
        errors.append(
            f"{path} contains {len(entries)} entries; "
            f"at most {CORRECTION_COLLECTION_MAX_ENTRIES} are allowed"
        )

    collection_lines = sum(1 for line in lines if line.strip())
    if collection_lines > AGENT_CORRECTION_MAX_COLLECTION_LINES:
        errors.append(
            f"{path} contains {collection_lines} non-empty lines; "
            f"at most {AGENT_CORRECTION_MAX_COLLECTION_LINES} are allowed"
        )

    for entry_index, entry in enumerate(entries):
        title = entry.title
        line_number = entry.start_line
        entry_end = (
            entries[entry_index + 1].start_line - 1
            if entry_index + 1 < len(entries)
            else len(lines)
        )
        entry_lines = sum(1 for line in lines[line_number - 1 : entry_end] if line.strip())
        if entry_lines > AGENT_CORRECTION_MAX_ENTRY_LINES:
            errors.append(
                f"{path} line {line_number}: correction entry `{title}` contains "
                f"{entry_lines} non-empty lines; at most "
                f"{AGENT_CORRECTION_MAX_ENTRY_LINES} are allowed"
            )

    overlong_lines = [
        (line_number, len(line))
        for line_number, line in enumerate(lines, start=1)
        if len(line) > AGENT_CORRECTION_MAX_LINE_LENGTH
    ]
    if overlong_lines:
        line_number, line_length = overlong_lines[0]
        errors.append(
            f"{path} has {len(overlong_lines)} lines over "
            f"{AGENT_CORRECTION_MAX_LINE_LENGTH} characters; "
            f"line {line_number} has {line_length} characters"
        )

    return errors


def validate_corrections_markdown(text: str) -> list[str]:
    """Validate the bounded RightMemory edit-correction collection shape."""
    errors: list[str] = []
    entries: list[tuple[str, int, list[tuple[str, int]]]] = []
    current: tuple[str, int, list[tuple[str, int]]] | None = None
    lines = text.splitlines()
    for line_number, line in _iter_unfenced_lines(lines):
        heading = re.match(r"^ {0,3}(#{2,3})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", line)
        if heading is None:
            continue
        level = len(heading.group(1))
        title = heading.group(2).strip()
        if level == 2:
            current = (title, line_number, [])
            entries.append(current)
            continue
        if current is None:
            errors.append(
                f"line {line_number}: correction section `{title}` appears before any `##` entry"
            )
            continue
        current[2].append((title, line_number))

    if len(entries) > CORRECTION_COLLECTION_MAX_ENTRIES:
        errors.append(
            f"corrections.md contains {len(entries)} entries; "
            f"at most {CORRECTION_COLLECTION_MAX_ENTRIES} are allowed"
        )

    expected = list(_SECTIONS)
    for entry_index, (title, line_number, sections) in enumerate(entries):
        names = [name for name, _section_line in sections]
        for name, section_line in sections:
            if name not in _SECTIONS:
                errors.append(
                    f"line {section_line}: correction entry `{title}` has unexpected "
                    f"`### {name}` section"
                )
        for name in expected:
            count = names.count(name)
            if count == 0:
                errors.append(
                    f"line {line_number}: correction entry `{title}` is missing `### {name}`"
                )
            elif count > 1:
                errors.append(
                    f"line {line_number}: correction entry `{title}` repeats `### {name}`"
                )
        recognized = [name for name in names if name in _SECTIONS]
        if all(names.count(name) == 1 for name in expected) and recognized != expected:
            errors.append(
                f"line {line_number}: correction entry `{title}` sections must be ordered as "
                "Candidate, Proposed edit, Accepted edit"
            )
        entry_end = (
            entries[entry_index + 1][1] - 1
            if entry_index + 1 < len(entries)
            else len(lines)
        )
        ordered_sections = sorted(sections, key=lambda item: item[1])
        for section_index, (name, section_line) in enumerate(ordered_sections):
            if name not in _SECTIONS or names.count(name) != 1:
                continue
            section_end = (
                ordered_sections[section_index + 1][1] - 1
                if section_index + 1 < len(ordered_sections)
                else entry_end
            )
            body = "\n".join(lines[section_line:section_end]).strip()
            if not body:
                errors.append(
                    f"line {section_line}: correction entry `{title}` has empty "
                    f"`### {name}` content"
                )
    return errors
