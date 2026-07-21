import subprocess
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from rightmemory.async_update import AsyncUpdateStore
from rightmemory.config import SyncConfig
from rightmemory.update_queue import (
    UpdateCandidate,
    UpdateQueueRecovery,
    UpdateQueueSnapshot,
    UpdateQueueStore,
    update_candidate_batch_id,
)
from rightmemory.update_queue_git import (
    GitUpdateQueueCoordinator,
    UpdateQueueLeaseLost,
    UpdateQueueUnavailable,
    UpdateQueueSemanticBaseChanged,
    _load_device_id,
    _select_candidates,
)


class GitUpdateQueueCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.first = self.root / "first"
        self.second = self.root / "second"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.first))
        self._git(self.root, "clone", str(self.remote), str(self.second))
        for repo in (self.first, self.second):
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n",
            encoding="utf-8",
        )
        (self.first / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.first / "PURSUIT_RULES.md").write_text("# Pursuit Rules\n", encoding="utf-8")
        self._git(self.first, "add", "MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md")
        self._git(self.first, "commit", "-m", "initial memory")
        self._git(self.first, "push", "-u", "origin", "HEAD:main")
        self._git(self.first, "branch", "--set-upstream-to", "origin/main")
        self._git(self.second, "fetch", "origin")
        self._git(self.second, "checkout", "-B", "main", "origin/main")
        self._git(self.second, "branch", "--set-upstream-to", "origin/main")

    def test_publishes_outbox_candidate_and_preserves_it_until_acknowledged(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)

        result = coordinator.publish_outbox()

        self.assertEqual(result.published_uids, (candidate.uid,))
        self.assertIsNotNone(UpdateQueueStore(self.first).read_outbox(candidate.uid))
        self._git(self.second, "fetch", "origin")
        self._git(self.second, "reset", "--hard", "origin/main")
        self.assertEqual(
            UpdateQueueStore(self.second).snapshot().candidates,
            (candidate,),
        )
        coordinator.clear_local_candidates(result.settled_uids)
        self.assertIsNone(UpdateQueueStore(self.first).read_outbox(candidate.uid))

    def test_publication_rejects_same_uid_with_different_remote_evidence(self):
        remote_candidate = UpdateCandidate(
            uid="a" * 32,
            session_id="session-a",
            display_id=1,
            message="remote evidence",
            submitted_at=datetime(2026, 7, 21, tzinfo=UTC).isoformat(),
        )
        UpdateQueueStore(self.first).write_candidate(remote_candidate)
        self._git(self.first, "add", "update_queue/candidates")
        self._git(self.first, "commit", "-m", "publish conflicting candidate")
        self._git(self.first, "push", "origin", "HEAD:main")
        local_candidate = UpdateCandidate(
            **{**remote_candidate.__dict__, "message": "local evidence"}
        )
        UpdateQueueStore(self.first).write_outbox(local_candidate)

        with self.assertRaisesRegex(ValueError, "different synchronized evidence"):
            self._coordinator(self.first, "1" * 32).publish_outbox()

    def test_publication_recovers_when_push_succeeds_with_ambiguous_response(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
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
            result = coordinator.publish_outbox()

        self.assertEqual(calls, 1)
        self.assertEqual(result.settled_uids, (candidate.uid,))
        self.assertEqual(result.unresolved_uids, ())

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
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        coordinator.finalize(claim, None)

        outcome = coordinator.cancel_attempted(candidate)

        self.assertEqual(outcome, "settled")

    def test_git_lease_allows_only_one_device_to_claim(self):
        self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()

        winner = first.claim_next(target_batch_candidates=1, max_wait_seconds=0)
        loser = second.claim_next(target_batch_candidates=1, max_wait_seconds=0)

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
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=claimed_at,
        ).claim
        self.assertIsNotNone(old_claim)

        new_claim = second.claim_next(
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

    def test_finalization_preserves_candidates_published_after_the_claim(self):
        first_candidate = self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        claim = first.claim_next(target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        later_candidate = self._outbox_candidate(self.second, "b" * 32)
        second.publish_outbox()

        first.finalize(claim, None)
        self._reset_to_remote(self.second)
        remaining = UpdateQueueStore(self.second).snapshot().candidates

        self.assertNotIn(first_candidate, remaining)
        self.assertIn(later_candidate, remaining)

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

    def test_finalization_publishes_semantic_commit_before_consuming_candidate(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        prepared = self.root / "prepared"
        self._git(self.first, "worktree", "add", "--detach", str(prepared), claim.lease_commit)
        (prepared / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` synchronized → []\n",
            encoding="utf-8",
        )
        self._git(prepared, "add", "MEMORY.md")
        self._git(prepared, "commit", "-m", "memory: synchronized update")
        candidate_commit = self._git(prepared, "rev-parse", "HEAD")

        coordinator.finalize(claim, candidate_commit)

        self.assertIn("synchronized", (self.first / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(UpdateQueueStore(self.first).snapshot().candidates, ())

    def test_semantic_change_after_claim_rejects_prepared_result(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        self._git(self.second, "fetch", "origin")
        self._git(self.second, "reset", "--hard", "origin/main")
        (self.second / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `remote` changed → []\n",
            encoding="utf-8",
        )
        self._git(self.second, "add", "MEMORY.md")
        self._git(self.second, "commit", "-m", "memory: concurrent change")
        self._git(self.second, "push")

        with self.assertRaises(UpdateQueueSemanticBaseChanged):
            coordinator.finalize(claim, None)

        self._git(self.second, "fetch", "origin")
        self.assertIsNotNone(UpdateQueueStore(self.second).snapshot().lease)

    def test_unpushed_local_semantic_change_after_claim_rejects_result(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(claim)
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `local` changed → []\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", "MEMORY.md")
        self._git(self.first, "commit", "-m", "memory: local concurrent change")

        with self.assertRaises(UpdateQueueSemanticBaseChanged):
            coordinator.finalize(claim, None)

    def test_claim_does_not_publish_lease_from_locally_ahead_head(self):
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

        result = coordinator.claim_next(target_batch_candidates=1, max_wait_seconds=0)

        self.assertIsNone(result.claim)
        self._reset_to_remote(self.second)
        self.assertIsNone(UpdateQueueStore(self.second).snapshot().lease)

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
                coordinator.claim_next(target_batch_candidates=1, max_wait_seconds=0)

        self._reset_to_remote(self.second)
        self.assertIsNone(UpdateQueueStore(self.second).snapshot().lease)

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
        claim = first.claim_next(target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        first.release(claim)

        canceled = second.cancel(candidate.session_id, candidate.uid)
        self.assertEqual(canceled, candidate)
        terminal = second.superseded_batch_commit(
            "HEAD",
            claim.batch_id,
        )

        self.assertIsNotNone(terminal)

    def test_publication_stops_when_local_scheduler_has_taken_candidate(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)

        with mock.patch(
            "rightmemory.update_queue_git.AsyncUpdateStore.begin_publication",
            return_value=None,
        ), mock.patch.object(coordinator, "_push_cas") as push:
            result = coordinator.publish_outbox()

        self.assertEqual(result.unresolved_uids, (candidate.uid,))
        push.assert_not_called()

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

    def test_finalized_lookup_requires_exact_batch_and_token_trailers(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
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
        claim = first.claim_next(target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        landed = first.finalize(claim, None)

        integrated = second.fetch_and_integrate_finalized_batch(
            claim.batch_id,
            token=claim.lease.token,
        )

        self.assertEqual(integrated, landed)
        self.assertEqual(self._git(self.second, "rev-parse", "HEAD"), landed)

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
                target_batch_candidates=1,
                max_wait_seconds=0,
                now=datetime(2026, 7, 22, tzinfo=UTC),
            )

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
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=datetime(2026, 7, 22, tzinfo=UTC),
        )

        self.assertEqual(selected, ())
        self.assertIsNone(deadline)

    def _outbox_candidate(self, root: Path, uid: str) -> UpdateCandidate:
        candidate = UpdateCandidate(
            uid=uid,
            session_id="session-a",
            display_id=1,
            message="remember this",
            submitted_at=datetime(2026, 7, 21, tzinfo=UTC).isoformat(),
        )
        UpdateQueueStore(root).write_outbox(candidate)
        return candidate

    def _coordinator(self, root: Path, device_id: str) -> GitUpdateQueueCoordinator:
        return GitUpdateQueueCoordinator(
            SyncConfig(memory_root=root, enabled=True),
            device_id=device_id,
        )

    def _manual_recovery_coordinator(self) -> GitUpdateQueueCoordinator:
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        first_claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(first_claim)
        coordinator.fail(first_claim, reason_code="processing_failed")
        second_claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        ).claim
        self.assertIsNotNone(second_claim)
        recovery = coordinator.fail(second_claim, reason_code="processing_failed")
        self.assertTrue(recovery.manual_recovery)
        return coordinator

    def _reset_to_remote(self, root: Path) -> None:
        self._git(root, "fetch", "origin")
        self._git(root, "reset", "--hard", "origin/main")

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            self.fail(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
