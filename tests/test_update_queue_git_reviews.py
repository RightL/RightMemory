from rightmemory.update_queue import UpdateQueueStore
from rightmemory.update_review import (
    UpdateReviewOutcome,
    UpdateReviewSourceChanged,
    UpdateReviewStore,
    parse_review_markdown,
)
from tests.update_queue_git_base import GitUpdateQueueTestBase


class GitUpdateQueueReviewTests(GitUpdateQueueTestBase):
    def test_review_candidate_is_claimed_immediately_and_resolution_deletes_review(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(self.first, review_id, "Remove the snapshot detail.")
        coordinator = self._coordinator(self.first, "1" * 32)
        publication = coordinator.publish_outbox({candidate.uid})

        claim = coordinator.claim_next(
            target_batch_candidates=100,
            max_wait_seconds=3600,
        ).claim
        self.assertIsNotNone(claim)
        self.assertEqual(claim.kind, "review")

        coordinator.finalize(
            claim,
            None,
            review_outcome=UpdateReviewOutcome.resolved(message="No state change needed."),
        )

        self.assertEqual(publication.published_uids, (candidate.uid,))
        self.assertFalse(UpdateReviewStore(self.first).review_path(review_id).exists())
        self.assertEqual(UpdateQueueStore(self.first).snapshot().candidates, ())

    def test_review_needs_input_is_rewritten_in_the_queue_finalization(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(self.first, review_id, "Use the stable project.")
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox({candidate.uid})
        claim = coordinator.claim_next(
            target_batch_candidates=100,
            max_wait_seconds=3600,
        ).claim
        self.assertIsNotNone(claim)

        coordinator.finalize(
            claim,
            None,
            review_outcome=UpdateReviewOutcome.needs_input("Which project is stable?"),
        )

        parsed = parse_review_markdown(
            UpdateReviewStore(self.first).review_path(review_id).read_text(encoding="utf-8")
        )
        self.assertFalse(parsed.ready)
        self.assertIn("Which project is stable?", parsed.question)
        self.assertEqual(UpdateQueueStore(self.first).snapshot().candidates, ())

    def test_applied_review_correction_and_review_deletion_publish_together(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(self.first, review_id, "Add the durable correction.")
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox({candidate.uid})
        claim = coordinator.claim_next(
            target_batch_candidates=100,
            max_wait_seconds=3600,
        ).claim
        self.assertIsNotNone(claim)
        prepared = self.root / "review-correction"
        self._git(self.first, "worktree", "add", "--detach", str(prepared), claim.lease_commit)
        with (prepared / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write("- `three` corrected → []\n")
        self._git(prepared, "add", "MEMORY.md")
        self._git(
            prepared,
            "commit",
            "-m",
            f"memory: correct review\n\nRightMemory-Operation: {claim.batch_id}",
        )
        correction_commit = self._git(prepared, "rev-parse", "HEAD")

        coordinator.finalize(
            claim,
            correction_commit,
            prepared_start_commit=claim.lease_commit,
            review_outcome=UpdateReviewOutcome.resolved(message="Applied."),
        )

        self.assertIn("corrected", (self.first / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertFalse(UpdateReviewStore(self.first).review_path(review_id).exists())
        self.assertEqual(UpdateQueueStore(self.first).snapshot().candidates, ())

    def test_equivalent_review_publication_ignores_only_submitted_at(self):
        review_id = self._create_tracked_review()
        uid = "a" * 32
        first_candidate = self._review_candidate(
            self.first,
            review_id,
            "Keep the durable value.",
            uid=uid,
            submitted_at="2026-07-21T08:30:00+00:00",
        )
        first = self._coordinator(self.first, "1" * 32)
        first.publish_outbox({uid})
        self._reset_to_remote(self.second)
        second_candidate = self._review_candidate(
            self.second,
            review_id,
            first_candidate.message,
            uid=uid,
            submitted_at="2026-07-21T08:31:00+00:00",
            review_commit=first_candidate.review_commit,
        )

        publication = self._coordinator(self.second, "2" * 32).publish_outbox({uid})

        self.assertNotEqual(first_candidate.submitted_at, second_candidate.submitted_at)
        self.assertEqual(publication.settled_uids, (uid,))
        self.assertEqual(publication.published_uids, ())
        self.assertEqual(
            UpdateQueueStore(self.second).snapshot().candidates,
            (first_candidate,),
        )

    def test_review_publication_rejects_other_same_uid_evidence_changes(self):
        review_id = self._create_tracked_review()
        uid = "a" * 32
        first_candidate = self._review_candidate(
            self.first,
            review_id,
            "Keep the durable value.",
            uid=uid,
        )
        self._coordinator(self.first, "1" * 32).publish_outbox({uid})
        self._reset_to_remote(self.second)
        self._review_candidate(
            self.second,
            review_id,
            "Different correction evidence.",
            uid=uid,
            submitted_at="2026-07-21T08:31:00+00:00",
            review_commit=first_candidate.review_commit,
        )

        with self.assertRaisesRegex(ValueError, "different synchronized evidence"):
            self._coordinator(self.second, "2" * 32).publish_outbox({uid})

    def test_missing_review_document_settles_stale_outbox_without_publication(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Apply this stale correction.",
        )
        review_path = UpdateReviewStore(self.first).review_path(review_id)
        review_path.unlink()
        self._git(self.first, "add", "-u", str(review_path.relative_to(self.first)))
        self._git(self.first, "commit", "-m", "remove settled review")
        self._git(self.first, "push", "origin", "HEAD:main")
        coordinator = self._coordinator(self.first, "1" * 32)

        publication = coordinator.publish_outbox({candidate.uid})

        self.assertEqual(publication.settled_uids, (candidate.uid,))
        self.assertEqual(publication.published_uids, ())
        self.assertEqual(publication.unresolved_uids, ())
        self._reset_to_remote(self.second)
        self.assertEqual(UpdateQueueStore(self.second).snapshot().candidates, ())
        coordinator.clear_local_candidates(publication.settled_uids)
        self.assertIsNone(UpdateQueueStore(self.first).read_outbox(candidate.uid))

    def test_review_publication_waits_for_its_source_revision_upstream(self):
        review_id = self._create_tracked_review()
        review_path = UpdateReviewStore(self.first).review_path(review_id)
        review_path.write_text(
            review_path.read_text(encoding="utf-8") + "\nLocally revised source.\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", str(review_path.relative_to(self.first)))
        self._git(self.first, "commit", "-m", "revise review locally")
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Apply after the source synchronizes.",
        )
        coordinator = self._coordinator(self.first, "1" * 32)

        waiting = coordinator.publish_outbox({candidate.uid})

        self.assertEqual(waiting.unresolved_uids, (candidate.uid,))
        self.assertEqual(waiting.settled_uids, ())
        self.assertEqual(UpdateQueueStore(self.first).publication_state(candidate.uid), "never_attempted")

        self._git(self.first, "push", "origin", "HEAD:main")
        published = coordinator.publish_outbox({candidate.uid})

        self.assertEqual(published.published_uids, (candidate.uid,))

    def test_changed_review_document_settles_stale_outbox_without_publication(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Apply this stale correction.",
        )
        review_path = UpdateReviewStore(self.first).review_path(review_id)
        review_path.write_text(
            review_path.read_text(encoding="utf-8") + "\nHuman revision after submission.\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", str(review_path.relative_to(self.first)))
        self._git(self.first, "commit", "-m", "revise submitted review")
        self._git(self.first, "push", "origin", "HEAD:main")

        publication = self._coordinator(self.first, "1" * 32).publish_outbox(
            {candidate.uid}
        )

        self.assertEqual(publication.settled_uids, (candidate.uid,))
        self.assertEqual(publication.published_uids, ())
        self.assertEqual(publication.unresolved_uids, ())
        self._reset_to_remote(self.second)
        self.assertEqual(UpdateQueueStore(self.second).snapshot().candidates, ())

    def test_restored_review_bytes_still_settle_stale_outbox_without_publication(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Do not apply after a newer review revision.",
        )
        review_path = UpdateReviewStore(self.first).review_path(review_id)
        original = review_path.read_text(encoding="utf-8")
        review_path.write_text(original + "\nTemporary revision.\n", encoding="utf-8")
        self._git(self.first, "add", str(review_path.relative_to(self.first)))
        self._git(self.first, "commit", "-m", "temporarily revise review")
        review_path.write_text(original, encoding="utf-8")
        self._git(self.first, "add", str(review_path.relative_to(self.first)))
        self._git(self.first, "commit", "-m", "restore review bytes")
        self._git(self.first, "push", "origin", "HEAD:main")

        publication = self._coordinator(self.first, "1" * 32).publish_outbox(
            {candidate.uid}
        )

        self.assertEqual(publication.settled_uids, (candidate.uid,))
        self.assertEqual(publication.published_uids, ())

    def test_changed_review_after_claim_requires_atomic_supersede(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Use the submitted revision only.",
        )
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox({candidate.uid})
        claim = coordinator.claim_next(
            target_batch_candidates=100,
            max_wait_seconds=3600,
        ).claim
        self.assertIsNotNone(claim)
        self._reset_to_remote(self.second)
        review_path = UpdateReviewStore(self.second).review_path(review_id)
        changed_document = (
            review_path.read_text(encoding="utf-8")
            + "\nHuman revision after the correction was claimed.\n"
        )
        review_path.write_text(changed_document, encoding="utf-8")
        self._git(self.second, "add", str(review_path.relative_to(self.second)))
        self._git(self.second, "commit", "-m", "revise claimed review")
        self._git(self.second, "push", "origin", "HEAD:main")

        with self.assertRaises(UpdateReviewSourceChanged):
            coordinator.finalize(
                claim,
                None,
                review_outcome=UpdateReviewOutcome.resolved(message="No change."),
            )

        self._reset_to_remote(self.second)
        pending = UpdateQueueStore(self.second).snapshot()
        self.assertEqual(pending.candidates, (candidate,))
        self.assertIsNotNone(pending.lease)
        self.assertEqual(review_path.read_text(encoding="utf-8"), changed_document)

        superseded = coordinator.supersede_review(claim)

        self.assertEqual(
            coordinator.superseded_batch_commit("HEAD", claim.batch_id),
            superseded,
        )
        self._reset_to_remote(self.second)
        settled = UpdateQueueStore(self.second).snapshot()
        self.assertEqual(settled.candidates, ())
        self.assertIsNone(settled.lease)
        self.assertEqual(review_path.read_text(encoding="utf-8"), changed_document)
        changed_paths = set(
            self._git(
                self.second,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                superseded,
            ).splitlines()
        )
        self.assertEqual(
            changed_paths,
            {
                f"update_queue/candidates/{candidate.uid}.json",
                "update_queue/lease.json",
            },
        )

    def test_restored_review_bytes_after_claim_still_require_supersede(self):
        review_id = self._create_tracked_review()
        candidate = self._review_candidate(
            self.first,
            review_id,
            "Use only the claimed review revision.",
        )
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox({candidate.uid})
        claim = coordinator.claim_next(
            target_batch_candidates=100,
            max_wait_seconds=3600,
        ).claim
        self.assertIsNotNone(claim)
        self._reset_to_remote(self.second)
        review_path = UpdateReviewStore(self.second).review_path(review_id)
        original = review_path.read_text(encoding="utf-8")
        review_path.write_text(original + "\nTemporary revision.\n", encoding="utf-8")
        self._git(self.second, "add", str(review_path.relative_to(self.second)))
        self._git(self.second, "commit", "-m", "temporarily revise claimed review")
        review_path.write_text(original, encoding="utf-8")
        self._git(self.second, "add", str(review_path.relative_to(self.second)))
        self._git(self.second, "commit", "-m", "restore claimed review bytes")
        self._git(self.second, "push", "origin", "HEAD:main")

        with self.assertRaises(UpdateReviewSourceChanged):
            coordinator.finalize(
                claim,
                None,
                review_outcome=UpdateReviewOutcome.resolved(message="No change."),
            )

        self.assertEqual(
            UpdateReviewStore(self.second).review_path(review_id).read_text(encoding="utf-8"),
            original,
        )
