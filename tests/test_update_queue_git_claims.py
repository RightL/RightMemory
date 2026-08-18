import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest import mock

from rightmemory.update_queue import UpdateCandidate, UpdateQueueSnapshot, UpdateQueueStore
from rightmemory.update_queue_git import (
    UpdateQueueLeaseLost,
    UpdateQueueUnavailable,
    _load_device_id,
    _select_candidates,
)
from tests.update_queue_git_base import GitUpdateQueueTestBase


class GitUpdateQueueClaimTests(GitUpdateQueueTestBase):
    def test_selection_uses_aggregate_trigger_before_session_quiet_period(self):
        candidates = tuple(
            UpdateCandidate(
                uid=f"{index:032x}",
                session_id=session_id,
                display_id=display_id,
                message=f"candidate {index}",
                submitted_at="2026-07-21T00:00:00+00:00",
            )
            for index, session_id, display_id in (
                (1, "session-a", 1),
                (2, "session-a", 2),
                (3, "session-b", 1),
            )
        )

        selected, deadline = _select_candidates(
            UpdateQueueSnapshot(candidates=candidates),
            trigger_candidates=3,
            target_batch_candidates=5,
            max_wait_seconds=86400,
            now=datetime(2026, 7, 21, tzinfo=UTC),
        )

        self.assertEqual(selected, candidates)
        self.assertIsNone(deadline)

    def test_selection_fills_to_target_without_splitting_sessions(self):
        candidates = tuple(
            UpdateCandidate(
                uid=f"{index:032x}",
                session_id=session_id,
                display_id=display_id,
                message=f"candidate {index}",
                submitted_at="2026-07-20T00:00:00+00:00",
            )
            for index, session_id, display_id in (
                (1, "session-a", 1),
                (2, "session-a", 2),
                (3, "session-b", 1),
                (4, "session-b", 2),
                (5, "session-c", 1),
                (6, "session-c", 2),
            )
        )

        selected, deadline = _select_candidates(
            UpdateQueueSnapshot(candidates=candidates),
            trigger_candidates=3,
            target_batch_candidates=5,
            max_wait_seconds=86400,
            now=datetime(2026, 7, 21, tzinfo=UTC),
        )

        self.assertEqual(selected, candidates)
        self.assertIsNone(deadline)

    def test_git_lease_allows_only_one_device_to_claim(self):
        self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()

        winner = first.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0)
        loser = second.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0)

        self.assertIsNotNone(winner.claim)
        self.assertIsNone(loser.claim)
        self.assertIsNotNone(loser.next_attempt_at)

    def test_expired_lease_takeover_fences_the_old_token(self):
        self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        claimed_at = datetime(2026, 7, 21, tzinfo=UTC)
        old_claim = first.claim_next(
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=claimed_at,
        ).claim
        self.assertIsNotNone(old_claim)

        new_claim = second.claim_next(
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=datetime(2026, 7, 22, tzinfo=UTC),
        ).claim
        self.assertIsNotNone(new_claim)
        self.assertNotEqual(new_claim.lease.token, old_claim.lease.token)

        with self.assertRaises(UpdateQueueLeaseLost) as caught:
            first.finalize(old_claim, None)
        self.assertIn("lease", str(caught.exception).lower())
        second.finalize(new_claim, None)

    def test_no_change_finalization_consumes_exact_claimed_candidate(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)

        landed = coordinator.finalize(claim, None)

        self.assertEqual(self._git(self.first, "rev-parse", "HEAD"), landed)
        snapshot = UpdateQueueStore(self.first).snapshot()
        self.assertEqual(snapshot.candidates, ())
        self.assertIsNone(snapshot.lease)
        history = self._git(
            self.first,
            "log",
            "--format=%H",
            "origin/main",
            "--",
            f"update_queue/candidates/{candidate.uid}.json",
        )
        self.assertTrue(history)

    def test_other_device_can_cancel_unleased_candidate_by_display_id(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()

        canceled = second.cancel(candidate.session_id, candidate.display_id)

        self.assertEqual(canceled, candidate)
        self._git(self.first, "fetch", "origin")
        self.assertEqual(
            self._git(
                self.first,
                "ls-tree",
                "-r",
                "--name-only",
                "origin/main",
                "--",
                f"update_queue/candidates/{candidate.uid}.json",
            ),
            "",
        )
        settled = first.publish_outbox()
        self.assertEqual(settled.settled_uids, (candidate.uid,))

    def test_claim_synchronizes_locally_ahead_state_before_publishing_lease(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        self._git(self.first, "fetch", "origin")
        self._git(self.first, "reset", "--hard", "origin/main")
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `local` ahead → []\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", "MEMORY.md")
        self._git(self.first, "commit", "-m", "memory: local ahead")

        result = coordinator.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0)

        self.assertIsNotNone(result.claim)
        self._reset_to_remote(self.second)
        self.assertIsNotNone(UpdateQueueStore(self.second).snapshot().lease)
        self.assertIn(
            "`local` ahead",
            (self.second / "MEMORY.md").read_text(encoding="utf-8"),
        )

    def test_stale_lease_holder_fresh_syncs_candidate_context_before_claim(self):
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one` first -> []\n"
            "- `context` published before candidate -> []\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", "MEMORY.md")
        self._git(self.first, "commit", "-m", "memory: candidate context")
        self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()

        result = second.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0)

        self.assertIsNotNone(result.claim)
        self.assertIn(
            "published before candidate",
            (self.second / "MEMORY.md").read_text(encoding="utf-8"),
        )

    def test_claim_releases_remote_lease_when_local_install_fails(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        self._reset_to_remote(self.first)
        original_advance = coordinator._advance_active_locked
        calls = 0

        def fail_second_advance(commit: str, *, queue_only: bool) -> bool:
            nonlocal calls
            calls += 1
            if calls == 2:
                return False
            return original_advance(commit, queue_only=queue_only)

        with mock.patch.object(
            coordinator,
            "_advance_active_locked",
            side_effect=fail_second_advance,
        ):
            with self.assertRaises(UpdateQueueUnavailable):
                coordinator.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0)

        self._reset_to_remote(self.second)
        self.assertIsNone(UpdateQueueStore(self.second).snapshot().lease)

    def test_finalized_lookup_requires_exact_batch_and_token_trailers(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            trigger_candidates=1,
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        landed = coordinator.finalize(claim, None)
        self._git(
            self.first,
            "commit",
            "--allow-empty",
            "-m",
            f"mentions RightMemory-Queue-Batch: {claim.batch_id} inline",
        )

        self.assertEqual(
            coordinator.finalized_batch_commit(
                "HEAD",
                claim.batch_id,
                token=claim.lease.token,
            ),
            landed,
        )
        self.assertIsNone(
            coordinator.finalized_batch_commit(
                "HEAD",
                claim.batch_id,
                token="f" * 32,
            )
        )

    def test_fetch_and_integrate_finalized_batch_advances_stale_checkout(self):
        self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        claim = first.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        landed = first.finalize(claim, None)

        integrated = second.fetch_and_integrate_finalized_batch(
            claim.batch_id,
            token=claim.lease.token,
        )

        self.assertEqual(integrated, landed)
        self.assertEqual(self._git(self.second, "rev-parse", "HEAD"), landed)

    def test_device_id_creation_is_serialized(self):
        root = self.root / "device-id"
        root.mkdir()
        calls = 0

        def slow_uuid4():
            nonlocal calls
            calls += 1
            time.sleep(0.01)
            return uuid.UUID(int=calls)

        with mock.patch("rightmemory.update_queue_git.uuid.uuid4", side_effect=slow_uuid4):
            with ThreadPoolExecutor(max_workers=8) as executor:
                device_ids = tuple(executor.map(lambda _item: _load_device_id(root), range(8)))

        self.assertEqual(len(set(device_ids)), 1)
        # One UUID is the device identity and one names its atomic temporary file.
        self.assertEqual(calls, 2)
        self.assertTrue((root / ".runtime" / "update_queue" / "device.json").is_file())
        self.assertFalse((root / ".runtime" / "async" / "update" / "device.json").exists())
