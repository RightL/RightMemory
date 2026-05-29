import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.status import (
    DashboardStatus,
    GitStatus,
    SectionStatus,
    collect_async_update_section,
    collect_dreamer_section,
    collect_git_status,
    collect_managed_watch_sections,
    collect_status,
    format_status_dashboard,
    read_log_preview,
)


class StatusDashboardTests(unittest.TestCase):
    def test_collect_git_status_reports_clean_branch_and_head(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")
            head = self._git(root, "rev-parse", "--short", "HEAD")
            branch = self._git(root, "branch", "--show-current")

            status = collect_git_status(root)

        self.assertEqual(status.summary, f"clean on {branch} @ {head}")
        self.assertIsNone(status.issue)

    def test_collect_git_status_reports_dirty_count(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")
            (root / "MEMORY.md").write_text("# Dirty\n", encoding="utf-8")

            status = collect_git_status(root)

        self.assertIn("dirty: 1 path", status.summary)
        self.assertEqual(status.issue, "dirty worktree: 1 path")

    def test_collect_git_status_reports_unavailable_repo(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status = collect_git_status(Path(tempdir))

        self.assertIn("unavailable", status.summary)
        self.assertIsNotNone(status.issue)

    def test_read_log_preview_prefers_recent_failure_line(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "dreamer.log"
            log.write_text(
                "[2026-05-29T08:00:00+00:00] rightmemory dreamer cycle\n"
                "ordinary success message\n"
                "rightmemory dreamer cycle failed: RuntimeError: boom\n"
                "rightmemory dreamer watch stopped\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertEqual(preview, "rightmemory dreamer cycle failed: RuntimeError: boom")

    def test_read_log_preview_caps_long_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "pruner.log"
            log.write_text("x" * 400 + "\n", encoding="utf-8")

            preview = read_log_preview(log)

        self.assertEqual(len(preview), 300)

    def test_format_status_dashboard_renders_grouped_sections(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            watches=[
                SectionStatus(
                    name="review",
                    state="running pid 123",
                    log_path=".runtime/watch/review.log",
                    last="reviewed 3 sessions",
                ),
                SectionStatus(
                    name="pruner",
                    state="stopped",
                    log_path=".runtime/watch/pruner.log",
                    last="failed: boom",
                    issue="pruner failed",
                ),
            ],
            dreamer=SectionStatus(
                name="dreamer",
                state="running pid 456",
                log_path=".runtime/watch/dreamer.log",
                detail="trigger: 12.5/50.0 points",
            ),
            update=SectionStatus(
                name="update",
                state="worker: idle",
                log_path=".runtime/async/update/",
                detail="pending: 0 candidates across 0 sessions",
            ),
            issues=["pruner failed"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("RightMemory\n  root: /memory/root\n  git: clean on main @ abc1234", output)
        self.assertIn("Managed Watches", output)
        self.assertIn("review: running pid 123", output)
        self.assertIn("Async Update", output)
        self.assertIn("Recent Issues\n  pruner failed", output)

    def test_collect_managed_watches_includes_sync_disabled_and_log_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "review.log"
            log.parent.mkdir(parents=True)
            log.write_text("reviewed 3 sessions\n", encoding="utf-8")

            statuses = {
                "review": type(
                    "WatchStatus",
                    (),
                    {"name": "review", "state": "running", "pid": 123, "log_path": log},
                )(),
                "dreamer": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "dreamer",
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / "dreamer.log",
                    },
                )(),
                "pruner": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "pruner",
                        "state": "stale",
                        "pid": 456,
                        "log_path": root / ".runtime" / "watch" / "pruner.log",
                    },
                )(),
                "sync": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "sync",
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / "sync.log",
                    },
                )(),
            }

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

        self.assertEqual([watch.name for watch in watches], ["review", "dreamer", "pruner", "sync"])
        self.assertEqual(watches[0].state, "running pid 123")
        self.assertEqual(watches[0].last, "reviewed 3 sessions")
        self.assertEqual(watches[2].state, "stale pid 456")
        self.assertEqual(watches[3].state, "disabled")
        self.assertIn("pruner: stale pid 456", issues)

    def test_collect_managed_watches_default_reader_does_not_create_lock_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_dir = root / ".runtime" / "watch"
            watch_dir.mkdir(parents=True)

            watches, issues = collect_managed_watch_sections(
                root,
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

            self.assertEqual([watch.name for watch in watches], ["review", "dreamer", "pruner", "sync"])
            self.assertEqual(issues, [])
            self.assertFalse((watch_dir / "review.lock").exists())
            self.assertFalse((watch_dir / "dreamer.lock").exists())
            self.assertFalse((watch_dir / "pruner.lock").exists())

    def test_collect_dreamer_section_reports_trigger_progress(self):
        state = type(
            "DreamerState",
            (),
            {
                "points": 37.5,
                "updated_at": "2026-05-29T08:00:00+00:00",
                "last_successful_dream_at": "2026-05-28T08:00:00+00:00",
                "last_recovery_at": None,
            },
        )()
        config = type("DreamerConfig", (), {"trigger_points": 50.0, "check_interval_seconds": 3000})()

        section = collect_dreamer_section(
            Path("/memory/root"),
            trigger_reader=lambda memory_root: state,
            config_loader=lambda: config,
        )

        self.assertEqual(section.name, "dreamer")
        self.assertEqual(section.state, "trigger progress")
        self.assertIn("trigger: 37.5/50.0 points", section.detail)
        self.assertIn("check interval: 3000 seconds", section.detail)

    def test_collect_dreamer_section_reports_malformed_trigger_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trigger_path = root / ".runtime" / "dreamer" / "trigger-state.json"
            trigger_path.parent.mkdir(parents=True)
            trigger_path.write_text("{not json", encoding="utf-8")
            config = type("DreamerConfig", (), {"trigger_points": 50.0, "check_interval_seconds": 3000})()

            section = collect_dreamer_section(root, config_loader=lambda: config)

            self.assertIn("error", section.state)
            self.assertEqual(trigger_path.read_text(encoding="utf-8"), "{not json")
            self.assertEqual(list(trigger_path.parent.glob("trigger-state.corrupt-*.json")), [])

    def test_collect_async_update_section_reports_idle_when_state_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            section, issues = collect_async_update_section(Path(tempdir))

        self.assertEqual(section.name, "update")
        self.assertEqual(section.state, "worker: idle")
        self.assertIn("pending: 0 candidates across 0 sessions", section.detail)
        self.assertEqual(issues, [])

    def test_collect_async_update_section_counts_pending_and_current_batches(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "agent-1",
                        "role": "update",
                        "phase": "waiting",
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": None,
                        "pid": None,
                        "result": None,
                        "error": None,
                        "next_flush_at": "2026-05-29T10:00:00+00:00",
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "message": "first",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            },
                            {
                                "id": 2,
                                "message": "second",
                                "submitted_at": "2026-05-29T08:05:00+00:00",
                            },
                        ],
                        "next_id": 3,
                    }
                ),
                encoding="utf-8",
            )
            (async_root / "agent-2.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "agent-2",
                        "role": "update",
                        "phase": "running",
                        "started_at": "2026-05-29T08:30:00+00:00",
                        "finished_at": None,
                        "pid": 111,
                        "result": "accepted 1 candidate",
                        "error": None,
                        "next_flush_at": None,
                        "current_batch": [
                            {
                                "id": 1,
                                "message": "running",
                                "submitted_at": "2026-05-29T08:30:00+00:00",
                            }
                        ],
                        "pending": [],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root, process_exists=lambda pid: True)

        self.assertEqual(section.state, "worker: idle")
        self.assertIn("pending: 2 candidates across 1 session", section.detail)
        self.assertIn("current batch: 1 candidate across 1 session", section.detail)
        self.assertIn("next flush: 2026-05-29T10:00:00+00:00", section.detail)
        self.assertEqual(section.last, "accepted 1 candidate")
        self.assertEqual(issues, [])

    def test_collect_async_update_section_reports_running_worker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            worker_root = root / ".runtime" / "async" / "update" / "_worker"
            worker_root.mkdir(parents=True)
            (worker_root / "state.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "pid": 999,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "batch_id": "update-batch-abc",
                        "session_ids": ["agent-1"],
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root, process_exists=lambda pid: True)

        self.assertEqual(section.state, "worker: running pid 999")
        self.assertIn("batch: update-batch-abc", section.detail)
        self.assertIn("sessions: agent-1", section.detail)
        self.assertEqual(issues, [])

    def test_collect_async_update_section_reports_malformed_state_locally(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text("{not json", encoding="utf-8")

            section, issues = collect_async_update_section(root)

        self.assertEqual(section.name, "update")
        self.assertIn("state error", section.state)
        self.assertEqual(len(issues), 1)
        self.assertIn("update: state error", issues[0])

    def test_collect_status_aggregates_sections_and_issues(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")

            dashboard = collect_status(
                root,
                watch_collector=lambda memory_root: (
                    [SectionStatus(name="pruner", state="stale pid 42", issue="pruner: stale pid 42")],
                    ["pruner: stale pid 42"],
                ),
                dreamer_collector=lambda memory_root: SectionStatus(
                    name="dreamer",
                    state="trigger progress",
                    detail="trigger: 1.0/50.0 points",
                ),
                update_collector=lambda memory_root: (SectionStatus(name="update", state="worker: idle"), []),
            )

        self.assertEqual(dashboard.root, root)
        self.assertEqual(len(dashboard.watches), 1)
        self.assertEqual(dashboard.dreamer.name, "dreamer")
        self.assertEqual(dashboard.update.name, "update")
        self.assertIn("pruner: stale pid 42", dashboard.issues)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
