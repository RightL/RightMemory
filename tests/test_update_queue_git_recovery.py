from datetime import UTC, datetime
from unittest import mock

from rightmemory.async_update import AsyncUpdateStore
from rightmemory.update_queue import (
    UpdateCandidate,
    UpdateQueueRecovery,
    UpdateQueueSnapshot,
    UpdateQueueStore,
    update_candidate_batch_id,
)
from rightmemory.update_queue_git import UpdateQueueUnavailable, _select_candidates
from tests.update_queue_git_base import GitUpdateQueueTestBase


class GitUpdateQueueRecoveryTests(GitUpdateQueueTestBase):
    def test_cancel_attempted_unpublished_candidate_fences_later_publication(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        marker = AsyncUpdateStore(self.first, "update").begin_publication(
            candidate,
            attempted_at=candidate.submitted_at,
        )
        self.assertIsNotNone(marker)

        outcome = coordinator.cancel_attempted(candidate)
        publication = coordinator.publish_outbox(frozenset({candidate.uid}))

        self.assertEqual(outcome, "canceled")
        self.assertEqual(publication.settled_uids, (candidate.uid,))
        self.assertEqual(publication.published_uids, ())
        self._reset_to_remote(self.second)
        self.assertEqual(UpdateQueueStore(self.second).snapshot().candidates, ())

    def test_cancel_attempted_recovers_ambiguous_final_push(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        AsyncUpdateStore(self.first, "update").begin_publication(
            candidate,
            attempted_at=candidate.submitted_at,
        )
        real_push = coordinator._push_cas

        def accept_then_report_failure(upstream, commit):
            self.assertTrue(real_push(upstream, commit))
            return False

        with (
            mock.patch("rightmemory.update_queue_git.QUEUE_PUSH_ATTEMPTS", 1),
            mock.patch.object(
                coordinator,
                "_push_cas",
                side_effect=accept_then_report_failure,
            ),
        ):
            outcome = coordinator.cancel_attempted(candidate)

        self.assertEqual(outcome, "canceled")

    def test_cancel_attempted_offline_preserves_local_evidence(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        marker = AsyncUpdateStore(self.first, "update").begin_publication(
            candidate,
            attempted_at=candidate.submitted_at,
        )

        with mock.patch.object(coordinator, "_fetch_upstream", return_value=None):
            with self.assertRaises(UpdateQueueUnavailable):
                coordinator.cancel_attempted(candidate)

        self.assertEqual(UpdateQueueStore(self.first).read_outbox(candidate.uid), candidate)
        self.assertEqual(UpdateQueueStore(self.first).read_publication_marker(candidate.uid), marker)

    def test_cancel_attempted_candidate_reports_already_settled_history(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        coordinator.finalize(claim, None)

        outcome = coordinator.cancel_attempted(candidate)

        self.assertEqual(outcome, "settled")

    def test_cancel_recovers_ambiguous_push_success(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        real_push = second._push_cas
        calls = 0

        def accept_then_report_failure(upstream, commit):
            nonlocal calls
            calls += 1
            accepted = real_push(upstream, commit)
            return False if calls == 1 and accepted else accepted

        with mock.patch.object(second, "_push_cas", side_effect=accept_then_report_failure):
            canceled = second.cancel(candidate.session_id, candidate.display_id)

        self.assertEqual(canceled, candidate)
        self.assertEqual(calls, 1)
        self._reset_to_remote(self.first)
        self.assertEqual(UpdateQueueStore(self.first).snapshot().candidates, ())

    def test_cancel_records_terminal_proof_for_a_released_batch(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        claim = first.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        first.release(claim)

        canceled = second.cancel(candidate.session_id, candidate.uid)
        self.assertEqual(canceled, candidate)
        terminal = second.superseded_batch_commit(
            "HEAD",
            claim.batch_id,
        )

        self.assertIsNotNone(terminal)

    def test_retry_manual_recovers_ambiguous_push_success(self):
        coordinator = self._manual_recovery_coordinator()
        real_push = coordinator._push_cas
        calls = 0

        def accept_then_report_failure(upstream, commit):
            nonlocal calls
            calls += 1
            accepted = real_push(upstream, commit)
            return False if calls == 1 and accepted else accepted

        with mock.patch.object(
            coordinator,
            "_push_cas",
            side_effect=accept_then_report_failure,
        ):
            retried = coordinator.retry_manual()

        self.assertEqual(retried, 1)
        self.assertEqual(calls, 1)
        self._reset_to_remote(self.second)
        recovery = UpdateQueueStore(self.second).snapshot().recoveries[0]
        self.assertFalse(recovery.manual_recovery)
        self.assertEqual(recovery.attempts, 0)
        self.assertEqual(recovery.reason_code, "manual_retry")

    def test_retry_manual_raises_when_offline_or_cas_never_settles(self):
        coordinator = self._manual_recovery_coordinator()
        with mock.patch.object(coordinator, "_fetch_upstream", return_value=None):
            with self.assertRaises(UpdateQueueUnavailable):
                coordinator.retry_manual()
        with mock.patch.object(coordinator, "_push_cas", return_value=False):
            with self.assertRaises(UpdateQueueUnavailable):
                coordinator.retry_manual()

    def test_recovery_batch_id_must_match_candidate_evidence(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        self._reset_to_remote(self.first)
        UpdateQueueStore(self.first).write_recovery(
            UpdateQueueRecovery(
                batch_id="update-batch-" + "f" * 64,
                candidate_uids=(candidate.uid,),
                attempts=1,
                reason_code="processing_failed",
                retry_at=datetime(2026, 7, 21, tzinfo=UTC).isoformat(),
            )
        )
        self._git(self.first, "add", "update_queue/recovery")
        self._git(self.first, "commit", "-m", "forge recovery identity")
        self._git(self.first, "push", "origin", "HEAD:main")

        with self.assertRaisesRegex(ValueError, "batch_id does not match"):
            coordinator.claim_next(
                trigger_candidates=1,
                target_batch_candidates=1,
                max_wait_seconds=0,
                now=datetime(2026, 7, 22, tzinfo=UTC),
            )

    def test_recovery_blocks_new_candidates_from_the_same_session(self):
        first = UpdateCandidate(
            uid="a" * 32,
            session_id="session-a",
            display_id=1,
            message="failed evidence",
            submitted_at="2026-07-20T00:00:00+00:00",
        )
        second = UpdateCandidate(
            uid="b" * 32,
            session_id="session-a",
            display_id=2,
            message="new evidence",
            submitted_at="2026-07-20T01:00:00+00:00",
        )
        recovery = UpdateQueueRecovery(
            batch_id=update_candidate_batch_id((first,)),
            candidate_uids=(first.uid,),
            attempts=2,
            reason_code="processing_failed",
            retry_at=None,
            manual_recovery=True,
        )

        selected, deadline = _select_candidates(
            UpdateQueueSnapshot(candidates=(first, second), recoveries=(recovery,)),
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=datetime(2026, 7, 22, tzinfo=UTC),
        )

        self.assertEqual(selected, ())
        self.assertIsNone(deadline)
