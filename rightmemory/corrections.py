from __future__ import annotations

import re


_SECTIONS = ("Candidate", "Proposed edit", "Accepted edit")


def validate_corrections_markdown(text: str) -> list[str]:
    """Validate the bounded RightMemory edit-correction collection shape."""
    errors: list[str] = []
    entries: list[tuple[str, int, list[tuple[str, int]]]] = []
    current: tuple[str, int, list[tuple[str, int]]] | None = None
    fence_char: str | None = None
    fence_length = 0

    lines = text.splitlines()
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
        if fence_char is not None:
            continue

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

    if len(entries) > 15:
        errors.append(f"corrections.md contains {len(entries)} entries; at most 15 are allowed")

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
