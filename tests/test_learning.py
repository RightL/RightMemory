import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.learning import (
    LEARNING_DOMAINS,
    learning_batch_candidate_ids,
    write_learning_batch,
    is_learning_state_path,
    learning_evidence_sha256,
    retire_processed_learning,
    LearningClaim,
    learning_claims,
    write_learning_claim,
    withdraw_learning_candidate,
    is_learning_path,
    learning_candidate_files,
    learning_curated_files,
    learning_files,
    learning_readable_files,
    learning_validation_errors,
    learning_write_path,
    list_learning_candidates,
    record_learning_processed,
    submit_learning,
    validate_learning_candidate_deletion,
)
from tests.isolated_write_test_base import IsolatedWriteTestBase


class LearningStoreTests(IsolatedWriteTestBase):
    def test_admission_is_program_owned_and_retires_only_when_entire_pool_is_consumed(self):
        first = submit_learning(self.root, "first", "first").candidate_id
        second = submit_learning(self.root, "second", "second").candidate_id
        before = learning_evidence_sha256(self.root)
        path = write_learning_batch(self.root, "memory", [first, second])
        self.assertNotEqual(learning_evidence_sha256(self.root), before)
        self.assertIn(path, learning_files(self.root))
        self.assertNotIn(path, learning_readable_files(self.root))
        self.assertTrue(is_learning_path("learning/batches/memory.json"))
        self.assertTrue(is_learning_state_path("learning/batches/memory.json"))
        for domain in LEARNING_DOMAINS:
            self.assertFalse(learning_write_path(domain, "learning/batches/memory.json"))
        record_learning_processed(self.root, "memory", [first])
        self.assertEqual(learning_batch_candidate_ids(self.root, "memory"), (first, second))
        withdrawn_paths = withdraw_learning_candidate(self.root, second)
        self.assertIn(path, withdrawn_paths)
        self.assertFalse(path.exists())
        self.assertEqual(learning_validation_errors(self.root), [])

    def test_admission_validates_identity_fields_and_cannot_replace_active_pool(self):
        first = submit_learning(self.root, "first", "first").candidate_id
        second = submit_learning(self.root, "second", "second").candidate_id
        path = write_learning_batch(self.root, "memory", [first])
        with self.assertRaisesRegex(ValueError, "already has an admitted batch"):
            write_learning_batch(self.root, "memory", [second])
        valid = path.read_bytes()
        invalid = [
            {"domain": "guidance", "candidate_ids": [first]},
            {"domain": "memory", "candidate_ids": []},
            {"domain": "memory", "candidate_ids": [first, first]},
            {"domain": "memory", "candidate_ids": ["f" * 64]},
            {"domain": "memory", "candidate_ids": first},
            {"domain": "memory", "candidate_ids": [first], "extra": True},
        ]
        for record in invalid:
            with self.subTest(record=record):
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertTrue(learning_validation_errors(self.root))
        path.write_bytes(valid)
        self.assertEqual(learning_validation_errors(self.root), [])

    def _candidate_path(self, candidate_id):
        return self.root / "learning" / "candidates" / f"{candidate_id}.json"

    def _receipt_path(self, domain, candidate_id):
        return self.root / "learning" / "processed" / domain / f"{candidate_id}.json"

    def _process_all(self, candidate_id):
        for domain in LEARNING_DOMAINS:
            record_learning_processed(self.root, domain, [candidate_id])

    def _commit_learning(self):
        self._git("add", "-f", "--", "learning")
        self._git("commit", "-m", "curate learning")

    def _assert_capture_cleanup(self):
        self.assertNotIn(".runtime/worktrees/", self._git("worktree", "list", "--porcelain"))
        self.assertEqual(self._git("branch", "--list", "rightmemory-learning-*"), "")

    def test_capture_is_committed_and_same_request_retry_is_idempotent(self):
        evidence = "  原始证据\n\n    code\n"
        first = submit_learning(self.root, "session", evidence, request_id="request")
        first_head = self._git("rev-parse", "HEAD")
        first_bytes = self._candidate_path(first.candidate_id).read_bytes()
        second = submit_learning(self.root, "session", evidence, request_id="request")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.candidate_id, second.candidate_id)
        self.assertEqual(first.pending_domains, LEARNING_DOMAINS)
        self.assertEqual(self._git("rev-parse", "HEAD"), first_head)
        self.assertEqual(self._candidate_path(first.candidate_id).read_bytes(), first_bytes)
        self.assertEqual(list_learning_candidates(self.root)[0].evidence, evidence)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_capture_cleanup()

    def test_implicit_identity_deduplicates_same_session_only(self):
        first = submit_learning(self.root, "session", "Evidence.")
        repeated = submit_learning(self.root, "session", "Evidence.")
        other_session = submit_learning(self.root, "other", "Evidence.")
        other_evidence = submit_learning(self.root, "session", "Different evidence.")
        self.assertEqual(first.candidate_id, repeated.candidate_id)
        self.assertFalse(repeated.created)
        self.assertEqual(len({first.candidate_id, other_session.candidate_id, other_evidence.candidate_id}), 3)

    def test_explicit_requests_are_distinct_from_content_identity_and_each_other(self):
        first = submit_learning(self.root, "session", "Evidence.", request_id="first")
        second = submit_learning(self.root, "session", "Evidence.", request_id="second")
        implicit = submit_learning(self.root, "session", "Evidence.")
        self.assertEqual(len({first.candidate_id, second.candidate_id, implicit.candidate_id}), 3)

    def test_same_request_with_changed_evidence_is_rejected(self):
        first = submit_learning(self.root, "session", "Evidence.", request_id="request")
        before = self._candidate_path(first.candidate_id).read_bytes()
        head = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "different evidence"):
            submit_learning(self.root, "session", "Changed.", request_id="request")
        self.assertEqual(self._candidate_path(first.candidate_id).read_bytes(), before)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)

    def test_three_domains_process_independently_then_raw_is_retired(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        before = self._candidate_path(candidate_id).read_bytes()
        for domain in ("guidance", "skills"):
            added = record_learning_processed(self.root, domain, [candidate_id])
            self.assertEqual(added, (self._receipt_path(domain, candidate_id),))
            self.assertEqual(record_learning_processed(self.root, domain, [candidate_id]), ())
            self.assertEqual(list_learning_candidates(self.root, domain=domain), ())
            self.assertEqual(self._candidate_path(candidate_id).read_bytes(), before)
            self.assertEqual(len(list_learning_candidates(self.root, domain="memory")), 1)
        self.assertEqual(learning_readable_files(self.root), (self._candidate_path(candidate_id),))
        changed = record_learning_processed(self.root, "memory", [candidate_id])
        self.assertEqual(set(changed), {self._receipt_path("memory", candidate_id), self._candidate_path(candidate_id)})
        self.assertEqual(learning_candidate_files(self.root), ())
        self.assertEqual(learning_readable_files(self.root), ())
        self.assertEqual(len(learning_files(self.root)), 3)
        self.assertEqual(list_learning_candidates(self.root), ())
        self.assertEqual(retire_processed_learning(self.root), ())
        self.assertEqual(learning_validation_errors(self.root), [])

    def test_completion_preserves_other_unprocessed_sources(self):
        first = submit_learning(self.root, "session", "One.").candidate_id
        unfinished = submit_learning(self.root, "session", "Two.").candidate_id
        self._process_all(first)
        record_learning_processed(self.root, "guidance", [unfinished])
        self.assertFalse(self._candidate_path(first).exists())
        self.assertTrue(self._candidate_path(unfinished).exists())
        self.assertEqual(retire_processed_learning(self.root), ())
        self.assertEqual(learning_validation_errors(self.root), [])

    def test_withdrawal_retires_raw_and_retries_do_not_resurrect_it(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.", request_id="request").candidate_id
        record_learning_processed(self.root, "memory", [candidate_id])
        changed = withdraw_learning_candidate(self.root, candidate_id)
        self.assertEqual(len(changed), 2)
        self.assertFalse(self._candidate_path(candidate_id).exists())
        self._commit_learning()
        repeated = submit_learning(self.root, "session", "Evidence.", request_id="request")
        self.assertEqual(repeated.status, "withdrawn")
        self.assertEqual(repeated.pending_domains, ())
        self.assertFalse(repeated.created)
        self.assertEqual(withdraw_learning_candidate(self.root, candidate_id), ())
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            record_learning_processed(self.root, "skills", [candidate_id])
        with self.assertRaisesRegex(ValueError, "different evidence"):
            submit_learning(self.root, "session", "Different.", request_id="request")

    def test_claims_prevent_withdrawal_and_are_not_retrieval_sources(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        claim = LearningClaim("memory", "a" * 32, "b" * 32, "memory-" + "c" * 32,
                              self._git("rev-parse", "HEAD"), (candidate_id,),
                              (list_learning_candidates(self.root)[0].evidence_sha256,))
        path = write_learning_claim(self.root, claim)
        self.assertEqual(learning_claims(self.root), (claim,))
        self.assertIn(path, learning_files(self.root))
        self.assertNotIn(path, learning_readable_files(self.root))
        with self.assertRaisesRegex(ValueError, "active curator task"):
            withdraw_learning_candidate(self.root, candidate_id)
        self.assertTrue(self._candidate_path(candidate_id).exists())

    def test_corrupt_claim_cannot_rebind_a_frozen_input(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        candidate = list_learning_candidates(self.root)[0]
        claim = LearningClaim("memory", "a" * 32, "b" * 32, "memory-" + "c" * 32,
                              self._git("rev-parse", "HEAD"), (candidate_id,), (candidate.evidence_sha256,))
        path = write_learning_claim(self.root, claim)
        original = json.loads(path.read_text())
        for hashes in ([], ["a" * 64], [["nested"]]):
            with self.subTest(hashes=hashes):
                path.write_text(json.dumps({**original, "candidate_hashes": hashes}))
                self.assertTrue(learning_validation_errors(self.root))
                with self.assertRaises(ValueError):
                    learning_claims(self.root)
        path.write_text(json.dumps(original))
        self.assertEqual(learning_validation_errors(self.root), [])
        with self.assertRaisesRegex(ValueError, "release the verified domain claim"):
            record_learning_processed(self.root, "memory", [candidate_id])
        self.assertFalse(self._receipt_path("memory", candidate_id).exists())

    @unittest.skipIf(os.name == "nt", "symlink semantics require POSIX")
    def test_capture_refuses_redirected_runtime_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            (self.root / ".runtime").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                submit_learning(self.root, "session", "Evidence.")
            self.assertEqual(list(outside.iterdir()), [])

    def test_completion_keeps_retry_identity_without_retaining_raw_evidence_in_receipts(self):
        submission = submit_learning(self.root, "session", "Full raw evidence.", request_id="request")
        candidate_id = submission.candidate_id
        self._process_all(candidate_id)
        self._commit_learning()
        head = self._git("rev-parse", "HEAD")
        retried = submit_learning(self.root, "session", "Full raw evidence.", request_id="request")
        self.assertFalse(retried.created)
        self.assertEqual(retried.candidate_id, candidate_id)
        self.assertEqual(retried.pending_domains, ())
        self.assertFalse(self._candidate_path(candidate_id).exists())
        self.assertEqual(list_learning_candidates(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        receipt = json.loads(self._receipt_path("guidance", candidate_id).read_text())
        self.assertNotIn("evidence", receipt)
        with self.assertRaisesRegex(ValueError, "different evidence"):
            submit_learning(self.root, "session", "Changed.", request_id="request")

    def test_delete_validator_uses_original_blob_even_after_source_removed(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.", request_id="request").candidate_id
        path = self._candidate_path(candidate_id)
        original = path.read_text()
        relative = path.relative_to(self.root).as_posix()
        with self.assertRaisesRegex(ValueError, "neither processed by all"):
            validate_learning_candidate_deletion(self.root, relative, original)
        self._process_all(candidate_id)
        validate_learning_candidate_deletion(self.root, relative, original)
        different_blob = json.loads(original)
        different_blob["evidence"] = "A substituted original."
        different_blob["evidence_sha256"] = hashlib.sha256(different_blob["evidence"].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "neither processed by all"):
            validate_learning_candidate_deletion(self.root, relative, json.dumps(different_blob))

    def test_missing_source_with_only_one_receipt_is_corruption(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        record_learning_processed(self.root, "guidance", [candidate_id])
        self._candidate_path(candidate_id).unlink()
        self.assertTrue(any("missing unprocessed evidence" in item for item in learning_validation_errors(self.root)))
        with self.assertRaisesRegex(ValueError, "missing unprocessed evidence"):
            list_learning_candidates(self.root, domain="skills")

    def test_receipts_must_match_the_recorded_evidence(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.", request_id="request").candidate_id
        self._process_all(candidate_id)
        path = self._receipt_path("guidance", candidate_id)
        data = json.loads(path.read_text())
        data["evidence_sha256"] = "f" * 64
        path.write_text(json.dumps(data))
        self.assertTrue(any("receipts do not match" in item for item in learning_validation_errors(self.root)))
        with self.assertRaises(ValueError):
            retire_processed_learning(self.root)
        self.assertFalse(self._candidate_path(candidate_id).exists())

    def test_record_batch_checks_all_inputs_before_writing(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        with self.assertRaisesRegex(ValueError, "unknown learning candidate"):
            record_learning_processed(self.root, "guidance", [candidate_id, "a" * 64])
        self.assertFalse(self._receipt_path("guidance", candidate_id).exists())

    def test_identity_is_deterministic_in_independent_repositories(self):
        first = submit_learning(self.root, "session", "Evidence.", request_id="request")
        with tempfile.TemporaryDirectory() as temporary:
            other_root = Path(temporary) / "other"
            self._git("clone", str(self._seed_root), str(other_root))
            self._git("config", "user.name", "Test User", cwd=other_root)
            self._git("config", "user.email", "test@example.com", cwd=other_root)
            second = submit_learning(other_root, "session", "Evidence.", request_id="request")
            self.assertEqual(first.candidate_id, second.candidate_id)
            self._process_all(first.candidate_id)
            for domain in LEARNING_DOMAINS:
                paths = record_learning_processed(other_root, domain, [second.candidate_id])
                self.assertEqual(paths[0].read_bytes(), self._receipt_path(domain, first.candidate_id).read_bytes())

    def test_capture_preserves_unrelated_staged_dirty_and_ignored_files(self):
        (self.root / "MEMORY.md").write_text("# Changed Memory\n")
        self._git("add", "MEMORY.md")
        (self.root / "PURSUITS.md").write_text("# Unstaged Pursuits\n")
        (self.root / ".gitignore").write_text("scratch.txt\n")
        (self.root / "scratch.txt").write_text("do not overwrite")
        staged_before = self._git("diff", "--cached")
        dirty_before = self._git("diff")
        submit_learning(self.root, "session", "Evidence.")
        self.assertEqual(self._git("diff", "--cached"), staged_before)
        self.assertEqual(self._git("diff"), dirty_before)
        self.assertEqual((self.root / "scratch.txt").read_text(), "do not overwrite")
        self.assertEqual(self._git("show", "HEAD:MEMORY.md"), self._git("show", f"{self.initial_head}:MEMORY.md"))
        self._assert_capture_cleanup()

    def test_retry_refuses_dirty_source_and_uncommitted_receipt(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        path = self._candidate_path(candidate_id)
        original = path.read_text()
        path.write_text(original + "\n")
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            submit_learning(self.root, "session", "Evidence.")
        path.write_text(original)
        record_learning_processed(self.root, "guidance", [candidate_id])
        with self.assertRaisesRegex(RuntimeError, "uncommitted changes"):
            submit_learning(self.root, "session", "Evidence.")

    def test_corrupt_json_hash_and_candidate_filename_are_rejected(self):
        candidate_id = submit_learning(self.root, "session", "Evidence.").candidate_id
        path = self._candidate_path(candidate_id)
        original = path.read_text()
        invalid_json = original.rstrip()[:-1] + ', "evidence": "duplicate"}'
        path.write_text(invalid_json)
        self.assertTrue(any("duplicate JSON field" in item for item in learning_validation_errors(self.root)))
        data = json.loads(original)
        data["evidence"] = "Changed behind its hash."
        path.write_text(json.dumps(data))
        self.assertTrue(any("evidence hash" in item for item in learning_validation_errors(self.root)))
        path.write_text(original)
        path.rename(path.with_name("f" * 64 + ".json"))
        self.assertTrue(any("filename" in item for item in learning_validation_errors(self.root)))

    def test_failed_capture_leaves_no_source_or_temporary_worktree(self):
        from rightmemory import learning
        original_git = learning._git

        def fail_commit(cwd, *args):
            if args[:1] == ("commit",):
                raise RuntimeError("simulated commit failure")
            return original_git(cwd, *args)

        with patch.object(learning, "_git", side_effect=fail_commit):
            with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
                submit_learning(self.root, "session", "Evidence.")
        self.assertFalse((self.root / "learning").exists())
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self._assert_capture_cleanup()


class LearningPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "memory"
        self.root.mkdir()

    def _write(self, relative, content=b"content"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_optional_learning_directory_can_be_absent(self):
        self.assertEqual(learning_validation_errors(self.root), [])
        self.assertEqual(learning_readable_files(self.root), ())
        self.assertEqual(list_learning_candidates(self.root), ())

    def test_inventory_includes_nested_curated_text_and_skill_assets(self):
        paths = [
            self._write("learning/guidance/project/rules.md"),
            self._write("learning/experiences/export/case.md"),
            self._write("learning/skills/export/SKILL.md"),
            self._write("learning/skills/export/assets/template.bin", b"\x00\xff"),
        ]
        self.assertEqual(learning_validation_errors(self.root), [])
        self.assertEqual(learning_candidate_files(self.root), ())
        self.assertEqual(learning_curated_files(self.root), tuple(sorted(paths)))
        self.assertEqual(learning_files(self.root), tuple(sorted(paths)))

    def test_skill_assets_require_a_regular_skill_entrypoint(self):
        self._write("learning/skills/export/assets/template.bin", b"\x00\xff")
        self.assertTrue(any("missing regular SKILL.md" in item for item in learning_validation_errors(self.root)))

    def test_curated_markdown_requires_utf8(self):
        self._write("learning/guidance/rules.md", b"\xff")
        self.assertTrue(learning_validation_errors(self.root))
        with self.assertRaises(ValueError):
            learning_curated_files(self.root)

    def test_unknown_and_malformed_storage_paths_are_rejected(self):
        paths = (
            ("learning/notes.md", "learning/notes.md"),
            ("learning/candidates/wrong.json", "learning/candidates/wrong.json"),
            ("learning/processed/other/item.json", "learning/processed/other"),
            ("learning/guidance/rules.txt", "learning/guidance/rules.txt"),
            ("learning/experiences/case.json", "learning/experiences/case.json"),
            ("learning/skills/bad.slug/SKILL.md", "learning/skills/bad.slug"),
        )
        for relative, diagnostic_path in paths:
            with self.subTest(relative=relative):
                path = self._write(relative)
                self.assertTrue(
                    any(item.startswith(f"{diagnostic_path}:") for item in learning_validation_errors(self.root))
                )
                path.unlink()

    @unittest.skipIf(os.name == "nt", "symlink semantics require POSIX")
    def test_inventory_rejects_symlinked_root_directories_and_assets(self):
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        (outside / "file.md").write_text("outside")
        paths = (
            "learning",
            "learning/guidance",
            "learning/guidance/rules.md",
            "learning/skills/export/SKILL.md",
            "learning/skills/export/assets/template.bin",
        )
        for relative in paths:
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside if not path.suffix else outside / "file.md")
                self.assertTrue(any("symbolic link" in item for item in learning_validation_errors(self.root)))
                with self.assertRaises(ValueError):
                    learning_readable_files(self.root)
                path.unlink()

    @unittest.skipIf(os.name == "nt", "FIFO semantics require POSIX")
    def test_inventory_rejects_nonregular_file_without_opening_it(self):
        path = self.root / "learning" / "guidance" / "blocked.md"
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        self.assertTrue(any("regular file" in item for item in learning_validation_errors(self.root)))

    def test_write_ownership_is_scoped_and_never_includes_raw_source(self):
        candidate_id = "a" * 64
        for domain in LEARNING_DOMAINS:
            self.assertFalse(learning_write_path(domain, f"learning/candidates/{candidate_id}.json"))
            self.assertFalse(learning_write_path(domain, f"learning/processed/{domain}/{candidate_id}.json"))
            self.assertTrue(is_learning_path(f"learning/processed/{domain}/{candidate_id}.json"))
        self.assertTrue(is_learning_path(f"learning/candidates/{candidate_id}.json"))
        self.assertTrue(learning_write_path("guidance", "learning/guidance/project/rules.md"))
        self.assertFalse(learning_write_path("skills", "learning/guidance/project/rules.md"))
        self.assertTrue(learning_write_path("skills", "learning/experiences/case.md"))
        self.assertTrue(learning_write_path("skills", "learning/skills/export/assets/template.bin"))
        self.assertFalse(learning_write_path("guidance", "learning/skills/export/SKILL.md"))
        self.assertFalse(learning_write_path("guidance", f"learning/processed/skills/{candidate_id}.json"))
        for path in (
            "/learning/guidance/rules.md",
            "learning/guidance/../rules.md",
            "learning//guidance/rules.md",
            "learning/skills/export/.git/config",
            "learning\\guidance\\rules.md",
            "learning/guidance/rules.md:stream.md",
        ):
            self.assertFalse(learning_write_path("guidance", path))
            self.assertFalse(learning_write_path("skills", path))
            self.assertFalse(is_learning_path(path))

    def test_invalid_domain_and_identity_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            list_learning_candidates(self.root, domain="other")
        with self.assertRaises(ValueError):
            record_learning_processed(self.root, "other", [])
        with self.assertRaises(ValueError):
            withdraw_learning_candidate(self.root, "../outside")
        for session, evidence, request in (
            ("", "Evidence.", None),
            ("session\nother", "Evidence.", None),
            ("session", "  ", None),
            ("session", "Evidence.", ""),
        ):
            with self.assertRaises(ValueError):
                submit_learning(self.root, session, evidence, request_id=request)


if __name__ == "__main__":
    unittest.main()
