import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant_write import AssistantStaleVersionError, AssistantWriteManager
from rightmemory.config import RuntimeConfig, load_config
from rightmemory.isolated_write import IsolatedWriteSupervisor
from rightmemory.learning import list_learning_candidates
from rightmemory.learning_cli import main as learning_main
from rightmemory.learning_service import (
    LEARNING_UPDATE_EVIDENCE_CHARS,
    LEARNING_UPDATE_MAX_CANDIDATES,
    LearningClaimLost,
    LearningService,
)
from rightmemory.semantic_operation import SemanticOperationStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class LearningServiceTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        self.service = LearningService(self.root)

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
        payload = json.loads(Path(task["candidates_file"]).read_text())
        self.assertEqual(payload["candidates"][0]["candidate_id"], candidate_id)
        self.assertTrue(Path(task["instructions_file"]).is_file())

    def test_domains_complete_independently_and_keep_raw_evidence(self):
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
        for domain in ("guidance", "skills"):
            self.assertEqual({item.candidate_id for item in list_learning_candidates(self.root, domain=domain)}, candidate_ids)
        self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))
        notice = next(item for item in completed["learning"] if item["domain"] == "guidance")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_candidate_count_and_leaves_backlog_ready(self):
        candidate_ids = [self._submit(f"Observation {index}", f"request-{index}") for index in range(LEARNING_UPDATE_MAX_CANDIDATES + 2)]
        task = self.service.begin("guidance")
        self.assertEqual(task["selected_candidate_count"], LEARNING_UPDATE_MAX_CANDIDATES)
        self.assertEqual(self.service.status(task["task_id"])["selected_candidate_count"], LEARNING_UPDATE_MAX_CANDIDATES)
        completed = self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="guidance")], candidate_ids[LEARNING_UPDATE_MAX_CANDIDATES:])
        self.assertEqual(len(list_learning_candidates(self.root, domain="skills")), len(candidate_ids))
        notice = next(item for item in completed["learning"] if item["domain"] == "guidance")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_evidence_text_before_the_count_limit(self):
        size = LEARNING_UPDATE_EVIDENCE_CHARS // 2 + 1
        first = self._submit("a" * size, "first")
        second = self._submit("b" * size, "second")
        task = self.service.begin("skills")
        self.assertEqual(task["selected_candidate_count"], 1)
        selected = AssistantWriteManager(self.root, role="skills-curator").read(task["task_id"]).input_data["candidates"]
        self.assertEqual(selected[0]["candidate_id"], first)
        self.assertLessEqual(sum(len(item.get("evidence", "")) for item in selected), LEARNING_UPDATE_EVIDENCE_CHARS)
        self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="skills")], [second])

    def test_oversized_first_source_uses_a_reference_and_keeps_its_frozen_proof(self):
        evidence = "x" * (LEARNING_UPDATE_EVIDENCE_CHARS + 5000)
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
        candidate_id = self._submit("x" * (LEARNING_UPDATE_EVIDENCE_CHARS + 1))
        task = self.service.begin("guidance")
        relative = f"learning/candidates/{candidate_id}.json"
        source = self.root / relative
        data = json.loads(source.read_text())
        data["evidence"] = "y" + data["evidence"][1:]
        data["evidence_sha256"] = hashlib.sha256(data["evidence"].encode("utf-8")).hexdigest()
        source.write_text(json.dumps(data))
        self._git("add", relative)
        self._git("commit", "-m", "simulate external evidence replacement")
        with self.assertRaisesRegex(AssistantStaleVersionError, "input evidence changed"):
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
        with self.assertRaisesRegex(RuntimeError, "cannot create or modify"):
            self.service.complete(task["task_id"])
        self.assertEqual(list_learning_candidates(self.root)[0].evidence, "A reusable observation")

    def test_curator_cannot_write_processing_receipts(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance")
        self._commit(task, f"learning/processed/guidance/{candidate_id}.json", "{}")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root, domain="guidance")), 1)

    def test_maintenance_cannot_retire_evidence_unprocessed_by_either_domain(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance", mode="maintain")
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{candidate_id}.json", cwd=worktree)
        self._git("commit", "-m", "premature retirement", cwd=worktree)
        with self.assertRaisesRegex(ValueError, "not processed by both"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_maintenance_can_deliberately_retire_fully_processed_raw(self):
        candidate_id = self._submit()
        for domain in ("guidance", "skills"):
            task = self.service.begin(domain)
            self.service.complete(task["task_id"])
        task = self.service.begin("skills", mode="maintain", scope="remove obsolete cases")
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{candidate_id}.json", cwd=worktree)
        self._git("commit", "-m", "retire obsolete raw evidence", cwd=worktree)
        result = self.service.complete(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(list_learning_candidates(self.root))
        for domain in ("guidance", "skills"):
            self.assertTrue((self.root / f"learning/processed/{domain}/{candidate_id}.json").is_file())
        retried = self.service.submit("session-one", "A reusable observation", "request-one")
        self.assertFalse(retried["created"])
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

    def test_update_cannot_retire_unrelated_fully_processed_evidence(self):
        old = self._submit()
        for domain in ("guidance", "skills"):
            task = self.service.begin(domain)
            self.service.complete(task["task_id"])
        self._submit("New work to consider", "request-two")
        task = self.service.begin("skills")
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{old}.json", cwd=worktree)
        self._git("commit", "-m", "retire an old case during update", cwd=worktree)
        with self.assertRaisesRegex(RuntimeError, "only during maintenance"):
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
        with patch.object(IsolatedWriteSupervisor, "publish_external", side_effect=RuntimeError("interrupted")):
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
        original = IsolatedWriteSupervisor.publish_external

        def lose_reply(supervisor, *args, **kwargs):
            original(supervisor, *args, **kwargs)
            raise RuntimeError("lost reply")

        with patch.object(IsolatedWriteSupervisor, "publish_external", lose_reply):
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
            notice = self.service.notices()[0]
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

    def test_cli_submit_and_maintenance_bind_an_explicit_root(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = learning_main(["--root", str(self.root), "submit", "--session", "s", "--request-id", "r", "raw evidence"])
        self.assertEqual(code, 0)
        captured = json.loads(output.getvalue())
        self.assertEqual(captured["active_root"], str(self.root))
        self.assertTrue(captured["created"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = learning_main(["--root", str(self.root), "begin", "--domain", "skills", "--mode", "maintain", "--scope", "related methods"])
        self.assertEqual(code, 0)
        prepared = json.loads(output.getvalue())
        self.assertEqual(prepared["mode"], "maintain")
        self.assertEqual(prepared["scope"], "related methods")


if __name__ == "__main__":
    unittest.main()
