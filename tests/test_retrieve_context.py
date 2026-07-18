import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    current_memory_head,
    format_memory_diff_block,
    format_recent_submitted_context_block,
    load_daily_snapshot,
    memory_diff_since,
    root_memory_paths,
)
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry


class RetrieveContextSnapshotTests(unittest.TestCase):
    def test_daily_snapshot_renders_both_roots_without_linked_or_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nroot body\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits {#pursuits}\n\nlive body\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("## Detail {#detail}\n", encoding="utf-8")
            (root / "PURSUIT_detail.md").write_text("## Pursuit Detail {#p-detail}\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("# Skill Body\n", encoding="utf-8")
            (root / ".runtime" / "shared_views" / "imports" / "mf-one").mkdir(parents=True)
            (root / ".runtime" / "shared_views" / "imports" / "mf-one" / "MEMORY.md").write_text(
                "external\n",
                encoding="utf-8",
            )

            snapshot = load_daily_snapshot(root, now=datetime(2026, 6, 29, tzinfo=UTC))

        self.assertEqual(snapshot.day, "2026-06-29")
        self.assertEqual(snapshot.scope, "rightmemory-roots-v2")
        self.assertEqual(snapshot.paths, ["MEMORY.md", "PURSUITS.md"])
        self.assertTrue(snapshot.text.startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("===== MEMORY.md =====", snapshot.text)
        self.assertIn("===== PURSUITS.md =====", snapshot.text)
        self.assertIn("# Root {#root}", snapshot.text)
        self.assertIn("# Pursuits {#pursuits}", snapshot.text)
        self.assertNotIn("MEMORY_detail.md", snapshot.text)
        self.assertNotIn("PURSUIT_detail.md", snapshot.text)
        self.assertNotIn("MEMORY_SKILL_demo.md", snapshot.text)
        self.assertNotIn(".runtime/shared_views/imports", snapshot.text)
        self.assertNotIn("2026-06-29", snapshot.text)

    def test_daily_snapshot_reuses_same_day_text_even_when_memory_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nfirst\n", encoding="utf-8")

            first = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))
            (root / "MEMORY.md").write_text("# Root {#root}\n\nsecond\n", encoding="utf-8")
            second = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))

        self.assertEqual(first.text, second.text)
        self.assertIn("first", second.text)
        self.assertNotIn("second", second.text)

    def test_root_memory_paths_returns_only_document_roots(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(root_memory_paths(root), ["MEMORY.md", "PURSUITS.md"])


class RetrieveContextDiffTests(unittest.TestCase):
    def test_memory_diff_since_returns_both_root_diffs_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Root\n\nfirst\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n\nactive\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("old detail\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("old skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "PURSUITS.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "initial memory")
            base = current_memory_head(root)

            (root / "MEMORY.md").write_text("# Root\n\nsecond\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n\nblocked\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("new detail\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("new skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "PURSUITS.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "update memory")
            head = current_memory_head(root)

            diff = memory_diff_since(root, base, head)

        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", diff)
        self.assertIn("diff --git a/PURSUITS.md b/PURSUITS.md", diff)
        self.assertIn("+second", diff)
        self.assertIn("+blocked", diff)
        self.assertNotIn("MEMORY_detail.md", diff)
        self.assertNotIn("MEMORY_SKILL_demo.md", diff)

    def test_format_memory_diff_block_omits_empty_diff(self):
        self.assertEqual(format_memory_diff_block(""), "")
        block = format_memory_diff_block("diff --git a/MEMORY.md b/MEMORY.md\n")
        self.assertIn("Apply this patch mentally", block)
        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", block)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


class RetrieveContextRequestTests(unittest.TestCase):
    def test_request_text_places_snapshot_first_and_query_last(self):
        text = build_retrieve_request_text(
            snapshot_text="Daily RightMemory root snapshot\n===== MEMORY.md =====\n# Root\n",
            turns=[("find alpha", "alpha answer")],
            diff_block="# RightMemory root changes since previous retrieve turn\n\n```diff\n+beta\n```",
            recent_block="Recent submitted RightMemory candidates\n\nremember gamma",
            query="find gamma",
        )

        self.assertTrue(text.startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("# Prior retrieve conversation\n\nUser: find alpha\nAssistant: alpha answer", text)
        self.assertIn("# RightMemory root changes since previous retrieve turn", text)
        self.assertIn("Recent submitted RightMemory candidates", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind gamma"))

    def test_request_text_omits_empty_diff_and_recent_blocks(self):
        text = build_retrieve_request_text(
            snapshot_text="Daily RightMemory root snapshot\n===== MEMORY.md =====\n# Root\n",
            turns=[],
            diff_block="",
            recent_block="",
            query="find root",
        )

        self.assertNotIn("RightMemory root changes since previous retrieve turn", text)
        self.assertNotIn("Recent submitted RightMemory candidates", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind root"))

    def test_recent_submitted_context_block_omits_empty_entries(self):
        self.assertEqual(format_recent_submitted_context_block([]), "")
        block = format_recent_submitted_context_block(
            [
                RecentSubmittedMemoryEntry(
                    update_session_id="update-a",
                    candidate_id=1,
                    submitted_at="2026-06-18T00:00:00+00:00",
                    message="remember delta",
                )
            ]
        )
        self.assertTrue(block.startswith("# Recent submitted RightMemory candidates"))
        self.assertIn("remember delta", block)

    def test_retrieve_context_store_persists_turns_and_commit_cursor(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = RetrieveContextStore(root)

            state = store.load("retrieve-a")
            self.assertEqual(state.turns, [])
            self.assertIsNone(state.delivered_memory_commit)

            store.record_success("retrieve-a", query="find alpha", answer="alpha answer", memory_commit="abc123")
            state = store.load("retrieve-a")

        self.assertEqual(state.delivered_memory_commit, "abc123")
        self.assertEqual([(turn.query, turn.answer) for turn in state.turns], [("find alpha", "alpha answer")])
