from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from rightmemory.assistant import AssistantService
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.update_queue import UpdateQueueStore
from rightmemory.update_queue_git import QUEUE_FINALIZER
from tests.isolated_write_test_base import IsolatedWriteTestBase
from tests.update_queue_git_base import GitUpdateQueueTestBase


class AssistantServiceRecoveryReviewTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        instructions = patch("rightmemory.assistant.build_assistant_update_instructions", return_value="test")
        instructions.start()
        self.addCleanup(instructions.stop)
        self.service = self.new_service()

    def new_service(self):
        service = AssistantService(self.root)
        service.batch_options = {
            "trigger_candidates": 1, "target_batch_candidates": 1, "max_wait_seconds": 0,
        }
        return service

    def begin(self):
        self.service.submit("first", "request-one", "test evidence")
        return self.service.begin_update()

    def commit_memory(self, task_id):
        task = self.service.writer.read(task_id)
        self._append_memory(task.worktree, "- `two` accepted fact → []\n")
        self._git("add", "MEMORY.md", cwd=task.worktree)
        self._git("commit", "-m", "memory: review fixture", cwd=task.worktree)

    def test_terminal_write_receipt_recovers_queue_ack_without_a_second_commit(self):
        begin = self.begin()
        task_id = begin["task_id"]
        self.commit_memory(task_id)
        with patch.object(self.service.store, "finish_reserved_batch", side_effect=OSError("ack interrupted")):
            with self.assertRaises(OSError):
                self.service.complete_update(task_id)
        landed = self._git("rev-parse", "HEAD")
        self.assertNotEqual(landed, self.initial_head)

        recovered = self.new_service().update_status(task_id)

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["landed_commit"], landed)
        self.assertEqual(self._git("rev-parse", "HEAD"), landed)
        self.assertEqual(self.service.store.read("first").current_batch, [])
        self.assertIsNone(self.service.store._read_reservation(begin["batch_id"]))

    def test_partial_cross_session_ack_recovers_the_remaining_participant(self):
        self.service.submit("first", "request-one", "test evidence")
        self.service.submit("second", "request-two", "test evidence")
        self.service.batch_options.update(trigger_candidates=2, target_batch_candidates=2)
        begin = self.service.begin_update()
        original_write = self.service.store._write

        def interrupt_second_ack(session_id, state):
            if session_id == "second" and state.last_operation_id == begin["batch_id"]:
                raise OSError("second ack interrupted")
            original_write(session_id, state)

        with patch.object(self.service.store, "_write", side_effect=interrupt_second_ack):
            with self.assertRaises(OSError):
                self.service.complete_update(begin["task_id"])
        landed = self._git("rev-parse", "HEAD")
        self.assertEqual(self.service.store._read_raw("first").current_batch, [])
        self.assertEqual(len(self.service.store._read_raw("second").current_batch), 1)

        recovered = self.new_service().update_status(begin["task_id"])

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(self._git("rev-parse", "HEAD"), landed)
        self.assertEqual(self.service.store.read("second").current_batch, [])
        self.assertEqual(UpdateQueueStore(self.root).outbox_candidates(), ())

    def test_retry_recovers_prepared_result_before_allocating_a_new_task(self):
        begin = self.begin()
        task_id = begin["task_id"]
        self.commit_memory(task_id)
        with patch.object(self.service.writer.supervisor, "publish_external", side_effect=OSError("publish interrupted")):
            with self.assertRaises(OSError):
                self.service.complete_update(task_id)
        task = self.service.writer.read(task_id)
        self.assertEqual(SemanticOperationStore(self.root).read(task.operation_id).phase, "prepared")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

        recovered = self.new_service().begin_update(retry_task=task_id)

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["task_id"], task_id)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")


    def test_retry_recovers_landed_commit_when_semantic_receipt_was_interrupted(self):
        begin = self.begin()
        task_id = begin["task_id"]
        self.commit_memory(task_id)
        task = self.service.writer.read(task_id)
        complete_commit = SemanticOperationStore.complete_commit

        def interrupt_receipt(store, operation_id, commit):
            if operation_id == task.operation_id:
                raise OSError("receipt interrupted")
            return complete_commit(store, operation_id, commit)

        with patch.object(SemanticOperationStore, "complete_commit", interrupt_receipt):
            with self.assertRaises(OSError):
                self.service.complete_update(task_id)
        landed = self._git("rev-parse", "HEAD")
        self.assertNotEqual(landed, self.initial_head)
        self.assertEqual(SemanticOperationStore(self.root).read(task.operation_id).phase, "prepared")

        recovered = self.new_service().begin_update(retry_task=task_id)

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["landed_commit"], landed)
        self.assertEqual(self._git("rev-parse", "HEAD"), landed)

    def test_repeated_submit_after_completion_does_not_enqueue_or_commit_again(self):
        begin = self.begin()
        first = self.service.complete_update(begin["task_id"])
        service = self.new_service()

        submission = service.submit("first", "request-one", "test evidence")
        repeated = service.complete_update(begin["task_id"])

        self.assertTrue(submission["duplicate"])
        self.assertFalse(submission["update"]["ready"])
        self.assertEqual(service.store.read("first").pending, [])
        self.assertEqual(repeated["landed_commit"], first["landed_commit"])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")


class AssistantForeignCompletionReviewTests(GitUpdateQueueTestBase):
    def setUp(self):
        super().setUp()
        instructions = patch("rightmemory.assistant.build_assistant_update_instructions", return_value="test")
        instructions.start()
        self.addCleanup(instructions.stop)
        for root in (self.first, self.second):
            (root / "rightmemory.toml").write_text("[sync]\nenabled = true\n", encoding="utf-8")
        self.first_service = self.service(self.first)
        self.second_service = self.service(self.second)

    def service(self, root):
        service = AssistantService(root)
        service.batch_options = {
            "trigger_candidates": 1, "target_batch_candidates": 1, "max_wait_seconds": 0,
        }
        return service

    def complete_on_another_device(self, *, prepare_first=True):
        self.first_service.submit("first", "request-one", "test evidence")
        first = self.first_service.begin_update()
        self.assertEqual(first["status"], "ready")
        if prepare_first:
            result = self.first_service.writer.complete(
                first["task_id"], external_finalizer=QUEUE_FINALIZER,
                check_current=lambda: self.first_service._check_current(first["task_id"]),
            )
            self.assertTrue(result.prepared)
        future = datetime.fromisoformat(first["expires_at"]) + timedelta(seconds=1)
        clock = patch("rightmemory.assistant._now", return_value=future)
        clock.start()
        self.addCleanup(clock.stop)
        queue_clock = patch("rightmemory.update_queue_git._utc_now", return_value=future)
        queue_clock.start()
        self.addCleanup(queue_clock.stop)
        second = self.second_service.begin_update()
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["batch_id"], first["batch_id"])
        self.assertNotEqual(second["task_id"], first["task_id"])
        completed = self.second_service.complete_update(second["task_id"])
        self.assertEqual(completed["status"], "completed")
        return first, completed

    def test_foreign_token_completion_releases_old_active_and_allows_the_next_batch(self):
        first, foreign = self.complete_on_another_device()

        recovered = self.first_service.begin_update(retry_task=first["task_id"])

        self.assertEqual(recovered["status"], "completed_elsewhere")
        self.assertEqual(recovered["landed_commit"], foreign["landed_commit"])
        self.assertIsNone(self.first_service._active())
        self.first_service.submit("first", "request-two", "new evidence")
        following = self.first_service.begin_update()
        self.assertEqual(following["status"], "ready")
        self.assertNotEqual(following["batch_id"], first["batch_id"])

    def test_foreign_terminal_receipt_survives_interruption_before_local_ack(self):
        first, foreign = self.complete_on_another_device()
        with patch.object(
            self.first_service.store, "acknowledge_synchronized", side_effect=OSError("ack interrupted")
        ):
            with self.assertRaises(OSError):
                self.first_service.begin_update(retry_task=first["task_id"])

        recovered = self.service(self.first).update_status(first["task_id"])

        self.assertEqual(recovered["status"], "completed_elsewhere")
        self.assertEqual(recovered["landed_commit"], foreign["landed_commit"])
        self.assertIsNone(self.first_service._active())

    def test_foreign_running_operation_recovers_without_reopening_dirty_native_edits(self):
        first, foreign = self.complete_on_another_device(prepare_first=False)
        task_id = first["task_id"]
        with patch.object(
            self.first_service.writer.supervisor, "_validate_candidate", side_effect=RuntimeError("validation rejected")
        ):
            with self.assertRaises(RuntimeError):
                self.first_service.writer.complete(task_id, external_finalizer=QUEUE_FINALIZER)
        task = self.first_service.writer.read(task_id)
        self.assertIsNone(task.source_commit)
        self.assertEqual(SemanticOperationStore(self.first).read(task.operation_id).phase, "running")
        (task.worktree / "MEMORY.md").write_text("# Unfinished native correction\n", encoding="utf-8")
        with patch.object(
            self.first_service.store, "acknowledge_synchronized", side_effect=OSError("ack interrupted")
        ):
            with self.assertRaises(OSError):
                self.first_service.begin_update(retry_task=task_id)

        recovered = self.service(self.first).update_status(task_id)

        self.assertEqual(recovered["status"], "completed_elsewhere")
        self.assertEqual(recovered["landed_commit"], foreign["landed_commit"])
        self.assertTrue(task.worktree.is_dir())
        self.assertTrue(self._git(task.worktree, "status", "--porcelain"))
        self.assertIsNone(self.first_service._active())
