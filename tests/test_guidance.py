import os
import tempfile
import unittest
from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.entrypoint import _guidance_validation_errors
from rightmemory.guidance import (
    GUIDANCE_INBOX_PATH,
    GuidanceConflictError,
    merge_guidance_inbox,
    parse_guidance_inbox,
    submit_guidance,
    validate_guidance_inbox,
)
from rightmemory.install_core import Installer, MEMORY_GITIGNORE
from rightmemory.sync import SyncManager
from rightmemory.tools import MemoryTools
from tests.isolated_write_test_base import IsolatedWriteTestBase
from tests.sync_test_base import SyncTestBase


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

    def test_validation_rejects_content_before_first_entry(self):
        malformed = "# Pending Agent Guidance\n\nOrphan evidence.\n"
        errors = validate_guidance_inbox(malformed)
        self.assertTrue(any("before first guidance entry" in error for error in errors))

    def test_parser_ignores_reserved_heading_inside_fence(self):
        fenced = SAMPLE_ONE.replace(
            "The agent over-structured a simple prompt change.",
            "```markdown\n## GI-20260815-deadbeef\n```\n\n"
            "The agent over-structured a simple prompt change.",
        )
        entries = parse_guidance_inbox(fenced)
        self.assertEqual(len(entries), 1)
        self.assertIn("## GI-20260815-deadbeef", entries[0].body)

    def test_validation_rejects_unclosed_fenced_body(self):
        malformed = SAMPLE_ONE.replace(
            "The agent over-structured a simple prompt change.",
            "```markdown\nThe agent over-structured a simple prompt change.",
        )
        errors = validate_guidance_inbox(malformed)
        self.assertTrue(any("unclosed fenced code block" in error for error in errors))

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
        self.assertNotIn("rightmemory-guidance-", self._git("branch", "--list"))

        marker = "UNREVIEWED-GUIDANCE-MARKER"
        inbox.write_text(inbox.read_text(encoding="utf-8") + marker + "\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="update")
        self.assertNotIn(marker, tools.git_diff())
        self.assertNotIn(GUIDANCE_INBOX_PATH, tools.git_status())
        self._git("checkout", "--", GUIDANCE_INBOX_PATH)
        self._assert_isolated_cleanup()

    @unittest.skipIf(os.name == "nt", "symlink semantics require POSIX")
    def test_submit_rejects_broken_symlink_inbox(self):
        inbox = self.root / GUIDANCE_INBOX_PATH
        target = self.root / "missing-guidance-target"
        inbox.symlink_to(target)
        self._git("add", "-f", GUIDANCE_INBOX_PATH)
        self._git("commit", "-m", "seed broken guidance symlink")

        with self.assertRaises(ValueError):
            submit_guidance(self.root, "session-one", "Evidence.")

        self.assertFalse(target.exists())
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

    def test_model_role_file_tools_cannot_see_inbox(self):
        for role in (
            None,
            "retrieve",
            "update",
            "dreamer",
            "pruner",
            "reviewer",
            "sync-reconciler",
            "insight",
            "shared-view-builder",
        ):
            with self.subTest(role=role):
                tools = MemoryTools(self.root, role=role)
                self.assertNotIn(GUIDANCE_INBOX_PATH, tools.list_files("*.md"))
                with self.assertRaises(ValueError):
                    tools.read(GUIDANCE_INBOX_PATH)
                with self.assertRaises(ValueError):
                    tools.read_command(f"cat {GUIDANCE_INBOX_PATH}")
                self.assertNotIn(GUIDANCE_INBOX_PATH, tools.read_command("rg --files"))

    def test_validation_boundary_keeps_inbox_out_of_semantic_graph(self):
        tools = MemoryTools(self.root)
        self.assertTrue(tools.validate_memory().startswith("validation passed:"))
        manifest_files = {
            path.relative_to(self.root).as_posix()
            for path in tools._memory_files()
        }
        self.assertNotIn(GUIDANCE_INBOX_PATH, manifest_files)
        self.assertEqual(_guidance_validation_errors(self.root), [])

        (self.root / GUIDANCE_INBOX_PATH).write_text("# Wrong Heading\n", encoding="utf-8")
        self.assertTrue(tools.validate_memory().startswith("validation passed:"))
        self.assertTrue(_guidance_validation_errors(self.root))


class GuidanceSyncTests(SyncTestBase):
    def test_background_sync_does_not_model_repair_dirty_guidance(self):
        inbox = self.device / GUIDANCE_INBOX_PATH
        inbox.write_text(SAMPLE_ONE, encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "seed local guidance")
        inbox.write_text(
            inbox.read_text(encoding="utf-8").replace(
                "keep the judgment direct", "keep the judgment concise"
            ),
            encoding="utf-8",
        )
        repairs = []

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True)
        ).background_sync(active_repair=repairs.append)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [GUIDANCE_INBOX_PATH])
        self.assertEqual(repairs, [])

    def test_background_sync_does_not_model_repair_invalid_guidance(self):
        (self.device / GUIDANCE_INBOX_PATH).write_text(
            "# Wrong Heading\n", encoding="utf-8"
        )
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "malformed local guidance")
        repairs = []

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True)
        ).background_sync(active_repair=repairs.append)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [GUIDANCE_INBOX_PATH])
        self.assertEqual(repairs, [])

    def test_sync_unions_independent_guidance_additions(self):
        (self.device / GUIDANCE_INBOX_PATH).write_text(SAMPLE_ONE, encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "local guidance")

        (self.other / GUIDANCE_INBOX_PATH).write_text(SAMPLE_TWO, encoding="utf-8")
        self._git(self.other, "add", GUIDANCE_INBOX_PATH)
        self._git(self.other, "commit", "-m", "remote guidance")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        entries = parse_guidance_inbox(
            (self.device / GUIDANCE_INBOX_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(
            {entry.entry_id for entry in entries},
            {"GI-20260815-a1b2c3d4", "GI-20260815-e5f6a7b8"},
        )
        self._assert_no_sync_candidates()

    def test_sync_preserves_guidance_deletion_against_unchanged_remote(self):
        (self.device / GUIDANCE_INBOX_PATH).write_text(SAMPLE_ONE, encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "seed guidance")
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")

        (self.device / GUIDANCE_INBOX_PATH).write_text("# Pending Agent Guidance\n", encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "review guidance")

        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote fact → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertEqual(
            parse_guidance_inbox(
                (self.device / GUIDANCE_INBOX_PATH).read_text(encoding="utf-8")
            ),
            [],
        )

    def test_sync_rejects_incompatible_edits_to_same_guidance_id(self):
        (self.device / GUIDANCE_INBOX_PATH).write_text(SAMPLE_ONE, encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "seed guidance")
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")

        local = SAMPLE_ONE.replace("keep the judgment direct", "keep the judgment concise")
        remote = SAMPLE_ONE.replace("keep the judgment direct", "always add a schema")
        (self.device / GUIDANCE_INBOX_PATH).write_text(local, encoding="utf-8")
        self._git(self.device, "add", GUIDANCE_INBOX_PATH)
        self._git(self.device, "commit", "-m", "local guidance edit")
        local_head = self._git(self.device, "rev-parse", "HEAD")

        (self.other / GUIDANCE_INBOX_PATH).write_text(remote, encoding="utf-8")
        self._git(self.other, "add", GUIDANCE_INBOX_PATH)
        self._git(self.other, "commit", "-m", "remote guidance edit")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, [GUIDANCE_INBOX_PATH])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), local_head)
        self.assertEqual(
            (self.device / GUIDANCE_INBOX_PATH).read_text(encoding="utf-8"),
            local,
        )
        self._assert_no_sync_candidates()


class GuidanceInstallTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.memory_root = self.root / "memory"
        self.skills_root = self.root / "skills"
        self.repo_root = Path(__file__).resolve().parents[1]
        self.installer = Installer(
            self.repo_root,
            "standalone",
            self.memory_root,
            [self.skills_root],
        )

    def test_new_root_allows_but_does_not_precreate_guidance_inbox(self):
        self.installer._bootstrap_state()

        self.assertIn("!AGENT_GUIDANCE_INBOX.md", MEMORY_GITIGNORE)
        self.assertFalse((self.memory_root / GUIDANCE_INBOX_PATH).exists())

    def test_existing_optional_inbox_is_included_in_initial_baseline(self):
        self.installer._bootstrap_state()
        (self.memory_root / GUIDANCE_INBOX_PATH).write_text(SAMPLE_ONE, encoding="utf-8")

        self.assertIn(GUIDANCE_INBOX_PATH, self.installer._initial_memory_files())

    def test_installer_keeps_guidance_review_independent_from_orchestration(self):
        self.installer._install_skills()

        self.assertTrue(
            (self.skills_root / "review-agent-guidance-inbox" / "SKILL.md").is_file()
        )
        self.assertTrue(
            (self.skills_root / "rightmemory-auto-orchestrator" / "SKILL.md").is_file()
        )

    def test_runtime_wrapper_dispatches_through_entrypoint(self):
        self.installer.is_windows = False
        self.installer.runtime_python = Path("/tmp/rightmemory-python")
        self.installer.runtime_command = self.root / "rightmemory"

        self.installer._write_runtime_wrapper()

        wrapper = self.installer.runtime_command.read_text(encoding="utf-8")
        self.assertIn("-m rightmemory.entrypoint", wrapper)


if __name__ == "__main__":
    unittest.main()
