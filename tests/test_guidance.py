import tempfile
import unittest
from pathlib import Path

from rightmemory.guidance import (
    GUIDANCE_INBOX_PATH,
    GuidanceConflictError,
    merge_guidance_inbox,
    parse_guidance_inbox,
    submit_guidance,
    validate_guidance_inbox,
)
from rightmemory.tools import MemoryTools
from tests.isolated_write_test_base import IsolatedWriteTestBase


SAMPLE_ONE = """# Pending Agent Guidance

## GI-20260815-a1b2c3d4

Session: session-one
Submitted: 2026-08-15T01:00:00Z

The agent over-structured a simple prompt change. The settled direction is to keep the judgment direct.
"""

SAMPLE_TWO = """# Pending Agent Guidance

## GI-20260815-e5f6a7b8

Session: session-two
Submitted: 2026-08-15T02:00:00Z

The agent should distinguish capture from semantic admission.
"""


class GuidanceFormatTests(unittest.TestCase):
    def test_parse_round_trips_free_form_entry_body(self):
        entries = parse_guidance_inbox(SAMPLE_ONE)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry_id, "GI-20260815-a1b2c3d4")
        self.assertEqual(entries[0].session_id, "session-one")
        self.assertIn("over-structured", entries[0].body)
        self.assertEqual(validate_guidance_inbox(SAMPLE_ONE), [])

    def test_validation_rejects_duplicate_ids_and_missing_provenance(self):
        malformed = """# Pending Agent Guidance

## GI-20260815-a1b2c3d4

Session: one
Submitted: 2026-08-15T01:00:00Z

One.

## GI-20260815-a1b2c3d4

Session: two

Two.
"""
        errors = validate_guidance_inbox(malformed)
        self.assertTrue(any("duplicate" in error for error in errors))
        self.assertTrue(any("Submitted" in error for error in errors))

    def test_three_way_merge_unions_independent_additions(self):
        merged = merge_guidance_inbox(
            "# Pending Agent Guidance\n",
            SAMPLE_ONE,
            SAMPLE_TWO,
        )
        entries = parse_guidance_inbox(merged)
        self.assertEqual(
            {entry.entry_id for entry in entries},
            {"GI-20260815-a1b2c3d4", "GI-20260815-e5f6a7b8"},
        )

    def test_three_way_merge_preserves_deletion_against_unchanged_entry(self):
        merged = merge_guidance_inbox(SAMPLE_ONE, "# Pending Agent Guidance\n", SAMPLE_ONE)
        self.assertEqual(parse_guidance_inbox(merged), [])

    def test_three_way_merge_rejects_incompatible_same_id_edits(self):
        changed = SAMPLE_ONE.replace("keep the judgment direct", "always use a schema")
        with self.assertRaises(GuidanceConflictError):
            merge_guidance_inbox(SAMPLE_ONE, changed, SAMPLE_ONE.replace("direct", "concise"))


class GuidanceSubmitTests(IsolatedWriteTestBase):
    def test_submit_creates_and_appends_tracked_inbox_without_update_state(self):
        first = submit_guidance(self.root, "session-one", "First settled guidance.")
        second = submit_guidance(self.root, "session-two", "Second settled guidance.")

        inbox = self.root / GUIDANCE_INBOX_PATH
        self.assertTrue(inbox.is_file())
        entries = parse_guidance_inbox(inbox.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry.session_id for entry in entries}, {"session-one", "session-two"})
        self.assertNotEqual(first.entry_id, second.entry_id)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual(self._git("ls-files", GUIDANCE_INBOX_PATH), GUIDANCE_INBOX_PATH)
        self.assertFalse((self.root / ".runtime" / "async" / "update").exists())
        self._assert_isolated_cleanup()

    def test_submit_refuses_a_malformed_existing_inbox(self):
        inbox = self.root / GUIDANCE_INBOX_PATH
        inbox.write_text("# Wrong Heading\n", encoding="utf-8")
        self._git("add", "-f", GUIDANCE_INBOX_PATH)
        self._git("commit", "-m", "seed malformed inbox")

        with self.assertRaises(ValueError):
            submit_guidance(self.root, "session-one", "Evidence.")


class GuidanceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory {#memory}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / GUIDANCE_INBOX_PATH).write_text(SAMPLE_ONE, encoding="utf-8")

    def test_retrieve_file_tools_cannot_see_inbox(self):
        tools = MemoryTools(self.root, role="retrieve")
        self.assertNotIn(GUIDANCE_INBOX_PATH, tools.list_files("*.md"))
        with self.assertRaises(ValueError):
            tools.read(GUIDANCE_INBOX_PATH)

    def test_validation_checks_inbox_without_counting_it_as_graph_file(self):
        tools = MemoryTools(self.root)
        self.assertTrue(tools.validate_memory().startswith("validation passed:"))
        manifest_files = {
            path.relative_to(self.root).as_posix()
            for path in tools._memory_files()
        }
        self.assertNotIn(GUIDANCE_INBOX_PATH, manifest_files)

        (self.root / GUIDANCE_INBOX_PATH).write_text("# Wrong Heading\n", encoding="utf-8")
        self.assertIn("validation failed:", tools.validate_memory())


if __name__ == "__main__":
    unittest.main()
