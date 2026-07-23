import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.isolated_write import IsolatedWriteSupervisor
from rightmemory.semantic_operation import SemanticOperationStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class IsolatedWriteOperationRecoveryTests(IsolatedWriteTestBase):
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
        self.assertEqual(recovered.output, "checkpoint")
        self.assertEqual(recovered.commits_landed, 1)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

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
        pursuit = (self.root / "PURSUITS.md").read_text(encoding="utf-8")
        receipt = SemanticOperationStore(self.root).read("dreamer-semantic-head-move")
        self.assertEqual(recovered.output, "fresh")
        self.assertNotIn("stale isolated result", memory)
        self.assertIn("fresh isolated result", memory)
        self.assertIn("## Direct {#direct}", pursuit)
        self.assertEqual(receipt.phase, "committed")


if __name__ == "__main__":
    unittest.main()
