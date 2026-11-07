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
from rightmemory.learning import (
    LEARNING_DOMAINS, learning_batch_candidate_ids, learning_claims, list_learning_candidates,
)
from rightmemory.curation_cli import main as curate_main, submit_main
from rightmemory.curation_worker import CurationWorker
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

    def test_notice_batch_identity_tracks_pending_evidence_and_survives_retry_and_reordering(self):
        self._use_default_batch_policy()

        def identities():
            return {notice["domain"]: notice["batch_id"] for notice in self.service.notices()}

        self.assertEqual(identities(), {"all": None})
        first = self._submit()
        initial = identities()
        self.assertTrue(all(isinstance(value, str) and value for value in initial.values()))
        retried = self.service.submit("session-one", "A reusable observation", "request-one")
        self.assertFalse(retried["created"])
        self.assertEqual(identities(), initial)
        second = self._submit("Another observation", "request-two")
        expanded = identities()
        self.assertNotEqual(expanded["all"], initial["all"])

        with patch(
            "rightmemory.learning_service.list_learning_candidates",
            side_effect=lambda *args, **kwargs: tuple(reversed(list_learning_candidates(*args, **kwargs))),
        ):
            self.assertEqual(identities(), expanded)
        self.assertEqual(
            {notice["domain"]: notice["batch_id"] for notice in LearningService(self.root).notices()},
            expanded,
        )
        self.service.withdraw(second)
        self.assertEqual(identities(), initial)
        self.service.withdraw(first)
        self.assertEqual(identities(), {"all": None})

    def test_admitted_batch_remainder_stays_ready_and_later_evidence_waits_its_own_deadline(self):
        self._use_default_batch_policy()
        initial_ids = {self._submit(f"Observation {index}", f"request-{index}") for index in range(31)}
        task = self.service.begin("all")
        self._finish_children(task)
        first_claim = self.service.coordinator.current_claim("all")
        self.assertEqual(len(first_claim.candidate_ids), 30)
        self.assertEqual(set(learning_batch_candidate_ids(self.root, "all")), initial_ids)
        remaining_ids = initial_ids - set(first_claim.candidate_ids)
        later = self._submit("New evidence outside the admitted batch", "later")
        self.service.complete(task["task_id"])

        self.service = LearningService(self.root)
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)
        self.assertIsNone(notice["next_ready_at"])
        followup = self.service.begin("all")
        self._finish_children(followup)
        self.assertEqual(followup["selected_candidate_count"], 1)
        self.assertEqual(set(self.service.coordinator.current_claim("all").candidate_ids), remaining_ids)
        self.service.complete(followup["task_id"])

        self.service = LearningService(self.root)
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
        self.assertFalse(notice["ready"])
        self.assertEqual(notice["candidate_count"], 1)
        self.assertEqual(self.service.begin("all")["status"], "not_ready")
        pending = list_learning_candidates(self.root, domain="guidance")
        self.assertEqual([candidate.candidate_id for candidate in pending], [later])
        deadline = datetime.fromisoformat(pending[0].submitted_at) + timedelta(hours=25)
        self.assertEqual(datetime.fromisoformat(notice["next_ready_at"]), deadline)
        with patch("rightmemory.curation_batch.datetime") as clock:
            clock.now.return_value = deadline
            clock.fromisoformat.side_effect = datetime.fromisoformat
            notice = next(item for item in self.service.notices() if item["domain"] == "all")
            self.assertTrue(notice["ready"])
            resumed = self.service.begin("all")
        self.assertEqual(resumed["selected_candidate_count"], 1)
        self.assertEqual(self.service.coordinator.current_claim("all").candidate_ids, (later,))

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
            self.assertEqual(self.service.begin()["status"], "not_ready")
            self.assertEqual(self.service.run()["status"], "not_ready")
        for notice in notices:
            self.assertFalse(notice["ready"])
            self.assertEqual(notice["status"], "waiting")
            self.assertEqual(notice["candidate_count"], 1)
            self.assertEqual(datetime.fromisoformat(notice["next_ready_at"]), expected_ready)
        self.assertEqual(learning_claims(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertFalse(list((self.root / ".runtime/worktrees").glob("*")))

    def test_default_count_trigger_starts_one_batch_for_all_domains(self):
        self._use_default_batch_policy()
        for index in range(15):
            self._submit(f"Observation {index}", f"request-{index}")
        notice, = self.service.notices()
        self.assertEqual(notice["domain"], "all")
        self.assertTrue(notice["ready"])
        self.assertIsNone(notice["next_ready_at"])
        task = self.service.begin()
        self.assertEqual(task["selected_candidate_count"], 15)
        self._finish_children(task)
        self.service.complete(task["task_id"])
        self._submit("A later observation", "later")
        notice, = self.service.notices()
        self.assertFalse(notice["ready"])
        self.assertEqual(notice["candidate_count"], 1)
        for domain in LEARNING_DOMAINS:
            self.assertEqual(len(list_learning_candidates(self.root, domain=domain)), 1)
        self.assertEqual(self.service.begin()["status"], "not_ready")

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
            task = self.service.begin("all")
        self.assertEqual(task["selected_candidate_count"], 1)

    def test_existing_task_can_resume_and_retry_after_default_threshold_is_restored(self):
        self._submit()
        task = self.service.begin("all")
        self._use_default_batch_policy()
        resumed = self.service.begin("all")
        self.assertEqual(resumed["task_id"], task["task_id"])
        self.assertFalse(resumed["claimed"])
        replacement = self.service.begin("all", retry_task=task["task_id"])
        self.assertTrue(replacement["claimed"])
        self.assertEqual(replacement["selected_candidate_count"], 1)
        self._finish_children(replacement)
        self.assertEqual(self.service.complete(replacement["task_id"])["status"], "completed")

    def test_default_maintenance_can_start_below_update_threshold(self):
        self._use_default_batch_policy()
        self._submit()
        task = self.service.begin("guidance", mode="maintain")
        self._finish_children(task)
        self.assertEqual(task["selected_candidate_count"], 0)
        completed = self.service.complete(task["task_id"])
        notice = next(notice for notice in completed["curation"] if notice["domain"] == "all")
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

    def _finish_children(self, task):
        completed = []
        for domain in task["children"]:
            child = self.service.begin_child(task["task_id"], domain)
            completed.append(self.service.complete_child(task["task_id"], domain, child["attempt"], "No change"))
        return completed

    @contextlib.contextmanager
    def _configured_executor(self, turn):
        class Executor:
            def __init__(self, config, *, curation_mode, curation_task_id):
                self.config = config
                self.mode = curation_mode
                self.task_id = curation_task_id

            def run_session_turn(self, _session, _message):
                return turn(self.config, self.mode, self.task_id)

            def cleanup(self):
                pass

        with (
            patch("rightmemory.config.load_config", side_effect=lambda role, memory_root: RuntimeConfig(
                role=role, model_id="fake", memory_root=memory_root, sync=load_sync_config(memory_root),
            )),
            patch("rightmemory.runtime.RightMemoryRuntime", Executor),
        ):
            yield

    def test_native_begin_is_model_free_and_reuses_a_durable_task(self):
        candidate_id = self._submit()
        with (self.root / "rightmemory.toml").open("a", encoding="utf-8") as handle:
            handle.write("\n[curation.token_limits]\nguidance = 0\n")
        with (
            patch("rightmemory.config.load_config", side_effect=AssertionError("model config loaded")),
            patch("rightmemory.learning_service.domain_budget", side_effect=AssertionError("counted update")),
        ):
            task = self.service.begin("all")
            again = LearningService(self.root).begin("all")
        self.assertTrue(task["claimed"])
        self.assertFalse(again["claimed"])
        self.assertEqual(task["task_id"], again["task_id"])
        self.assertEqual(Path(task["worktree"]).parent, self.root / ".runtime" / "worktrees")
        for prepared in (task, again):
            self.assertNotIn("token_budgets", prepared)
        payload = AssistantWriteManager(self.root, role="curation-parent").read(task["task_id"]).input_data
        self.assertEqual(payload["candidates"][0]["candidate_id"], candidate_id)
        self.assertTrue(Path(task["instructions_file"]).is_file())

    def test_native_maintenance_budget_counts_edited_worktree_with_active_root_limit(self):
        with (self.root / "rightmemory.toml").open("a", encoding="utf-8") as handle:
            handle.write("\n[curation.token_limits]\nguidance = 20\n")
        self._submit()
        active = self.service.count("guidance")
        with patch("rightmemory.config.load_config", side_effect=AssertionError("model config loaded")):
            task = self.service.begin("guidance", mode="maintain")
        worktree = Path(task["worktree"])
        self.assertFalse((worktree / "rightmemory.toml").exists())
        self.assertEqual(set(task["token_budgets"]), {"guidance"})
        budget = task["token_budgets"]["guidance"]
        self.assertEqual(budget["token_limit"], 20)
        self.assertEqual(budget["retained_text_tokens"], active["retained_text_tokens"])
        with self.assertRaises(ValueError):
            self.service.count("skills", task["task_id"])

        edit = worktree / "learning/guidance/work.md"
        edit.parent.mkdir(parents=True, exist_ok=True)
        edit.write_text("# Guidance\n\n" + "example " * 30, encoding="utf-8")
        before = self._git("status", "--porcelain", cwd=worktree)
        count = self.service.count("guidance", task["task_id"])
        self.assertEqual(count["token_limit"], 20)
        self.assertTrue(count["over_limit"])
        self.assertIn("learning/guidance/work.md", count["tokens_by_text_file"])
        self.assertEqual(self.service.count("guidance"), active)
        self.assertEqual(self._git("status", "--porcelain", cwd=worktree), before)

    def test_children_share_one_worktree_and_only_parent_publishes_all_results(self):
        candidate_id = self._submit()
        task = self.service.begin()
        task_id = task["task_id"]
        worktree = Path(task["worktree"])
        head = self._git("rev-parse", "HEAD")
        source = self.root / "learning/candidates" / f"{candidate_id}.json"
        guidance = "learning/guidance/working.md"
        skill = "learning/skills/review/SKILL.md"
        initial_memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        edits = {
            "memory": ("MEMORY.md", initial_memory.replace("initial memory", "reviewed memory")),
            "guidance": (guidance, "# Working preferences\n\nAn observed preference.\n"),
            "skills": (skill, "# Review\n\nCheck the source.\n"),
        }

        with self.assertRaisesRegex(RuntimeError, "completed domain work"):
            self.service.complete(task_id)
        children = {domain: self.service.begin_child(task_id, domain) for domain in LEARNING_DOMAINS}
        for child in children.values():
            self.assertEqual(child["worktree"], task["worktree"])
            self.assertTrue(Path(child["instructions_file"]).is_file())
        for domain, (relative, content) in edits.items():
            with self.subTest(domain=domain):
                child = children[domain]
                path = worktree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "completed domain work"):
                    self.service.complete(task_id)
                returned = self.service.complete_child(task_id, domain, child["attempt"], f"Reviewed {domain}")
                self.assertEqual(returned["status"], "completed")
                self.assertEqual(self._git("rev-parse", "HEAD"), head)
                self.assertTrue(source.is_file())
                self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))
                self.assertEqual((self.root / "MEMORY.md").read_text(encoding="utf-8"), initial_memory)
                self.assertFalse((self.root / guidance).exists())
                self.assertFalse((self.root / skill).exists())

        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            self.service.complete(task_id)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        changed_paths = {relative for relative, _content in edits.values()}
        self._git("add", "--", *sorted(changed_paths), cwd=worktree)
        self._git("commit", "-m", "review combined contributions", cwd=worktree)
        result = self.service.complete(task_id, "Reviewed all contributions")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["changed_paths"]), changed_paths)
        for relative, content in edits.values():
            self.assertEqual((self.root / relative).read_text(encoding="utf-8"), content)
        self.assertFalse(worktree.exists())
        self.assertFalse(source.exists())
        self.assertFalse(list_learning_candidates(self.root))
        for domain in LEARNING_DOMAINS:
            self.assertTrue((self.root / f"learning/processed/{domain}/{candidate_id}.json").is_file())
        replay = LearningService(self.root).complete(task_id)
        self.assertEqual(replay["landed_commit"], result["landed_commit"])
        self.assertEqual(self._git("rev-parse", "HEAD"), result["landed_commit"])
        retried = self.service.submit("session-one", "A reusable observation", "request-one")
        self.assertFalse(retried["created"])
        self.assertFalse(list_learning_candidates(self.root))

    def test_retry_child_fences_the_old_attempt_and_resumes_its_replacement(self):
        self._submit()
        task = self.service.begin()
        task_id = task["task_id"]
        previous = self.service.begin_child(task_id, "memory")
        self.assertEqual(self.service.begin_child(task_id, "memory")["attempt"], previous["attempt"])
        restarted = LearningService(self.root)
        replacement = restarted.begin_child(task_id, "memory", retry=True)
        self.assertNotEqual(previous["attempt"], replacement["attempt"])
        self.assertEqual(previous["worktree"], replacement["worktree"])
        with self.assertRaises(LearningClaimLost):
            self.service.complete_child(task_id, "memory", previous["attempt"])
        returned = restarted.complete_child(task_id, "memory", replacement["attempt"], "No change")
        self.assertEqual(returned["status"], "completed")
        self.assertEqual(restarted.complete_child(task_id, "memory", replacement["attempt"], "No change"), returned)

    def test_child_git_staging_and_commits_are_rejected(self):
        self._submit()
        task = self.service.begin("guidance", mode="maintain")
        task_id = task["task_id"]
        worktree = Path(task["worktree"])
        head = self._git("rev-parse", "HEAD")
        edit = worktree / "learning/guidance/work.md"
        edit.parent.mkdir(parents=True)
        edit.write_text("# Working guidance\n", encoding="utf-8")
        for commit in (False, True):
            with self.subTest(commit=commit):
                attempt = self.service.begin_child(task_id, "guidance", retry=True)["attempt"]
                self._git("add", "learning/guidance/work.md", cwd=worktree)
                if commit:
                    self._git("commit", "-m", "child commit is forbidden", cwd=worktree)
                with self.assertRaisesRegex(RuntimeError, "Git history or staging"):
                    self.service.complete_child(task_id, "guidance", attempt)
                self.assertEqual(self.service.status(task_id)["children"]["guidance"]["status"], "failed")
                with self.assertRaisesRegex(RuntimeError, "completed domain work"):
                    self.service.complete(task_id)
                if not commit:
                    self._git("reset", "HEAD", "--", "learning/guidance/work.md", cwd=worktree)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertFalse((self.root / "learning/guidance/work.md").exists())

    def test_new_raw_evidence_waits_for_the_next_batch(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
        later = self._submit("A later observation", "request-two")
        self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="guidance")], [later])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root)], [later])

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
        self._finish_children(task)
        self.assertEqual(task["selected_candidate_count"], 0)
        self._commit(task, "learning/guidance/working.md", "# Existing guidance\n\nClarified scoped preference.\n")
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["selected_candidate_count"], 0)
        for domain in LEARNING_DOMAINS:
            self.assertEqual({item.candidate_id for item in list_learning_candidates(self.root, domain=domain)}, candidate_ids)
        self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))
        notice = next(item for item in completed["curation"] if item["domain"] == "all")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_candidate_count_and_leaves_backlog_ready(self):
        candidate_ids = [self._submit(f"Observation {index}", f"request-{index}") for index in range(20 + 2)]
        task = self.service.begin("all")
        self._finish_children(task)
        self.assertEqual(task["selected_candidate_count"], 20)
        self.assertEqual(self.service.status(task["task_id"])["selected_candidate_count"], 20)
        completed = self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="guidance")], candidate_ids[20:])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root)], candidate_ids[20:])
        notice = next(item for item in completed["curation"] if item["domain"] == "all")
        self.assertTrue(notice["ready"])
        self.assertEqual(notice["candidate_count"], 2)

    def test_update_bounds_evidence_text_before_the_count_limit(self):
        size = 24_000 // 2 + 1
        first = self._submit("a" * size, "first")
        second = self._submit("b" * size, "second")
        task = self.service.begin("all")
        self._finish_children(task)
        self.assertEqual(task["selected_candidate_count"], 1)
        selected = AssistantWriteManager(self.root, role="curation-parent").read(task["task_id"]).input_data["candidates"]
        self.assertEqual(selected[0]["candidate_id"], first)
        self.assertTrue(all("evidence" not in item for item in selected))
        self.service.complete(task["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="skills")], [second])

    def test_oversized_first_source_uses_a_reference_and_keeps_its_frozen_proof(self):
        evidence = "x" * (24_000 + 5000)
        candidate_id = self._submit(evidence)
        task = self.service.begin("all")
        manager = AssistantWriteManager(self.root, role="curation-parent")
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
        repeated = LearningService(self.root).begin("all")
        self.assertEqual(repeated["task_id"], task["task_id"])
        replacement = self.service.begin("all", retry_task=task["task_id"])
        self.assertEqual(manager.read(replacement["task_id"]).input_data, frozen)
        self._finish_children(replacement)
        self.service.complete(replacement["task_id"])
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="skills")], [later])

    def test_referenced_source_is_hash_checked_at_completion(self):
        candidate_id = self._submit("x" * (24_000 + 1))
        task = self.service.begin("all")
        self._finish_children(task)
        relative = f"learning/candidates/{candidate_id}.json"
        source = self.root / relative
        data = json.loads(source.read_text(encoding="utf-8"))
        data["evidence"] = "y" + data["evidence"][1:]
        data["evidence_sha256"] = hashlib.sha256(data["evidence"].encode("utf-8")).hexdigest()
        source.write_text(json.dumps(data))
        self._git("add", relative)
        self._git("commit", "-m", "simulate external evidence replacement")
        with self.assertRaisesRegex(ValueError, "candidate evidence changed"):
            self.service.complete(task["task_id"])
        self.assertFalse((self.root / f"learning/processed/guidance/{candidate_id}.json").exists())

    def test_all_update_rejects_stale_result_after_external_skill_edit(self):
        relative = "learning/skills/review/SKILL.md"
        skill = self.root / relative
        skill.parent.mkdir(parents=True)
        skill.write_text("# Review\n\nReview the source.\n", encoding="utf-8")
        self._git("add", relative)
        self._git("commit", "-m", "seed skill")
        self._submit()
        task = self.service.begin()
        self._finish_children(task)
        skill.write_text("# Review\n\nReview the source and check the result.\n", encoding="utf-8")
        self._git("add", relative)
        self._git("commit", "-m", "another writer changed skills")
        head = self._git("rev-parse", "HEAD")
        with self.assertRaises(AssistantStaleVersionError):
            self.service.complete(task["task_id"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_single_domain_maintenance_cannot_publish_other_domain_edits(self):
        self._submit()
        task = self.service.begin("guidance", mode="maintain")
        with self.assertRaises(ValueError):
            self.service.begin_child(task["task_id"], "skills")
        self._finish_children(task)
        worktree = Path(task["worktree"])
        base = self._git("rev-parse", "HEAD", cwd=worktree)
        relative = "learning/experiences/case.md"
        self._commit(task, relative, "# Case\n\nOutside this curator.\n")
        head = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "outside its requested domains"):
            self.service.complete(task["task_id"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertFalse((self.root / relative).exists())
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

        self._git("reset", "--hard", base, cwd=worktree)
        guidance = "learning/guidance/work.md"
        self._commit(task, guidance, "# Scoped guidance\n")
        corrected = self.service.complete(task["task_id"])
        self.assertEqual(corrected["changed_paths"], [guidance])
        self.assertTrue((self.root / guidance).is_file())
        self.assertFalse((self.root / relative).exists())
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_skills_maintenance_cannot_publish_memory_edits(self):
        self._submit()
        task = self.service.begin("skills", mode="maintain")
        self._finish_children(task)
        self._commit(task, "MEMORY.md", "# Domain {#domain}\n\n- `one` changed → []\n")
        with self.assertRaisesRegex(ValueError, "outside its requested domains"):
            self.service.complete(task["task_id"])
        self.assertIn("initial memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_curator_cannot_modify_raw_evidence(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance", mode="maintain")
        self._finish_children(task)
        relative = f"learning/candidates/{candidate_id}.json"
        content = json.loads((Path(task["worktree"]) / relative).read_text(encoding="utf-8"))
        content["evidence"] = "rewritten evidence"
        self._commit(task, relative, json.dumps(content))
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(list_learning_candidates(self.root)[0].evidence, "A reusable observation")

    def test_curator_cannot_write_processing_receipts(self):
        candidate_id = self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
        self._commit(task, f"learning/processed/guidance/{candidate_id}.json", "{}")
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root, domain="guidance")), 1)

    def test_maintenance_cannot_delete_program_owned_raw(self):
        candidate_id = self._submit()
        task = self.service.begin("guidance", mode="maintain")
        self._finish_children(task)
        worktree = Path(task["worktree"])
        self._git("rm", f"learning/candidates/{candidate_id}.json", cwd=worktree)
        self._git("commit", "-m", "premature retirement", cwd=worktree)
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_completed_parent_keeps_proofs_and_removes_active_manifests(self):
        evidence = "A concrete piece of evidence that should leave the candidate store"
        candidate_id = self._submit(evidence)
        task = self.service.begin()
        children = self._finish_children(task)
        result = self.service.complete(task["task_id"])
        record = self.service._task(task["task_id"])
        selected = record["input"]["candidates"]
        self.assertEqual([item["candidate_id"] for item in selected], [candidate_id])
        self.assertNotIn("evidence", selected[0])
        operation = SemanticOperationStore(self.root).read(result["operation_id"])
        self.assertEqual(operation.input_data, {
            "role": "curation-parent", "kind": "curation", "task_id": task["task_id"],
        })
        for artifact in ("candidates_file", "instructions_file", "worktree"):
            self.assertFalse(Path(task[artifact]).exists())
        for child in children:
            self.assertFalse(Path(child["instructions_file"]).exists())
        self.assertFalse(list_learning_candidates(self.root))

    def test_external_guidance_change_requires_parent_retry_and_fences_old_task(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
        path = self.root / "learning/guidance/new.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# New related guidance\n")
        self._git("add", "learning/guidance/new.md")
        self._git("commit", "-m", "another writer changed guidance")
        with self.assertRaises(AssistantStaleVersionError):
            self.service.complete(task["task_id"])
        replacement = self.service.begin("all", retry_task=task["task_id"])
        self._finish_children(replacement)
        self.assertNotEqual(replacement["task_id"], task["task_id"])
        self.assertTrue(Path(task["worktree"]).exists())
        with self.assertRaises(LearningClaimLost):
            self.service.complete(task["task_id"])
        self.service.complete(replacement["task_id"])

    def test_update_cannot_delete_evidence_outside_its_selected_batch(self):
        (self.root / "rightmemory.toml").write_text(
            "[curation.batch]\ntrigger_candidates = 1\ntarget_batch_candidates = 1\n", encoding="utf-8",
        )
        self.service = LearningService(self.root)
        self._submit()
        self._submit("New work to consider", "request-two")
        task = self.service.begin()
        selected = set(self.service.coordinator.current_claim("all").candidate_ids)
        remaining, = {item.candidate_id for item in list_learning_candidates(self.root)} - selected
        self._finish_children(task)
        worktree = Path(task["worktree"])
        relative = f"learning/candidates/{remaining}.json"
        self._git("rm", relative, cwd=worktree)
        self._git("commit", "-m", "retire unselected evidence", cwd=worktree)
        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            self.service.complete(task["task_id"])
        self.assertTrue((self.root / relative).is_file())

    def test_skill_assets_publish_as_regular_files(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
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
        task = self.service.begin("all")
        self._finish_children(task)
        self._commit(task, "learning/experiences/case.md", "# Case\n\nUseful rough evidence.\n")
        with patch.object(LearningCoordinator, "finalize", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.service.complete(task["task_id"], "Retained a case")
        writer = AssistantWriteManager(self.root, role="curation-parent")
        operation = SemanticOperationStore(self.root).read(writer.read(task["task_id"]).operation_id)
        self.assertEqual(operation.phase, "prepared")
        self.assertFalse((self.root / "learning/experiences/case.md").exists())
        completed = LearningService(self.root).complete(task["task_id"])
        self.assertEqual(completed["output"], "Retained a case")
        self.assertEqual(completed["status"], "completed")

    def test_lost_reply_after_publication_is_recovered_by_status(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
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

    def test_worker_recovers_publication_before_next_batch_retry_budget(self):
        self._submit()
        task = self.service.begin(_executor="configured")
        self._finish_children(task)
        original = LearningCoordinator.finalize

        def lose_reply(coordinator, *args, **kwargs):
            original(coordinator, *args, **kwargs)
            raise RuntimeError("lost reply")

        with patch.object(LearningCoordinator, "finalize", lose_reply):
            with self.assertRaisesRegex(RuntimeError, "lost reply"):
                self.service.complete(task["task_id"])
        self._submit("A separate later observation", "request-two")
        worker = CurationWorker(self.root)
        worker._write({"failures": {"all": {
            "task_id": task["task_id"], "attempts": 1,
            "error": "lost reply", "next_retry_at": None,
        }}})
        calls = []

        def turn(config, _mode, task_id):
            if config.role == "curation-parent":
                calls.append(task_id)
                if len(calls) == 1:
                    raise RuntimeError("first attempt for the new batch failed")
                for domain in LEARNING_DOMAINS:
                    self.service.run_child(task_id, domain)
            return "No additional guidance needed"

        with (
            self._configured_executor(turn),
            patch("rightmemory.curation_worker.RETRY_COOLDOWN_SECONDS", 0),
        ):
            self.assertEqual(worker.run(self.service), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertNotEqual(calls[0], task["task_id"])
        self.assertEqual(worker.status()["failures"], {})
        self.assertFalse(list_learning_candidates(self.root))

    def test_configured_parent_resumes_failed_task_and_runs_children_in_same_worktree(self):
        self._submit()
        calls = []
        relative = "learning/guidance/work.md"

        def turn(config, mode, task_id):
            calls.append((config, mode, task_id))
            if len(calls) == 1:
                raise RuntimeError("provider unavailable")
            if config.role == "curation-parent":
                head = self._git("rev-parse", "HEAD")
                for domain in LEARNING_DOMAINS:
                    child = self.service.run_child(task_id, domain)
                    self.assertEqual(child["status"], "completed")
                    self.assertEqual(Path(child["worktree"]), config.memory_root)
                    self.assertEqual(self._git("rev-parse", "HEAD"), head)
                self._git("add", relative, cwd=config.memory_root)
                self._git("commit", "-m", "review combined contributions", cwd=config.memory_root)
            elif config.role == "guidance-curator":
                edit = config.memory_root / relative
                edit.parent.mkdir(parents=True)
                edit.write_text("# Working guidance\n", encoding="utf-8")
            return "Done"

        with self._configured_executor(turn):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                self.service.run()
            self._use_default_batch_policy()
            notice, = self.service.notices()
            self.assertTrue(notice["ready"])
            self.assertEqual(notice["status"], "resume")
            completed = self.service.run()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["changed_paths"], [relative])
        self.assertEqual([config.role for config, _, _ in calls], [
            "curation-parent", "curation-parent", "update", "guidance-curator", "skills-curator",
        ])
        worktree = calls[0][0].memory_root
        self.assertNotEqual(worktree, self.root)
        for config, mode, task_id in calls:
            self.assertEqual(config.memory_root, worktree)
            self.assertEqual(config.state_root, self.root)
            self.assertFalse(config.sync.enabled)
            self.assertEqual(mode, "update")
            self.assertEqual(task_id, completed["task_id"])
        self.assertTrue((self.root / relative).is_file())
        self.assertFalse(list_learning_candidates(self.root))

    def test_configured_child_cannot_edit_a_different_domain(self):
        self._submit()
        task = self.service.begin()
        head = self._git("rev-parse", "HEAD")

        def turn(config, _mode, _task_id):
            self._append_memory(config.memory_root, "\n- `changed` foreign domain → []\n")
            return "Done"

        with self._configured_executor(turn):
            with self.assertRaisesRegex(ValueError, "outside its ownership"):
                self.service.run_child(task["task_id"], "guidance")
        self.assertEqual(self.service.status(task["task_id"])["children"]["guidance"]["status"], "failed")
        with self.assertRaisesRegex(RuntimeError, "completed domain work"):
            self.service.complete(task["task_id"])
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_configured_maintenance_retry_keeps_frozen_mode(self):
        calls = []

        def turn(config, mode, task_id):
            calls.append((config.role, mode))
            if len(calls) == 1:
                raise RuntimeError("provider unavailable")
            if config.role == "curation-parent":
                self.service.run_child(task_id, "guidance")
            return "Done"

        with self._configured_executor(turn):
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                self.service.run("guidance", mode="maintain", scope="existing guidance")
            notice, = self.service.notices()
            retry = self.service.begin("guidance", retry_task=notice["task_id"])
            self.assertEqual(retry["mode"], "maintain")
            self.assertEqual(set(retry["token_budgets"]), {"guidance"})
            completed = self.service.run("guidance", mode="maintain", scope="existing guidance")
        self.assertEqual(completed["task_id"], retry["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(calls, [
            ("curation-parent", "maintain"), ("curation-parent", "maintain"), ("guidance-curator", "maintain"),
        ])

    def test_curator_executor_inherits_update_configuration(self):
        (self.root / "rightmemory.toml").write_text('[update.model]\nmodel_id = "example"\n')
        for domain in ("guidance", "skills"):
            config = load_config(f"{domain}-curator", self.root)
            self.assertEqual(config.model_id, "example")
            self.assertEqual(config.role, f"{domain}-curator")

    def test_requested_maintenance_is_not_satisfied_by_an_active_update(self):
        self._submit()
        active = self.service.begin("all")
        self._finish_children(active)
        result = self.service.run("skills", mode="maintain", scope="older cases")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["task_id"], active["task_id"])
        self.assertEqual(result["requested_mode"], "maintain")
        self.assertEqual(result["requested_scope"], "older cases")
        self.assertNotIn("token_budgets", result)
        self.assertEqual(len(list_learning_candidates(self.root, domain="skills")), 1)
        self.service.complete(active["task_id"])
        maintenance = self.service.begin("skills", mode="maintain", scope="older cases")
        self.assertEqual(maintenance["mode"], "maintain")
        self.assertNotEqual(maintenance["task_id"], active["task_id"])

    def test_memory_maintenance_uses_shared_writer_without_consuming_candidates(self):
        candidate_id = self._submit()
        task = self.service.begin("memory", mode="maintain", scope="domain")
        self._finish_children(task)
        self.assertEqual(task["selected_candidate_count"], 0)
        text = (Path(task["worktree"]) / "MEMORY.md").read_text(encoding="utf-8")
        self._commit(task, "MEMORY.md", text.replace("initial memory", "maintained memory"))
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["changed_paths"], ["MEMORY.md"])
        self.assertIn("maintained memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        for domain in LEARNING_DOMAINS:
            self.assertEqual(
                [item.candidate_id for item in list_learning_candidates(self.root, domain=domain)],
                [candidate_id],
            )
        self.assertFalse(list((self.root / "learning/processed").glob("*/*.json")))

    def test_explicit_retry_can_replace_foreign_claim_without_local_task(self):
        candidate_id = self._submit()
        foreign = LearningCoordinator(load_sync_config(self.root), holder_id="1" * 32)
        claim = foreign.claim("all", task_id="all-" + "a" * 32).claim
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
        self.assertTrue(notice["foreign"])
        self.assertFalse(notice["ready"])
        self.assertEqual(self.service.begin("all")["task_id"], claim.task_id)
        replacement = self.service.begin("all", retry_task=claim.task_id)
        self._finish_children(replacement)
        self.assertTrue(replacement["claimed"])
        self.assertNotEqual(replacement["task_id"], claim.task_id)
        self.assertEqual(replacement["selected_candidate_count"], 1)
        with self.assertRaises(LearningClaimLost):
            foreign.finalize(claim, None, prepared_start_commit=claim.base_commit)
        self.service.complete(replacement["task_id"])
        self.assertFalse(list_learning_candidates(self.root, domain="guidance"))
        self.assertEqual(list_learning_candidates(self.root), ())
        for domain in LEARNING_DOMAINS:
            self.assertTrue((self.root / f"learning/processed/{domain}/{candidate_id}.json").is_file())

    def test_replaced_local_task_is_reported_superseded_and_never_resumed(self):
        self._submit()
        task = self.service.begin("all")
        old = self.service.coordinator.current_claim("all")
        foreign = LearningCoordinator(load_sync_config(self.root), holder_id="1" * 32)
        replacement = foreign.claim(
            "all", task_id="all-" + "b" * 32, replace_token=old.token,
        ).claim
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
        self.assertEqual(notice["task_id"], replacement.task_id)
        self.assertTrue(notice["foreign"])
        status = self.service.status(task["task_id"])
        self.assertEqual(status["status"], "superseded")
        self.assertEqual(status["replacement_task"], replacement.task_id)
        self.assertEqual(self.service.begin("all")["task_id"], replacement.task_id)
        with self.assertRaises(LearningClaimLost):
            self.service.complete(task["task_id"])
        self.assertTrue(Path(task["worktree"]).is_dir())

    def test_notices_find_all_update_after_a_local_maintenance_parent_is_fenced(self):
        self._submit()
        maintenance = self.service.begin("guidance", mode="maintain")
        old = self.service.coordinator.current_claim("guidance")
        self.assertEqual(old.task_id, maintenance["task_id"])
        foreign = LearningCoordinator(load_sync_config(self.root), holder_id="1" * 32)
        replacement = foreign.claim("guidance", mode="maintain", replace_token=old.token).claim
        foreign.finalize(replacement, None, prepared_start_commit=self._git("rev-parse", "HEAD"))
        active = foreign.claim("all").claim

        notice, = self.service.notices()
        self.assertEqual(notice["domain"], "all")
        self.assertEqual(notice["task_id"], active.task_id)
        self.assertTrue(notice["foreign"])
        self.assertFalse(notice["ready"])

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
        task = self.service.begin("all")
        self._finish_children(task)
        text = (Path(task["worktree"]) / "MEMORY.md").read_text(encoding="utf-8")
        self._commit(task, "MEMORY.md", text + "- `retained` durable result → []\n")
        with patch("rightmemory.automatic_effects.publish_file_view_outbox", side_effect=OSError("hub offline")):
            completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertIn("retained", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual({effect["name"]: effect["status"] for effect in completed["effects"]}, {
            "memory-pressure": "done", "file-view-publish": "failed",
        })
        points = InsightTriggerStore(self.root).read().points
        self.assertGreater(points, 0)
        later = self._submit("New evidence arriving before publication recovery", "later")
        self._use_default_batch_policy()
        before = {path.relative_to(self.root): path.read_bytes() for path in (self.root / ".runtime").rglob("*") if path.is_file() and not path.is_symlink()}
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
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
            retried = self.service.run("all")
        self.assertEqual(retried["status"], "completed")
        self.assertTrue(all(effect["status"] == "done" for effect in retried["effects"]))
        self.assertEqual(retried["task_id"], task["task_id"])
        self.assertEqual(InsightTriggerStore(self.root).read().points, points)
        self.assertEqual(self._git("rev-parse", "HEAD"), head)
        self.assertEqual([item.candidate_id for item in list_learning_candidates(self.root, domain="memory")], [later])
        notice = next(item for item in self.service.notices() if item["domain"] == "all")
        self.assertEqual(notice["status"], "waiting")
        self.assertEqual(notice["candidate_count"], 1)
        installer._require_installable_tasks()

    def test_lost_pressure_effect_receipt_does_not_increment_twice(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
        text = (Path(task["worktree"]) / "MEMORY.md").read_text(encoding="utf-8")
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
        task = self.service.begin("all")
        self._finish_children(task)
        completed = self.service.complete(task["task_id"])
        self.assertEqual(completed["changed_paths"], [])
        self.assertEqual(InsightTriggerStore(self.root).read().points, 0)
        self.assertEqual([effect["name"] for effect in completed["effects"]], ["file-view-publish"])

    def test_effect_store_failure_keeps_completed_outcome_recoverable(self):
        self._submit()
        task = self.service.begin("all")
        self._finish_children(task)
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
