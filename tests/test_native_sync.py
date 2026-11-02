from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant import AssistantService
from rightmemory.native_sync import NativeSyncRepair
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.sync import SyncManager, SyncRepairStaleError, SyncResult
from tests.sync_test_base import SyncTestBase


class NativeSyncTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        (self.device / "rightmemory.toml").write_text("[sync]\nenabled = true\n", encoding="utf-8")
        exclude = self.device / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\nrightmemory.toml\n")
        self.prompt = patch("rightmemory.prompt.build_cli_agent_instructions", return_value="test instructions")
        self.prompt.start()
        self.addCleanup(self.prompt.stop)
        self.repair = NativeSyncRepair(self.device)
        self.service = AssistantService(self.device)
        self._create_remote_local_conflict()
        self.start = self._git(self.device, "rev-parse", "HEAD")
        self.memory = (self.device / "MEMORY.md").read_bytes()

    def _begin(self):
        task = self.repair.begin()
        self.assertTrue(task["claimed"])
        self.assertEqual(task["status"], "prepared")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)
        return task

    def _resolve(self, task):
        self._commit_candidate_resolution(Path(task["worktree"]))

    def _local_change(self):
        with (self.device / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write("- `later` Local later fact. → []\n")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local advancement")

    def test_discovery_returns_actionable_notice_without_preparing_or_using_model(self):
        with patch("rightmemory.runtime.RightMemoryRuntime", side_effect=AssertionError("model created")):
            result = self.service.sync("pull")
        self.assertEqual(result["sync"]["status"], "conflict")
        self.assertTrue(result["sync"]["repairable"])
        self.assertEqual(result["sync_repair"]["status"], "required")
        self.assertEqual(result["sync_repair"]["begin_argv"][-2:], ["sync-repair", "begin"])
        self.assertIsNone(self.repair.manager.native_repair_record())
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), self.memory)
        self._assert_no_sync_candidates()

    def test_prepare_resume_preserves_partial_edits_and_releases_process_lease(self):
        task = self._begin()
        worktree = Path(task["worktree"])
        self.assertIn("MEMORY.md", self._git(worktree, "diff", "--name-only", "--diff-filter=U"))
        (worktree / "MEMORY.md").write_text("partial repair", encoding="utf-8")
        resumed = NativeSyncRepair(self.device).begin()
        self.assertFalse(resumed["claimed"])
        self.assertEqual(resumed["task_id"], task["task_id"])
        self.assertEqual((worktree / "MEMORY.md").read_text(), "partial repair")
        self.assertTrue(Path(resumed["instructions_file"]).is_file())
        self.assertEqual(list((self.device / ".runtime" / "worktree-leases").glob("sync-*.json")), [])
        self.assertEqual(self.repair.status()["operation_phase"], "running")

    def test_complete_publishes_both_facts_and_repeat_does_not_duplicate_semantic_work(self):
        task = self._begin()
        self._resolve(task)
        self.assertTrue(self.repair.validate(task["task_id"])["ok"])
        receipt = self.repair.complete(task["task_id"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["sync"]["status"], "pushed")
        head = self._git(self.device, "rev-parse", "HEAD")
        self.assertEqual(head, receipt["landed_commit"])
        self.assertEqual(self._git(self.remote, "rev-parse", "main"), head)
        memory = (self.device / "MEMORY.md").read_text()
        self.assertIn("local durable fact", memory)
        self.assertIn("remote durable fact", memory)
        again = NativeSyncRepair(self.device).complete(task["task_id"])
        self.assertEqual(again["landed_commit"], head)
        self.assertEqual(self._git(self.remote, "rev-parse", "main"), head)
        self.assertEqual(self.repair.status()["status"], "idle")
        self._assert_no_sync_candidates()

    def test_uncommitted_and_invalid_results_keep_active_root_and_workspace(self):
        task = self._begin()
        with self.assertRaisesRegex(ValueError, "not committed"):
            self.repair.complete(task["task_id"])
        worktree = Path(task["worktree"])
        (worktree / "MEMORY.md").write_text(
            "# Domain\n\n- `one` A → []\n- `one` B → []\n", encoding="utf-8",
        )
        self._git(worktree, "add", "MEMORY.md")
        self._git(worktree, "commit", "-m", "invalid merge")
        self.assertFalse(self.repair.validate(task["task_id"])["ok"])
        with self.assertRaises(RuntimeError):
            self.repair.complete(task["task_id"])
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), self.memory)
        self.assertTrue(worktree.is_dir())
        (worktree / "MEMORY.md").write_text("# Domain\n\n- `one` Resolved fact → []\n", encoding="utf-8")
        self._git(worktree, "add", "MEMORY.md")
        self._git(worktree, "commit", "--amend", "--no-edit")
        self.assertTrue(self.repair.complete(task["task_id"])["applied"])

    def test_configured_and_ordinary_sync_do_not_take_over_native_work(self):
        task = self._begin()
        with patch.object(self.repair.manager, "_run_or_adopt_repair", side_effect=AssertionError("taken over")):
            for call in (
                lambda: self.repair.manager.pull(repair=lambda *_: "unused"),
                lambda: self.repair.manager.push(repair=lambda *_: "unused"),
                lambda: self.repair.manager.background_sync(repair=lambda *_: "unused"),
            ):
                result = call()
                self.assertEqual(result.status, "repair_required")
                self.assertEqual(result.operation_id, task["task_id"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)

    def test_prepared_result_retries_without_semantic_execution(self):
        task = self._begin()
        self._resolve(task)
        with patch.object(SyncManager, "_publish_candidate", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.repair.complete(task["task_id"])
        self.assertEqual(self.repair.status(task["task_id"])["operation_phase"], "prepared")
        self.assertEqual(self.repair.manager.pull().status, "repair_required")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)
        self.assertTrue(self.repair.complete(task["task_id"])["applied"])

    def test_begin_retry_recovers_prepared_completion_and_finishes_sync(self):
        self.service.sync("pull")
        task = self._begin()
        self._resolve(task)
        with patch.object(SyncManager, "_publish_candidate", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.repair.complete(task["task_id"])
        receipt = self.repair.begin(retry_task=task["task_id"])
        self.assertEqual(receipt["task_id"], task["task_id"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["sync"]["status"], "pushed")
        self.assertIsNone(receipt["sync_repair"])
        self.assertFalse(Path(task["instructions_file"]).exists())

    def test_lost_receipt_after_landing_and_cleanup_recovers_same_commit(self):
        task = self._begin()
        self._resolve(task)
        with patch.object(SemanticOperationStore, "complete_commit", side_effect=OSError("lost receipt")):
            with self.assertRaises(OSError):
                self.repair.complete(task["task_id"])
        landed = self._git(self.device, "rev-parse", "HEAD")
        self.assertFalse(Path(task["worktree"]).exists())
        receipt = self.repair.complete(task["task_id"])
        self.assertEqual(receipt["landed_commit"], landed)
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), landed)

    def test_later_remote_commits_do_not_discard_completed_repair(self):
        task = self._begin()
        self._resolve(task)
        (self.other / "PURSUITS.md").write_text("# Pursuits\n\n# Later context\n", encoding="utf-8")
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "remote advancement")
        self._git(self.other, "push")
        receipt = self.repair.complete(task["task_id"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["sync"]["status"], "pushed")
        self.assertIn("Later context", (self.device / "PURSUITS.md").read_text())
        self._git(self.device, "merge-base", "--is-ancestor", receipt["landed_commit"], "HEAD")

    def test_local_staleness_requires_explicit_replacement_and_fences_old_result(self):
        task = self._begin()
        self._resolve(task)
        self._local_change()
        changed = self._git(self.device, "rev-parse", "HEAD")
        with self.assertRaises(SyncRepairStaleError):
            self.repair.complete(task["task_id"])
        resumed = self.repair.begin()
        self.assertEqual(resumed["task_id"], task["task_id"])
        replacement = self.repair.begin(retry_task=task["task_id"])
        self.assertNotEqual(replacement["task_id"], task["task_id"])
        self.assertTrue(Path(replacement["retained_worktree"]).is_dir())
        self.assertEqual(self.repair.complete(task["task_id"])["status"], "superseded")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), changed)
        self._resolve(replacement)
        self.assertTrue(self.repair.complete(replacement["task_id"])["applied"])

    def test_explicit_replacement_of_same_input_has_a_new_identity(self):
        task = self._begin()
        replacement = self.repair.begin(retry_task=task["task_id"])
        self.assertNotEqual(replacement["task_id"], task["task_id"])
        self.assertEqual(self.repair.status(task["task_id"])["status"], "superseded")
        self.assertEqual(self.repair.begin()["task_id"], replacement["task_id"])

    def test_prepared_stale_result_can_be_replaced_without_blocking_future_sync(self):
        task = self._begin()
        self._resolve(task)
        with patch.object(SyncManager, "_publish_candidate", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.repair.complete(task["task_id"])
        self._local_change()
        replacement = self.repair.begin(retry_task=task["task_id"])
        self.assertEqual(self.repair.status(task["task_id"])["status"], "superseded")
        self._resolve(replacement)
        self.assertTrue(self.repair.complete(replacement["task_id"])["applied"])

    def test_failed_push_retries_transport_without_changing_repair_identity(self):
        task = self._begin()
        self._resolve(task)
        with patch.object(SyncManager, "push", return_value=SyncResult("offline", "offline")):
            receipt = self.repair.complete(task["task_id"])
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["sync"]["status"], "offline")
        retried = self.repair.complete(task["task_id"])
        self.assertEqual(retried["landed_commit"], receipt["landed_commit"])
        self.assertEqual(retried["sync"]["status"], "pushed")

    def test_native_candidate_cannot_change_valid_program_owned_state(self):
        from rightmemory.guidance import GUIDANCE_INBOX_PATH, GUIDANCE_INBOX_HEADING

        task = self._begin()
        worktree = Path(task["worktree"])
        (worktree / GUIDANCE_INBOX_PATH).write_text(GUIDANCE_INBOX_HEADING + "\n", encoding="utf-8")
        self._git(worktree, "add", GUIDANCE_INBOX_PATH)
        self._resolve(task)
        with self.assertRaisesRegex(RuntimeError, "program-owned"):
            self.repair.complete(task["task_id"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)

    def test_sync_validation_allows_feedback_overflow_for_later_maintenance(self):
        task = self._begin()
        worktree = Path(task["worktree"])
        (worktree / "corrections.md").write_text(self._corrections_markdown(12), encoding="utf-8")
        self._git(worktree, "add", "corrections.md")
        self._resolve(task)
        self.assertTrue(self.repair.validate(task["task_id"])["ok"])
        self.assertTrue(self.repair.complete(task["task_id"])["applied"])

    def test_missing_workspace_requires_explicit_recovery(self):
        task = self._begin()
        self._git(self.device, "worktree", "remove", "--force", task["worktree"])
        with self.assertRaisesRegex(FileNotFoundError, "missing"):
            self.repair.complete(task["task_id"])
        self.assertFalse(Path(task["worktree"]).exists())

    def test_non_merge_repair_commit_is_rejected(self):
        task = self._begin()
        worktree = Path(task["worktree"])
        self._git(worktree, "merge", "--abort")
        self._resolve(task)
        with self.assertRaisesRegex(RuntimeError, "expected merge"):
            self.repair.complete(task["task_id"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)

    def test_prepared_workspace_later_edits_are_not_discarded(self):
        task = self._begin()
        self._resolve(task)
        with patch.object(SyncManager, "_publish_candidate", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.repair.complete(task["task_id"])
        worktree = Path(task["worktree"])
        (worktree / "MEMORY.md").write_text("later uncommitted work", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "retained for review"):
            self.repair.complete(task["task_id"])
        self.assertEqual((worktree / "MEMORY.md").read_text(), "later uncommitted work")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), self.start)

    def test_configured_runtime_preflight_stops_at_a_native_task(self):
        from rightmemory.runtime import RightMemoryRuntime
        from types import SimpleNamespace

        self._begin()
        runtime = object.__new__(RightMemoryRuntime)
        runtime.config = SimpleNamespace(memory_root=self.device)
        runtime._sync_manager = self.repair.manager
        with self.assertRaisesRegex(RuntimeError, "native sync repair"):
            runtime._sync_preflight_locked()

    def test_clean_but_invalid_incoming_merge_is_repairable(self):
        # Establish a clean valid base, then publish structurally invalid state
        # without a textual Git conflict.
        task = self._begin()
        self._resolve(task)
        self.repair.complete(task["task_id"])
        self._git(self.other, "pull", "--ff-only")
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` First → []\n- `one` Second → []\n", encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "invalid remote")
        self._git(self.other, "push")
        repair = self.repair.begin()
        self.assertTrue(repair["claimed"])
        self.assertEqual(self._git(Path(repair["worktree"]), "diff", "--name-only", "--diff-filter=U"), "")
        (Path(repair["worktree"]) / "MEMORY.md").write_text(
            "# Domain\n\n- `one` First → []\n- `two` Second → []\n", encoding="utf-8",
        )
        self._git(Path(repair["worktree"]), "add", "MEMORY.md")
        self._git(Path(repair["worktree"]), "commit", "-m", "repair invalid graph")
        self.assertTrue(self.repair.complete(repair["task_id"])["applied"])

    def _process_call(self, *args):
        script = r'''
import importlib.abc, sys
class NoModel(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('rightmemory.runtime', 'rightmemory.cli', 'rightmemory.agent_cli', 'pydantic_ai'):
            raise AssertionError('unexpected model runtime: ' + fullname)
sys.meta_path.insert(0, NoModel())
import rightmemory.prompt
rightmemory.prompt.build_cli_agent_instructions = lambda *args, **kwargs: 'test instructions'
from rightmemory.entrypoint import main
raise SystemExit(main(sys.argv[1:]))
'''
        result = subprocess.run(
            [sys.executable, "-c", script, "assistant", "--root", str(self.device), "sync-repair", *args],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_cli_task_outlives_its_process_and_completes_without_model_imports(self):
        task = self._process_call("begin")
        resumed = self._process_call("begin")
        self.assertEqual(task["task_id"], resumed["task_id"])
        self.assertFalse(resumed["claimed"])
        self._resolve(task)
        self.assertTrue(self._process_call("validate", "--task", task["task_id"])["ok"])
        self.assertTrue(self._process_call("complete", "--task", task["task_id"])["applied"])
        self.assertEqual(self._process_call("status", "--task", task["task_id"])["status"], "completed")

    def test_concurrent_prepare_commands_claim_only_one_task(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._process_call("begin"), range(2)))
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(sum(item["claimed"] for item in results), 1)
