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

    def test_single_submission_and_three_no_change_domains_retire_source(self):
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
        captured = self._git("rev-parse", "HEAD")
        _, repeated = self.invoke(args, submit=True)
        self.assertFalse(repeated["created"])
        self.assertEqual(self._git("rev-parse", "HEAD"), captured)
        source_id = f"L#candidates/{saved['candidate_id']}.json"
        self.assertIn(source_id, learning_sources(self.root))
        for domain in ("memory", "guidance", "skills"):
            _, task = self.invoke(["begin", "--domain", domain])
            self.assertEqual(task["status"], "prepared")
            _, done = self.invoke(["complete", "--task", task["task_id"]])
            self.assertEqual(done["status"], "completed")
            self.assertEqual(source_id in learning_sources(self.root), domain != "skills")
        self.assertEqual(list_learning_candidates(self.root), ())
        _, repeated = self.invoke(args, submit=True)
        self.assertFalse(repeated["created"])
        self.assertNotIn(source_id, learning_sources(self.root))

    def test_default_single_submission_waits_without_starting_curation(self):
        code, saved = self.invoke(
            ["--executor", "native", "--session", "s", "One pending observation."], submit=True,
        )
        self.assertEqual(code, 0)
        for notice in saved["curation"]:
            self.assertFalse(notice["ready"])
            self.assertEqual(notice["candidate_count"], 1)
            self.assertIsNotNone(notice["next_ready_at"])
        captured = self._git("rev-parse", "HEAD")
        with patch("rightmemory.learning_coordinator.LearningCoordinator._worktree", side_effect=AssertionError("worktree created")):
            code, waiting = self.invoke(["begin", "--domain", "memory"])
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
        self.invoke(["complete", "--task", task["task_id"]])
        self.assertEqual(len(list_learning_candidates(self.root, domain="memory")), 1)

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
