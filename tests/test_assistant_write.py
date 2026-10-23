import json
import os
import unittest
from unittest.mock import patch

from rightmemory.assistant_write import AssistantStaleVersionError, AssistantWriteManager
from rightmemory.isolated_write import IsolatedWriteSupervisor
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.update_queue import UpdateCandidate, update_candidate_batch_id
from rightmemory.update_record import UpdateRecordStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class AssistantWriteTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        self.manager = AssistantWriteManager(self.root)
        self.candidates = (
            UpdateCandidate(
                uid="a" * 32,
                session_id="assistant-session",
                display_id=1,
                message="Retain the user's new project constraint.",
                submitted_at="2026-10-23T10:00:00+00:00",
            ),
        )
        self.batch_id = update_candidate_batch_id(self.candidates)

    def _prepare(self, task_id="task-one"):
        return self.manager.prepare(task_id, self.batch_id, self.candidates)

    def _commit_memory(self, task, text="- `two` native assistant edit → []\n"):
        self._append_memory(task.worktree, text)
        self._git("add", "MEMORY.md", cwd=task.worktree)
        self._git("commit", "-m", "memory: retain assistant update", cwd=task.worktree)
        return self._git("rev-parse", "HEAD", cwd=task.worktree)

    def test_native_worktree_survives_new_manager_and_finishes_with_batch_evidence(self):
        task = self._prepare()
        self._commit_memory(task)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

        manager = AssistantWriteManager(self.root)
        repeated = manager.prepare(task.task_id, self.batch_id, self.candidates)
        self.assertEqual(repeated, task)
        result = manager.complete(task.task_id, output="Retained the new constraint.")

        self.assertIn("native assistant edit", (self.root / "MEMORY.md").read_text())
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.changed_paths, ("MEMORY.md",))
        self.assertEqual(result.operation_id, task.operation_id)
        evidence = UpdateRecordStore(self.root).read(self.batch_id)
        self.assertEqual(evidence.candidates, self.candidates)
        self.assertNotEqual(evidence.operation_id, result.operation_id)
        self.assertTrue(task.worktree.is_dir())

    def test_repeated_completion_returns_original_receipt_after_new_writes_and_cleanup(self):
        task = self._prepare()
        self._commit_memory(task)
        first = self.manager.complete(task.task_id, output="original report")
        self.manager.cleanup(task.task_id)
        self._append_memory(self.root, "- `later` later independent edit → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: later edit")
        head = self._git("rev-parse", "HEAD")

        second = AssistantWriteManager(self.root).complete(task.task_id)
        self.assertEqual(second.output, "original report")
        self.assertEqual(second.landed_commit, first.landed_commit)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertFalse(task.worktree.exists())
        with self.assertRaisesRegex(ValueError, "different output"):
            self.manager.complete(task.task_id, output="a different report")

    def test_no_semantic_change_still_retains_evidence_once(self):
        task = self._prepare()
        original = (self.root / "MEMORY.md").read_bytes()
        first = self.manager.complete(task.task_id, output="No useful changes.")
        second = self.manager.complete(task.task_id)
        self.assertEqual(first.changed_paths, ())
        self.assertEqual(first.landed_commit, second.landed_commit)
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), original)
        self.assertIsNotNone(UpdateRecordStore(self.root).read(self.batch_id))
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")

    def test_prepare_rejects_same_task_for_different_batch(self):
        self._prepare()
        other = (UpdateCandidate(**{**self.candidates[0].__dict__, "uid": "b" * 32}),)
        with self.assertRaisesRegex(ValueError, "different candidate evidence"):
            self.manager.prepare("task-one", update_candidate_batch_id(other), other)

    def test_dirty_native_worktree_is_not_submitted(self):
        task = self._prepare()
        self._append_memory(task.worktree, "- `two` still editing → []\n")
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            self.manager.complete(task.task_id)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertIsNone(self.manager.read(task.task_id).source_commit)

    def test_untracked_native_files_are_not_silently_discarded(self):
        task = self._prepare()
        (task.worktree / "notes.txt").write_text("unfinished scratch work")
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            self.manager.complete(task.task_id)
        self.assertTrue((task.worktree / "notes.txt").exists())

    def test_illegal_path_is_rejected_even_if_later_reverted(self):
        task = self._prepare()
        (task.worktree / "outside-policy.txt").write_text("forbidden")
        self._git("add", "outside-policy.txt", cwd=task.worktree)
        self._git("commit", "-m", "invalid intermediate path", cwd=task.worktree)
        self._git("rm", "outside-policy.txt", cwd=task.worktree)
        self._git("commit", "-m", "remove invalid path", cwd=task.worktree)
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.manager.complete(task.task_id)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    @unittest.skipIf(os.name == "nt", "symlinks require Windows privileges")
    def test_symlink_candidate_does_not_land(self):
        task = self._prepare()
        target = self.root / "private.txt"
        target.write_text("unchanged")
        (task.worktree / "MEMORY_link.md").symlink_to(target)
        self._git("add", "MEMORY_link.md", cwd=task.worktree)
        self._git("commit", "-m", "memory: invalid symlink", cwd=task.worktree)
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            self.manager.complete(task.task_id)
        self.assertEqual(target.read_text(), "unchanged")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_invalid_graph_can_be_corrected_in_the_same_task(self):
        task = self._prepare()
        self._commit_memory(task, "- `one` duplicate id → []\n")
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            self.manager.complete(task.task_id, output="invalid attempt")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertIsNone(self.manager.read(task.task_id).source_commit)

        self._git("reset", "--hard", task.base_commit, cwd=task.worktree)
        self._commit_memory(task)
        result = self.manager.complete(task.task_id, output="corrected attempt")
        self.assertEqual(result.output, "corrected attempt")
        self.assertIn("native assistant edit", (self.root / "MEMORY.md").read_text())

    def test_stale_semantic_base_does_not_overwrite_new_memory(self):
        task = self._prepare()
        self._commit_memory(task)
        self._append_memory(self.root, "- `fresh` concurrent memory → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: concurrent change")
        head = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(AssistantStaleVersionError, "memory changed since assistant task began"):
            self.manager.complete(task.task_id)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertNotIn("native assistant edit", (self.root / "MEMORY.md").read_text())

    def test_prepared_stale_result_has_distinct_conflict_error(self):
        task = self._prepare()
        self._commit_memory(task)
        with patch.object(IsolatedWriteSupervisor, "_land_operation_commit", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.manager.complete(task.task_id)
        self._append_memory(self.root, "- `fresh` newer memory → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: changed before retry")
        head = self._git("rev-parse", "HEAD")

        with self.assertRaisesRegex(AssistantStaleVersionError, "before prepared memory write could land"):
            self.manager.complete(task.task_id)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(SemanticOperationStore(self.root).read(task.operation_id).phase, "prepared")

    def test_lost_ownership_before_preparation_does_not_publish(self):
        task = self._prepare()
        self._commit_memory(task)

        def reject():
            raise RuntimeError("claim was replaced")

        with self.assertRaisesRegex(RuntimeError, "claim was replaced"):
            self.manager.complete(task.task_id, check_current=reject)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        receipt = SemanticOperationStore(self.root).read(task.operation_id)
        self.assertEqual(receipt.phase, "running")

    def test_lost_ownership_before_publication_is_not_bypassed_by_legacy_recovery(self):
        task = self._prepare()
        self._commit_memory(task)
        calls = 0

        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("claim was replaced")

        with self.assertRaisesRegex(RuntimeError, "claim was replaced"):
            self.manager.complete(task.task_id, check_current=check)
        self.assertEqual(calls, 2)
        receipt = SemanticOperationStore(self.root).read(task.operation_id)
        self.assertEqual(receipt.phase, "prepared")
        IsolatedWriteSupervisor(self.root, "update").recover_prepared()
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

        result = self.manager.complete(task.task_id, check_current=lambda: None)
        self.assertEqual(result.commits_landed, 1)

    def test_prepared_completion_recovers_after_landing_interruption_and_gc(self):
        task = self._prepare()
        self._commit_memory(task)
        with patch.object(IsolatedWriteSupervisor, "_land_operation_commit", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.manager.complete(task.task_id, output="original completion")
        self._git("gc", "--prune=now")
        result = AssistantWriteManager(self.root).complete(task.task_id)
        self.assertEqual(result.output, "original completion")
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")

    def test_prepared_retry_publishes_frozen_commit_without_reopening_native_workspace(self):
        task = self._prepare()
        self._commit_memory(task)
        with patch.object(IsolatedWriteSupervisor, "_land_operation_commit", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.manager.complete(task.task_id)
        self._commit_memory(task, "- `late` changes after completion was submitted → []\n")
        self._git("worktree", "remove", str(task.worktree))

        result = AssistantWriteManager(self.root).complete(task.task_id)
        self.assertEqual(result.commits_landed, 1)
        memory = (self.root / "MEMORY.md").read_text()
        self.assertIn("native assistant edit", memory)
        self.assertNotIn("changes after completion was submitted", memory)

    def test_landed_completion_recovers_when_receipt_write_is_interrupted(self):
        task = self._prepare()
        self._commit_memory(task)
        original = SemanticOperationStore.complete_commit

        def fail_receipt(store, operation_id, commit):
            if operation_id == task.operation_id:
                raise OSError("receipt interrupted")
            return original(store, operation_id, commit)

        with patch.object(SemanticOperationStore, "complete_commit", fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt interrupted"):
                self.manager.complete(task.task_id, output="written once")
        head = self._git("rev-parse", "HEAD")
        self._append_memory(self.root, "- `later` memory after landed outcome → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: advance after lost receipt")
        latest_head = self._git("rev-parse", "HEAD")
        result = self.manager.complete(task.task_id)
        self.assertEqual(result.landed_commit, head)
        self.assertEqual(self._git("rev-parse", "HEAD"), latest_head)
        self.assertEqual((self.root / "MEMORY.md").read_text().count("native assistant edit"), 1)

    def test_external_publication_requires_explicit_completion(self):
        task = self._prepare()
        self._commit_memory(task)
        prepared = self.manager.complete(task.task_id, external_finalizer="queue:token", output="prepared")
        self.assertTrue(prepared.prepared)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        with self.assertRaisesRegex(ValueError, "different finalizer"):
            self.manager.complete(task.task_id)
        self._git("merge", "--ff-only", prepared.candidate_commit)
        result = self.manager.complete_external(
            task.task_id, external_finalizer="queue:token", landed_commit=prepared.candidate_commit
        )
        self.assertFalse(result.prepared)
        self.assertEqual(result.landed_commit, prepared.candidate_commit)

    def test_cleanup_retains_work_added_after_completion(self):
        task = self._prepare()
        self._commit_memory(task)
        self.manager.complete(task.task_id)
        (task.worktree / "notes.txt").write_text("do not discard")
        with self.assertRaisesRegex(RuntimeError, "retained for review"):
            self.manager.cleanup(task.task_id)
        self.assertTrue((task.worktree / "notes.txt").exists())

    def test_old_update_cleanup_preserves_persistent_assistant_worktree(self):
        task = self._prepare()
        IsolatedWriteSupervisor(self.root, "update").cleanup_stale()
        self.assertTrue(task.worktree.is_dir())
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=task.worktree), task.base_commit)

    def test_mismatched_task_record_is_rejected(self):
        task = self._prepare()
        path = self.manager._record_path(task.task_id)
        record = json.loads(path.read_text())
        record["task_id"] = "another-task"
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "invalid assistant task identity"):
            self.manager.complete(task.task_id)

    @unittest.skipIf(os.name == "nt", "symlinks require Windows privileges")
    def test_runtime_symlink_is_rejected_before_creating_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".runtime").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self._prepare()
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
