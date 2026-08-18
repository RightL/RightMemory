from datetime import UTC, datetime
from unittest import mock

from rightmemory.update_queue import UpdateCandidate, UpdateQueueStore
from rightmemory.update_queue_git import UpdateQueueSemanticBaseChanged
from tests.update_queue_git_base import GitUpdateQueueTestBase


class GitUpdateQueuePublicationTests(GitUpdateQueueTestBase):
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

    def test_publication_pushes_local_state_before_candidate_commit(self):
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one` first -> []\n"
            "- `context` local state before candidate -> []\n",
            encoding="utf-8",
        )
        self._git(self.first, "add", "MEMORY.md")
        self._git(self.first, "commit", "-m", "memory: candidate context")
        state_commit = self._git(self.first, "rev-parse", "HEAD")
        candidate = self._outbox_candidate(self.first, "a" * 32)

        result = self._coordinator(self.first, "1" * 32).publish_outbox()

        self.assertEqual(result.published_uids, (candidate.uid,))
        self._git(self.second, "fetch", "origin")
        candidate_commit = self._git(
            self.second,
            "log",
            "-1",
            "--format=%H",
            "origin/main",
            "--",
            f"update_queue/candidates/{candidate.uid}.json",
        )
        self.assertEqual(
            self._git(self.second, "show", "-s", "--format=%P", candidate_commit),
            state_commit,
        )
        self.assertIn(
            "local state before candidate",
            self._git(self.second, "show", "origin/main:MEMORY.md"),
        )

    def test_publication_retains_outbox_when_state_sync_fails(self):
        candidate = self._outbox_candidate(self.first, "a" * 32)
        (self.first / "rightmemory.toml").write_text("[sync]\nenabled = true\n", encoding="utf-8")
        self._git(self.first, "add", "rightmemory.toml")
        self._git(self.first, "commit", "-m", "local machine config")
        remote_head = self._git(self.second, "rev-parse", "origin/main")

        result = self._coordinator(self.first, "1" * 32).publish_outbox()

        self.assertEqual(result.published_uids, ())
        self.assertEqual(result.unresolved_uids, (candidate.uid,))
        self.assertFalse(result.online)
        self.assertIsNotNone(UpdateQueueStore(self.first).read_outbox(candidate.uid))
        self._git(self.second, "fetch", "origin")
        self.assertEqual(self._git(self.second, "rev-parse", "origin/main"), remote_head)

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

    def test_finalization_preserves_candidates_published_after_the_claim(self):
        first_candidate = self._outbox_candidate(self.first, "a" * 32)
        first = self._coordinator(self.first, "1" * 32)
        second = self._coordinator(self.second, "2" * 32)
        first.publish_outbox()
        claim = first.claim_next(trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0).claim
        self.assertIsNotNone(claim)
        later_candidate = self._outbox_candidate(self.second, "b" * 32)
        second.publish_outbox()

        first.finalize(claim, None)
        self._reset_to_remote(self.second)
        remaining = UpdateQueueStore(self.second).snapshot().candidates

        self.assertNotIn(first_candidate, remaining)
        self.assertIn(later_candidate, remaining)

    def test_finalization_publishes_semantic_commit_before_consuming_candidate(self):
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        claim = coordinator.claim_next(
            trigger_candidates=1,
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
            trigger_candidates=1,
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
            trigger_candidates=1,
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
