import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.learning import LEARNING_DOMAINS, learning_claims, list_learning_candidates, submit_learning
from rightmemory.learning_coordinator import (
    LearningClaimLost, LearningCoordinator, LearningSemanticBaseChanged, LearningUnavailable,
)
from tests.isolated_write_test_base import IsolatedWriteTestBase
from tests.sync_test_base import SyncTestBase


class LocalLearningCoordinatorTests(IsolatedWriteTestBase):
    def _coordinator(self, holder="a" * 32):
        return LearningCoordinator(SyncConfig(memory_root=self.root), holder_id=holder)

    def _submit(self, evidence="Evidence."):
        return submit_learning(self.root, "session", evidence, request_id="request").candidate_id

    def _complete(self, coordinator, claim):
        return coordinator.finalize(claim, None, prepared_start_commit=self._git("rev-parse", "HEAD"))

    def _prepared(self, role, relative, text):
        base = self._git("rev-parse", "HEAD")
        worktree = Path(self.tempdir.name).parent / ("prepared-" + self.root.name)
        self._git("worktree", "add", "--detach", str(worktree), base)
        try:
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self._git("add", "-f", "--", relative, cwd=worktree)
            self._git("commit", "-m", role + ": prepared", cwd=worktree)
            return base, self._git("rev-parse", "HEAD", cwd=worktree)
        finally:
            self._git("worktree", "remove", "--force", str(worktree))

    def test_no_change_consumers_finish_once_and_last_completion_retires_raw(self):
        candidate_id = self._submit()
        coordinator = self._coordinator()
        source = self.root / "learning" / "candidates" / f"{candidate_id}.json"
        for position, domain in enumerate(LEARNING_DOMAINS):
            result = coordinator.claim(domain)
            self.assertTrue(result.claimed)
            self.assertEqual(result.candidates[0].candidate_id, candidate_id)
            claim = result.claim
            prepared_base = self._git("rev-parse", "HEAD")
            landed = coordinator.finalize(claim, None, prepared_start_commit=prepared_base)
            self.assertEqual(coordinator.finalized_commit(claim), landed)
            self.assertEqual(coordinator.finalize(claim, None, prepared_start_commit=prepared_base), landed)
            self.assertEqual(self._git("rev-parse", "HEAD"), landed)
            self.assertEqual(list_learning_candidates(self.root, domain=domain), ())
            self.assertEqual(source.exists(), position < 2)
        self.assertEqual(learning_claims(self.root), ())
        self.assertEqual(list_learning_candidates(self.root), ())

    def test_existing_claim_is_never_stolen_implicitly(self):
        self._submit()
        first = self._coordinator().claim("memory")
        same = self._coordinator().claim("memory")
        other = self._coordinator("b" * 32).claim("memory")
        self.assertEqual(same.claim, first.claim)
        self.assertFalse(same.claimed)
        self.assertFalse(same.foreign)
        self.assertFalse(other.claimed)
        self.assertTrue(other.foreign)
        self.assertEqual(other.claim, first.claim)

    def test_explicit_replacement_fences_the_old_worker(self):
        self._submit()
        coordinator = self._coordinator()
        first = coordinator.claim("memory").claim
        before = self._git("rev-parse", "HEAD")
        second = coordinator.claim("memory", replace_token=first.token).claim
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(first.candidate_hashes, second.candidate_hashes)
        with self.assertRaises(LearningClaimLost):
            coordinator.finalize(first, None, prepared_start_commit=before)
        self._complete(coordinator, second)

    def test_publish_interruption_keeps_claim_and_can_retry_exact_prepared_result(self):
        self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("memory").claim
        base, prepared = self._prepared("update", "MEMORY.md", "# Memory\n\n- `new` retained context → []\n")
        with patch.object(coordinator, "_publish", side_effect=LearningUnavailable("interrupted")):
            with self.assertRaises(LearningUnavailable):
                coordinator.finalize(claim, prepared, prepared_start_commit=base)
        self.assertEqual(self._git("rev-parse", "HEAD"), base)
        self.assertEqual(coordinator.current_claim("memory"), claim)
        landed = coordinator.finalize(claim, prepared, prepared_start_commit=base)
        self.assertEqual(self._git("rev-parse", "HEAD"), landed)
        self.assertIn("retained context", (self.root / "MEMORY.md").read_text())
        self.assertEqual(list_learning_candidates(self.root, domain="memory"), ())

    def test_changed_domain_context_refuses_stale_prepared_judgment(self):
        self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("memory").claim
        base = self._git("rev-parse", "HEAD")
        self._append_memory(self.root, "\n- `other` newer context → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: other change")
        with self.assertRaises(LearningSemanticBaseChanged):
            coordinator.finalize(claim, None, prepared_start_commit=base)
        self.assertEqual(coordinator.current_claim("memory"), claim)
        self.assertEqual(len(list_learning_candidates(self.root, domain="memory")), 1)

    def test_semantic_result_cannot_forge_internal_receipts(self):
        candidate_id = self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("memory").claim
        relative = f"learning/processed/memory/{candidate_id}.json"
        base, prepared = self._prepared("update", relative, "{}")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            coordinator.finalize(claim, prepared, prepared_start_commit=base)
        self.assertFalse((self.root / relative).exists())

    def test_evidence_hash_is_frozen_in_the_claim(self):
        candidate_id = self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("memory").claim
        base = self._git("rev-parse", "HEAD")
        path = self.root / "learning" / "candidates" / f"{candidate_id}.json"
        data = json.loads(path.read_text())
        data["evidence"] = "Substituted evidence."
        data["evidence_sha256"] = hashlib.sha256(data["evidence"].encode()).hexdigest()
        path.write_text(json.dumps(data))
        self._git("add", "-f", "--", str(path))
        self._git("commit", "-m", "bad evidence substitution")
        with self.assertRaisesRegex(LearningUnavailable, "evidence changed"):
            coordinator.finalize(claim, None, prepared_start_commit=base)

    def test_withdrawal_is_fenced_by_every_active_domain_and_keeps_retry_identity(self):
        candidate_id = self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("skills").claim
        with self.assertRaisesRegex(ValueError, "active curator task"):
            coordinator.withdraw(candidate_id)
        coordinator.release(claim)
        self.assertEqual(coordinator.withdraw(candidate_id), "withdrawn")
        self.assertEqual(coordinator.withdraw(candidate_id), "withdrawn")
        self.assertEqual(submit_learning(self.root, "session", "Evidence.", request_id="request").status, "withdrawn")
        with self.assertRaises(LearningClaimLost):
            self._complete(coordinator, claim)

    def test_maintenance_claim_does_not_acknowledge_pending_candidates(self):
        self._submit()
        coordinator = self._coordinator()
        claim = coordinator.claim("memory", mode="maintain", scope="Existing context").claim
        self.assertEqual(claim.candidate_ids, ())
        self._complete(coordinator, claim)
        self.assertEqual(len(list_learning_candidates(self.root, domain="memory")), 1)

    def test_notices_identity_reads_do_not_create_runtime_state(self):
        coordinator = LearningCoordinator(SyncConfig(memory_root=self.root))
        self.assertIsNone(coordinator.holder_id)
        self.assertIsNone(coordinator.current_claim("memory"))
        self.assertFalse((self.root / ".runtime").exists())
        self._submit()
        coordinator.claim("memory")
        identity = coordinator.holder_id
        self.assertEqual(LearningCoordinator(SyncConfig(memory_root=self.root)).holder_id, identity)

    def test_default_claims_bound_count_and_evidence_without_losing_large_first_item(self):
        for index in range(22):
            submit_learning(self.root, "session", f"Evidence {index}.")
        coordinator = self._coordinator()
        claim = coordinator.claim("memory").claim
        self.assertEqual(len(claim.candidate_ids), 20)
        self._complete(coordinator, claim)
        next_claim = coordinator.claim("memory").claim
        self.assertEqual(len(next_claim.candidate_ids), 2)
        self._complete(coordinator, next_claim)
        submit_learning(self.root, "session", "x" * 30_000)
        submit_learning(self.root, "session", "Tail.")
        large = coordinator.claim("memory")
        self.assertEqual(len(large.candidates), 1)
        self.assertEqual(len(large.candidates[0].evidence), 30_000)


class SynchronizedLearningCoordinatorTests(SyncTestBase):
    def _coordinator(self, root, holder):
        return LearningCoordinator(SyncConfig(memory_root=root, enabled=True), holder_id=holder * 32)

    def _seed(self):
        return submit_learning(self.device, "session", "Evidence.", request_id="request").candidate_id

    def test_domains_on_different_devices_share_evidence_and_receipts(self):
        candidate_id = self._seed()
        local = self._coordinator(self.device, "a")
        remote = self._coordinator(self.other, "b")
        first = local.claim("memory").claim
        second = remote.claim("guidance").claim
        foreign = remote.claim("memory")
        self.assertTrue(foreign.foreign)
        self.assertEqual(foreign.claim, first)
        for coordinator, root, claim in ((local, self.device, first), (remote, self.other, second)):
            base = self._git(root, "rev-parse", "HEAD")
            coordinator.finalize(claim, None, prepared_start_commit=base)
        third = local.claim("skills").claim
        local.finalize(third, None, prepared_start_commit=self._git(self.device, "rev-parse", "HEAD"))
        remote.claim("skills")
        for root in (self.device, self.other):
            self.assertFalse((root / "learning" / "candidates" / f"{candidate_id}.json").exists())
            self.assertEqual(list_learning_candidates(root), ())

    def test_explicit_cross_device_replacement_prevents_old_publication(self):
        self._seed()
        local = self._coordinator(self.device, "a")
        remote = self._coordinator(self.other, "b")
        old = local.claim("memory").claim
        old_base = self._git(self.device, "rev-parse", "HEAD")
        new = remote.claim("memory", replace_token=old.token).claim
        with self.assertRaises(LearningClaimLost):
            local.finalize(old, None, prepared_start_commit=old_base)
        remote.finalize(new, None, prepared_start_commit=self._git(self.other, "rev-parse", "HEAD"))

    def test_lost_publication_ack_is_recovered_from_remote_proof(self):
        self._seed()
        coordinator = self._coordinator(self.device, "a")
        claim = coordinator.claim("memory").claim
        base = self._git(self.device, "rev-parse", "HEAD")
        original = coordinator._advance_active

        def lose_ack(commit):
            if coordinator._find_finalized(commit, claim) is not None:
                raise LearningUnavailable("lost after remote publication")
            return original(commit)

        with patch.object(coordinator, "_advance_active", side_effect=lose_ack):
            with self.assertRaises(LearningUnavailable):
                coordinator.finalize(claim, None, prepared_start_commit=base)
        remote_head = self._git(self.remote, "rev-parse", "main")
        landed = coordinator.finalize(claim, None, prepared_start_commit=base)
        self.assertEqual(landed, remote_head)
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), remote_head)
        self.assertEqual(list_learning_candidates(self.device, domain="memory"), ())

    def test_offline_capture_remains_durable_but_cannot_claim_synchronized_work(self):
        self._git(self.device, "remote", "set-url", "origin", str(self.root / "offline.git"))
        candidate_id = self._seed()
        with self.assertRaises(LearningUnavailable):
            self._coordinator(self.device, "a").claim("memory")
        self.assertEqual(list_learning_candidates(self.device)[0].candidate_id, candidate_id)
        self.assertEqual(learning_claims(self.device), ())

    def test_new_local_capture_during_remote_publication_survives_recovery(self):
        self._seed()
        coordinator = self._coordinator(self.device, "a")
        claim = coordinator.claim("memory").claim
        base = self._git(self.device, "rev-parse", "HEAD")
        original = coordinator._advance_active
        submitted = []

        def advance(commit):
            if not submitted and coordinator._find_finalized(commit, claim) is not None:
                submitted.append(submit_learning(self.device, "later", "New evidence.").candidate_id)
            return original(commit)

        with patch.object(coordinator, "_advance_active", side_effect=advance):
            with self.assertRaisesRegex(LearningUnavailable, "requires sync"):
                coordinator.finalize(claim, None, prepared_start_commit=base)
        coordinator.finalize(claim, None, prepared_start_commit=base)
        pending = list_learning_candidates(self.device, domain="memory")
        self.assertEqual([item.candidate_id for item in pending], submitted)
        self.assertEqual(pending[0].evidence, "New evidence.")
