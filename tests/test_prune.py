import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.config import PrunerConfig
from rightmemory.prune import build_pruner_message, parse_prune_ledger, prune_due_status


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")

    def test_prune_not_due_without_previous_prune(self):
        self._commit_memory("one", "memory: one")
        self._commit_memory("two", "memory: two")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertFalse(status.due)
        self.assertEqual(status.commits_since_boundary, 2)
        self.assertIn("2/3 commits", status.message)

    def test_first_prune_due_uses_oldest_available_boundary(self):
        self._commit_memory("one", "memory: one")
        self._commit_memory("two", "memory: two")
        self._commit_memory("three", "memory: three")
        root_commit = self._git("rev-list", "--max-parents=0", "HEAD")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertTrue(status.due)
        self.assertEqual(status.boundary_commit, root_commit)
        self.assertEqual(status.latest_prune_commit, None)
        self.assertEqual(status.commits_since_boundary, 3)

    def test_previous_prune_controls_next_generation_window(self):
        self._commit_memory("one", "memory: one")
        body = (
            "Boundary: abc123\n\n"
            "Removed:\n"
            "- MEMORY.md#old-node | old summary\n\n"
            "Revival grace:\n"
            "- MEMORY.md#revived-node | grace 1/2 | revived summary\n"
        )
        self._git("commit", "--allow-empty", "-m", "prune: expired active memory", "-m", body)
        prune_commit = self._git("rev-parse", "HEAD")
        self._commit_memory("two", "memory: two")

        not_due = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=2))
        self.assertFalse(not_due.due)
        self.assertIn("1/2 commits", not_due.message)

        self._commit_memory("three", "memory: three")
        due = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=2))

        self.assertTrue(due.due)
        self.assertEqual(due.boundary_commit, prune_commit)
        self.assertEqual(due.latest_prune_commit, prune_commit)
        self.assertEqual(due.previous_ledger.removed[0].ref, "MEMORY.md#old-node")
        self.assertEqual(due.previous_ledger.grace[0].used, 1)
        self.assertEqual(due.previous_ledger.grace[0].total, 2)

    def test_prune_detection_ignores_non_prune_subject_with_prune_body(self):
        self._commit_memory("one", "memory: one")
        self._git("commit", "--allow-empty", "-m", "memory: body mentions prune", "-m", "prune: not a checkpoint")
        self._commit_memory("two", "memory: two")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=2))

        self.assertTrue(status.due)
        self.assertIsNone(status.latest_prune_commit)

    def test_first_late_prune_reports_generation_window_count(self):
        for index in range(5):
            self._commit_memory(f"item-{index}", f"memory: {index}")
        expected_boundary = self._git("rev-parse", "HEAD~3")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertTrue(status.due)
        self.assertEqual(status.boundary_commit, expected_boundary)
        self.assertEqual(status.commits_since_boundary, 3)

    def test_parse_prune_ledger_extracts_removed_and_grace_sections(self):
        ledger = parse_prune_ledger(
            "Removed:\n"
            "- MEMORY.md#alpha | alpha summary\n"
            "- MEMORY_detail.md#beta | beta summary\n\n"
            "Revival grace:\n"
            "- MEMORY.md#gamma | grace 2/3 | gamma summary\n\n"
            "Skipped:\n"
            "- MEMORY.md#keep | still active\n"
        )

        self.assertEqual([entry.ref for entry in ledger.removed], ["MEMORY.md#alpha", "MEMORY_detail.md#beta"])
        self.assertEqual(ledger.removed[1].summary, "beta summary")
        self.assertEqual(ledger.grace[0].ref, "MEMORY.md#gamma")
        self.assertEqual(ledger.grace[0].used, 2)
        self.assertEqual(ledger.grace[0].total, 3)
        self.assertEqual(ledger.grace[0].summary, "gamma summary")

    def test_parse_prune_ledger_accepts_backtick_id_shape(self):
        ledger = parse_prune_ledger(
            "Removed:\n"
            "- `old-node` path: # Domain > Topic; old summary\n\n"
            "Revival grace:\n"
            "- `revived-node` grace 1/2; revived from: abc123; path: # Domain\n"
        )

        self.assertEqual(ledger.removed[0].ref, "`old-node`")
        self.assertIn("path: # Domain", ledger.removed[0].summary)
        self.assertEqual(ledger.grace[0].ref, "`revived-node`")
        self.assertEqual(ledger.grace[0].used, 1)
        self.assertEqual(ledger.grace[0].total, 2)

    def test_build_pruner_message_includes_boundary_and_revival_context(self):
        self._commit_memory("one", "memory: one")
        body = "Removed:\n- MEMORY.md#old-node | old summary\n"
        self._git("commit", "--allow-empty", "-m", "prune: expired active memory", "-m", body)
        self._commit_memory("two", "memory: two")
        status = prune_due_status(
            self.root,
            PrunerConfig(memory_root=self.root, generation_commits=1, revival_grace_checkpoints=2),
        )

        message = build_pruner_message(status)

        self.assertIn("Prune generation due.", message)
        self.assertIn(f"Boundary commit: {status.boundary_commit}", message)
        self.assertIn("Revival grace checkpoints: 2", message)
        self.assertIn("MEMORY.md#old-node", message)
        self.assertIn("prune: checkpoint", message)

    def _commit_memory(self, value: str, message: str) -> None:
        (self.root / "MEMORY.md").write_text(f"# Memory\n\n- `{value}` value\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", message)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
