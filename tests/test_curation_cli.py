from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rightmemory.curation_cli import main, submit_main
from rightmemory.learning import learning_claims, list_learning_candidates
from rightmemory.learning_retrieval import learning_sources
from tests.isolated_write_test_base import IsolatedWriteTestBase


class CurationCliTests(IsolatedWriteTestBase):
    def invoke(self, args, *, submit=False):
        output = io.StringIO()
        with redirect_stdout(output):
            status = (submit_main if submit else main)(["--root", str(self.root), *args])
        return status, json.loads(output.getvalue())

    def report_worker(self, task_id, domain):
        code, worker = self.invoke(["worker-begin", "--task", task_id, "--domain", domain])
        self.assertEqual(code, 0, worker)
        code, reported = self.invoke([
            "worker-complete", "--task", task_id, "--domain", domain, "--attempt", worker["attempt"],
        ])
        self.assertEqual(code, 0, reported)

    def test_only_parent_completion_retires_source(self):
        (self.root / ".git/info").mkdir(exist_ok=True)
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as stream:
            stream.write("\n/rightmemory.toml\n")
        (self.root / "rightmemory.toml").write_text(
            "[curation.batch]\ntrigger_candidates = 1\n", encoding="utf-8",
        )
        args = ["--executor", "native", "--session", "s", "--request-id", "r", "Unsettled but useful case."]
        code, saved = self.invoke(args, submit=True)
        self.assertEqual(code, 0)
        self.assertEqual(saved["pending_domains"], ["memory", "guidance", "skills"])
        self.assertEqual([notice["domain"] for notice in saved["curation"]], ["all"])
        captured = self._git("rev-parse", "HEAD")
        _, repeated = self.invoke(args, submit=True)
        self.assertFalse(repeated["created"])
        self.assertEqual(self._git("rev-parse", "HEAD"), captured)
        source_id = f"L#candidates/{saved['candidate_id']}.json"
        self.assertIn(source_id, learning_sources(self.root))
        code, task = self.invoke(["begin"])
        self.assertEqual(code, 0, task)
        self.assertEqual(task["status"], "prepared")
        task_id = task["task_id"]
        claimed = self._git("rev-parse", "HEAD")
        for domain in ("memory", "guidance", "skills"):
            code, counted = self.invoke(["count", "--domain", domain, "--task", task_id])
            self.assertEqual(code, 0)
            self.assertEqual(counted["task_id"], task_id)
            self.report_worker(task_id, domain)
            self.assertEqual(self._git("rev-parse", "HEAD"), claimed)
            self.assertIn(source_id, learning_sources(self.root))
        code, done = self.invoke(["complete", "--task", task_id])
        self.assertEqual(code, 0, done)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(list_learning_candidates(self.root), ())
        _, repeated = self.invoke(args, submit=True)
        self.assertFalse(repeated["created"])
        self.assertNotIn(source_id, learning_sources(self.root))

    def test_default_single_submission_waits_without_starting_curation(self):
        code, saved = self.invoke(
            ["--executor", "native", "--session", "s", "One pending observation."], submit=True,
        )
        self.assertEqual(code, 0)
        notice, = saved["curation"]
        self.assertEqual(notice["domain"], "all")
        self.assertFalse(notice["ready"])
        self.assertEqual(notice["candidate_count"], 1)
        self.assertIsNotNone(notice["next_ready_at"])
        captured = self._git("rev-parse", "HEAD")
        with patch("rightmemory.learning_coordinator.LearningCoordinator._worktree", side_effect=AssertionError("worktree created")):
            code, waiting = self.invoke(["begin"])
        self.assertEqual(code, 0)
        self.assertEqual(waiting["status"], "not_ready")
        self.assertEqual(learning_claims(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), captured)

    def test_configured_wake_failure_reports_saved_candidate(self):
        with patch("rightmemory.curation_worker.CurationWorker.wake", side_effect=OSError("cannot spawn")):
            code, result = self.invoke(["--executor", "configured", "--session", "s", "Case."], submit=True)
        self.assertEqual(code, 0)
        self.assertTrue(result["saved"])
        self.assertIn("error", result["worker"])
        self.assertEqual(len(list_learning_candidates(self.root)), 1)

    def test_maintain_does_not_consume_candidates(self):
        self.invoke(["--executor", "native", "--session", "s", "Case."], submit=True)
        _, task = self.invoke(["begin", "--domain", "memory", "--mode", "maintain", "--scope", "existing context"])
        self.assertEqual(task["selected_candidate_count"], 0)
        self.report_worker(task["task_id"], "memory")
        code, done = self.invoke(["complete", "--task", task["task_id"]])
        self.assertEqual(code, 0, done)
        self.assertEqual(len(list_learning_candidates(self.root, domain="memory")), 1)

    def test_configured_run_dispatches_parent_and_child_separately(self):
        with patch("rightmemory.curation_cli.LearningService") as factory:
            service = factory.return_value
            service.run.return_value = {"status": "completed"}
            service.run_child.return_value = {"status": "completed"}
            self.assertEqual(self.invoke(["run"])[0], 0)
            service.run.assert_called_once_with("all", mode="update", scope="")
            self.assertEqual(self.invoke(["worker-run", "--task", "parent", "--domain", "skills"])[0], 0)
            service.run_child.assert_called_once_with("parent", "skills", instructions="")
            service.complete.assert_not_called()

    def test_count_routes_to_active_root_or_prepared_task(self):
        code, active = self.invoke(["count", "--domain", "memory"])
        self.assertEqual(code, 0)
        self.assertTrue(active["ok"])
        self.assertEqual(active["active_root"], str(self.root))
        self.assertIsNone(active["task_id"])
        self.assertGreater(active["retained_text_tokens"], 0)
        self.assertFalse((self.root / ".runtime").exists())

        _, task = self.invoke(["begin", "--domain", "memory", "--mode", "maintain"])
        code, count = self.invoke(["count", "--domain", "memory", "--task", task["task_id"]])
        self.assertEqual(code, 0)
        self.assertEqual(count["task_id"], task["task_id"])
        self.assertEqual(count["retained_text_tokens"], active["retained_text_tokens"])

        for args in (
            ["--domain", "invalid"],
            ["--domain", "all"],
            ["--domain", "memory", "--task", "invalid"],
            ["--domain", "guidance", "--task", task["task_id"]],
        ):
            with self.subTest(args=args):
                code, result = self.invoke(["count", *args])
                self.assertEqual(code, 1)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["type"], "ValueError")

    def test_count_uses_explicit_config_root_for_direct_worktree(self):
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / "rightmemory.toml").write_text("invalid TOML", encoding="utf-8")
        (worktree / "MEMORY.md").write_text("# Counted memory\n", encoding="utf-8")
        (self.root / "rightmemory.toml").write_text(
            "[curation.token_limits]\nmemory = 100\n", encoding="utf-8",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--root", str(worktree), "count", "--domain", "memory", "--config-root", str(self.root),
            ])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["token_limit"], 100)
        self.assertGreater(result["retained_text_tokens"], 0)
        self.assertFalse((worktree / ".runtime").exists())

    def test_withdraw_uses_shared_candidate_identity(self):
        _, saved = self.invoke(["--executor", "native", "--session", "s", "Case."], submit=True)
        code, result = self.invoke(["withdraw", "--candidate", saved["candidate_id"]])
        self.assertEqual(code, 0)
        self.assertEqual(list_learning_candidates(self.root), ())

    def test_file_and_inline_evidence_are_rejected_without_mutation(self):
        path = self.root / "evidence.txt"
        path.write_text("File evidence.")
        code, result = self.invoke(["--session", "s", "--file", str(path), "Inline evidence."], submit=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["type"], "ValueError")
        self.assertEqual(list_learning_candidates(self.root), ())
