import json
import subprocess
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.sync import SyncManager
from tests.sync_test_base import SyncTestBase


class SyncPublicationTests(SyncTestBase):
    def test_clean_divergent_merge_lands_exact_candidate_commit(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `local` local fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        local_tip = self._git(self.device, "rev-parse", "HEAD")

        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Remote work {#remote-work}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "remote pursuit")
        remote_tip = self._git(self.other, "rev-parse", "HEAD")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        landed = self._git(self.device, "rev-parse", "HEAD")
        self.assertNotEqual(landed, local_tip)
        self.assertEqual(
            self._git(self.device, "show", "-s", "--format=%P", landed).split(),
            [local_tip, remote_tip],
        )
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_remote_non_sync_path_is_rejected_before_candidate_publication(self):
        start_head = self._git(self.device, "rev-parse", "HEAD")
        (self.other / "rightmemory.toml").write_text(
            "[sync]\nenabled = true\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "rightmemory.toml")
        self._git(self.other, "commit", "-m", "remote config")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, ["rightmemory.toml"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertFalse((self.device / "rightmemory.toml").exists())

    def test_malformed_remote_queue_fails_closed_without_model_repair(self):
        start_head = self._git(self.device, "rev-parse", "HEAD")
        relative = f"update_queue/candidates/{'a' * 32}.json"
        path = self.other / relative
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._git(self.other, "add", "-f", relative)
        self._git(self.other, "commit", "-m", "queue: add malformed candidate")
        self._git(self.other, "push")
        repair_calls = []

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
            repair=lambda *args: repair_calls.append(args) or "repaired"
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("invalid JSON", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertFalse((self.device / relative).exists())

    def test_queue_conflict_fails_closed_without_model_repair(self):
        relative = "update_queue/lease.json"
        for root, owner in ((self.device, "local"), (self.other, "remote")):
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"owner": owner}) + "\n", encoding="utf-8")
            self._git(root, "add", "-f", relative)
            self._git(root, "commit", "-m", f"queue: {owner} lease")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        local_bytes = (self.device / relative).read_bytes()
        self._git(self.other, "push")
        repair_calls = []

        with patch("rightmemory.sync.validate_update_queue", return_value=[]):
            result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
                repair=lambda *args: repair_calls.append(args) or "repaired"
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("coordination conflict", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / relative).read_bytes(), local_bytes)

    def test_malformed_queue_with_memory_conflict_never_reaches_model_repair(self):
        relative = f"update_queue/candidates/{'a' * 32}.json"
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote durable fact → []\n",
            encoding="utf-8",
        )
        path = self.other / relative
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "add", "-f", relative)
        self._git(self.other, "commit", "-m", "remote memory and malformed queue")
        self._git(self.other, "push")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory edit")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        repair_calls = []

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
            repair=lambda *args: repair_calls.append(args) or "repaired"
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("invalid JSON", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)

    def test_repair_exception_leaves_active_checkout_unchanged(self):
        self._create_remote_local_conflict()
        start_head = self._git(self.device, "rev-parse", "HEAD")
        start_bytes = (self.device / "MEMORY.md").read_bytes()

        def fail(candidate, result, operation_id):
            self.assertIn("<<<<<<<", (candidate / "MEMORY.md").read_text(encoding="utf-8"))
            raise RuntimeError("model unavailable")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
            repair=fail
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), start_bytes)
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_running_repair_commit_is_adopted_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        original_prepare = SemanticOperationStore.prepare_outcome
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash before prepared receipt")
            return original_prepare(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "prepare_outcome", fail_once):
            first = manager.pull(repair=repair)
        second = manager.pull(
            repair=lambda *_args: self.fail(
                "durable candidate commit must prevent a second model run"
            )
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(len(calls), 1)
        self.assertIn("one-remote", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_prepared_repair_resumes_publication_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        with patch.object(
            manager,
            "_publish_candidate",
            side_effect=RuntimeError("crash before publish"),
        ):
            first = manager.pull(repair=repair)
        second = manager.pull(
            repair=lambda *_args: self.fail("prepared repair must not run the model again")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(len(calls), 1)

    def test_publication_crash_is_recognized_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        original_complete = SemanticOperationStore.complete_commit
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash after fast-forward")
            return original_complete(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "complete_commit", fail_once):
            first = manager.pull(repair=repair)
        self._assert_no_sync_candidates()
        published_head = self._git(self.device, "rev-parse", "HEAD")
        second = manager.pull(
            repair=lambda *_args: self.fail("published repair must not run the model again")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), published_head)
        self.assertEqual(len(calls), 1)
        self._assert_no_sync_candidates()

    def test_durable_no_change_conflict_does_not_rerun_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def no_change(candidate, result, operation_id):
            calls.append(operation_id)
            return "no safe repair"

        first = manager.pull(repair=no_change)
        second = manager.pull(repair=no_change)

        self.assertEqual(first.status, "conflict")
        self.assertEqual(second.status, "conflict")
        self.assertEqual(len(calls), 1)
        record = SemanticOperationStore(self.device).read(calls[0])
        self.assertIsNotNone(record)
        self.assertEqual(record.phase, "no_change")

    def test_no_change_completion_crash_cleans_candidate_and_does_not_rerun_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def no_change(candidate, result, operation_id):
            calls.append(operation_id)
            return "no safe repair"

        original_complete = SemanticOperationStore.complete_no_change
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash after no-change preparation")
            return original_complete(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "complete_no_change", fail_once):
            first = manager.pull(repair=no_change)
        self._assert_no_sync_candidates()
        second = manager.pull(
            repair=lambda *_args: self.fail("prepared no-change must not rerun the model")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "conflict")
        self.assertEqual(len(calls), 1)
        record = SemanticOperationStore(self.device).read(calls[0])
        self.assertIsNotNone(record)
        self.assertEqual(record.phase, "no_change")
        self._assert_no_sync_candidates()

    def test_recorded_candidate_identity_uses_operation_digest_for_lease(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        digest = "b" * 64
        operation_id = f"sync-repair-{digest}"
        branch = f"rightmemory-sync-{digest}"
        relative = f".runtime/worktrees/sync-{digest}"
        path = self.device / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(self.device, "worktree", "add", "-b", branch, str(path), "HEAD")
        record = SemanticOperationStore(self.device).begin(
            operation_id,
            {
                "kind": "sync-repair",
                "role": "sync-reconciler",
                "candidate_branch": branch,
                "candidate_worktree": relative,
            },
        )

        candidate = manager._open_recorded_candidate(record)

        self.assertEqual(candidate.lease.identifier, digest)
        candidate.lease.release()
        manager._cleanup_recorded_candidate(record, require_removed=True)
        self.assertFalse(path.exists())
        self.assertEqual(self._git(self.device, "branch", "--list", branch), "")

    def test_corrupt_recorded_routing_cannot_remove_another_worktree(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        other_branch = "rightmemory-sync-other-live-operation"
        other_relative = ".runtime/worktrees/sync-other-live-operation"
        other_path = self.device / other_relative
        other_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(self.device, "worktree", "add", "-b", other_branch, str(other_path), "HEAD")

        def cleanup_other_worktree():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(other_path)],
                cwd=self.device,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            subprocess.run(
                ["git", "branch", "-D", other_branch],
                cwd=self.device,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.addCleanup(cleanup_other_worktree)
        record = SemanticOperationStore(self.device).begin(
            f"sync-repair-{'c' * 64}",
            {
                "kind": "sync-repair",
                "role": "sync-reconciler",
                "candidate_branch": other_branch,
                "candidate_worktree": other_relative,
            },
        )

        with self.assertRaisesRegex(RuntimeError, "does not match its operation id"):
            manager._cleanup_recorded_candidate(record, require_removed=True)

        self.assertTrue(other_path.is_dir())
        self.assertIn(other_branch, self._git(self.device, "branch", "--list", other_branch))

    def test_active_head_race_blocks_publication_without_overwrite(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))

        def repair(candidate, result, operation_id):
            self._commit_candidate_resolution(candidate)
            (self.device / "MEMORY.md").write_text(
                "# Domain\n\n- `external` independently committed → []\n",
                encoding="utf-8",
            )
            self._git(self.device, "add", "MEMORY.md")
            self._git(self.device, "commit", "-m", "external active change")
            return "repaired"

        result = manager.pull(repair=repair)

        self.assertEqual(result.status, "error")
        self.assertIn("external", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertNotIn("one-remote", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
