import contextlib
import hashlib
import io
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant_write import AssistantStaleVersionError, AssistantWriteManager
from rightmemory.config import RuntimeConfig, load_config, load_sync_config
from rightmemory.insight_trigger import InsightTriggerStore
from rightmemory.learning import LEARNING_DOMAINS, learning_claims, list_learning_candidates
from rightmemory.curation_cli import main as curate_main, submit_main
from rightmemory.learning_coordinator import LearningCoordinator
from rightmemory.learning_service import (
    LearningClaimLost,
    LearningService,
)
from rightmemory.semantic_operation import SemanticOperationStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class LearningServiceTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        (self.root / ".git/info").mkdir(exist_ok=True)
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as stream:
            stream.write("\n/rightmemory.toml\n")
        (self.root / "rightmemory.toml").write_text(
            "[curation.batch]\ntrigger_candidates = 1\ntarget_batch_candidates = 20\n",
            encoding="utf-8",
        )
        self.service = LearningService(self.root)

    def _use_default_batch_policy(self):
        (self.root / "rightmemory.toml").unlink()
        self.service = LearningService(self.root)

    def test_default_small_backlog_waits_without_claim_worktree_or_model(self):
        self._use_default_batch_policy()
        self._submit()
        candidate = list_learning_candidates(self.root)[0]
        expected_ready = datetime.fromisoformat(candidate.submitted_at) + timedelta(hours=25)
        head = self._git("rev-parse", "HEAD")
        with (
            patch("rightmemory.learning_coordinator.LearningCoordinator._worktree", side_effect=AssertionError("worktree created")),
            patch("rightmemory.runtime.RightMemoryRuntime", side_effect=AssertionError("model invoked")),
            patch("rightmemory.config.load_config", side_effect=AssertionError("model config loaded")),
        ):
            notices = self.service.notices()
            for domain in LEARNING_DOMAINS:
                self.assertEqual(self.service.begin(domain)["status"], "not_ready")
                self.assertEqual(self.service.run(domain)["status"], "not_ready")
        for notice in notices:
            self.assertFalse(notice["ready"])
            self.assertEqual(notice["status"], "waiting")
            self.assertEqual(notice["candidate_count"], 1)
            self.assertEqual(datetime.fromisoformat(notice["next_ready_at"]), expected_ready)
        self.assertEqual(learning_claims(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertFalse(list((self.root / ".runtime/worktrees").glob("*")))

    def test_default_count_trigger_is_immediate_and_domains_wait_independently(self):
        self._use_default_batch_policy()
        for index in range(15):
            self._submit(f"Observation {index}", f"request-{index}")
        for notice in self.service.notices():
            self.assertTrue(notice["ready"])
            self.assertIsNone(notice["next_ready_at"])
        task = self.service.begin("guidance")
        self.assertEqual(task["selected_candidate_count"], 15)
        self.service.complete(task["task_id"])
        self._submit("A later observation", "later")
        notices = {notice["domain"]: notice for notice in self.service.notices()}
        self.assertFalse(notices["guidance"]["ready"])
        self.assertEqual(notices["guidance"]["candidate_count"], 1)
        for domain in ("memory", "skills"):
            self.assertTrue(notices[domain]["ready"])
            self.assertEqual(notices[domain]["candidate_count"], 16)
        self.assertEqual(self.service.begin("guidance")["status"], "not_ready")

    def test_default_elapsed_wait_allows_a_single_candidate(self):
        self._use_default_batch_policy()
        self._submit()
        submitted = datetime.fromisoformat(list_learning_candidates(self.root)[0].submitted_at)
        with patch("rightmemory.curation_batch.datetime") as clock:
            clock.now.return_value = submitted + timedelta(hours=25)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            notice = self.service.notices()[0]
            self.assertTrue(notice["ready"])
            self.assertIsNone(notice["next_ready_at"])
            task = self.service.begin("memory")
        self.assertEqual(task["selected_candidate_count"], 1)

    def test_existing_task_can_resume_and_retry_after_default_threshold_is_restored(self):
        self._submit()
        task = self.service.begin("guidance")
        self._use_default_batch_policy()
        resumed = self.service.begin("guidance")
        self.assertEqual(resumed["task_id"], task["task_id"])
        self.assertFalse(resumed["claimed"])
        replacement = self.service.begin("guidance", retry_task=task["task_id"])
        self.assertTrue(replacement["claimed"])
        self.assertEqual(replacement["selected_candidate_count"], 1)
        self.assertEqual(self.service.complete(replacement["task_id"])["status"], "completed")

    def test_default_maintenance_can_start_below_update_threshold(self):
        self._use_default_batch_policy()
        self._submit()
        task = self.service.begin("guidance", mode="maintain")
        self.assertEqual(task["selected_candidate_count"], 0)
        completed = self.service.complete(task["task_id"])
        notice = next(notice for notice in completed["curation"] if notice["domain"] == "guidance")
        self.assertFalse(notice["ready"])
        self.assertEqual(notice["candidate_count"], 1)

    def _submit(self, evidence="A reusable observation", request="request-one"):
        return self.service.submit("session-one", evidence, request)["candidate_id"]

    def _commit(self, task, relative, text):
        worktree = Path(task["worktree"])
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._git("add", "-f", "--", relative, cwd=worktree)
        self._git("commit", "-m", "curate evidence", cwd=worktree)

    def test_native_begin_is_model_free_and_reuses_a_durable_task(self):
        candidate_id = self._submit()
        with patch("rightmemory.config.load_config", side_effect=AssertionError("model config loaded")):
            task = self.service.begin("guidance")
            again = LearningService(self.root).begin("guidance")
        self.assertTrue(task["claimed"])
        self.assertFalse(again["claimed"])
        self.assertEqual(task["task_id"], again["task_id"])
        self.assertEqual(Path(task["worktree"]).parent, self.root / ".runtime" / "worktrees")
        payload = AssistantWriteManager(self.root, role="guidance-curator").read(task["task_id"]).input_data
        self.assertEqual(payload["candidates"][0]["candidate_id"], candidate_id)
        self.assertTrue(Path(task["instructions_file"]).is_file())

    def test_domains_complete_independently_and_last_completion_retires_raw(self):
        candidate_id = self._submit()
        guidance = self.service.begin("guidance")
        skills = self.service.begin("skills")
        self._commit(guidance, "learning/guidance/working.md", "# Working preferences\n\nAn observed preference.\n")
        result = self.service.complete(guidance["task_id"], "Retained one preference")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(list_learning_candidates(self.root, domain="guidance"))
        self.assertEqual(len(list_learning_candidates(self.root, domain="skills")), 1)
        self.assertEqual(len(list_learning_candidates(self.root)), 1)
        self.assertFalse(Path(guidance["worktree"]).exists())

        no_change = self.service.complete(skills["task_id"], "No durable method yet")
        self.assertEqual(no_change["changed_paths"], [])
        self.assertFalse(list_learning_candidates(self.root, domain="skills"))
        self.assertTrue((self.root / "learning/candidates" / f"{candidate_id}.json").is_file())
        head = self._git("rev-parse", "HEAD")
        replay = LearningService(self.root).complete(skills["task_id"])
        self.assertEqual(replay["landed_commit"], no_change["landed_commit"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        memory = self.service.begin("memory")
        memory_result = self.service.complete(memory["task_id"])
        self.assertEqual(memory_result["changed_paths"], [])
        self.assertFalse(list_learning_candidates(self.root))
        for domain in LEARNING_DOMAINS:
            self.assertTrue((self.root / f"learning/processed/{domain}/{candidate_id}.json").is_file())
        retried = self.service.submit("session-one", "A reusable observation", "request-one")
        self.assertFalse(retried["created"])
        self.assertFalse(list_learning_candidates(self.root))

    def test_new_raw_evidence_waits_for_the_next_batch(self):
        first = self._submit()
        task = self.service.begin("guidance")
        later = self._submit("A later observation", "request-two")
        self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="guidance")], [later])
        self.assertEqual({item.candidate_id for item in list_learning_candidates(self.root)}, {first, later})

    def test_maintenance_leaves_pending_evidence_ready_for_update(self):
        existing = self.root / "learning/guidance/working.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("# Existing guidance\n\nA scoped preference.\n")
        self._git("add", "learning/guidance/working.md")
        self._git("commit", "-m", "seed existing scoped guidance")
        candidate_ids = {
            self._submit("An unrelated pending observation", "first"),
            self._submit("Another pending observation", "second"),
        }
        task = self.service.begin("guidance", mode="maintain", scope="working.md")
        self.assertEqual(task["selected_candidate_count"], 0)
        self._commit(task, "learning/guidance/working.md", "# Existing guidance\n\nClarified scoped preference.\n")
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["selected_candidate_count"], 0)
        for domain in LEARNING_DOMAINS:
            self.assertEqual({item.candidate_id for item in list_learning_candidates(self.root, domain=domain)}, candidate_ids)
        self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))
        notice = next(item for item in completed["curation"] if item["domain"] == "guidance")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_candidate_count_and_leaves_backlog_ready(self):
        candidate_ids = [self._submit(f"Observation {index}", f"request-{index}") for index in range(20 + 2)]
        task = self.service.begin("guidance")
        self.assertEqual(task["selected_candidate_count"], 20)
        self.assertEqual(self.service.status(task["task_id"])["selected_candidate_count"], 20)
        completed = self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="guidance")], candidate_ids[20:])
        self.assertEqual(len(list_learning_candidates(self.root, domain="skills")), len(candidate_ids))
        notice = next(item for item in completed["curation"] if item["domain"] == "guidance")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_evidence_text_before_the_count_limit(self):
        size = 24_000 // 2 + 1
        first = self._submit("a" * size, "first")
        second = self._submit("b" * size, "second")
        task = self.service.begin("skills")
        self.assertEqual(task["selected_candidate_count"], 1)
        selected = AssistantWriteManager(self.root, role="skills-curator").read(task["task_id"]).input_data["candidates"]
        self.assertEqual(selected[0]["candidate_id"], first)
        self.assertTrue(all("evidence" not in item for item in selected))
        self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="skills")], [second])

    def test_oversized_first_source_uses_a_reference_and_keeps_its_frozen_proof(self):
        evidence = "x" * (24_000 + 5000)
        candidate_id = self._submit(evidence)
        task = self.service.begin("skills")
        manager = AssistantWriteManager(self.root, role="skills-curator")
        frozen = manager.read(task["task_id"]).input_data
        selected = frozen["candidates"]
        self.assertEqual(len(selected), 1)
        self.assertNotIn("evidence", selected[0])
        self.assertEqual(selected[0]["source_id"], f"L#candidates/{candidate_id}.json")
        self.assertEqual(selected[0]["source_file"], f"learning/candidates/{candidate_id}.json")
        self.assertEqual(selected[0]["evidence_chars"], len(evidence))
        stored = list_learning_candidates(self.root)[0]
        self.assertEqual(selected[0]["evidence_sha256"], stored.evidence_sha256)
        self.assertTrue((Path(task["worktree"]) / selected[0]["source_file"]).is_file())
        later = self._submit("New arrival after preparation", "later")
        repeated = LearningService(self.root).begin("skills")
        self.assertEqual(repeated["task_id"], task["task_id"])
        replacement = self.service.begin("skills", retry_task=task["task_id"])
        self.assertEqual(manager.read(replacement["task_id"]).input_data, frozen)
        self.service.complete(replacement["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="skills")], [later])

    def test_referenced_source_is_hash_checked_at_completion(self):
        candidate_id = self._submit("x" * (24_000 + 1))
        task = self.service.begin("guidance")
        relative = f"learning/candidates/{candidate_id}.json"
        source = self.root / relative
        data = json.loads(source.read_text())
        data["evidence"] = "y" + data["evidence"][1:]
        data["evidence_sha256"] = hashlib.sha256(data["evidence"].encode("utf-8")).hexdigest()
        source.write_text(json.dumps(data))
        self._git("add", relative)
        self._git("commit", "-m", "simulate external evidence replacement")
        with self.assertRaisesRegex(ValueError, "candidate evidence changed"):
            self.service.complete(task["task_id"])
        self.assertFalse((self.root / f"learning/processed/guidance/{candidate_id}.json").exists())

    def test_legacy_skill_edits_do_not_invalidate_the_guidance_task(self):
        self._append_memory(self.root, "\n## Review {S#review}\n")
        (self.root / "MEMORY_SKILL_review.md").write_text("Review the source.\n")
        self._git("add", "MEMORY.md", "MEMORY_SKILL_review.md")
        self._git("commit", "-m", "seed legacy skill")
        self._submit()
        guidance = self.service.begin("guidance")
        skills = self.service.begin("skills")
        self._commit(skills, "MEMORY_SKILL_review.md", "Review the source and check the result.\n")
        self.service.complete(skills["task_id"])
        result = self.service.complete(guidance["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(list_learning_candidates(self.root, domain="guidance"))

    def test_curator_cannot_edit_the_other_domain(self):
        self._submit()
        task = self.service.begin("guidance")
        self._commit(task, "learning/experiences/case.md", "# Case\n\nOutside this curator.\n")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertFalse((self.root / "learning/experiences/case.md").exists())
        self.assertEqual(len(list_learning_candidates(self.root, domain="guidance")), 1)

    def test_curator_cannot_edit_normal_memory(self):
        self._submit()
        task = self.service.begin("skills")
        self._commit(task, "MEMORY.md", "# Domain {#domain}\n\n- `one` changed → []\n")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertIn("initial memory", (self.root / "MEMORY.md").read_text())

    def test_curator_cannot_modify_raw_evidence(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance", mode="maintain")
        relative = f"learning/candidates/{candidate_id}.json"
        content = json.loads((Path(task["worktree"]) / relative).read_text())
        content["evidence"] = "rewritten evidence"
        self._commit(task, relative, json.dumps(content))
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(list_learning_candidates(self.root)[0].evidence, "A reusable observation")

    def test_curator_cannot_write_processing_receipts(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance")
        self._commit(task, f"learning/processed/guidance/{candidate_id}.json", "{}")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root, domain="guidance")), 1)

    def test_maintenance_cannot_delete_program_owned_raw(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance", mode="maintain")
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{candidate_id}.json", cwd=worktree)
        self._git("commit", "-m", "premature retirement", cwd=worktree)
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_completed_tasks_keep_proofs_and_remove_active_manifests(self):
        evidence = "A concrete piece of evidence that should leave the candidate store"
        candidate_id = self._submit(evidence)
        for domain in LEARNING_DOMAINS:
            task = self.service.begin(domain)
            result = self.service.complete(task["task_id"])
            record = self.service._task(task["task_id"])
            selected = record["input"]["candidates"]
            self.assertEqual([item["candidate_id"] for item in selected], [candidate_id])
            self.assertNotIn("evidence", selected[0])
            operation = SemanticOperationStore(self.root).read(result["operation_id"])
            self.assertEqual(operation.input_data, {
                "role": "update" if domain == "memory" else f"{domain}-curator",
                "kind": "curation", "task_id": task["task_id"],
            })
            self.assertFalse(Path(task["candidates_file"]).exists())
            self.assertFalse(Path(task["instructions_file"]).exists())
            self.assertFalse(Path(task["worktree"]).exists())
        self.assertFalse(list_learning_candidates(self.root))

    def test_same_domain_changes_require_retry_and_fence_old_task(self):
        self._submit()
        task = self.service.begin("guidance")
        path = self.root / "learning/guidance/new.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# New related guidance\n")
        self._git("add", "learning/guidance/new.md")
        self._git("commit", "-m", "another writer changed guidance")
        with self.assertRaises(AssistantStaleVersionError):
            self.service.complete(task["task_id"])
        replacement = self.service.begin("guidance", retry_task=task["task_id"])
        self.assertNotEqual(replacement["task_id"], task["task_id"])
        self.assertTrue(Path(task["worktree"]).exists())
        with self.assertRaises(LearningClaimLost):
            self.service.complete(task["task_id"])
        self.service.complete(replacement["task_id"])

    def test_update_cannot_delete_evidence_still_pending_for_another_domain(self):
        old = self._submit()
        for domain in ("guidance", "skills"):
            task = self.service.begin(domain)
            self.service.complete(task["task_id"])
        self._submit("New work to consider", "request-two")
        task = self.service.begin("skills")
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{old}.json", cwd=worktree)
        self._git("commit", "-m", "retire an old case during update", cwd=worktree)
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertTrue((self.root / f"learning/candidates/{old}.json").is_file())

    def test_skill_assets_publish_as_regular_files(self):
        self._submit()
        task = self.service.begin("skills")
        worktree = Path(task["worktree"])
        skill = worktree / "learning/skills/reproduce-report"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Reproduce report\n\nUse the accompanying fixture.\n")
        (skill / "fixture.bin").write_bytes(b"\x00\x01\xff")
        self._git("add", "learning/skills/reproduce-report", cwd=worktree)
        self._git("commit", "-m", "retain reproducible skill and fixture", cwd=worktree)
        self.service.complete(task["task_id"])
        self.assertEqual((self.root / "learning/skills/reproduce-report/fixture.bin").read_bytes(), b"\x00\x01\xff")

    def test_prepared_completion_retries_without_repeating_curation(self):
        self._submit()
        task = self.service.begin("skills")
        self._commit(task, "learning/experiences/case.md", "# Case\n\nUseful rough evidence.\n")
        with patch.object(LearningCoordinator, "finalize", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.service.complete(task["task_id"], "Retained a case")
        writer = AssistantWriteManager(self.root, role="skills-curator")
        operation = SemanticOperationStore(self.root).read(writer.read(task["task_id"]).operation_id)
        self.assertEqual(operation.phase, "prepared")
        self.assertFalse((self.root / "learning/experiences/case.md").exists())
        completed = LearningService(self.root).complete(task["task_id"])
        self.assertEqual(completed["output"], "Retained a case")
        self.assertEqual(completed["status"], "completed")

    def test_lost_reply_after_publication_is_recovered_by_status(self):
        self._submit()
        task = self.service.begin("guidance")
        original = LearningCoordinator.finalize

        def lose_reply(coordinator, *args, **kwargs):
            original(coordinator, *args, **kwargs)
            raise RuntimeError("lost reply")

        with patch.object(LearningCoordinator, "finalize", lose_reply):
            with self.assertRaisesRegex(RuntimeError, "lost reply"):
                self.service.complete(task["task_id"], "No rule yet")
        head = self._git("rev-parse", "HEAD")
        result = LearningService(self.root).status(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"], "No rule yet")
        self.assertEqual(self._git("rev-parse", "HEAD"), head)

    def test_configured_execution_resumes_failed_task_and_uses_prepared_worktree(self):
        self._submit()
        configurations = []
        calls = []
        owner = self

        class Executor:
            def __init__(self, config):
                configurations.append(config)
                self.config = config

            def run_session_turn(self, task_id, _message):
                calls.append(task_id)
                if len(calls) == 1:
                    raise RuntimeError("provider unavailable")
                owner._commit({"worktree": str(self.config.memory_root)}, "learning/guidance/work.md", "# Working guidance\n")
                return "Done"

            def cleanup(self):
                pass

        config = RuntimeConfig(role="guidance-curator", model_id="fake", memory_root=self.root, state_root=self.root)
        with patch("rightmemory.config.load_config", return_value=config), patch("rightmemory.runtime.RightMemoryRuntime", Executor):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                self.service.run("guidance")
            self._use_default_batch_policy()
            notice = next(item for item in self.service.notices() if item["domain"] == "guidance")
            self.assertTrue(notice["ready"])
            self.assertEqual(notice["status"], "resume")
            completed = LearningService(self.root).run("guidance")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(configurations[-1].state_root, self.root)
        self.assertNotEqual(configurations[-1].memory_root, self.root)
        self.assertFalse(configurations[-1].sync.enabled)

    def test_curator_executor_inherits_update_configuration(self):
        (self.root / "rightmemory.toml").write_text('[update.model]\nmodel_id = "example"\n')
        for domain in ("guidance", "skills"):
            config = load_config(f"{domain}-curator", self.root)
            self.assertEqual(config.model_id, "example")
            self.assertEqual(config.role, f"{domain}-curator")

    def test_requested_maintenance_is_not_satisfied_by_an_active_update(self):
        self._submit()
        active = self.service.begin("skills")
        result = self.service.run("skills", mode="maintain", scope="older cases")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["task_id"], active["task_id"])
        self.assertEqual(result["requested_mode"], "maintain")
        self.assertEqual(result["requested_scope"], "older cases")
        self.assertEqual(len(list_learning_candidates(self.root, domain="skills")), 1)
        self.service.complete(active["task_id"])
        maintenance = self.service.begin("skills", mode="maintain", scope="older cases")
        self.assertEqual(maintenance["mode"], "maintain")
        self.assertNotEqual(maintenance["task_id"], active["task_id"])

    def test_memory_maintenance_uses_shared_writer_without_consuming_candidates(self):
        candidate_id = self._submit()
        task = self.service.begin("memory", mode="maintain", scope="domain")
        self.assertEqual(task["selected_candidate_count"], 0)
        text = (Path(task["worktree"]) / "MEMORY.md").read_text()
        self._commit(task, "MEMORY.md", text.replace("initial memory", "maintained memory"))
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["changed_paths"], ["MEMORY.md"])
        self.assertIn("maintained memory", (self.root / "MEMORY.md").read_text())
        for domain in LEARNING_DOMAINS:
            self.assertEqual(
                [item.candidate_id for item in list_learning_candidates(self.root, domain=domain)],
                [candidate_id],
            )
        self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))

    def test_explicit_retry_can_replace_foreign_claim_without_local_task(self):
        candidate_id = self._submit()
        foreign = LearningCoordinator(load_sync_config(self.root), holder_id="1" * 32)
        claim = foreign.claim("guidance", task_id="guidance-" + "a" * 32).claim
        notice = next(item for item in self.service.notices() if item["domain"] == "guidance")
        self.assertTrue(notice["foreign"])
        self.assertFalse(notice["ready"])
        self.assertEqual(self.service.begin("guidance")["task_id"], claim.task_id)
        replacement = self.service.begin("guidance", retry_task=claim.task_id)
        self.assertTrue(replacement["claimed"])
        self.assertNotEqual(replacement["task_id"], claim.task_id)
        self.assertEqual(replacement["selected_candidate_count"], 1)
        with self.assertRaises(LearningClaimLost):
            foreign.finalize(claim, None, prepared_start_commit=claim.base_commit)
        self.service.complete(replacement["task_id"])
        self.assertFalse(list_learning_candidates(self.root, domain="guidance"))
        self.assertEqual(list_learning_candidates(self.root)[0].candidate_id, candidate_id)

    def test_replaced_local_task_is_reported_superseded_and_never_resumed(self):
        self._submit()
        task = self.service.begin("guidance")
        old = self.service.coordinator.current_claim("guidance")
        foreign = LearningCoordinator(load_sync_config(self.root), holder_id="1" * 32)
        replacement = foreign.claim(
            "guidance", task_id="guidance-" + "b" * 32, replace_token=old.token,
        ).claim
        notice = next(item for item in self.service.notices() if item["domain"] == "guidance")
        self.assertEqual(notice["task_id"], replacement.task_id)
        self.assertTrue(notice["foreign"])
        status = self.service.status(task["task_id"])
        self.assertEqual(status["status"], "superseded")
        self.assertEqual(status["replacement_task"], replacement.task_id)
        self.assertEqual(self.service.begin("guidance")["task_id"], replacement.task_id)
        with self.assertRaises(LearningClaimLost):
            self.service.complete(task["task_id"])
        self.assertTrue(Path(task["worktree"]).is_dir())

    def test_submission_executor_controls_wake_without_ambiguating_saved_evidence(self):
        with patch("rightmemory.curation_worker.CurationWorker.wake", side_effect=OSError("worker unavailable")) as wake:
            native = self.service.submit("s", "First observation", "native", executor="native")
            wake.assert_not_called()
            configured = self.service.submit("s", "Second observation", "configured", executor="configured")
        self.assertTrue(native["saved"])
        self.assertTrue(configured["saved"])
        self.assertEqual(configured["worker"]["status"], "error")
        self.assertEqual(len(list_learning_candidates(self.root)), 2)

    def test_pending_effects_remain_visible_and_recover_before_new_candidates(self):
        from rightmemory.install_core import InstallError, Installer

        self._submit()
        task = self.service.begin("memory")
        text = (Path(task["worktree"]) / "MEMORY.md").read_text()
        self._commit(task, "MEMORY.md", text + "- `retained` durable result → []\n")
        with patch("rightmemory.automatic_effects.publish_file_view_outbox", side_effect=OSError("hub offline")):
            completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertIn("retained", (self.root / "MEMORY.md").read_text())
        self.assertEqual({effect["name"]: effect["status"] for effect in completed["effects"]}, {
            "memory-pressure": "done", "file-view-publish": "failed",
        })
        points = InsightTriggerStore(self.root).read().points
        self.assertGreater(points, 0)
        later = self._submit("New evidence arriving before publication recovery", "later")
        self._use_default_batch_policy()
        before = {path.relative_to(self.root): path.read_bytes() for path in (self.root / ".runtime").rglob("*") if path.is_file() and not path.is_symlink()}
        notice = next(item for item in self.service.notices() if item["domain"] == "memory")
        after = {path.relative_to(self.root): path.read_bytes() for path in (self.root / ".runtime").rglob("*") if path.is_file() and not path.is_symlink()}
        self.assertEqual(before, after)
        self.assertEqual(notice["status"], "effects_pending")
        self.assertEqual(notice["task_id"], task["task_id"])
        self.assertEqual(notice["candidate_count"], 1)
        installer = Installer(Path(__file__).resolve().parents[1], "assistant", self.root, [])
        with self.assertRaisesRegex(InstallError, task["task_id"]):
            installer._require_installable_tasks()
        head = self._git("rev-parse", "HEAD")
        with (
            patch("rightmemory.runtime.RightMemoryRuntime", side_effect=AssertionError("model must not rerun")),
            patch("rightmemory.automatic_effects.publish_file_view_outbox", return_value=[]),
        ):
            retried = self.service.run("memory")
        self.assertEqual(retried["status"], "completed")
        self.assertTrue(all(effect["status"] == "done" for effect in retried["effects"]))
        self.assertEqual(retried["task_id"], task["task_id"])
        self.assertEqual(InsightTriggerStore(self.root).read().points, points)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="memory")], [later])
        notice = next(item for item in self.service.notices() if item["domain"] == "memory")
        self.assertEqual(notice["status"], "waiting")
        self.assertEqual(notice["candidate_count"], 1)
        installer._require_installable_tasks()

    def test_lost_pressure_effect_receipt_does_not_increment_twice(self):
        self._submit()
        task = self.service.begin("memory")
        text = (Path(task["worktree"]) / "MEMORY.md").read_text()
        self._commit(task, "MEMORY.md", text + "- `retained` durable result → []\n")
        mark = SemanticOperationStore.mark_effect
        interrupted = []

        def lose_receipt(store, operation_id, effect_name, status, **kwargs):
            if effect_name == "memory-pressure" and status == "done" and not interrupted:
                interrupted.append(operation_id)
                raise OSError("effect receipt interrupted")
            return mark(store, operation_id, effect_name, status, **kwargs)

        with patch.object(SemanticOperationStore, "mark_effect", lose_receipt):
            completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["status"], "completed")
        points = InsightTriggerStore(self.root).read().points
        self.assertGreater(points, 0)
        retried = self.service.complete(task["task_id"])
        self.assertTrue(all(effect["status"] == "done" for effect in retried["effects"]))
        self.assertEqual(InsightTriggerStore(self.root).read().points, points)

    def test_no_change_completion_does_not_add_memory_pressure(self):
        self._submit()
        task = self.service.begin("memory")
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["changed_paths"], [])
        self.assertEqual(InsightTriggerStore(self.root).read().points, 0)
        self.assertEqual([effect["name"] for effect in completed["effects"]], ["file-view-publish"])

    def test_effect_store_failure_keeps_completed_outcome_recoverable(self):
        self._submit()
        task = self.service.begin("memory")
        with patch("rightmemory.learning_service.replay_memory_operation_effects", side_effect=OSError("receipt store unavailable")):
            completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertIn("receipt store unavailable", completed["effects_error"])
        self.assertFalse(list_learning_candidates(self.root, domain="memory"))
        retried = self.service.complete(task["task_id"])
        self.assertNotIn("effects_error", retried)
        self.assertTrue(all(effect["status"] == "done" for effect in retried["effects"]))

    def test_cli_submit_and_maintenance_bind_an_explicit_root(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = submit_main(["--root", str(self.root), "--session", "s", "--request-id", "r", "--executor", "native", "raw evidence"])
        self.assertEqual(code, 0)
        captured = json.loads(output.getvalue())
        self.assertEqual(captured["active_root"], str(self.root))
        self.assertTrue(captured["created"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = curate_main(["--root", str(self.root), "begin", "--domain", "skills", "--mode", "maintain", "--scope", "related methods"])
        self.assertEqual(code, 0)
        prepared = json.loads(output.getvalue())
        self.assertEqual(prepared["mode"], "maintain")
        self.assertEqual(prepared["scope"], "related methods")


if __name__ == "__main__":
    unittest.main()
