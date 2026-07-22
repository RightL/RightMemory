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
REVIEW_A = f"review-{'a' * 64}"
REVIEW_B = f"review-{'b' * 64}"
REVIEW_COMMIT = "c" * 40
REVIEW_BLOB_OID = "e" * 40
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

    def test_review_candidate_round_trips_with_typed_review_context(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            candidate = _review_candidate(
                UID_A,
                previous_question="Which project did this refer to?",
            )

            path = store.write_candidate(candidate)
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(store.read_candidate(UID_A), candidate)
            self.assertEqual(data["schema_version"], 2)
            self.assertEqual(data["kind"], "review")
            self.assertEqual(data["review_id"], REVIEW_A)
            self.assertEqual(data["review_commit"], REVIEW_COMMIT)
            self.assertEqual(
                data["review_blob_oid"],
                REVIEW_BLOB_OID,
            )
            self.assertEqual(
                data["previous_question"],
                "Which project did this refer to?",
            )

    def test_candidate_kind_enforces_review_specific_fields(self):
        invalid_candidates = (
            (
                "kind must be update or review",
                {"kind": "other"},
            ),
            (
                "must be null",
                {"review_id": REVIEW_A},
            ),
            (
                "review_id must use review-",
                {"kind": "review"},
            ),
            (
                "review_id must use review-",
                {"kind": "review", "review_id": "review-not-a-digest"},
            ),
            (
                "message must be a non-empty string",
                {
                    "kind": "review",
                    "review_id": REVIEW_A,
                    "review_commit": REVIEW_COMMIT,
                    "review_blob_oid": REVIEW_BLOB_OID,
                    "message": "   ",
                },
            ),
            (
                "previous_question must be a non-empty string",
                {
                    "kind": "review",
                    "review_id": REVIEW_A,
                    "review_commit": REVIEW_COMMIT,
                    "review_blob_oid": REVIEW_BLOB_OID,
                    "previous_question": "\t",
                },
            ),
            (
                "review_commit must be a lowercase Git object id",
                {"kind": "review", "review_id": REVIEW_A},
            ),
            (
                "review_blob_oid must be a lowercase Git object id",
                {
                    "kind": "review",
                    "review_id": REVIEW_A,
                    "review_commit": REVIEW_COMMIT,
                },
            ),
            (
                "review_blob_oid must be a lowercase Git object id",
                {
                    "kind": "review",
                    "review_id": REVIEW_A,
                    "review_commit": REVIEW_COMMIT,
                    "review_blob_oid": "not-an-object-id",
                },
            ),
            (
                "must be null",
                {"review_blob_oid": REVIEW_BLOB_OID},
            ),
            (
                "must be null",
                {"review_commit": REVIEW_COMMIT},
            ),
        )
        for expected, overrides in invalid_candidates:
            values = {
                "uid": UID_A,
                "session_id": REVIEW_A,
                "display_id": 1,
                "message": "candidate",
                "submitted_at": SUBMITTED_AT,
            }
            values.update(overrides)
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(UpdateQueueFormatError, expected):
                    UpdateCandidate(**values)

    def test_candidate_parser_requires_typed_fields_and_nullable_strings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = UpdateQueueStore(Path(tempdir))
            path = store.write_candidate(_candidate(UID_A))
            data = json.loads(path.read_text(encoding="utf-8"))

            data.pop("kind")
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn("missing field(s): kind", validate_update_queue(Path(tempdir))[0])

            data["kind"] = "update"
            data.pop("review_blob_oid")
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn(
                "missing field(s): review_blob_oid",
                validate_update_queue(Path(tempdir))[0],
            )

            data["kind"] = "review"
            data["review_commit"] = REVIEW_COMMIT
            data["review_blob_oid"] = REVIEW_BLOB_OID
            data["review_id"] = 42
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIn(
                "review_id must be a string or null",
                validate_update_queue(Path(tempdir))[0],
            )

    def test_batch_identity_includes_review_candidate_context(self):
        ordinary = _candidate(UID_A)
        review = _review_candidate(UID_A)
        other_review = _review_candidate(UID_A, review_id=REVIEW_B)
        other_commit = _review_candidate(
            UID_A,
            review_commit="d" * 40,
        )
        other_document = _review_candidate(
            UID_A,
            review_blob_oid="f" * 40,
        )
        follow_up = _review_candidate(
            UID_A,
            previous_question="Which project did this refer to?",
        )

        identities = {
            update_candidate_batch_id((ordinary,)),
            update_candidate_batch_id((review,)),
            update_candidate_batch_id((other_review,)),
            update_candidate_batch_id((other_commit,)),
            update_candidate_batch_id((other_document,)),
            update_candidate_batch_id((follow_up,)),
        }

        self.assertEqual(len(identities), 6)

    def test_validator_rejects_multiple_tracked_candidates_for_one_review(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateQueueStore(root)
            store.write_candidate(_review_candidate(UID_A))
            store.write_candidate(_review_candidate(UID_B))

            errors = validate_update_queue(root)

            self.assertTrue(
                any(
                    REVIEW_A in error and UID_A in error and UID_B in error
                    for error in errors
                ),
                errors,
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
                    "schema_version": 2,
                    "uid": UID_A,
                    "session_id": "session-a",
                    "display_id": 1,
                    "kind": "update",
                    "message": "candidate",
                    "previous_question": None,
                    "review_id": None,
                    "review_commit": None,
                    "review_blob_oid": None,
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
                        "schema_version": 2,
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


def _review_candidate(
    uid: str,
    *,
    review_id: str = REVIEW_A,
    previous_question: str | None = None,
    review_commit: str = REVIEW_COMMIT,
    review_blob_oid: str = REVIEW_BLOB_OID,
) -> UpdateCandidate:
    return UpdateCandidate(
        uid=uid,
        session_id=review_id,
        display_id=1,
        message=f"candidate {uid}",
        submitted_at=SUBMITTED_AT,
        kind="review",
        review_id=review_id,
        review_commit=review_commit,
        previous_question=previous_question,
        review_blob_oid=review_blob_oid,
    )


if __name__ == "__main__":
    unittest.main()
