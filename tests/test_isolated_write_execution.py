import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.isolated_write import (
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
)
from tests.isolated_write_test_base import IsolatedWriteTestBase


class IsolatedWriteExecutionTests(IsolatedWriteTestBase):
    def test_committed_temp_change_lands_as_ordinary_commit(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` isolated memory → []\n")
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
            self._append_memory(worktree, "- `two` failed isolated memory → []\n")
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

    def test_completed_no_change_operation_does_not_run_callback_again(self):
        supervisor = IsolatedWriteSupervisor(self.root, "dreamer")
        first = supervisor.run(
            lambda _worktree: "nothing to change",
            operation_id="dreamer-no-change-1",
            operation_input={"message": "dream"},
        )
        second = supervisor.run(
            lambda _worktree: self.fail("completed operation must not rerun"),
            operation_id="dreamer-no-change-1",
            operation_input={"message": "dream"},
        )

        self.assertEqual(first.commits_landed, 0)
        self.assertEqual(second.output, "nothing to change")
        self.assertEqual(second.commits_landed, 0)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_semantic_operation_squashes_multiple_model_commits(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` first step → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: first step", cwd=worktree)
            self._append_memory(worktree, "- `three` second step → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: second step", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(
            callback,
            operation_id="dreamer-squash-1",
            operation_input={"message": "dream"},
        )

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertIn("first step", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertIn("second step", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_successful_noop_returns_output_and_lands_zero_commits(self):
        output = {"status": "noop"}

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(lambda _worktree: output)

        self.assertIs(result.output, output)
        self.assertEqual(result.commits_landed, 0)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
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

    def test_main_head_change_before_landing_preserves_main_commit(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` isolated memory → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: isolated update", cwd=worktree)
            self._append_memory(self.root, "- `main-change` outside main memory → []\n")
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
            self._append_memory(worktree, "- `one` duplicate memory → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: invalid isolated update", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual(self._git("log", "-1", "--format=%s"), "initial memory")

    def test_noop_rechecks_main_head_before_accepting(self):
        def callback(_worktree: Path) -> str:
            self._append_memory(self.root, "- `main-change` outside main memory → []\n")
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
            self._append_memory(self.root, "- `main-dirty` outside main memory → []\n")
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
            "- `one` dirty memory → []\n",
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
        self._append_memory(self.root, "- `two` uncommitted active memory → []\n")

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

    def test_uncommitted_temp_change_does_not_land(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` uncommitted isolated memory → []\n")
            return "dirty"

        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("uncommitted isolated", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_cleanup_stale_removes_temp_branch_and_worktree(self):
        branch, worktree = self._add_isolated_worktree("dreamer", "0123456789abcdef0123456789abcdef")

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(worktree.exists())
        self.assertEqual(self._git("branch", "--list", branch), "")
        self._assert_isolated_cleanup()

    def test_cleanup_removes_empty_directory_left_by_git(self):
        branch, worktree = self._add_isolated_worktree(
            "dreamer", "0123456789abcdef0123456789abcdef"
        )
        supervisor = IsolatedWriteSupervisor(self.root, "dreamer")
        run_git = supervisor._run_git

        def leave_empty_directory(cwd: Path, *args: str, **kwargs):
            result = run_git(cwd, *args, **kwargs)
            if args[:3] == ("worktree", "remove", "--force"):
                worktree.mkdir(parents=True, exist_ok=True)
            return result

        with patch.object(supervisor, "_run_git", side_effect=leave_empty_directory):
            supervisor._cleanup(worktree, branch)

        self.assertFalse(worktree.exists())
        self._assert_isolated_cleanup()

    def test_cleanup_stale_removes_empty_orphaned_worktree_directory(self):
        worktree = (
            self.root
            / ".runtime"
            / "worktrees"
            / "dreamer-0123456789abcdef0123456789abcdef"
        )
        worktree.mkdir(parents=True)

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(worktree.exists())

    def test_cleanup_stale_preserves_nonempty_orphaned_worktree_directory(self):
        worktree = (
            self.root
            / ".runtime"
            / "worktrees"
            / "dreamer-0123456789abcdef0123456789abcdef"
        )
        worktree.mkdir(parents=True)
        (worktree / "preserve.txt").write_text("preserve\n", encoding="utf-8")

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "preserve.txt").is_file())

    def test_cleanup_stale_preserves_live_same_role_run(self):
        observed_lease = None

        def callback(worktree: Path) -> str:
            nonlocal observed_lease
            branch = self._git("branch", "--show-current", cwd=worktree)
            leases = list((self.root / ".runtime" / "worktree-leases").glob("dreamer-*.json"))
            self.assertEqual(len(leases), 1)
            observed_lease = leases[0]

            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

            self.assertTrue(worktree.exists())
            self.assertIn(branch, self._git("branch", "--list", branch))
            self.assertTrue(observed_lease.exists())
            return "no memory change"

        with patch("rightmemory.isolated_write.process_identity", return_value="proc:live"):
            result = IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(result.commits_landed, 0)
        self.assertIsNotNone(observed_lease)
        self.assertFalse(observed_lease.exists())
        self._assert_isolated_cleanup()

    def test_cleanup_stale_removes_reused_pid_lease(self):
        identifier = "55555555555555555555555555555555"
        branch, worktree = self._add_isolated_worktree("dreamer", identifier)
        lease = self._write_lease("dreamer", identifier, pid=1234, identity="proc:old")

        with (
            patch("rightmemory.isolated_write.process_identity", return_value="proc:new"),
            patch("rightmemory.isolated_write.process_exists", return_value=True),
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(worktree.exists())
        self.assertEqual(self._git("branch", "--list", branch), "")
        self.assertFalse(lease.exists())

    def test_cleanup_stale_preserves_owner_when_identity_is_temporarily_unavailable(self):
        identifier = "66666666666666666666666666666666"
        branch, worktree = self._add_isolated_worktree("dreamer", identifier)
        lease = self._write_lease("dreamer", identifier, pid=1234, identity="proc:owner")

        with (
            patch("rightmemory.isolated_write.process_identity", return_value=None),
            patch("rightmemory.isolated_write.process_exists", return_value=True),
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertTrue(worktree.exists())
        self.assertIn(branch, self._git("branch", "--list", branch))
        self.assertTrue(lease.exists())

        with (
            patch("rightmemory.isolated_write.process_identity", return_value=None),
            patch("rightmemory.isolated_write.process_exists", return_value=False),
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()
        self._assert_isolated_cleanup()

    def test_cleanup_stale_preserves_live_branch_before_worktree_registration(self):
        identifier = "77777777777777777777777777777777"
        branch = f"rightmemory-isolated-dreamer-{identifier}"
        self._git("branch", branch)
        lease = self._write_lease("dreamer", identifier, pid=1234, identity="proc:live")

        with patch("rightmemory.isolated_write.process_identity", return_value="proc:live"):
            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertIn(branch, self._git("branch", "--list", branch))
        self.assertTrue(lease.exists())

        with patch("rightmemory.isolated_write.process_identity", return_value="proc:gone"):
            IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()
        self._assert_isolated_cleanup()

    def test_run_removes_lease_after_success(self):
        observed = []

        def callback(_worktree: Path) -> str:
            leases = list((self.root / ".runtime" / "worktree-leases").glob("dreamer-*.json"))
            self.assertEqual(len(leases), 1)
            payload = json.loads(leases[0].read_text(encoding="utf-8"))
            self.assertEqual(payload, {"pid": os.getpid(), "process_identity": "proc:owner"})
            observed.extend(leases)
            return "no memory change"

        with patch("rightmemory.isolated_write.process_identity", return_value="proc:owner"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self._assert_isolated_cleanup()

    def test_run_removes_lease_after_failure(self):
        observed = []

        def callback(_worktree: Path) -> str:
            observed.extend((self.root / ".runtime" / "worktree-leases").glob("dreamer-*.json"))
            raise RuntimeError("agent failed")

        with (
            patch("rightmemory.isolated_write.process_identity", return_value="proc:owner"),
            self.assertRaisesRegex(RuntimeError, "agent failed"),
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
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


if __name__ == "__main__":
    unittest.main()
