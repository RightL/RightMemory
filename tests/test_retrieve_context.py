import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.retrieve_context import (
    active_memory_paths,
    current_memory_head,
    format_memory_diff_block,
    load_daily_snapshot,
    memory_diff_since,
)


class RetrieveContextSnapshotTests(unittest.TestCase):
    def test_daily_snapshot_renders_active_memory_without_skill_or_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nroot body\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("## Detail {#detail}\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("# Skill Body\n", encoding="utf-8")
            (root / ".runtime" / "shared_views" / "imports" / "mf-one").mkdir(parents=True)
            (root / ".runtime" / "shared_views" / "imports" / "mf-one" / "MEMORY.md").write_text(
                "external\n",
                encoding="utf-8",
            )

            snapshot = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))

        self.assertEqual(snapshot.day, "2026-06-18")
        self.assertIn("===== MEMORY.md =====", snapshot.text)
        self.assertIn("# Root {#root}", snapshot.text)
        self.assertIn("===== MEMORY_detail.md =====", snapshot.text)
        self.assertNotIn("MEMORY_SKILL_demo.md", snapshot.text)
        self.assertNotIn(".runtime/shared_views/imports", snapshot.text)
        self.assertNotIn("2026-06-18", snapshot.text)

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

    def test_active_memory_paths_excludes_memory_skill_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(active_memory_paths(root), ["MEMORY.md", "MEMORY_alpha.md"])


class RetrieveContextDiffTests(unittest.TestCase):
    def test_memory_diff_since_returns_active_memory_diff_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Root\n\nfirst\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("old skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "initial memory")
            base = current_memory_head(root)

            (root / "MEMORY.md").write_text("# Root\n\nsecond\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("new skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "update memory")
            head = current_memory_head(root)

            diff = memory_diff_since(root, base, head)

        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", diff)
        self.assertIn("+second", diff)
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
