import hashlib
import json
import os
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.learning import (
    LEARNING_DOMAINS,
    withdraw_learning_candidate,
    list_learning_candidates,
    record_learning_processed,
    submit_learning,
)
from rightmemory.sync import SyncManager, _is_sync_path
from rightmemory.semantic_operation import SemanticOperationStore
from tests.sync_test_base import SyncTestBase


class LearningSyncTests(SyncTestBase):
    def _manager(self):
        return SyncManager(SyncConfig(memory_root=self.device, enabled=True))

    def _candidate_path(self, root, candidate_id):
        return root / "learning" / "candidates" / f"{candidate_id}.json"

    def _seed_candidate(self):
        candidate_id = submit_learning(self.device, "session", "Evidence.", request_id="request").candidate_id
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")
        return candidate_id

    def _commit_learning(self, root):
        self._git(root, "add", "-f", "--", "learning")
        self._git(root, "commit", "-m", "curate learning")

    def _process_all(self, root, candidate_id):
        for domain in LEARNING_DOMAINS:
            record_learning_processed(root, domain, [candidate_id])

    def test_learning_sync_paths_share_store_grammar(self):
        candidate_id = "a" * 64
        for path in (
            f"learning/candidates/{candidate_id}.json",
            f"learning/processed/guidance/{candidate_id}.json",
            f"learning/processed/skills/{candidate_id}.json",
            "learning/guidance/项目/习惯 偏好.md",
            "learning/experiences/export/case.md",
            "learning/skills/export/SKILL.md",
            "learning/skills/export/assets/template.bin",
        ):
            self.assertTrue(_is_sync_path(path), path)
        for path in (
            "learning/candidates/malformed.json",
            "learning/processed/other/item.json",
            "learning/guidance/rules.json",
            "learning/unknown.md",
            "learning/skills/export/.git/config",
        ):
            self.assertFalse(_is_sync_path(path), path)

    def test_pull_transports_raw_sources_curated_content_and_regular_assets(self):
        submission = submit_learning(self.other, "session", "Evidence.")
        paths = {
            "learning/guidance/项目/rules.md": b"guidance",
            "learning/experiences/export/case.md": b"case",
            "learning/skills/export/SKILL.md": b"skill",
            "learning/skills/export/assets/template.bin": b"\x00\xff",
        }
        for relative, content in paths.items():
            path = self.other / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "synced", result.message)
        self.assertEqual(list_learning_candidates(self.device)[0].candidate_id, submission.candidate_id)
        for relative, content in paths.items():
            self.assertEqual((self.device / relative).read_bytes(), content)
        self._assert_no_sync_candidates()

    def test_independent_domain_receipts_merge_and_retire_completed_source(self):
        candidate_id = self._seed_candidate()
        record_learning_processed(self.device, "memory", [candidate_id])
        record_learning_processed(self.device, "guidance", [candidate_id])
        self._commit_learning(self.device)
        record_learning_processed(self.other, "skills", [candidate_id])
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "synced", result.message)
        for domain in LEARNING_DOMAINS:
            self.assertEqual(list_learning_candidates(self.device, domain=domain), ())
        self.assertFalse(self._candidate_path(self.device, candidate_id).exists())
        self.assertEqual(list_learning_candidates(self.device), ())
        self.assertEqual(self._git(self.device, "status", "--short"), "")

    def test_concurrent_identical_submissions_merge_differing_capture_times(self):
        local = submit_learning(self.device, "session", "Evidence.", request_id="request")
        remote = submit_learning(self.other, "session", "Evidence.", request_id="request")
        self.assertEqual(local.candidate_id, remote.candidate_id)
        local_candidate = list_learning_candidates(self.device)[0]
        remote_candidate = list_learning_candidates(self.other)[0]
        self.assertNotEqual(local_candidate.submitted_at, remote_candidate.submitted_at)
        record_learning_processed(self.device, "guidance", [local.candidate_id])
        self._commit_learning(self.device)
        record_learning_processed(self.other, "skills", [local.candidate_id])
        self._commit_learning(self.other)
        self._git(self.other, "push")
        repair_calls = []

        result = self._manager().pull(repair=lambda *args: repair_calls.append(args) or "repaired")

        self.assertEqual(result.status, "synced", result.message)
        self.assertEqual(list_learning_candidates(self.device)[0], local_candidate)
        self.assertEqual(repair_calls, [])
        self.assertEqual(list_learning_candidates(self.device, domain="guidance"), ())
        self.assertEqual(list_learning_candidates(self.device, domain="skills"), ())

    def test_stale_capture_does_not_resurrect_a_completed_candidate(self):
        candidate_id = submit_learning(self.device, "session", "Evidence.", request_id="request").candidate_id
        self._process_all(self.device, candidate_id)
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        stale = submit_learning(self.other, "session", "Evidence.", request_id="request")
        self.assertEqual(stale.candidate_id, candidate_id)

        result = SyncManager(SyncConfig(memory_root=self.other, enabled=True)).pull()

        self.assertEqual(result.status, "synced", result.message)
        self.assertFalse(self._candidate_path(self.other, candidate_id).exists())
        self.assertEqual(list_learning_candidates(self.other), ())
        repeated = submit_learning(self.other, "session", "Evidence.", request_id="request")
        self.assertEqual(repeated.status, "completed")

    def test_withdrawn_identity_dominates_a_stale_capture_without_false_processed_receipts(self):
        candidate_id = submit_learning(self.device, "session", "Evidence.", request_id="request").candidate_id
        withdraw_learning_candidate(self.device, candidate_id)
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        submit_learning(self.other, "session", "Evidence.", request_id="request")

        result = SyncManager(SyncConfig(memory_root=self.other, enabled=True)).pull()

        self.assertEqual(result.status, "synced", result.message)
        self.assertFalse(self._candidate_path(self.other, candidate_id).exists())
        self.assertEqual(list((self.other / "learning" / "processed").glob("*/*.json")), [])
        self.assertEqual(submit_learning(self.other, "session", "Evidence.", request_id="request").status, "withdrawn")

    def test_completed_identity_rejects_a_conflicting_stale_capture(self):
        candidate_id = submit_learning(self.device, "session", "Original.", request_id="request").candidate_id
        self._process_all(self.device, candidate_id)
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        submit_learning(self.other, "session", "Different.", request_id="request")
        before = self._git(self.other, "rev-parse", "HEAD")

        result = SyncManager(SyncConfig(memory_root=self.other, enabled=True)).pull()

        self.assertEqual(result.status, "error")
        self.assertIn("receipts do not match", result.message)
        self.assertEqual(self._git(self.other, "rev-parse", "HEAD"), before)

    def test_withdrawal_receipt_cannot_be_removed_to_resurrect_evidence(self):
        candidate_id = self._seed_candidate()
        withdraw_learning_candidate(self.device, candidate_id)
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")
        path = self.other / "learning" / "withdrawn" / f"{candidate_id}.json"
        path.unlink()
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertTrue((self.device / "learning" / "withdrawn" / f"{candidate_id}.json").exists())

    def test_conflicting_submissions_with_same_request_fail_without_model_repair(self):
        local = submit_learning(self.device, "session", "Local.", request_id="request")
        remote = submit_learning(self.other, "session", "Remote.", request_id="request")
        self.assertEqual(local.candidate_id, remote.candidate_id)
        self._git(self.other, "push")
        head = self._git(self.device, "rev-parse", "HEAD")
        original = self._candidate_path(self.device, local.candidate_id).read_bytes()
        repair_calls = []

        result = self._manager().pull(repair=lambda *args: repair_calls.append(args) or "repaired")

        self.assertEqual(result.status, "error")
        self.assertIn("conflicting learning evidence", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), head)
        self.assertEqual(self._candidate_path(self.device, local.candidate_id).read_bytes(), original)
        self._assert_no_sync_candidates()

    def test_clean_fast_forward_cannot_replace_recorded_evidence(self):
        candidate_id = self._seed_candidate()
        path = self._candidate_path(self.other, candidate_id)
        data = json.loads(path.read_text())
        data["evidence"] = "Rewritten."
        data["evidence_sha256"] = hashlib.sha256(data["evidence"].encode()).hexdigest()
        path.write_text(json.dumps(data))
        self._commit_learning(self.other)
        self._git(self.other, "push")
        head = self._git(self.device, "rev-parse", "HEAD")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertIn("conflicting learning evidence", result.message)
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), head)
        self.assertEqual(list_learning_candidates(self.device)[0].evidence, "Evidence.")

    def test_unprocessed_raw_deletion_is_rejected_even_without_receipts(self):
        candidate_id = self._seed_candidate()
        self._candidate_path(self.other, candidate_id).unlink()
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertIn("neither processed by all", result.message)
        self.assertTrue(self._candidate_path(self.device, candidate_id).is_file())

    def test_processed_raw_retirement_is_admitted_and_retry_stays_idempotent(self):
        candidate_id = self._seed_candidate()
        self._process_all(self.other, candidate_id)
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "synced", result.message)
        self.assertFalse(self._candidate_path(self.device, candidate_id).exists())
        repeated = submit_learning(self.device, "session", "Evidence.", request_id="request")
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.pending_domains, ())

    def test_retirement_conflict_with_equivalent_capture_time_change_keeps_deletion(self):
        candidate_id = self._seed_candidate()
        local_path = self._candidate_path(self.device, candidate_id)
        data = json.loads(local_path.read_text())
        data["submitted_at"] = "2026-01-01T00:00:00Z"
        local_path.write_text(json.dumps(data))
        self._commit_learning(self.device)
        self._process_all(self.other, candidate_id)
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "synced", result.message)
        self.assertFalse(local_path.exists())

    def test_added_then_deleted_unprocessed_source_cannot_hide_in_incoming_history(self):
        candidate_id = submit_learning(self.other, "session", "Evidence.").candidate_id
        self._candidate_path(self.other, candidate_id).unlink()
        self._commit_learning(self.other)
        self._git(self.other, "push")
        head = self._git(self.device, "rev-parse", "HEAD")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertIn("neither processed by all", result.message)
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), head)

    def test_push_rejects_unprocessed_retirement_before_sending_history(self):
        remote_head = self._git(self.remote, "rev-parse", "main")
        candidate_id = submit_learning(self.device, "session", "Evidence.").candidate_id
        self._candidate_path(self.device, candidate_id).unlink()
        self._commit_learning(self.device)

        result = self._manager().push()

        self.assertEqual(result.status, "error")
        self.assertIn("neither processed by all", result.message)
        self.assertEqual(self._git(self.remote, "rev-parse", "main"), remote_head)

    def test_processed_receipt_cannot_be_removed_to_make_evidence_pending_again(self):
        candidate_id = self._seed_candidate()
        self._process_all(self.device, candidate_id)
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")
        path = self.other / "learning" / "processed" / "guidance" / f"{candidate_id}.json"
        path.unlink()
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertTrue("receipts cannot be deleted" in result.message or "missing unprocessed evidence" in result.message)
        self.assertEqual(list_learning_candidates(self.device, domain="guidance"), ())

    def test_ignored_untracked_unicode_learning_file_is_dirty_and_preserved(self):
        (self.device / ".gitignore").write_text("learning/\n")
        self._git(self.device, "add", ".gitignore")
        self._git(self.device, "commit", "-m", "legacy ignore")
        relative = "learning/guidance/项目/习惯 偏好.md"
        path = self.device / relative
        path.parent.mkdir(parents=True)
        path.write_text("local pending content")
        head = self._git(self.device, "rev-parse", "HEAD")

        result = self._manager().pull()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, [relative])
        self.assertEqual(path.read_text(), "local pending content")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), head)

    def test_sync_repair_cannot_invent_processed_receipts(self):
        candidate_id = self._seed_candidate()
        self._create_remote_local_conflict()
        head = self._git(self.device, "rev-parse", "HEAD")

        def repair(candidate, result, operation_id):
            record_learning_processed(candidate, "guidance", [candidate_id])
            self._git(candidate, "add", "-f", "learning")
            self._commit_candidate_resolution(candidate)
            return "resolved"

        result = self._manager().pull(repair=repair)

        self.assertEqual(result.status, "error")
        self.assertIn("immutable learning evidence or processed receipts", result.message)
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), head)
        self.assertEqual(len(list_learning_candidates(self.device, domain="guidance")), 1)

    def test_prepared_repair_retains_learning_snapshot_and_resumes_without_model(self):
        candidate_id = self._seed_candidate()
        self._create_remote_local_conflict()
        original = self._candidate_path(self.device, candidate_id).read_bytes()
        manager = self._manager()
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "resolved"

        with patch.object(manager, "_publish_candidate", side_effect=RuntimeError("publication interrupted")):
            first = manager.pull(repair=repair)
        records = SemanticOperationStore(self.device).list_outstanding_records()
        self.assertEqual(first.status, "error")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0].input_data["learning_input_sha256"]), 64)

        resumed = manager.pull(repair=lambda *args: self.fail("prepared repair must not rerun"))

        self.assertEqual(resumed.status, "synced", resumed.message)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self._candidate_path(self.device, candidate_id).read_bytes(), original)
        self._assert_no_sync_candidates()

    def test_sync_repair_can_resolve_curated_unicode_content_without_changing_sources(self):
        candidate_id = self._seed_candidate()
        relative = "learning/guidance/项目/习惯.md"
        path = self.device / relative
        path.parent.mkdir(parents=True)
        path.write_text("initial\n")
        self._commit_learning(self.device)
        self._git(self.device, "push", "origin", "HEAD:main")
        self._git(self.other, "pull", "--ff-only")
        path.write_text("local\n")
        self._commit_learning(self.device)
        (self.other / relative).write_text("remote\n")
        self._commit_learning(self.other)
        self._git(self.other, "push")
        original = self._candidate_path(self.device, candidate_id).read_bytes()
        repair_calls = []

        def repair(candidate, result, operation_id):
            repair_calls.append(result.files)
            (candidate / relative).write_text("resolved\n")
            self._git(candidate, "add", "-f", "--", relative)
            self._git(candidate, "commit", "--no-edit")
            return "resolved"

        result = self._manager().pull(repair=repair)

        self.assertEqual(result.status, "synced", result.message)
        self.assertEqual(repair_calls, [[relative]])
        self.assertEqual(path.read_text(), "resolved\n")
        self.assertEqual(self._candidate_path(self.device, candidate_id).read_bytes(), original)

    def test_malformed_raw_source_never_reaches_model_alongside_memory_conflict(self):
        candidate_id = self._seed_candidate()
        self._candidate_path(self.other, candidate_id).write_text("{broken")
        self._commit_learning(self.other)
        self._create_remote_local_conflict()
        calls = []

        result = self._manager().pull(repair=lambda *args: calls.append(args) or "repaired")

        self.assertEqual(result.status, "error")
        self.assertEqual(calls, [])
        self.assertEqual(list_learning_candidates(self.device)[0].evidence, "Evidence.")

    def test_nonregular_skill_asset_is_rejected_before_publication(self):
        if os.name == "nt":
            self.skipTest("symlink semantics require POSIX")
        skill = self.other / "learning" / "skills" / "export"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill")
        (skill / "asset.md").symlink_to("../../../../outside")
        self._commit_learning(self.other)
        self._git(self.other, "push")

        result = self._manager().pull()

        self.assertEqual(result.status, "error")
        self.assertFalse((self.device / "learning").exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
