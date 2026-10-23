from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant import AssistantClaimLost, AssistantService
from rightmemory.assistant_write import AssistantStaleVersionError
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.update_record import UpdateRecordStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class AssistantServiceTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        (self.root / "rightmemory.toml").write_text(
            "[update.async]\ntrigger_candidates = 2\ntarget_batch_candidates = 2\nmax_wait_seconds = 1\n"
        )
        self.service = AssistantService(self.root)
        update_prompt = patch("rightmemory.assistant.build_assistant_update_instructions", return_value="")
        retrieve_prompt = patch("rightmemory.assistant_prompt.build_assistant_retrieve_instructions", return_value="")
        update_prompt.start()
        retrieve_prompt.start()
        self.addCleanup(update_prompt.stop)
        self.addCleanup(retrieve_prompt.stop)

    def _submit_batch(self):
        first = self.service.submit("conversation-a", "evidence-a", "Project fictional code is Orion.")
        second = self.service.submit("conversation-b", "evidence-b", "Project code applies to the export plan.")
        return first, second

    def _begin(self):
        self._submit_batch()
        result = self.service.begin_update()
        self.assertTrue(result["claimed"])
        return result

    def _edit(self, task, line="- `project` Project fictional code is Orion. → []\n"):
        worktree = Path(task["worktree"])
        self._append_memory(worktree, line)
        self._git("add", "MEMORY.md", cwd=worktree)
        self._git("commit", "-m", "memory: record project", cwd=worktree)

    def test_submit_batch_native_edit_complete_and_independent_context(self):
        first, second = self._submit_batch()
        self.assertFalse(first["update"]["ready"])
        self.assertTrue(second["update"]["ready"])
        pending = self.service.context("independent-reader")
        self.assertEqual(pending["pending_count"], 2)
        task = self.service.begin_update()
        self.assertEqual(task["batch_id"], second["update"]["batch_id"])
        self._edit(task)
        receipt = self.service.complete_update(task["task_id"], "recorded")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["changed_paths"], ["MEMORY.md"])
        head = self._git("rev-parse", "HEAD")
        self.assertEqual(receipt["landed_commit"], head)
        retained = UpdateRecordStore(self.root).read(task["batch_id"])
        self.assertEqual(len(retained.candidates), 2)
        fresh = AssistantService(self.root).context("another-reader")
        self.assertEqual(fresh["pending_count"], 0)
        self.assertEqual(fresh["memory_commit"], head)
        self.assertIn("Orion", (self.root / "MEMORY.md").read_text())
        self.assertFalse(Path(task["worktree"]).exists())

    def test_submit_and_completion_ids_are_immutable_after_success(self):
        task = self._begin()
        self._edit(task)
        first = self.service.complete_update(task["task_id"], "done")
        head = self._git("rev-parse", "HEAD")
        duplicate = AssistantService(self.root).complete_update(task["task_id"])
        self.assertEqual(first["landed_commit"], duplicate["landed_commit"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        saved = self.service.submit("conversation-a", "evidence-a", "Project fictional code is Orion.")
        self.assertTrue(saved["duplicate"])
        self.assertEqual(self.service.context("fresh")["pending_count"], 0)
        with self.assertRaises(ValueError):
            self.service.submit("conversation-a", "evidence-a", "different evidence")
        with self.assertRaises(ValueError):
            self.service.complete_update(task["task_id"], "changed result")

    def test_repeat_begin_returns_existing_task_without_duplicate_claim(self):
        first = self._begin()
        second = AssistantService(self.root).begin_update()
        self.assertFalse(second["claimed"])
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["worktree"], second["worktree"])

    def test_new_submissions_stay_pending_after_claimed_batch_finishes(self):
        task = self._begin()
        late = self.service.submit("conversation-a", "late", "Another finding arrived during Update.")
        self.service.complete_update(task["task_id"])
        state = self.service.store.read("conversation-a")
        self.assertEqual([job.candidate_uid for job in state.pending], [late["candidate_uid"]])
        self.assertEqual(self.service.context("reader")["pending_count"], 1)

    def test_all_skipped_batch_still_settles_once(self):
        task = self._begin()
        before = (self.root / "MEMORY.md").read_bytes()
        receipt = self.service.complete_update(task["task_id"], "no durable change needed")
        self.assertEqual(receipt["changed_paths"], [])
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), before)
        self.assertEqual(self.service.context("reader")["pending_count"], 0)
        self.assertIsNotNone(UpdateRecordStore(self.root).read(task["batch_id"]))
        head = self._git("rev-parse", "HEAD")
        self.service.complete_update(task["task_id"])
        self.assertEqual(head, self._git("rev-parse", "HEAD"))

    def test_stale_edit_retries_same_batch_with_new_attempt_and_fences_old_task(self):
        task = self._begin()
        self._edit(task)
        self._append_memory(self.root, "- `concurrent` Concurrent evidence. → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: concurrent update")
        with self.assertRaises(AssistantStaleVersionError):
            self.service.complete_update(task["task_id"])
        newer_head = self._git("rev-parse", "HEAD")
        replacement = self.service.begin_update(retry_task=task["task_id"])
        self.assertEqual(task["batch_id"], replacement["batch_id"])
        self.assertNotEqual(task["task_id"], replacement["task_id"])
        self.assertEqual(replacement["base_commit"], newer_head)
        with self.assertRaises(AssistantClaimLost):
            self.service.complete_update(task["task_id"])
        self._edit(replacement)
        self.service.complete_update(replacement["task_id"])
        memory = (self.root / "MEMORY.md").read_text()
        self.assertIn("Concurrent evidence", memory)
        self.assertIn("Orion", memory)

    def test_expired_unfinished_claim_can_be_replaced_and_old_no_change_is_fenced(self):
        task = self._begin()
        later = datetime(2100, 1, 1, tzinfo=UTC)
        with patch("rightmemory.assistant._now", return_value=later):
            replacement = self.service.begin_update()
        self.assertNotEqual(task["task_id"], replacement["task_id"])
        with self.assertRaises(AssistantClaimLost):
            self.service.complete_update(task["task_id"])
        self.service.complete_update(replacement["task_id"])

    def test_validation_failure_can_be_corrected_before_completion(self):
        task = self._begin()
        self._edit(task, "- `bad` Broken edge. → [does-not-exist]\n")
        with self.assertRaises(RuntimeError):
            self.service.complete_update(task["task_id"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        worktree = Path(task["worktree"])
        text = (worktree / "MEMORY.md").read_text().replace("→ [does-not-exist]", "→ []")
        (worktree / "MEMORY.md").write_text(text)
        self._git("add", "MEMORY.md", cwd=worktree)
        self._git("commit", "--amend", "--no-edit", cwd=worktree)
        receipt = self.service.complete_update(task["task_id"])
        self.assertEqual(receipt["status"], "completed")

    def test_illegal_committed_path_never_lands(self):
        task = self._begin()
        worktree = Path(task["worktree"])
        (worktree / "outside.txt").write_text("unexpected")
        self._git("add", "outside.txt", cwd=worktree)
        self._git("commit", "-m", "illegal candidate", cwd=worktree)
        with self.assertRaises(RuntimeError):
            self.service.complete_update(task["task_id"])
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_interrupted_workspace_preparation_resumes_same_task(self):
        self._submit_batch()
        with patch.object(self.service.writer, "prepare", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.service.begin_update()
        task_id = self.service.update_status()["task_id"]
        resumed = AssistantService(self.root).begin_update()
        self.assertEqual(resumed["task_id"], task_id)
        self.assertTrue(Path(resumed["worktree"]).is_dir())
        self.service.complete_update(task_id)

    def test_interrupted_after_publication_before_queue_ack_recovers_without_second_commit(self):
        task = self._begin()
        self._edit(task)
        with patch.object(self.service, "_finish", side_effect=OSError("interrupted before acknowledgement")):
            with self.assertRaises(OSError):
                self.service.complete_update(task["task_id"], "done")
        head = self._git("rev-parse", "HEAD")
        operation = SemanticOperationStore(self.root).read(self.service.writer.read(task["task_id"]).operation_id)
        self.assertEqual(operation.phase, "committed")
        status = AssistantService(self.root).update_status(task["task_id"])
        self.assertEqual(status["landed_commit"], head)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(self.service.context("reader")["pending_count"], 0)

    def test_interrupted_after_partial_ack_recovers_remaining_participants(self):
        task = self._begin()
        original = self.service.store._write

        def fail_one(session_id, state):
            if session_id == "conversation-b" and state.status == "succeeded":
                raise OSError("interrupted during acknowledgement")
            return original(session_id, state)

        with patch.object(self.service.store, "_write", side_effect=fail_one):
            with self.assertRaises(OSError):
                self.service.complete_update(task["task_id"])
        head = self._git("rev-parse", "HEAD")
        self.service.complete_update(task["task_id"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(self.service.context("reader")["pending_count"], 0)

    def test_rejects_path_like_task_id_and_runtime_symlink(self):
        with self.assertRaises(ValueError):
            self.service.complete_update("../../escape")
        external = self.root / "external"
        external.mkdir()
        runtime = self.root / ".runtime"
        runtime.mkdir(exist_ok=True)
        (runtime / "assistant").rename(runtime / "assistant-saved")
        (runtime / "assistant").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            AssistantService(self.root)
        self.assertEqual(list(external.iterdir()), [])

    def test_interrupted_submit_keeps_exact_request_identity(self):
        with patch.object(self.service.store, "submit", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.service.submit("conversation", "request", "exact evidence")
        saved = AssistantService(self.root).submit("conversation", "request", "exact evidence")
        self.assertTrue(saved["saved"])
        with self.assertRaises(ValueError):
            self.service.submit("conversation", "request", "different evidence")
        self.assertEqual(len(self.service.store.read("conversation").pending), 1)
