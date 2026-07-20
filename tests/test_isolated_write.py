import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.isolated_write import (
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
)
from rightmemory.semantic_operation import SemanticOperationStore


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
            "- `one` initial memory → []\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "PURSUITS.md")
        self._git("commit", "-m", "initial memory")
        self.initial_head = self._git("rev-parse", "HEAD")

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

    def test_semantic_operation_lands_one_commit_with_receipt_trailer(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` durable operation → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: durable operation", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(
            callback,
            operation_id="dreamer-operation-1",
            operation_input={"message": "dream"},
        )

        receipt = SemanticOperationStore(self.root).read("dreamer-operation-1")
        message = self._git("log", "-1", "--format=%B")
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.operation_id, "dreamer-operation-1")
        self.assertIn("RightMemory-Operation: dreamer-operation-1", message)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.phase, "committed")
        self.assertEqual(receipt.outcome.landed_commit, result.landed_commit)

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

    def test_landed_operation_recovers_when_receipt_finalization_was_interrupted(self):
        calls = []

        def callback(worktree: Path) -> str:
            calls.append("model")
            self._append_memory(worktree, "- `two` recover landed operation → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: recover operation", cwd=worktree)
            return "updated once"

        original_complete = SemanticOperationStore.complete_commit
        failed = False

        def fail_once(store, operation_id, landed_commit):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated receipt interruption")
            return original_complete(store, operation_id, landed_commit)

        with patch.object(SemanticOperationStore, "complete_commit", fail_once):
            with self.assertRaisesRegex(OSError, "receipt interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    callback,
                    operation_id="dreamer-recovery-1",
                    operation_input={"message": "dream"},
                )

        recovered = IsolatedWriteSupervisor(self.root, "dreamer").run(
            lambda _worktree: self.fail("recovery must not rerun the model"),
            operation_id="dreamer-recovery-1",
            operation_input={"message": "dream"},
        )

        self.assertEqual(calls, ["model"])
        self.assertEqual(recovered.output, "updated once")
        self.assertEqual((self.root / "MEMORY.md").read_text(encoding="utf-8").count("recover landed"), 1)
        self.assertEqual(
            self._git("log", "--format=%B").count("RightMemory-Operation: dreamer-recovery-1"),
            1,
        )

    def test_prepared_candidate_remains_recoverable_after_cleanup_and_gc(self):
        calls = []

        def callback(worktree: Path) -> str:
            calls.append("model")
            self._append_memory(worktree, "- `two` pinned candidate → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: pinned candidate", cwd=worktree)
            return "updated once"

        with patch.object(
            IsolatedWriteSupervisor,
            "_land_operation_commit",
            side_effect=OSError("simulated landing interruption"),
        ):
            with self.assertRaisesRegex(OSError, "landing interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    callback,
                    operation_id="dreamer-pinned-candidate-1",
                    operation_input={"message": "dream"},
                )

        self.assertTrue(self._git("for-each-ref", "--format=%(refname)", "refs/rightmemory/operations"))
        self._git("gc", "--prune=now")

        recovered = IsolatedWriteSupervisor(self.root, "dreamer").run(
            lambda _worktree: self.fail("recovery must not rerun the model"),
            operation_id="dreamer-pinned-candidate-1",
            operation_input={"message": "dream"},
        )

        self.assertEqual(calls, ["model"])
        self.assertEqual(recovered.commits_landed, 1)
        self.assertEqual(self._git("for-each-ref", "--format=%(refname)", "refs/rightmemory/operations"), "")

    def test_new_operation_finishes_an_older_prepared_commit_before_running_model(self):
        calls = []

        def first_callback(worktree: Path) -> str:
            calls.append("first")
            self._append_memory(worktree, "- `two` first prepared operation → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: first prepared operation", cwd=worktree)
            return "first result"

        with patch.object(
            IsolatedWriteSupervisor,
            "_land_operation_commit",
            side_effect=OSError("simulated landing interruption"),
        ):
            with self.assertRaisesRegex(OSError, "landing interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    first_callback,
                    operation_id="dreamer-prepared-first",
                    operation_input={"message": "first"},
                )

        def second_callback(worktree: Path) -> str:
            calls.append("second")
            self.assertIn(
                "first prepared operation",
                (worktree / "MEMORY.md").read_text(encoding="utf-8"),
            )
            self._append_memory(worktree, "- `three` second operation → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: second operation", cwd=worktree)
            return "second result"

        second = IsolatedWriteSupervisor(self.root, "dreamer").run(
            second_callback,
            operation_id="dreamer-prepared-second",
            operation_input={"message": "second"},
        )

        first = SemanticOperationStore(self.root).read("dreamer-prepared-first")
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(first.phase, "committed")
        self.assertEqual(second.output, "second result")
        self.assertIn("second operation", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_prepared_no_change_recovery_keeps_its_original_snapshot(self):
        original_complete = SemanticOperationStore.complete_no_change
        failed = False

        def fail_once(store, operation_id, completed_commit=None):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated no-change finalization interruption")
            return original_complete(store, operation_id, completed_commit)

        with patch.object(SemanticOperationStore, "complete_no_change", fail_once):
            with self.assertRaisesRegex(OSError, "finalization interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    lambda _worktree: "nothing changed",
                    operation_id="dreamer-prepared-no-change",
                    operation_input={"message": "dream"},
                )

        (self.root / "direct.txt").write_text("unrelated direct change\n", encoding="utf-8")
        self._git("add", "-f", "direct.txt")
        self._git("commit", "-m", "direct: unrelated change")

        recovered = IsolatedWriteSupervisor(self.root, "dreamer").run(
            lambda _worktree: self.fail("prepared no-change recovery must not rerun the model"),
            operation_id="dreamer-prepared-no-change",
            operation_input={"message": "dream"},
        )

        receipt = SemanticOperationStore(self.root).read("dreamer-prepared-no-change")
        self.assertEqual(recovered.landed_commit, self.initial_head)
        self.assertEqual(receipt.outcome.landed_commit, self.initial_head)
        self.assertNotEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_recovery_recognizes_a_rebased_operation_after_second_crash(self):
        calls = []

        def callback(worktree: Path) -> str:
            calls.append("model")
            self._append_memory(worktree, "- `two` recover rebased operation → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: recover rebased operation", cwd=worktree)
            return "recovered output"

        with patch.object(
            IsolatedWriteSupervisor,
            "_land_operation_commit",
            side_effect=OSError("simulated first landing interruption"),
        ):
            with self.assertRaisesRegex(OSError, "first landing interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    callback,
                    operation_id="dreamer-rebased-recovery",
                    operation_input={"message": "dream"},
                )

        (self.root / "direct.txt").write_text("unrelated direct change\n", encoding="utf-8")
        self._git("add", "-f", "direct.txt")
        self._git("commit", "-m", "direct: unrelated change")

        original_complete = SemanticOperationStore.complete_commit
        failed = False

        def fail_once(store, operation_id, landed_commit):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated receipt interruption")
            return original_complete(store, operation_id, landed_commit)

        with patch.object(SemanticOperationStore, "complete_commit", fail_once):
            with self.assertRaisesRegex(OSError, "receipt interruption"):
                IsolatedWriteSupervisor(self.root, "dreamer").run(
                    lambda _worktree: self.fail("prepared recovery must not rerun the model"),
                    operation_id="dreamer-rebased-recovery",
                    operation_input={"message": "dream"},
                )

        recovered = IsolatedWriteSupervisor(self.root, "dreamer").run(
            lambda _worktree: self.fail("second recovery must not rerun the model"),
            operation_id="dreamer-rebased-recovery",
            operation_input={"message": "dream"},
        )

        receipt = SemanticOperationStore(self.root).read("dreamer-rebased-recovery")
        self.assertEqual(calls, ["model"])
        self.assertEqual(recovered.output, "recovered output")
        self.assertEqual(receipt.phase, "committed")
        self.assertEqual(
            self._git("log", "--format=%B").count("RightMemory-Operation: dreamer-rebased-recovery"),
            1,
        )

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
            (worktree / "MEMORY.md").write_text(original + "- `two` restored memory → []\n", encoding="utf-8")
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
            secret.write_text("# Secret\n\n- `secret` restored regular memory → []\n", encoding="utf-8")
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

    def test_empty_prune_checkpoint_recovers_without_rerunning(self):
        calls = []

        def callback(worktree: Path) -> str:
            calls.append("model")
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

        original_complete = SemanticOperationStore.complete_commit
        failed = False

        def fail_once(store, operation_id, landed_commit):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("simulated receipt interruption")
            return original_complete(store, operation_id, landed_commit)

        with patch.object(SemanticOperationStore, "complete_commit", fail_once):
            with self.assertRaisesRegex(OSError, "receipt interruption"):
                IsolatedWriteSupervisor(self.root, "pruner").run(
                    callback,
                    operation_id="pruner-empty-recovery-1",
                    operation_input={"kind": "prune"},
                )

        recovered = IsolatedWriteSupervisor(self.root, "pruner").run(
            lambda _worktree: self.fail("recovery must not rerun the model"),
            operation_id="pruner-empty-recovery-1",
            operation_input={"kind": "prune"},
        )

        self.assertEqual(calls, ["model"])
        self.assertEqual(recovered.commits_landed, 1)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")

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

    def test_insight_commit_lands_when_active_memory_is_invalid(self):
        self._append_memory(self.root, "- `one` duplicate active memory → []\n")
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
            self._append_memory(worktree, "- `two` invalid insight memory edit → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "insight: invalid memory edit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-insight paths: MEMORY\.md"):
            IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertNotIn("invalid insight", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_uncommitted_temp_change_does_not_land(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` uncommitted isolated memory → []\n")
            return "dirty"

        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("uncommitted isolated", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

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

    def test_tracked_operation_rebases_before_preparing_when_unrelated_head_moves(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` isolated memory → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: isolated update", cwd=worktree)
            (self.root / "DIRECT.md").write_text("direct writer\n", encoding="utf-8")
            self._git("add", "DIRECT.md")
            self._git("commit", "-m", "direct: unrelated update")
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "dreamer").run(
            callback,
            operation_id="dreamer-head-move-1",
            operation_input={"message": "dream"},
        )

        receipt = SemanticOperationStore(self.root).read("dreamer-head-move-1")
        self.assertEqual(result.commits_landed, 1)
        self.assertTrue((self.root / "DIRECT.md").is_file())
        self.assertIn("isolated memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(receipt.phase, "committed")
        self.assertIn("RightMemory-Operation: dreamer-head-move-1", self._git("log", "-1", "--format=%B"))

    def test_tracked_operation_reruns_when_nonconflicting_semantic_state_moves(self):
        def stale_callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` stale isolated result → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: stale isolated result", cwd=worktree)
            (self.root / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Direct {#direct}\n",
                encoding="utf-8",
            )
            self._git("add", "PURSUITS.md")
            self._git("commit", "-m", "pursuit: direct semantic update")
            return "stale"

        supervisor = IsolatedWriteSupervisor(self.root, "dreamer")
        with self.assertRaisesRegex(RuntimeError, "main semantic state changed"):
            supervisor.run(
                stale_callback,
                operation_id="dreamer-semantic-head-move",
                operation_input={"message": "dream"},
            )

        receipt = SemanticOperationStore(self.root).read("dreamer-semantic-head-move")
        self.assertEqual(receipt.phase, "running")

        def fresh_callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` fresh isolated result → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: fresh isolated result", cwd=worktree)
            return "fresh"

        recovered = supervisor.run(
            fresh_callback,
            operation_id="dreamer-semantic-head-move",
            operation_input={"message": "dream"},
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertEqual(recovered.output, "fresh")
        self.assertNotIn("stale isolated result", memory)
        self.assertIn("fresh isolated result", memory)

    def test_tracked_operation_head_conflict_stays_rerunnable_not_prepared(self):
        def conflicting_callback(worktree: Path) -> str:
            memory = worktree / "MEMORY.md"
            memory.write_text(memory.read_text(encoding="utf-8").replace("initial memory", "isolated memory"), encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: isolated replacement", cwd=worktree)
            main_memory = self.root / "MEMORY.md"
            main_memory.write_text(main_memory.read_text(encoding="utf-8").replace("initial memory", "direct memory"), encoding="utf-8")
            self._git("add", "MEMORY.md")
            self._git("commit", "-m", "memory: direct replacement")
            return "conflicted"

        supervisor = IsolatedWriteSupervisor(self.root, "dreamer")
        with self.assertRaises(RuntimeError):
            supervisor.run(
                conflicting_callback,
                operation_id="dreamer-head-conflict-1",
                operation_input={"message": "dream"},
            )

        failed = SemanticOperationStore(self.root).read("dreamer-head-conflict-1")
        self.assertEqual(failed.phase, "running")

        def retry_callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` recovered after conflict → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: recovered operation", cwd=worktree)
            return "recovered"

        recovered = supervisor.run(
            retry_callback,
            operation_id="dreamer-head-conflict-1",
            operation_input={"message": "dream"},
        )

        self.assertEqual(recovered.output, "recovered")
        self.assertIn("direct memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertIn("recovered after conflict", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

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

    def test_update_lands_memory_and_pursuit_as_one_transaction(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `durable` durable result → []\n")
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Continue {#continue} \u2192 [dep:durable]\n",
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: unified update", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.start_commit, self._git("rev-parse", f"{result.landed_commit}^"))
        self.assertEqual(result.changed_paths, ("MEMORY.md", "PURSUITS.md"))
        self.assertIn("continue", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_update_rejects_multiple_commits(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `first` first state → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: first", cwd=worktree)
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Continue {#continue}\n",
                encoding="utf-8",
            )
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: second", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "at most one commit"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_normal_update_rejects_corrections_file(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: invalid normal correction", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: corrections\\.md"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

    def test_review_correction_lands_state_and_feedback_in_one_commit(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `corrected` accepted state → []\n")
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n\n"
                "## Keep accepted scope\n\n"
                "### Background\n\nThe updater broadened the edit.\n\n"
                "### Proposed edit\n\nRewrite unrelated state.\n\n"
                "### Accepted edit\n\nChange only the reviewed state.\n",
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: apply reviewed correction", cwd=worktree)
            return "corrected"

        result = IsolatedWriteSupervisor(
            self.root,
            "update",
            update_mode="review-correction",
        ).run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.changed_paths, ("MEMORY.md", "corrections.md"))
        self.assertTrue((self.root / "corrections.md").is_file())

    def test_review_correction_rejects_feedback_only_commit(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: feedback only", cwd=worktree)
            return "corrected"

        with self.assertRaisesRegex(RuntimeError, "corrections.md-only"):
            IsolatedWriteSupervisor(
                self.root,
                "update",
                update_mode="review-correction",
            ).run(callback)

    def test_review_correction_enforces_updater_correction_ceiling(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `corrected` accepted state → []\n")
            (worktree / "corrections.md").write_text(
                self._corrections_markdown(16),
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: oversized correction curation", cwd=worktree)
            return "corrected"

        with self.assertRaisesRegex(RuntimeError, "at most 15 are allowed"):
            IsolatedWriteSupervisor(
                self.root,
                "update",
                update_mode="review-correction",
            ).run(callback)

    def test_review_correction_discards_speculative_commit_when_input_is_needed(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `speculative` must not land → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: speculative correction", cwd=worktree)
            return "Needs input: Which scope should change?"

        result = IsolatedWriteSupervisor(
            self.root,
            "update",
            update_mode="review-correction",
        ).run(callback)

        self.assertEqual(result.commits_landed, 0)
        self.assertEqual(result.landed_commit, self.initial_head)
        self.assertNotIn("speculative", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_dreamer_rejects_pursuit_write(self):
        def callback(worktree: Path) -> None:
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Changed {#changed}\n",
                encoding="utf-8",
            )
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "dreamer: invalid pursuit edit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: PURSUITS\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

    def test_dreamer_rejects_fixed_correction_collection_write(self):
        def callback(worktree: Path) -> None:
            path = worktree / "MEMORY_agent-corrections-writing.md"
            path.write_text("# Curated writing corrections\n", encoding="utf-8")
            self._git("add", path.name, cwd=worktree)
            self._git("commit", "-m", "dreamer: invalid correction curation", cwd=worktree)

        with self.assertRaisesRegex(
            RuntimeError,
            "non-memory paths: MEMORY_agent-corrections-writing\\.md",
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

    def test_narrow_memory_role_preserves_pursuit_cross_tree_reference(self):
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Continue {#continue} \u2192 [dep:one]\n",
            encoding="utf-8",
        )
        self._git("add", "PURSUITS.md")
        self._git("commit", "-m", "pursuit: depend on memory")
        current_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> None:
            memory = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            (worktree / "MEMORY.md").write_text(memory.replace("`one`", "`renamed`"), encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "dreamer: break pursuit edge", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "dangling edge `dep:one`"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), current_head)

    def test_dirty_pursuit_rules_or_corrections_blocks_narrow_writer(self):
        for name in ("PURSUITS.md", "PURSUIT_RULES.md", "corrections.md"):
            with self.subTest(name=name):
                path = self.root / name
                existed = path.exists()
                original = path.read_text(encoding="utf-8") if existed else None
                path.write_text("local synchronized state\n", encoding="utf-8")

                with self.assertRaises(MainMemoryDirtyError) as caught:
                    IsolatedWriteSupervisor(self.root, "dreamer").run(lambda _worktree: None)

                self.assertEqual(caught.exception.paths, (name,))
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_reviewer_cannot_land_graph_edits(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `reviewed` invalid reviewer edit → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "reviewer: invalid graph write", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: MEMORY\\.md"):
            IsolatedWriteSupervisor(self.root, "reviewer").run(callback)

    def test_sync_reconciler_preserves_structured_corrections_over_updater_ceiling(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                self._corrections_markdown(16),
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: preserve correction union", cwd=worktree)
            return "repaired"

        result = IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

        self.assertEqual(result.commits_landed, 1)
        text = (self.root / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Entry "), 16)

    def test_correction_overflow_does_not_block_unrelated_normal_update(self):
        (self.root / "corrections.md").write_text(
            self._corrections_markdown(16),
            encoding="utf-8",
        )
        self._git("add", "corrections.md")
        self._git("commit", "-m", "sync: preserve unresolved correction union")

        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `later` unrelated durable state → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: unrelated update", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertIn("later", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "corrections.md").read_text(encoding="utf-8").count("## Entry "),
            16,
        )

    def test_sync_reconciler_still_rejects_malformed_correction_entries(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n\n"
                "## Incomplete\n\n### Background\n\nOnly one section.\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: malformed correction", cwd=worktree)
            return "repaired"

        with self.assertRaisesRegex(RuntimeError, "missing `### Proposed edit`"):
            IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

    def test_cleanup_stale_removes_temp_branch_and_worktree(self):
        branch, worktree = self._add_isolated_worktree("dreamer", "0123456789abcdef0123456789abcdef")

        IsolatedWriteSupervisor(self.root, "dreamer").cleanup_stale()

        self.assertFalse(worktree.exists())
        self.assertEqual(self._git("branch", "--list", branch), "")
        self._assert_isolated_cleanup()

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

    def _add_isolated_worktree(self, role: str, identifier: str) -> tuple[str, Path]:
        branch = f"rightmemory-isolated-{role}-{identifier}"
        worktree = self.root / ".runtime" / "worktrees" / f"{role}-{identifier}"
        self._git("worktree", "add", "-b", branch, str(worktree), self.initial_head)
        return branch, worktree

    def _write_lease(self, role: str, identifier: str, *, pid: int, identity: str) -> Path:
        path = self.root / ".runtime" / "worktree-leases" / f"{role}-{identifier}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "process_identity": identity}) + "\n",
            encoding="utf-8",
        )
        return path

    def _append_memory(self, root: Path, text: str) -> None:
        memory = root / "MEMORY.md"
        memory.write_text(memory.read_text(encoding="utf-8") + text, encoding="utf-8")

    def _corrections_markdown(self, count: int) -> str:
        entries = []
        for index in range(count):
            entries.append(
                f"## Entry {index}\n\n"
                "### Background\n\nBackground.\n\n"
                "### Proposed edit\n\nProposed.\n\n"
                "### Accepted edit\n\nAccepted.\n"
            )
        return "# RightMemory Update Corrections\n\n" + "\n".join(entries)

    def _assert_isolated_cleanup(self) -> None:
        worktrees = self._git("worktree", "list", "--porcelain")
        branches = self._git("branch", "--list", "rightmemory-isolated-*")
        leases = list((self.root / ".runtime" / "worktree-leases").glob("*.json"))
        self.assertNotIn(".runtime/worktrees/", worktrees)
        self.assertEqual(branches, "")
        self.assertEqual(leases, [])

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
