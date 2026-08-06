import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.sync import (
    RETRIEVE_SYNC_FETCH_TIMEOUT_SECONDS,
    RETRIEVE_SYNC_REFRESH_SECONDS,
    SyncManager,
    SyncResult,
    retrieve_sync_needs_deferred,
    schedule_deferred_sync,
)
from tests.sync_test_base import SyncTestBase


class SyncSchedulingTests(SyncTestBase):
    def test_retrieve_refresh_throttles_recent_failed_attempt_and_uses_short_fetch_timeout(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
        failed_fetch = subprocess.CompletedProcess(["git", "fetch"], 124, "", "timed out")

        with (
            patch.object(manager, "_upstream", return_value="origin/main"),
            patch.object(manager, "_git", return_value=failed_fetch) as git,
        ):
            first = manager.refresh_for_retrieve(now=checked_at)
            second = manager.refresh_for_retrieve(
                now=checked_at + timedelta(seconds=RETRIEVE_SYNC_REFRESH_SECONDS - 1)
            )

        self.assertEqual(first.status, "offline")
        self.assertEqual(second.status, "fresh")
        git.assert_called_once_with(
            "fetch",
            timeout_seconds=RETRIEVE_SYNC_FETCH_TIMEOUT_SECONDS,
        )

    def test_retrieve_refresh_is_due_at_five_minute_boundary(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        checked_at = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "last_remote_check_attempt_at": (
                        checked_at - timedelta(seconds=RETRIEVE_SYNC_REFRESH_SECONDS)
                    ).isoformat()
                }
            ),
            encoding="utf-8",
        )
        failed_fetch = subprocess.CompletedProcess(["git", "fetch"], 1, "", "offline")

        with (
            patch.object(manager, "_upstream", return_value="origin/main"),
            patch.object(manager, "_git", return_value=failed_fetch) as git,
        ):
            result = manager.refresh_for_retrieve(now=checked_at)

        self.assertEqual(result.status, "offline")
        git.assert_called_once_with(
            "fetch",
            timeout_seconds=RETRIEVE_SYNC_FETCH_TIMEOUT_SECONDS,
        )

    def test_retrieve_refresh_reports_busy_when_another_cycle_holds_the_lock(self):
        first = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        second = SyncManager(SyncConfig(memory_root=self.device, enabled=True))

        with first._cycle_locked_nonblocking() as acquired:
            self.assertTrue(acquired)
            result = second.refresh_for_retrieve()

        self.assertEqual(result.status, "busy")

    def test_deferred_sync_policy_and_scheduler(self):
        for status in ("offline", "error", "dirty", "conflict", "ahead"):
            self.assertTrue(retrieve_sync_needs_deferred(SyncResult(status, status)))
        for status in ("disabled", "unconfigured", "fresh", "synced", "pushed", "busy"):
            self.assertFalse(retrieve_sync_needs_deferred(SyncResult(status, status)))

        with (
            patch("rightmemory.sync.python_module_child_env", return_value={"BASE": "1"}),
            patch(
                "rightmemory.sync.detached_process_kwargs",
                return_value={"start_new_session": True},
            ),
            patch("rightmemory.sync.subprocess.Popen") as popen,
        ):
            schedule_deferred_sync(self.device)

        args = popen.call_args.args
        kwargs = popen.call_args.kwargs
        self.assertEqual(
            args[0],
            [sys.executable, "-m", "rightmemory.cli", "sync", "_deferred"],
        )
        self.assertEqual(kwargs["cwd"], self.device.resolve())
        self.assertEqual(kwargs["env"]["RIGHTMEMORY_ROOT"], str(self.device.resolve()))
        self.assertEqual(
            Path(kwargs["stdout"].name),
            self.device / ".runtime" / "sync" / "deferred.log",
        )
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertTrue(kwargs["start_new_session"])

    def test_background_pull_skips_fresh_state(self):
        manager = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        )
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "fresh")

    def test_background_pull_fetches_remote_change_even_when_state_is_fresh(self):
        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Remote work {#remote-work}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "pursuit: remote change")
        self._git(self.other, "push")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        ).background_pull()

        self.assertEqual(result.status, "synced")
        self.assertIn("remote-work", (self.device / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_background_pull_pushes_ahead_commits_even_when_pull_state_fresh(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` local committed → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        ).background_pull()

        self.assertEqual(result.status, "pushed")
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertIn("local committed", remote_memory)

    def test_background_pull_reports_conflict_even_when_pull_state_fresh(self):
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
        self._git(self.device, "fetch", "origin")
        merge = subprocess.run(
            ["git", "merge", "--no-edit", "origin/main"],
            cwd=self.device,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(merge.returncode, 0, merge.stdout + merge.stderr)

        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        ).background_pull()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_background_pull_reports_dirty_even_when_pull_state_fresh(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` dirty → []\n",
            encoding="utf-8",
        )
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        ).background_pull()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])

    def test_background_sync_gives_active_dirty_state_one_bounded_repair(self):
        original = (self.device / "MEMORY.md").read_text(encoding="utf-8")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\nlocal dirty state\n",
            encoding="utf-8",
        )
        diagnostics = []

        def repair(result):
            diagnostics.append(result)
            (self.device / "MEMORY.md").write_text(original, encoding="utf-8")

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True)
        ).background_sync(active_repair=repair)

        self.assertEqual(result.status, "synced")
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].status, "dirty")

    def test_background_pull_runs_when_stale(self):
        manager = SyncManager(
            SyncConfig(
                memory_root=self.device,
                enabled=True,
                stale_pull_after_hours=24,
            )
        )
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "last_successful_pull_at": (
                        datetime.now(UTC) - timedelta(hours=25)
                    ).isoformat()
                }
            ),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "synced")
