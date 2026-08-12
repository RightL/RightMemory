import unittest

from rightmemory.corrections import (
    agent_correction_entries,
    validate_agent_correction_markdown,
    validate_corrections_markdown,
)


class AgentCorrectionMarkdownValidationTests(unittest.TestCase):
    def test_entry_positions_are_one_based_and_ignore_fenced_headings(self):
        entries = agent_correction_entries(
            "# Agent Corrections\n\n"
            "### First\n\nBody.\n\n"
            "```md\n### Example only\n```\n\n"
            "### Second\n\nBody.\n"
        )

        self.assertEqual(
            [(entry.position, entry.start_line) for entry in entries],
            [(1, 3), (2, 11)],
        )

    def test_accepts_entry_and_line_limits_and_ignores_fenced_headings(self):
        lines = [
            "# Agent Corrections",
            "### 1. Compact correction",
            "```md",
            "### 2. Example heading",
            "```",
            *(f"evidence {index}" for index in range(1, 12)),
            "汉" * 200,
        ]

        self.assertEqual(
            validate_agent_correction_markdown(
                "\n".join(lines),
                "MEMORY_agent-corrections-writing.md",
            ),
            [],
        )

    def test_rejects_more_than_fifteen_entries(self):
        text = "# Agent Corrections\n" + "\n".join(
            f"### {index}. Entry {index}\nevidence" for index in range(1, 17)
        )

        errors = validate_agent_correction_markdown(
            text,
            "MEMORY_agent-corrections-design.md",
        )

        self.assertTrue(any("contains 16 entries" in error for error in errors))

    def test_rejects_entry_over_sixteen_non_empty_lines(self):
        text = "\n".join(
            ["# Agent Corrections", "### 1. Oversized entry"]
            + [f"evidence {index}" for index in range(1, 17)]
        )

        errors = validate_agent_correction_markdown(
            text,
            "MEMORY_agent-corrections-writing.md",
        )

        self.assertTrue(any("contains 17 non-empty lines" in error for error in errors))

    def test_rejects_collection_over_one_hundred_eighty_non_empty_lines(self):
        lines = ["# Agent Corrections"]
        for entry in range(1, 16):
            lines.append(f"### {entry}. Entry {entry}")
            lines.extend(f"evidence {entry}-{line}" for line in range(1, 12))

        self.assertEqual(
            validate_agent_correction_markdown(
                "\n".join(lines[:-1]),
                "MEMORY_agent-corrections-design.md",
            ),
            [],
        )

        errors = validate_agent_correction_markdown(
            "\n".join(lines),
            "MEMORY_agent-corrections-design.md",
        )

        self.assertTrue(any("contains 181 non-empty lines" in error for error in errors))

    def test_rejects_lines_over_two_hundred_characters(self):
        text = "\n".join(
            ["# Agent Corrections", "### 1. Wide entry", "汉" * 201]
        )

        errors = validate_agent_correction_markdown(
            text,
            "MEMORY_agent-corrections-writing.md",
        )

        self.assertTrue(any("line 3 has 201 characters" in error for error in errors))


class CorrectionsMarkdownValidationTests(unittest.TestCase):
    def test_valid_collection_ignores_headings_inside_fences(self):
        text = """\
# RightMemory Edit Corrections

## Stable paths

### Candidate ###

The updater candidate included snapshot values.

### Proposed edit

```md
## This is evidence, not another entry
### Candidate
```

### Accepted edit

Keep only the stable path.
"""

        self.assertEqual(validate_corrections_markdown(text), [])

    def test_reports_missing_duplicate_and_unexpected_sections(self):
        text = """\
# RightMemory Edit Corrections

## Bad entry

### Accepted edit

x

### Candidate

y

### Candidate

z

### Lesson

extra
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("missing `### Proposed edit`" in error for error in errors))
        self.assertTrue(any("repeats `### Candidate`" in error for error in errors))
        self.assertTrue(any("unexpected `### Lesson`" in error for error in errors))

    def test_rejects_legacy_glued_and_out_of_order_sections(self):
        text = """\
## Legacy and loose headings

### Background
legacy

### Candidate#
glued

### Proposed edit
proposed

### Accepted edit
accepted

## Out of order

### Accepted edit
accepted

### Candidate
candidate

### Proposed edit
proposed
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("unexpected `### Background`" in error for error in errors))
        self.assertTrue(any("unexpected `### Candidate#`" in error for error in errors))
        self.assertTrue(any("missing `### Candidate`" in error for error in errors))
        self.assertTrue(any("sections must be ordered" in error for error in errors))

    def test_rejects_more_than_fifteen_entries(self):
        entry = """\
## Entry {number}

### Candidate
a
### Proposed edit
b
### Accepted edit
c
"""
        text = "# RightMemory Edit Corrections\n\n" + "\n".join(
            entry.format(number=index) for index in range(1, 17)
        )

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("contains 16 entries" in error for error in errors))

    def test_rejects_empty_required_section_content(self):
        text = """\
## Empty proposal

### Candidate
context

### Proposed edit

### Accepted edit
accepted
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("empty `### Proposed edit` content" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
