import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.sync import SyncManager


class SyncManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.device = self.root / "device"
        self.other = self.root / "other"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.device))
        self._git(self.root, "clone", str(self.remote), str(self.other))
        for repo in (self.device, self.other):
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` first → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "initial memory")
        self._git(self.device, "push", "-u", "origin", "HEAD:main")
        self._git(self.device, "branch", "--set-upstream-to", "origin/main")
        self._git(self.other, "fetch", "origin")
        self._git(self.other, "checkout", "-B", "main", "origin/main")
        self._git(self.other, "branch", "--set-upstream-to", "origin/main")

    def test_preflight_disabled(self):
        result = SyncManager(SyncConfig(memory_root=self.device, enabled=False)).preflight()

        self.assertEqual(result.status, "disabled")
        self.assertIn("disabled", result.message)

    def test_preflight_fast_forwards_clean_repo(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertIn("two", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_preflight_reports_dirty_memory_without_merging(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])

    def test_push_merges_remote_change_and_reports_conflict(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_state_records_successful_pull(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        result = manager.preflight()

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "synced")
        self.assertIn("last_successful_pull_at", state)
        parsed = datetime.fromisoformat(state["last_successful_pull_at"])
        self.assertEqual(parsed.tzinfo, UTC)

    def test_background_pull_skips_fresh_state(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24))
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "fresh")

    def test_background_pull_runs_when_stale(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24))
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "synced")

    def _git(self, cwd: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr)
        return process.stdout.strip()
