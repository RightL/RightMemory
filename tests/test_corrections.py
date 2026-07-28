import unittest

from rightmemory.corrections import validate_corrections_markdown


class CorrectionsMarkdownValidationTests(unittest.TestCase):
    def test_valid_collection_ignores_headings_inside_fences(self):
        text = """\
# RightMemory Update Corrections

## Stable paths

### Background

The updater included snapshot values.

### Proposed edit

```md
## This is evidence, not another entry
### Background
```

### Accepted edit

Keep only the stable path.
"""

        self.assertEqual(validate_corrections_markdown(text), [])

    def test_reports_missing_duplicate_and_unexpected_sections(self):
        text = """\
# RightMemory Update Corrections

## Bad entry

### Accepted edit

x

### Background

y

### Background

z

### Lesson

extra
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("missing `### Proposed edit`" in error for error in errors))
        self.assertTrue(any("repeats `### Background`" in error for error in errors))
        self.assertTrue(any("unexpected `### Lesson`" in error for error in errors))

    def test_rejects_more_than_fifteen_entries(self):
        entry = """\
## Entry {number}

### Background
a
### Proposed edit
b
### Accepted edit
c
"""
        text = "# RightMemory Update Corrections\n\n" + "\n".join(
            entry.format(number=index) for index in range(1, 17)
        )

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("contains 16 entries" in error for error in errors))

    def test_rejects_empty_required_section_content(self):
        text = """\
## Empty proposal

### Background
context

### Proposed edit

### Accepted edit
accepted
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("empty `### Proposed edit` content" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
