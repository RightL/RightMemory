import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.update_queue import (
    PublicationMarker,
    UpdateCandidate,
    UpdateQueueFormatError,
    UpdateQueueLease,
    UpdateQueueRecovery,
    UpdateQueueStore,
    candidate_sha256,
    update_candidate_batch_id,
    validate_update_queue,
)


UID_A = "00112233445566778899aabbccddeeff"
UID_B = "102132435465768798a9bacbdcedfe0f"
HOLDER = "11223344556677889900aabbccddeeff"
TOKEN = "ffeeddccbbaa00998877665544332211"
ATTEMPT = "1234567890abcdef1234567890abcdef"
BATCH_A = f"update-batch-{'a' * 64}"
BATCH_B = f"update-batch-{'b' * 64}"
COMMIT = "c" * 40
SUBMITTED_AT = "2026-07-21T08:30:00+00:00"
EXPIRES_AT = "2026-07-21T09:30:00+00:00"


class UpdateQueueWireFormatTests(unittest.TestCase):
    def test_valid_queue_round_trips_candidate_lease_and_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            candidate = _candidate(UID_A)
            batch_id = update_candidate_batch_id((candidate,))
            recovery = UpdateQueueRecovery(
                batch_id=batch_id,
                candidate_uids=(UID_A,),
                attempts=1,
                reason_code="model_error",
                retry_at=EXPIRES_AT,
            )
            lease = UpdateQueueLease(
                holder=HOLDER,
                token=TOKEN,
                base_commit=COMMIT,
                batch_id=batch_id,
                candidate_uids=(UID_A,),
                expires_at=EXPIRES_AT,
            )

            store.write_candidate(candidate)
            store.write_recovery(recovery)
            store.write_lease(lease)
            snapshot = store.snapshot()

            self.assertEqual(validate_update_queue(root), [])
            self.assertEqual(
                store.candidate_path(UID_A),
                root / "update_queue" / "candidates" / f"{UID_A}.json",
            )
            self.assertEqual(
                store.recovery_path(batch_id),
                root / "update_queue" / "recovery" / f"{batch_id}.json",
            )
            self.assertEqual(snapshot.candidates, (candidate,))
            self.assertEqual(snapshot.recoveries, (recovery,))
            self.assertEqual(snapshot.lease, lease)

    def test_candidates_are_keyed_by_uid_and_immutable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            original = _candidate(UID_A)

            first_path = store.write_candidate(original)
            second_path = store.write_candidate(original)

            self.assertEqual(first_path, second_path)
            self.assertEqual(first_path.name, f"{UID_A}.json")
            with self.assertRaisesRegex(FileExistsError, "immutable"):
                store.write_candidate(
                    UpdateCandidate(
                        uid=UID_A,
                        session_id="session-a",
                        display_id=1,
                        message="different payload",
                        submitted_at=SUBMITTED_AT,
                    )
                )

    def test_display_id_is_not_global_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            store.write_candidate(_candidate(UID_A, display_id=1))
            store.write_candidate(_candidate(UID_B, display_id=1))

            self.assertEqual(validate_update_queue(root), [])
            self.assertEqual(
                [candidate.display_id for candidate in store.snapshot().candidates],
                [1, 1],
            )

    def test_candidate_requires_lowercase_uuid_hex_and_canonical_utc_time(self):
        with self.assertRaisesRegex(UpdateQueueFormatError, "lowercase UUID hex"):
            _candidate(UID_A.upper())
        with self.assertRaisesRegex(UpdateQueueFormatError, "canonical UTC datetime"):
            UpdateCandidate(
                uid=UID_A,
                session_id="session-a",
                display_id=1,
                message="candidate",
                submitted_at="2026-07-21T16:30:00+08:00",
            )

    def test_validator_rejects_unknown_fields_and_boolean_display_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            path = store.write_candidate(_candidate(UID_A))
            data = json.loads(path.read_text(encoding="utf-8"))
            data["unexpected"] = "not runtime-owned"
            path.write_text(json.dumps(data), encoding="utf-8")

            errors = validate_update_queue(root)

            self.assertEqual(len(errors), 1)
            self.assertTrue(errors[0].startswith(f"update_queue/candidates/{UID_A}.json: "))
            self.assertIn("unsupported field", errors[0])

            data.pop("unexpected")
            data["display_id"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
            errors = validate_update_queue(root)
            self.assertIn("display_id must be an integer", errors[0])

    def test_validator_rejects_filename_mismatch_duplicate_json_keys_and_unknown_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            candidates = root / "update_queue" / "candidates"
            candidates.mkdir(parents=True)
            source = json.dumps(
                {
                    "schema_version": 1,
                    "uid": UID_A,
                    "session_id": "session-a",
                    "display_id": 1,
                    "message": "candidate",
                    "submitted_at": SUBMITTED_AT,
                }
            )
            (candidates / f"{UID_B}.json").write_text(source, encoding="utf-8")
            (candidates / f"{UID_A}.json").write_text(
                source[:-1] + ', "message": "duplicate"}',
                encoding="utf-8",
            )
            (root / "update_queue" / "notes.txt").write_text("unexpected", encoding="utf-8")

            errors = validate_update_queue(root)

            self.assertTrue(
                any(error.startswith("update_queue/notes.txt: unexpected") for error in errors)
            )
            self.assertTrue(any("duplicate JSON field: message" in error for error in errors))
            self.assertTrue(any("filename does not match embedded candidate identity" in error for error in errors))

    def test_validator_rejects_symlinked_queue_records(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            candidates = root / "update_queue" / "candidates"
            candidates.mkdir(parents=True)
            target = root / "outside.json"
            target.write_text("{}", encoding="utf-8")
            link = candidates / f"{UID_A}.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation is unavailable")

            errors = validate_update_queue(root)

            self.assertEqual(
                errors,
                [f"update_queue/candidates/{UID_A}.json: must be a regular file"],
            )

    def test_lease_and_recovery_must_reference_live_candidates(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            store.write_candidate(_candidate(UID_A))
            store.write_lease(
                UpdateQueueLease(
                    holder=HOLDER,
                    token=TOKEN,
                    base_commit=COMMIT,
                    batch_id=BATCH_A,
                    candidate_uids=(UID_B,),
                    expires_at=EXPIRES_AT,
                )
            )
            store.write_recovery(
                UpdateQueueRecovery(
                    batch_id=BATCH_B,
                    candidate_uids=(UID_B,),
                    attempts=2,
                    reason_code="worker_exit",
                    retry_at=None,
                    manual_recovery=True,
                )
            )

            errors = validate_update_queue(root)

            self.assertTrue(any("references missing candidate uid(s)" in error for error in errors))
            self.assertTrue(any("membership conflicts with recovery" in error for error in errors))

    def test_recovery_rejects_human_error_text_and_inconsistent_retry_state(self):
        with self.assertRaisesRegex(UpdateQueueFormatError, "machine-readable code"):
            UpdateQueueRecovery(
                batch_id=BATCH_A,
                candidate_uids=(UID_A,),
                attempts=1,
                reason_code="Connection failed: secret token",
                retry_at=EXPIRES_AT,
            )
        with self.assertRaisesRegex(UpdateQueueFormatError, "automatic recovery requires"):
            UpdateQueueRecovery(
                batch_id=BATCH_A,
                candidate_uids=(UID_A,),
                attempts=1,
                reason_code="model_error",
                retry_at=None,
            )
        with self.assertRaisesRegex(UpdateQueueFormatError, "must not contain retry_at"):
            UpdateQueueRecovery(
                batch_id=BATCH_A,
                candidate_uids=(UID_A,),
                attempts=2,
                reason_code="model_error",
                retry_at=EXPIRES_AT,
                manual_recovery=True,
            )

    def test_recovery_updates_cannot_change_batch_membership(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            store.write_recovery(
                UpdateQueueRecovery(
                    batch_id=BATCH_A,
                    candidate_uids=(UID_A,),
                    attempts=1,
                    reason_code="model_error",
                    retry_at=EXPIRES_AT,
                )
            )

            with self.assertRaisesRegex(UpdateQueueFormatError, "cannot change"):
                store.write_recovery(
                    UpdateQueueRecovery(
                        batch_id=BATCH_A,
                        candidate_uids=(UID_B,),
                        attempts=2,
                        reason_code="worker_exit",
                        retry_at=None,
                        manual_recovery=True,
                    )
                )

    def test_validator_returns_empty_for_missing_queue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self.assertEqual(validate_update_queue(Path(tempdir)), [])


class LocalUpdateOutboxTests(unittest.TestCase):
    def test_publication_marker_changes_offline_authority_before_commit_exists(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            candidate = _candidate(UID_A)
            store.write_outbox(candidate)

            self.assertEqual(store.publication_state(UID_A), "never_attempted")
            marker = store.begin_publication(
                UID_A,
                attempted_at=SUBMITTED_AT,
                attempt_id=ATTEMPT,
            )

            self.assertEqual(store.publication_state(UID_A), "attempted")
            self.assertIsNone(marker.proposed_commit)
            self.assertEqual(marker.candidate_sha256, candidate_sha256(candidate))
            self.assertEqual(store.begin_publication(UID_A, attempted_at=EXPIRES_AT), marker)

            updated = store.record_publication_commit(UID_A, COMMIT)
            self.assertEqual(updated.proposed_commit, COMMIT)
            self.assertEqual(store.read_publication_marker(UID_A), updated)

    def test_orphaned_marker_remains_attempted_and_never_becomes_offline_eligible(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            store.write_outbox(_candidate(UID_A))
            store.begin_publication(
                UID_A,
                attempted_at=SUBMITTED_AT,
                attempt_id=ATTEMPT,
            )

            store.remove_outbox(UID_A)

            self.assertEqual(store.publication_state(UID_A), "attempted")
            store.clear_publication_marker(UID_A)
            self.assertEqual(store.publication_state(UID_A), "missing")

    def test_outbox_is_ignored_and_immutable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            candidate = _candidate(UID_A)

            store.write_outbox(candidate)

            self.assertEqual((root / ".runtime" / ".gitignore").read_text(encoding="utf-8"), "*\n")
            self.assertEqual(store.read_outbox(UID_A), candidate)
            self.assertEqual(store.outbox_candidates(), (candidate,))
            with self.assertRaisesRegex(FileExistsError, "immutable"):
                store.write_outbox(
                    UpdateCandidate(
                        uid=UID_A,
                        session_id="session-a",
                        display_id=1,
                        message="changed",
                        submitted_at=SUBMITTED_AT,
                    )
                )

    def test_outbox_listing_strictly_rejects_mismatched_filename(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            path = store.write_outbox(_candidate(UID_A))
            path.rename(path.with_name(f"{UID_B}.json"))

            with self.assertRaisesRegex(UpdateQueueFormatError, "filename does not match"):
                store.outbox_candidates()

    def test_outbox_listing_reaps_only_known_atomic_temp_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            candidate = _candidate(UID_A)
            store.write_outbox(candidate)
            temporary = store.outbox_root / f".999999999.{UID_B}.tmp"
            temporary.write_text("partial", encoding="utf-8")
            legacy_temporary = store.outbox_root / f".{UID_A}.json.999999999.{UID_B}.tmp"
            legacy_temporary.write_text("partial", encoding="utf-8")

            with patch("rightmemory.update_queue.process_exists", return_value=False):
                self.assertEqual(store.outbox_candidates(), (candidate,))
            self.assertFalse(temporary.exists())
            self.assertFalse(legacy_temporary.exists())

            junk = store.outbox_root / "notes.txt"
            junk.write_text("not runtime state", encoding="utf-8")
            with self.assertRaisesRegex(UpdateQueueFormatError, "unexpected local outbox path"):
                store.outbox_candidates()

    def test_outbox_listing_rejects_temp_shaped_symlink(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            candidate_path = store.write_outbox(_candidate(UID_A))
            temporary = store.outbox_root / f".999999999.{UID_B}.tmp"
            try:
                temporary.symlink_to(candidate_path)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaisesRegex(UpdateQueueFormatError, "temporary artifact must be a regular file"):
                store.outbox_candidates()

    def test_outbox_listing_rejects_temp_shaped_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            store.write_outbox(_candidate(UID_A))
            temporary = store.outbox_root / f".999999999.{UID_B}.tmp"
            temporary.mkdir()

            with self.assertRaisesRegex(UpdateQueueFormatError, "temporary artifact must be a regular file"):
                store.outbox_candidates()

    def test_publication_marker_is_strict_runtime_owned_json(self):
        with self.assertRaisesRegex(UpdateQueueFormatError, "lowercase SHA-256"):
            PublicationMarker(
                candidate_uid=UID_A,
                attempt_id=ATTEMPT,
                attempted_at=SUBMITTED_AT,
                candidate_sha256="not-a-digest",
            )

    def test_publication_marker_filename_must_match_embedded_uid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            store.write_outbox(_candidate(UID_A))
            marker_path = store.publication_root / f"{UID_A}.json"
            marker_path.parent.mkdir(parents=True)
            marker_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidate_uid": UID_B,
                        "attempt_id": ATTEMPT,
                        "attempted_at": SUBMITTED_AT,
                        "candidate_sha256": "d" * 64,
                        "proposed_commit": None,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(UpdateQueueFormatError, "filename does not match"):
                store.publication_state(UID_A)


def _candidate(uid: str, *, display_id: int = 1) -> UpdateCandidate:
    return UpdateCandidate(
        uid=uid,
        session_id="session-a",
        display_id=display_id,
        message=f"candidate {uid}",
        submitted_at=SUBMITTED_AT,
    )


if __name__ == "__main__":
    unittest.main()
