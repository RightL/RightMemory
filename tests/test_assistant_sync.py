from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant import AssistantService
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.sync import SyncManager
from rightmemory.update_queue import UpdateQueueStore
from rightmemory.update_queue_git import (
    GitUpdateQueueCoordinator,
    UpdateQueueLeaseLost,
    UpdateQueueUnavailable,
)
from rightmemory.update_record import UpdateRecordStore
from tests.update_queue_git_base import GitUpdateQueueTestBase


class AssistantSyncTests(GitUpdateQueueTestBase):
    def setUp(self):
        super().setUp()
        for root in (self.first, self.second):
            (root / "rightmemory.toml").write_text(
                "[sync]\nenabled = true\n\n[update.async]\n"
                "trigger_candidates = 1\ntarget_batch_candidates = 1\n",
                encoding="utf-8",
            )
            exclude = root / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write("\nrightmemory.toml\n")
        for target in (
            "rightmemory.assistant.build_assistant_update_instructions",
            "rightmemory.assistant_prompt.build_assistant_retrieve_instructions",
        ):
            instructions = patch(target, return_value="test")
            instructions.start()
            self.addCleanup(instructions.stop)
        self.service = AssistantService(self.first)
        self.other = AssistantService(self.second)

    def _begin(self):
        submission = self.service.submit("first-session", "request-one", "retain the project constraint")
        self.assertTrue(submission["saved"])
        self.assertTrue(submission["update"]["ready"])
        begin = self.service.begin_update()
        self.assertEqual(begin["status"], "ready")
        return submission, begin

    def _commit_memory(self, begin, *, key="two", value="native assistant update"):
        worktree = Path(begin["worktree"])
        with (worktree / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write(f"- `{key}` {value} → []\n")
        self._git(worktree, "add", "MEMORY.md")
        self._git(worktree, "commit", "-m", "memory: native assistant edit")

    def _operation(self, begin):
        task = self.service.writer.read(begin["task_id"])
        return SemanticOperationStore(self.first).read(task.operation_id)

    def _remote_head(self):
        return self._git(self.remote, "rev-parse", "refs/heads/main")

    def test_shared_submission_native_update_and_other_clone_retrieve(self):
        submission, begin = self._begin()
        active = self.service.begin_update()
        self.assertEqual(active["task_id"], begin["task_id"])
        self.assertFalse(active["claimed"])
        self._commit_memory(begin)

        completed = self.service.complete_update(begin["task_id"], output="retained the constraint")
        retrieved = self.other.context("independent-retrieve")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["sync"], "published")
        self.assertEqual(completed["changed_paths"], ["MEMORY.md"])
        self.assertEqual(retrieved["memory_commit"], completed["landed_commit"])
        self.assertEqual(retrieved["pending_count"], 0)
        self.assertIn(str(self.second / "MEMORY.md"), retrieved["source_files"])
        self.assertIn("native assistant update", (self.second / "MEMORY.md").read_text())
        record = UpdateRecordStore(self.second).read(begin["batch_id"])
        self.assertEqual([item.uid for item in record.candidates], [submission["candidate_uid"]])
        snapshot = UpdateQueueStore(self.second).snapshot()
        self.assertEqual(snapshot.candidates, ())
        self.assertIsNone(snapshot.lease)
        self.assertFalse(Path(begin["worktree"]).exists())
        remote_head = self._remote_head()
        repeated = AssistantService(self.first).complete_update(begin["task_id"])
        self.assertEqual(repeated["landed_commit"], completed["landed_commit"])
        self.assertEqual(self._remote_head(), remote_head)

    def test_default_worker_on_same_root_cannot_reclaim_assistant_active_lease(self):
        _submission, begin = self._begin()
        worker = GitUpdateQueueCoordinator(self.service.sync_config)

        worker_claim = worker.claim_next(**self.service.batch_options)

        self.assertIsNone(worker_claim.claim)
        self.assertIsNotNone(worker_claim.next_attempt_at)
        native_claim = AssistantService(self.first)._coordinator().claim_next(**self.service.batch_options)
        self.assertIsNotNone(native_claim.claim)
        self.assertEqual(native_claim.claim.batch_id, begin["batch_id"])
        repeated = AssistantService(self.first).begin_update()
        self.assertEqual(repeated["task_id"], begin["task_id"])
        self.assertFalse(repeated["claimed"])

    def test_failed_publication_preserves_prepared_task_and_retry_needs_no_native_workspace(self):
        _submission, begin = self._begin()
        self._commit_memory(begin)
        start_head = self._git(self.first, "rev-parse", "HEAD")
        remote_head = self._remote_head()
        with patch.object(GitUpdateQueueCoordinator, "_push_cas", return_value=False):
            with self.assertRaises(UpdateQueueUnavailable):
                self.service.complete_update(begin["task_id"], output="fixed completion")
            prepared = self._operation(begin)
            self.assertEqual(prepared.phase, "prepared")
            with self.assertRaises(UpdateQueueUnavailable):
                AssistantService(self.first).begin_update(retry_task=begin["task_id"])
        self.assertEqual(self._git(self.first, "rev-parse", "HEAD"), start_head)
        self.assertEqual(self._remote_head(), remote_head)
        self.assertEqual(self.service.update_status()["task_id"], begin["task_id"])
        self._git(self.first, "worktree", "remove", begin["worktree"])

        retried = AssistantService(self.first).begin_update(retry_task=begin["task_id"])

        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["task_id"], begin["task_id"])
        self.assertEqual(retried["output"], "fixed completion")
        self.assertEqual(self._operation(begin).outcome.metadata["candidate_commit"],
                         prepared.outcome.metadata["candidate_commit"])
        self.assertEqual((self.first / "MEMORY.md").read_text().count("native assistant update"), 1)
        landed_operations = self._git(
            self.first, "log", "--format=%H", "--fixed-strings",
            f"--grep=RightMemory-Operation: {prepared.operation_id}", "HEAD",
        ).splitlines()
        self.assertEqual(len(landed_operations), 1)
        self.assertEqual(self._remote_head(), retried["landed_commit"])

    def test_published_submission_stays_local_until_its_queue_state_is_admitted(self):
        initial_head = self._git(self.first, "rev-parse", "HEAD")
        with patch.object(SyncManager, "_publish_candidate", side_effect=OSError("admission interrupted")):
            submitted = self.service.submit("first-session", "request-one", "retain pending evidence")
        self.assertTrue(submitted["saved"])
        self.assertEqual(submitted["queue"]["status"], "pending")
        self.assertEqual(self._git(self.first, "rev-parse", "HEAD"), initial_head)
        self.assertEqual(
            [item.uid for item in UpdateQueueStore(self.first).outbox_candidates()],
            [submitted["candidate_uid"]],
        )
        remote_head = self._remote_head()
        self.assertNotEqual(remote_head, initial_head)

        retrieved = AssistantService(self.first).context("pending-retrieve")

        self.assertEqual(retrieved["pending_count"], 1)
        self.assertTrue(retrieved["update"]["ready"])
        self.assertEqual(UpdateQueueStore(self.first).outbox_candidates(), ())
        self.assertEqual(self._remote_head(), remote_head)

    def test_shared_landing_with_lost_receipt_recovers_without_a_second_publication(self):
        _submission, begin = self._begin()
        self._commit_memory(begin)
        with patch.object(self.service.writer, "complete_external", side_effect=OSError("receipt interrupted")):
            with self.assertRaisesRegex(OSError, "receipt interrupted"):
                self.service.complete_update(begin["task_id"], output="published once")
        remote_head = self._remote_head()
        self.assertEqual(self._git(self.first, "rev-parse", "HEAD"), remote_head)
        self.assertEqual(self._operation(begin).phase, "prepared")
        self._git(self.first, "worktree", "remove", begin["worktree"])

        recovered = AssistantService(self.first).begin_update(retry_task=begin["task_id"])

        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["landed_commit"], remote_head)
        self.assertEqual(recovered["output"], "published once")
        self.assertEqual(recovered["changed_paths"], ["MEMORY.md"])
        self.assertEqual(self._remote_head(), remote_head)
        self.assertEqual(self._operation(begin).phase, "committed")
        self.assertEqual((self.first / "MEMORY.md").read_text().count("native assistant update"), 1)

    def test_remote_lease_takeover_fences_old_native_task(self):
        _submission, old = self._begin()
        self._commit_memory(old, key="old-owner", value="obsolete attempt")
        self.assertEqual(self.other.begin_update()["status"], "not_ready")
        expires = datetime.fromisoformat(old["expires_at"]) + timedelta(seconds=1)
        with (
            patch("rightmemory.update_queue_git._utc_now", return_value=expires),
            patch("rightmemory.assistant._now", return_value=expires),
        ):
            replacement = self.other.begin_update()
        self.assertEqual(replacement["status"], "ready")
        self.assertEqual(replacement["batch_id"], old["batch_id"])
        self.assertNotEqual(replacement["task_id"], old["task_id"])
        remote_head = self._remote_head()
        with self.assertRaises(UpdateQueueLeaseLost):
            self.service.complete_update(old["task_id"])
        self.assertEqual(self._remote_head(), remote_head)
        self.assertNotIn("obsolete attempt", self._git(self.remote, "show", "main:MEMORY.md"))

        self._commit_memory(replacement, key="new-owner", value="current owner result")
        completed = self.other.complete_update(replacement["task_id"])

        self.assertEqual(completed["status"], "completed")
        obsolete = self.service.complete_update(old["task_id"])
        self.assertEqual(obsolete["status"], "completed_elsewhere")
        self.assertEqual(obsolete["changed_paths"], [])
        self.assertEqual(self._remote_head(), completed["landed_commit"])
        memory = self._git(self.remote, "show", "main:MEMORY.md")
        self.assertIn("current owner result", memory)
        self.assertNotIn("obsolete attempt", memory)

    def test_expired_ordinary_begin_keeps_native_task_but_does_not_bypass_foreign_lease(self):
        _submission, old = self._begin()
        self._commit_memory(old, key="old-owner", value="retained native attempt")
        expires = datetime.fromisoformat(old["expires_at"]) + timedelta(seconds=1)
        with (
            patch("rightmemory.update_queue_git._utc_now", return_value=expires),
            patch("rightmemory.assistant._now", return_value=expires),
        ):
            resumed = AssistantService(self.first).begin_update()
            self.assertFalse(resumed["claimed"])
            self.assertEqual(resumed["task_id"], old["task_id"])
            self.assertEqual(resumed["worktree"], old["worktree"])
            replacement = self.other.begin_update()
            self.assertTrue(replacement["claimed"])
            remote_head = self._remote_head()
            with self.assertRaises(UpdateQueueLeaseLost):
                self.service.complete_update(old["task_id"])
            resumed_after_takeover = AssistantService(self.first).begin_update()
        self.assertFalse(resumed_after_takeover["claimed"])
        self.assertEqual(resumed_after_takeover["task_id"], old["task_id"])
        self.assertTrue(Path(old["worktree"]).is_dir())
        self.assertEqual(self._remote_head(), remote_head)
        self.assertNotIn("retained native attempt", self._git(self.remote, "show", "main:MEMORY.md"))

    def test_ordinary_pull_conflict_stops_update_without_internal_model_repair(self):
        for root, value in ((self.first, "local fact"), (self.second, "remote fact")):
            (root / "MEMORY.md").write_text(f"# Domain\n\n- `one` {value} → []\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "memory: concurrent fact")
        self._git(self.second, "push")
        before_head = self._git(self.first, "rev-parse", "HEAD")
        before_memory = (self.first / "MEMORY.md").read_bytes()
        with patch("rightmemory.runtime.RightMemoryRuntime", side_effect=AssertionError("internal runtime started")):
            begin = self.service.begin_update()
            synced = self.service.sync("pull")

        self.assertEqual(begin["status"], "sync_required")
        self.assertEqual(begin["sync"]["status"], "conflict")
        self.assertEqual(synced["sync"]["status"], "conflict")
        self.assertEqual(self._git(self.first, "rev-parse", "HEAD"), before_head)
        self.assertEqual((self.first / "MEMORY.md").read_bytes(), before_memory)
        self.assertEqual(self._git(self.first, "status", "--porcelain"), "")
        self.assertEqual(SemanticOperationStore(self.first).list_records(), ())
