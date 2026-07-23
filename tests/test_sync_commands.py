import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.sync import (
    GIT_TIMEOUT_SECONDS,
    SyncManager,
    retrieve_sync_needs_deferred,
)
from tests.sync_test_base import SyncTestBase


class SyncCommandTests(SyncTestBase):
    def test_push_merges_remote_change_and_reports_conflict(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        start_memory = (self.device / "MEMORY.md").read_bytes()

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), start_memory)
        self.assertNotIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_conflict_can_be_resolved_committed_and_pushed(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        active_head = self._git(self.device, "rev-parse", "HEAD")

        def repair(candidate, result, operation_id):
            self.assertNotEqual(candidate, self.device)
            self.assertEqual(result.status, "conflict")
            self.assertTrue(operation_id.startswith("sync-repair-"))
            self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), active_head)
            self.assertNotIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
            (candidate / "MEMORY.md").write_text(
                "# Domain\n\n"
                "- `one-remote` remote durable fact → []\n"
                "- `one-local` local durable fact → []\n",
                encoding="utf-8",
            )
            self._git(candidate, "add", "MEMORY.md")
            self._git(candidate, "commit", "-m", "memory: resolve staged sync conflict")
            return "staged conflict repaired"

        pushed = manager.push(repair=repair)

        self.assertEqual(pushed.status, "pushed")
        self._git(self.other, "pull", "--ff-only")
        text = (self.other / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("one-remote", text)
        self.assertIn("one-local", text)

    def test_push_reports_dirty_memory_without_pushing(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` committed local → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one` first → []\n"
            "- `two` committed local → []\n"
            "- `three` dirty local → []\n",
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertNotIn("committed local", remote_memory)
        self.assertNotIn("dirty local", remote_memory)

    def test_push_refuses_committed_paths_outside_synchronized_state(self):
        remote_head = self._git(self.other, "rev-parse", "origin/main")
        (self.device / "rightmemory.toml").write_text(
            '[retrieve.model]\nmodel_id = "private/provider"\n',
            encoding="utf-8",
        )
        self._git(self.device, "add", "rightmemory.toml")
        self._git(self.device, "commit", "-m", "local machine config")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, ["rightmemory.toml"])
        self.assertIn("outside the synchronized state", result.message)
        self._git(self.other, "fetch", "origin")
        self.assertEqual(self._git(self.other, "rev-parse", "origin/main"), remote_head)

    def test_push_reports_dirty_insight_log(self):
        insight = self.device / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["insight_logs/2026-05-30-143012.md"])

    def test_push_ignores_untracked_retired_dream_log(self):
        dream = self.device / "dream_logs" / "2026-05-30.md"
        dream.parent.mkdir()
        dream.write_text("# Dream\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "pushed")

    def test_push_uses_upstream_even_when_local_branch_name_differs(self):
        self._git(self.device, "checkout", "-B", "memory-device", "origin/main")
        self._git(self.device, "branch", "--set-upstream-to", "origin/main")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` local branch diff → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local branch memory")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "pushed")
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertIn("local branch diff", remote_memory)

    def test_state_records_successful_pull(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        result = manager.preflight()

        state = json.loads(
            (self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8")
        )

        self.assertEqual(result.status, "synced")
        self.assertIn("last_successful_pull_at", state)
        parsed = datetime.fromisoformat(state["last_successful_pull_at"])
        self.assertEqual(parsed.tzinfo, UTC)

    def test_preflight_dirty_records_state(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local dirty → []\n",
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        state = json.loads(
            (self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(result.status, "dirty")
        self.assertEqual(state["last_status"], "dirty")
        self.assertEqual(state["last_files"], ["MEMORY.md"])

    def test_git_runs_noninteractive_with_timeout(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")

        with patch("rightmemory.sync.subprocess.run", return_value=completed) as run:
            result = manager._git("status")

        self.assertIs(result, completed)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_ASKPASS"], "true")
        self.assertEqual(kwargs["timeout"], GIT_TIMEOUT_SECONDS)

    def test_retrieve_refresh_fast_forwards_remote_change(self):
        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Remote work {#remote-work}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "pursuit: remote change")
        self._git(self.other, "push")
        checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True)
        ).refresh_for_retrieve(now=checked_at)

        self.assertEqual(result.status, "synced")
        self.assertIn("remote-work", (self.device / "PURSUITS.md").read_text(encoding="utf-8"))
        state = json.loads(
            (self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["last_remote_check_attempt_at"], checked_at.isoformat())
        self.assertEqual(state["last_successful_remote_check_at"], checked_at.isoformat())

    def test_retrieve_refresh_defers_push_for_local_ahead_commit(self):
        (self.device / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Local work {#local-work}\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "PURSUITS.md")
        self._git(self.device, "commit", "-m", "local pending memory")

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True)
        ).refresh_for_retrieve()

        self.assertEqual(result.status, "ahead")
        self.assertTrue(retrieve_sync_needs_deferred(result))
        self._git(self.other, "fetch", "origin")
        remote_pursuits = self._git(self.other, "show", "origin/main:PURSUITS.md")
        self.assertNotIn("Local work", remote_pursuits)
