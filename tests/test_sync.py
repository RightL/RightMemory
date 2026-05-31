import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.sync import GIT_TIMEOUT_SECONDS, SyncManager, SyncResult


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

    def test_preflight_rejects_memory_root_nested_in_outer_git_repo(self):
        outer_remote = self.root / "outer.git"
        outer = self.root / "outer"
        peer = self.root / "outer-peer"
        self._git(self.root, "init", "--bare", str(outer_remote))
        self._git(self.root, "clone", str(outer_remote), str(outer))
        self._git(outer, "config", "user.email", "test@example.com")
        self._git(outer, "config", "user.name", "Test User")
        nested = outer / "memory"
        nested.mkdir()
        (nested / "MEMORY.md").write_text("# Domain\n\n- `one` nested memory → []\n", encoding="utf-8")
        self._git(outer, "add", "memory/MEMORY.md")
        self._git(outer, "commit", "-m", "initial outer memory")
        self._git(outer, "push", "-u", "origin", "HEAD:main")
        self._git(outer, "branch", "--set-upstream-to", "origin/main")
        outer_head = self._git(outer, "rev-parse", "HEAD")

        self._git(self.root, "clone", str(outer_remote), str(peer))
        self._git(peer, "config", "user.email", "test@example.com")
        self._git(peer, "config", "user.name", "Test User")
        self._git(peer, "checkout", "-B", "main", "origin/main")
        self._git(peer, "branch", "--set-upstream-to", "origin/main")
        (peer / "memory" / "MEMORY.md").write_text(
            "# Domain\n\n- `one` nested memory → []\n- `two` remote outer change → []\n",
            encoding="utf-8",
        )
        self._git(peer, "add", "memory/MEMORY.md")
        self._git(peer, "commit", "-m", "remote outer memory")
        self._git(peer, "push")

        result = SyncManager(SyncConfig(memory_root=nested, enabled=True)).preflight()

        self.assertEqual(result.status, "unconfigured")
        self.assertEqual(self._git(outer, "rev-parse", "HEAD"), outer_head)
        self.assertNotIn("remote outer change", (nested / "MEMORY.md").read_text(encoding="utf-8"))

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
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote only → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        memory = (self.device / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("local dirty", memory)
        self.assertNotIn("remote only", memory)

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

    def test_conflict_can_be_resolved_committed_and_pushed(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote durable fact → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local durable fact → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        conflict = manager.push()
        self.assertEqual(conflict.status, "conflict")

        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one-remote` remote durable fact → []\n"
            "- `one-local` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "memory: resolve sync conflict")

        pushed = manager.push()

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
            "# Domain\n\n- `one` first → []\n- `two` committed local → []\n- `three` dirty local → []\n",
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertNotIn("committed local", remote_memory)
        self.assertNotIn("dirty local", remote_memory)

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

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "synced")
        self.assertIn("last_successful_pull_at", state)
        parsed = datetime.fromisoformat(state["last_successful_pull_at"])
        self.assertEqual(parsed.tzinfo, UTC)

    def test_preflight_dirty_records_state(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))
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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "pushed")
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertIn("local committed", remote_memory)

    def test_background_pull_reports_conflict_even_when_pull_state_fresh(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local → []\n", encoding="utf-8")
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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_background_pull_reports_dirty_even_when_pull_state_fresh(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` dirty → []\n", encoding="utf-8")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])

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

    def test_repair_message_describes_dirty_and_conflict_states(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))

        dirty = manager.repair_message(SyncResult("dirty", "dirty memory", ["MEMORY.md"]))
        conflict = manager.repair_message(SyncResult("conflict", "memory sync conflict", ["MEMORY.md"]))

        self.assertIn("inspect and repair dirty memory state", dirty)
        self.assertIn("resolve conflict markers", conflict)

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
