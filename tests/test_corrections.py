import unittest

from rightmemory.corrections import validate_corrections_markdown


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
