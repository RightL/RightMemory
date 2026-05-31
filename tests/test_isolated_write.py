import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.isolated_write import (
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
)


class IsolatedWriteSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` initial memory\n",
            encoding="utf-8",
        )
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        self.initial_head = self._git("rev-parse", "HEAD")

    def test_committed_temp_change_lands_as_ordinary_commit(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` isolated memory\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: isolated update", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(result.output, "updated")
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "memory: isolated update")
        self.assertEqual(self._git("log", "--merges", "--oneline"), "")
        self.assertEqual(self._git("status", "--short"), "")
        self.assertIn("two", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self._assert_isolated_cleanup()

    def test_failed_callback_after_temp_commit_does_not_land(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` failed isolated memory\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: failed isolated update", cwd=worktree)
            raise RuntimeError("agent failed")

        with self.assertRaisesRegex(RuntimeError, "agent failed"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "initial memory")
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("failed isolated", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self._assert_isolated_cleanup()

    def test_non_memory_temp_commit_does_not_land(self):
        def callback(worktree: Path) -> None:
            (worktree / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")
            self._git("add", "rightmemory.toml", cwd=worktree)
            self._git("commit", "-m", "memory: bad config update", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: rightmemory\\.toml"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "rightmemory.toml").exists())
        self._assert_isolated_cleanup()

    def test_deleted_memory_md_does_not_land(self):
        def callback(worktree: Path) -> None:
            self._git("rm", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: delete root memory", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY\\.md.*regular file"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertTrue((self.root / "MEMORY.md").is_file())
        self._assert_isolated_cleanup()

    def test_intermediate_deleted_memory_md_does_not_land(self):
        def callback(worktree: Path) -> None:
            original = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            self._git("rm", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: delete root memory", cwd=worktree)
            (worktree / "MEMORY.md").write_text(original + "- `two` restored memory\n", encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: restore root memory", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY\\.md.*regular file"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("restored memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self._assert_isolated_cleanup()

    def test_allowed_memory_symlink_does_not_land(self):
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to("MEMORY.md")
        except OSError as exc:
            self.skipTest(f"symlink creation is not available: {exc}")
        finally:
            probe.unlink(missing_ok=True)

        def callback(worktree: Path) -> None:
            (worktree / "MEMORY_secret.md").symlink_to("MEMORY.md")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: add symlink detail", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY_secret\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "MEMORY_secret.md").exists())
        self._assert_isolated_cleanup()

    def test_intermediate_allowed_memory_symlink_does_not_land(self):
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to("MEMORY.md")
        except OSError as exc:
            self.skipTest(f"symlink creation is not available: {exc}")
        finally:
            probe.unlink(missing_ok=True)

        def callback(worktree: Path) -> None:
            secret = worktree / "MEMORY_secret.md"
            secret.symlink_to("MEMORY.md")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: add symlink detail", cwd=worktree)
            secret.unlink()
            secret.write_text("# Secret\n\n- `secret` restored regular memory\n", encoding="utf-8")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: replace symlink detail", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY_secret\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "MEMORY_secret.md").exists())
        self._assert_isolated_cleanup()

    def test_successful_noop_returns_output_and_lands_zero_commits(self):
        output = {"status": "noop"}

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(lambda _worktree: output)

        self.assertIs(result.output, output)
        self.assertEqual(result.commits_landed, 0)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_empty_prune_checkpoint_lands(self):
        def callback(worktree: Path) -> str:
            self._git(
                "commit",
                "--allow-empty",
                "-m",
                "prune: checkpoint",
                "-m",
                "Boundary: HEAD\n\nRemoved:\n(none)",
                cwd=worktree,
            )
            return "checkpoint"

        result = IsolatedWriteSupervisor(self.root, "pruner").run(callback)

        self.assertEqual(result.output, "checkpoint")
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_empty_non_prune_commit_does_not_land(self):
        def callback(worktree: Path) -> None:
            self._git("commit", "--allow-empty", "-m", "memory: empty noop", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "empty commits are limited"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_noop_rechecks_main_head_before_accepting(self):
        def callback(_worktree: Path) -> str:
            self._append_memory(self.root, "- `main-change` outside main memory\n")
            self._git("add", "MEMORY.md")
            self._git("commit", "-m", "memory: outside main update")
            return "noop"

        with self.assertRaisesRegex(RuntimeError, "main HEAD changed"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("log", "-1", "--format=%s"), "memory: outside main update")
        self.assertEqual(self._git("status", "--short"), "")
        text = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("main-change", text)
        self._assert_isolated_cleanup()

    def test_noop_rechecks_dirty_main_memory_before_accepting(self):
        def callback(_worktree: Path) -> str:
            self._append_memory(self.root, "- `main-dirty` outside main memory\n")
            return "noop"

        with self.assertRaises(MainMemoryDirtyError) as caught:
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(caught.exception.paths, ("MEMORY.md",))
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short", "--", "MEMORY.md"), "M MEMORY.md")
        self.assertIn("main-dirty", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self._assert_isolated_cleanup()

    def test_dirty_main_memory_file_blocks_before_callback(self):
        called = False
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` dirty memory\n",
            encoding="utf-8",
        )

        def callback(_worktree: Path) -> None:
            nonlocal called
            called = True

        with self.assertRaises(MainMemoryDirtyError):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertFalse(called)
        self.assertEqual(self._git("status", "--short", "--", "MEMORY.md"), "M MEMORY.md")

    def test_untracked_insight_log_blocks_insight_before_callback(self):
        called = False
        insight = self.root / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n", encoding="utf-8")

        def callback(_worktree: Path) -> None:
            nonlocal called
            called = True

        with self.assertRaises(MainMemoryDirtyError) as caught:
            IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertEqual(caught.exception.paths, ("insight_logs/2026-05-30-143012.md",))
        self.assertFalse(called)

    def test_dirty_active_memory_blocks_insight_before_callback(self):
        called = False
        self._append_memory(self.root, "- `two` uncommitted active memory\n")

        def callback(_worktree: Path) -> None:
            nonlocal called
            called = True

        with self.assertRaises(MainMemoryDirtyError) as caught:
            IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertEqual(caught.exception.paths, ("MEMORY.md",))
        self.assertFalse(called)

    def test_untracked_main_dream_log_does_not_block_dreamer(self):
        called = False
        dream_log = self.root / "dream_logs" / "2026-05-22.md"
        dream_log.parent.mkdir()
        dream_log.write_text("# Dream\n", encoding="utf-8")

        def callback(_worktree: Path) -> None:
            nonlocal called
            called = True

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertIsNone(result.output)
        self.assertTrue(called)

    def test_insight_commit_lands_insight_log(self):
        def callback(worktree: Path) -> str:
            insight = worktree / "insight_logs" / "2026-05-30-143012.md"
            insight.parent.mkdir()
            insight.write_text("# Insight\n\nUseful reflection.\n", encoding="utf-8")
            self._git("add", "insight_logs/2026-05-30-143012.md", cwd=worktree)
            self._git("commit", "-m", "insight: reflect on memory shape", cwd=worktree)
            return "insight"

        result = IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertEqual(result.output, "insight")
        self.assertTrue((self.root / "insight_logs" / "2026-05-30-143012.md").is_file())
        self.assertEqual(self._git("log", "-1", "--format=%s"), "insight: reflect on memory shape")

    def test_insight_commit_lands_when_active_memory_is_invalid(self):
        self._append_memory(self.root, "- `one` duplicate active memory\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: preexisting invalid active memory")

        def callback(worktree: Path) -> str:
            insight = worktree / "insight_logs" / "2026-05-30-143012.md"
            insight.parent.mkdir()
            insight.write_text("# Insight\n\nUseful reflection.\n", encoding="utf-8")
            self._git("add", "insight_logs/2026-05-30-143012.md", cwd=worktree)
            self._git("commit", "-m", "insight: reflect on memory shape", cwd=worktree)
            return "insight"

        result = IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertEqual(result.output, "insight")
        self.assertTrue((self.root / "insight_logs" / "2026-05-30-143012.md").is_file())
        self.assertEqual(self._git("log", "-1", "--format=%s"), "insight: reflect on memory shape")

    def test_insight_commit_rejects_memory_edit(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` invalid insight memory edit\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "insight: invalid memory edit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-insight paths: MEMORY\.md"):
            IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertNotIn("invalid insight", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_uncommitted_temp_change_does_not_land(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` uncommitted isolated memory\n")
            return "dirty"

        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("uncommitted isolated", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_main_head_change_before_landing_preserves_main_commit(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` isolated memory\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: isolated update", cwd=worktree)
            self._append_memory(self.root, "- `main-change` outside main memory\n")
            self._git("add", "MEMORY.md")
            self._git("commit", "-m", "memory: outside main update")

        with self.assertRaisesRegex(RuntimeError, "main HEAD changed"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("log", "-1", "--format=%s"), "memory: outside main update")
        self.assertEqual(self._git("status", "--short"), "")
        text = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("main-change", text)
        self.assertNotIn("isolated memory", text)

    def test_validation_failure_does_not_land_temp_commit(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `one` duplicate memory\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: invalid isolated update", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual(self._git("log", "-1", "--format=%s"), "initial memory")

    def test_cleanup_stale_removes_temp_branch_and_worktree(self):
        branch, worktree = self._add_isolated_worktree("dreamer", "0123456789abcdef0123456789abcdef")

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(worktree.exists())
        self.assertEqual(self._git("branch", "--list", branch), "")
        self._assert_isolated_cleanup()

    def test_cleanup_stale_preserves_other_role_temp_branch_and_worktree(self):
        dreamer_branch, dreamer_worktree = self._add_isolated_worktree("dreamer", "0123456789abcdef0123456789abcdef")
        update_branch, update_worktree = self._add_isolated_worktree("update", "11111111111111111111111111111111")

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(dreamer_worktree.exists())
        self.assertEqual(self._git("branch", "--list", dreamer_branch), "")
        self.assertTrue(update_worktree.exists())
        self.assertIn(update_branch, self._git("branch", "--list", update_branch))

    def test_cleanup_stale_for_reviewer_preserves_dreamer_and_update_temp_artifacts(self):
        reviewer_branch, reviewer_worktree = self._add_isolated_worktree("reviewer", "22222222222222222222222222222222")
        dreamer_branch, dreamer_worktree = self._add_isolated_worktree("dreamer", "33333333333333333333333333333333")
        update_branch, update_worktree = self._add_isolated_worktree("update", "44444444444444444444444444444444")

        IsolatedWriteSupervisor(self.root, "reviewer").cleanup_stale()

        self.assertFalse(reviewer_worktree.exists())
        self.assertEqual(self._git("branch", "--list", reviewer_branch), "")
        self.assertTrue(dreamer_worktree.exists())
        self.assertTrue(update_worktree.exists())
        self.assertIn(dreamer_branch, self._git("branch", "--list", dreamer_branch))
        self.assertIn(update_branch, self._git("branch", "--list", update_branch))

    def test_cleanup_stale_preserves_unrelated_branch_and_worktree(self):
        branch = "keep-me"
        prefixed_branch = "rightmemory-isolated-user-not-temp"
        worktree = self.root / ".runtime" / "worktrees" / "keep-me"
        self._git("branch", prefixed_branch)
        self._git("worktree", "add", "-b", branch, str(worktree), self.initial_head)

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertTrue(worktree.exists())
        self.assertIn(branch, self._git("branch", "--list", branch))
        self.assertIn(prefixed_branch, self._git("branch", "--list", prefixed_branch))

    def _add_isolated_worktree(self, role: str, identifier: str) -> tuple[str, Path]:
        branch = f"rightmemory-isolated-{role}-{identifier}"
        worktree = self.root / ".runtime" / "worktrees" / f"{role}-{identifier}"
        self._git("worktree", "add", "-b", branch, str(worktree), self.initial_head)
        return branch, worktree

    def _append_memory(self, root: Path, text: str) -> None:
        memory = root / "MEMORY.md"
        memory.write_text(memory.read_text(encoding="utf-8") + text, encoding="utf-8")

    def _assert_isolated_cleanup(self) -> None:
        worktrees = self._git("worktree", "list", "--porcelain")
        branches = self._git("branch", "--list", "rightmemory-isolated-*")
        self.assertNotIn(".runtime/worktrees/", worktrees)
        self.assertEqual(branches, "")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
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
