import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.semantic_operation import OperationEffect, SemanticOperationStore
from rightmemory.status import (
    DashboardStatus,
    GitStatus,
    SectionStatus,
    collect_async_update_section,
    collect_dreamer_section,
    collect_git_status,
    collect_insight_section,
    collect_managed_watch_sections,
    collect_semantic_operation_section,
    collect_status,
    format_status_dashboard,
    read_log_preview,
)


class StatusDashboardTests(unittest.TestCase):
    def test_semantic_operation_status_shows_failed_effect_as_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = SemanticOperationStore(root)
            store.begin("update-op-1", {"role": "update", "session_id": "agent-1"})
            store.prepare_outcome(
                "update-op-1",
                output="no change",
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("file-view-publish"),),
            )
            store.complete_no_change("update-op-1")
            store.mark_effect(
                "update-op-1",
                "file-view-publish",
                "failed",
                error="hub offline",
            )

            section, issues = collect_semantic_operation_section(root)

        self.assertEqual(section.state, "1 effect pending")
        self.assertIn("pending effects: 1", section.detail)
        self.assertEqual(issues, ["semantic effect: update-op-1: file-view-publish: hub offline"])

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

    def test_collect_git_status_reports_missing_root_without_crashing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status = collect_git_status(Path(tempdir) / "missing")

        self.assertIn("unavailable", status.summary)
        self.assertIn("git unavailable", status.issue)

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

    def test_read_log_preview_ignores_old_failure_before_later_event(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "dreamer.log"
            log.write_text(
                "[2026-05-29T08:00:00+00:00] rightmemory dreamer cycle\n"
                "rightmemory dreamer cycle failed: RuntimeError: old boom\n"
                "[2026-05-29T09:00:00+00:00] rightmemory dreamer cycle\n"
                "## Dream Cycle Complete\n"
                "Validation: Passed\n"
                "Commit: abc1234\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertEqual(preview, "## Dream Cycle Complete\nValidation: Passed\nCommit: abc1234")

    def test_read_log_preview_uses_bounded_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "pruner.log"
            log.write_text(
                "rightmemory pruner check failed: ancient\n"
                + ("ordinary success message\n" * 4000)
                + "latest success\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertIn("latest success", preview)
        self.assertNotIn("ancient", preview)

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
            insight=SectionStatus(
                name="insight",
                state="trigger progress",
                detail="trigger: 88.0/150.0 points",
            ),
            update=SectionStatus(
                name="update",
                state="worker: idle",
                detail="pending: 0 candidates across 0 sessions\nstate: .runtime/async/update",
            ),
            issues=["pruner failed"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn(f"RightMemory\n  root: {Path('/memory/root')}\n  git: clean on main @ abc1234", output)
        self.assertIn("Managed Watches", output)
        self.assertIn("review: running pid 123", output)
        self.assertIn("Insight", output)
        self.assertIn("Async Update", output)
        self.assertIn("state: .runtime/async/update", output)
        self.assertNotIn("log: .runtime/async/update", output)
        self.assertIn("Recent Issues\n  pruner failed", output)

    def test_format_status_dashboard_marks_missing_logs(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            watches=[
                SectionStatus(
                    name="review",
                    state="stopped",
                    log_path=".runtime/watch/review.log",
                    log_missing=True,
                ),
            ],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("log: .runtime/watch/review.log (missing)", output)

    def test_format_status_dashboard_renders_recovery_hints_for_known_issue_shapes(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="dirty: 1 path", issue="dirty worktree: 1 path"),
            issues=[
                "git unavailable: not a git repository",
                "review: stale pid 456",
                "dreamer: running outside manager",
                "review: status error: ValueError: bad state",
                "pruner: rightmemory pruner check failed: RuntimeError: boom",
                "sync config error: ValueError: bad sync config",
                "dreamer trigger error: ValueError: dreamer trigger points must be a number",
                "insight trigger error: ValueError: insight trigger points must be a number",
                "update worker: stale pid 4",
                "update worker: state error: ValueError: async update worker state must contain string field: status",
                "update: state error: JSONDecodeError: Expecting value",
                "update: manual: manual recovery required: permanent boom",
                "update: manual-two: manual recovery required: another boom",
                "update: retrying: retrying after error: temporary boom",
                "update: agent-1: error: boom",
                "managed watches: status error: RuntimeError: collector failed",
                "dreamer: status error: RuntimeError: collector failed",
                "insight: status error: RuntimeError: collector failed",
                "update: status error: RuntimeError: collector failed",
            ],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recovery", output)
        expected_hints = [
            "git: inspect with `git status --short`; resolve local changes before automatic writes continue",
            "git: inspect the configured memory root and repair Git before retrying",
            "review: run `rightmemory watch restart review`",
            "dreamer: stop the foreground process directly, then run `rightmemory watch start dreamer`",
            "review: rerun `rightmemory status`; inspect watch state if it persists",
            "pruner: inspect the shown log path, then run `rightmemory watch restart pruner` when appropriate",
            "sync: fix `rightmemory.toml`, then rerun `rightmemory status`",
            "dreamer: inspect `.runtime/dreamer/trigger-state.json`",
            "insight: inspect `.runtime/insight/trigger-state.json`",
            "update worker: inspect `.runtime/async/update/`; run `rightmemory update retry` only for manual recovery",
            "update worker: inspect `.runtime/async/update/_worker/state.json`",
            "update: inspect `.runtime/async/update/` for malformed session JSON",
            "update manual recovery: run `rightmemory update retry`",
            "update retrying: automatic retry is pending; inspect with `rightmemory update pull --session retrying`",
            "update agent-1: inspect with `rightmemory update pull --session agent-1`",
            "managed watches: rerun `rightmemory status`; inspect watch state if it persists",
            "dreamer: rerun `rightmemory status`; inspect dreamer state if it persists",
            "insight: rerun `rightmemory status`; inspect insight state if it persists",
            "update: rerun `rightmemory status`; inspect async update state if it persists",
        ]
        for hint in expected_hints:
            self.assertIn(f"  {hint}", output)
        self.assertEqual(output.count("update manual recovery: run `rightmemory update retry`"), 1)

    def test_format_status_dashboard_omits_recovery_when_no_hints_exist(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            issues=["plain informational issue without known recovery"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recent Issues", output)
        self.assertNotIn("Recovery", output)

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
                "update-review": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "update-review",
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / "update-review.log",
                    },
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
                "insight": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "insight",
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / "insight.log",
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
                "agent-cli-cleanup": type(
                    "WatchStatus",
                    (),
                    {
                        "name": "agent-cli-cleanup",
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / "agent-cli-cleanup.log",
                    },
                )(),
            }

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

        self.assertEqual(
            [watch.name for watch in watches],
            ["review", "update-review", "dreamer", "pruner", "insight", "sync", "agent-cli-cleanup"],
        )
        self.assertEqual(watches[0].state, "running pid 123")
        self.assertEqual(watches[0].last, "reviewed 3 sessions")
        self.assertEqual(next(watch.state for watch in watches if watch.name == "pruner"), "stale pid 456")
        self.assertEqual(next(watch.state for watch in watches if watch.name == "sync"), "disabled")
        self.assertIn("pruner: stale pid 456", issues)

    def test_collect_managed_watches_surfaces_failure_preview_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "pruner.log"
            log.parent.mkdir(parents=True)
            log.write_text("rightmemory pruner check failed: RuntimeError: boom\n", encoding="utf-8")

            statuses = {}
            for name in ("review", "update-review", "dreamer", "pruner", "insight", "sync", "agent-cli-cleanup"):
                statuses[name] = type(
                    "WatchStatus",
                    (),
                    {
                        "name": name,
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / f"{name}.log",
                    },
                )()
            statuses["pruner"].log_path = log

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": True})(),
            )

        pruner = next(watch for watch in watches if watch.name == "pruner")
        review = next(watch for watch in watches if watch.name == "review")
        self.assertEqual(pruner.last, "rightmemory pruner check failed: RuntimeError: boom")
        self.assertIn("pruner: rightmemory pruner check failed: RuntimeError: boom", issues)
        self.assertTrue(review.log_missing)

    def test_collect_managed_watches_does_not_surface_zero_failed_counter_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "review.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "[2026-05-29T15:00:00+00:00] rightmemory review scan\n"
                "reviewed: 0\n"
                "retried: 0\n"
                "failed: 0\n",
                encoding="utf-8",
            )
            statuses = {}
            for name in ("review", "update-review", "dreamer", "pruner", "insight", "sync", "agent-cli-cleanup"):
                statuses[name] = type(
                    "WatchStatus",
                    (),
                    {
                        "name": name,
                        "state": "running" if name == "review" else "stopped",
                        "pid": 123 if name == "review" else None,
                        "log_path": root / ".runtime" / "watch" / f"{name}.log",
                    },
                )()
            statuses["review"].log_path = log

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

        review = next(watch for watch in watches if watch.name == "review")
        self.assertIn("failed: 0", review.last)
        self.assertNotIn("review: failed: 0", issues)

    def test_collect_managed_watches_does_not_surface_graceful_stop_notice_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "dreamer.log"
            log.parent.mkdir(parents=True)
            log.write_text("rightmemory dreamer watch stopping after current work\n", encoding="utf-8")
            statuses = {}
            for name in ("review", "update-review", "dreamer", "pruner", "insight", "sync", "agent-cli-cleanup"):
                statuses[name] = type(
                    "WatchStatus",
                    (),
                    {
                        "name": name,
                        "state": "stopped",
                        "pid": None,
                        "log_path": root / ".runtime" / "watch" / f"{name}.log",
                    },
                )()
            statuses["dreamer"].log_path = log

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

        dreamer = next(watch for watch in watches if watch.name == "dreamer")
        self.assertEqual(dreamer.last, "rightmemory dreamer watch stopping after current work")
        self.assertEqual(issues, [])

    def test_collect_managed_watches_default_reader_does_not_create_lock_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_dir = root / ".runtime" / "watch"
            watch_dir.mkdir(parents=True)

            watches, issues = collect_managed_watch_sections(
                root,
                sync_config_loader=lambda: type("SyncConfig", (), {"enabled": False})(),
            )

            self.assertEqual(
                [watch.name for watch in watches],
                ["review", "update-review", "dreamer", "pruner", "insight", "sync", "agent-cli-cleanup"],
            )
            self.assertEqual(issues, [])
            self.assertFalse((watch_dir / "review.lock").exists())
            self.assertFalse((watch_dir / "update-review.lock").exists())
            self.assertFalse((watch_dir / "dreamer.lock").exists())
            self.assertFalse((watch_dir / "pruner.lock").exists())
            self.assertFalse((watch_dir / "insight.lock").exists())
            self.assertFalse((watch_dir / "agent-cli-cleanup.lock").exists())

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

    def test_collect_insight_section_reports_trigger_progress(self):
        state = type(
            "InsightState",
            (),
            {
                "points": 88.0,
                "updated_at": "2026-05-30T08:00:00+00:00",
                "last_successful_insight_at": "2026-05-29T08:00:00+00:00",
                "last_successful_insight_result": "artifact",
                "last_recovery_at": None,
            },
        )()
        config = type("InsightConfig", (), {"trigger_points": 150.0, "check_interval_seconds": 3000})()

        section = collect_insight_section(
            Path("/memory/root"),
            trigger_reader=lambda memory_root: state,
            config_loader=lambda: config,
        )

        self.assertEqual(section.name, "insight")
        self.assertEqual(section.state, "trigger progress")
        self.assertIn("trigger: 88.0/150.0 points", section.detail)
        self.assertIn("check interval: 3000 seconds", section.detail)
        self.assertIn("last result: artifact", section.detail)

    def test_collect_insight_section_reads_noop_last_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trigger_path = root / ".runtime" / "insight" / "trigger-state.json"
            trigger_path.parent.mkdir(parents=True)
            trigger_path.write_text(
                json.dumps(
                    {
                        "points": 0.0,
                        "updated_at": "2026-05-30T08:00:00+00:00",
                        "last_successful_insight_at": "2026-05-30T08:00:00+00:00",
                        "last_successful_insight_result": "noop",
                        "last_recovery_at": None,
                    }
                ),
                encoding="utf-8",
            )
            config = type("InsightConfig", (), {"trigger_points": 150.0, "check_interval_seconds": 3000})()

            section = collect_insight_section(root, config_loader=lambda: config)

        self.assertEqual(section.last, "2026-05-30T08:00:00+00:00")
        self.assertIn("last result: noop", section.detail)

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

    def test_collect_dreamer_section_reports_invalid_trigger_timestamp(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trigger_path = root / ".runtime" / "dreamer" / "trigger-state.json"
            trigger_path.parent.mkdir(parents=True)
            trigger_path.write_text(
                json.dumps({"points": 5.0, "updated_at": "not-a-date"}),
                encoding="utf-8",
            )
            config = type("DreamerConfig", (), {"trigger_points": 50.0, "check_interval_seconds": 3000})()

            section = collect_dreamer_section(root, config_loader=lambda: config)

        self.assertIn("error", section.state)
        self.assertIn("updated_at must be an ISO datetime string or null", section.issue)

    def test_collect_async_update_section_reports_idle_when_state_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            section, issues = collect_async_update_section(Path(tempdir))

        self.assertEqual(section.name, "update")
        self.assertEqual(section.state, "worker: idle")
        self.assertIn("pending: 0 candidates across 0 sessions", section.detail)
        self.assertIn("state: .runtime/async/update", section.detail)
        self.assertIsNone(section.log_path)
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
        self.assertIn("retrying: 0 candidates across 0 sessions", section.detail)
        self.assertIn("manual recovery: 0 candidates across 0 sessions", section.detail)
        self.assertIn("current batch: 1 candidate across 1 session", section.detail)
        self.assertIn("next flush: 2026-05-29T10:00:00+00:00", section.detail)
        self.assertEqual(section.last, "accepted 1 candidate")
        self.assertEqual(issues, [])

    def test_collect_async_update_section_separates_retrying_and_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            base = {
                "role": "update",
                "phase": None,
                "started_at": "2026-05-29T08:00:00+00:00",
                "finished_at": None,
                "pid": None,
                "result": None,
                "next_flush_at": None,
                "current_batch": [],
                "next_id": 2,
            }
            (async_root / "retrying.json").write_text(
                json.dumps(
                    {
                        **base,
                        "status": "failed",
                        "session_id": "retrying",
                        "error": "temporary boom",
                        "attempts": 1,
                        "next_retry_at": "2026-05-29T09:00:00+00:00",
                        "last_error": "temporary boom",
                        "pending": [
                            {
                                "id": 1,
                                "message": "retrying",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (async_root / "manual.json").write_text(
                json.dumps(
                    {
                        **base,
                        "status": "needs_manual_recovery",
                        "session_id": "manual",
                        "error": "permanent boom",
                        "attempts": 2,
                        "next_retry_at": None,
                        "last_error": "permanent boom",
                        "pending": [
                            {
                                "id": 1,
                                "message": "manual",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            },
                            {
                                "id": 2,
                                "message": "manual two",
                                "submitted_at": "2026-05-29T08:01:00+00:00",
                            },
                        ],
                        "next_id": 3,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertIn("pending: 0 candidates across 0 sessions", section.detail)
        self.assertIn("retrying: 1 candidate across 1 session", section.detail)
        self.assertIn("manual recovery: 2 candidates across 1 session", section.detail)
        self.assertIn("current batch: 0 candidates across 0 sessions", section.detail)
        self.assertIn("update: retrying: retrying after error: temporary boom", issues)
        self.assertIn("update: manual: manual recovery required: permanent boom", issues)

    def test_collect_async_update_section_counts_legacy_failed_pending_as_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "legacy.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "session_id": "legacy",
                        "role": "update",
                        "phase": None,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": "2026-05-29T09:00:00+00:00",
                        "pid": None,
                        "result": None,
                        "error": "old boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "message": "legacy",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertIn("manual recovery: 1 candidate across 1 session", section.detail)
        self.assertIn("update: legacy: manual recovery required: old boom", issues)

    def test_collect_async_update_section_counts_manual_current_batch_as_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "manual-current.json").write_text(
                json.dumps(
                    {
                        "status": "needs_manual_recovery",
                        "session_id": "manual-current",
                        "role": "update",
                        "phase": None,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": "2026-05-29T09:00:00+00:00",
                        "pid": None,
                        "result": None,
                        "error": "old boom",
                        "attempts": 2,
                        "next_retry_at": None,
                        "last_error": "old boom",
                        "next_flush_at": None,
                        "current_batch": [
                            {
                                "id": 1,
                                "message": "manual current",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            }
                        ],
                        "pending": [],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertIn("manual recovery: 1 candidate across 1 session", section.detail)
        self.assertIn("current batch: 0 candidates across 0 sessions", section.detail)
        self.assertIn("update: manual-current: manual recovery required: old boom", issues)

    def test_collect_async_update_section_uses_recent_outcome_for_last_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            base_state = {
                "status": "succeeded",
                "role": "update",
                "phase": None,
                "started_at": "2026-05-29T08:00:00+00:00",
                "pid": None,
                "error": None,
                "next_flush_at": None,
                "current_batch": [],
                "pending": [],
                "next_id": 1,
            }
            (async_root / "agent-a.json").write_text(
                json.dumps(
                    {
                        **base_state,
                        "session_id": "agent-a",
                        "finished_at": "2026-05-29T11:00:00+00:00",
                        "result": "newer result",
                    }
                ),
                encoding="utf-8",
            )
            (async_root / "agent-z.json").write_text(
                json.dumps(
                    {
                        **base_state,
                        "session_id": "agent-z",
                        "finished_at": "2026-05-29T10:00:00+00:00",
                        "result": "older result",
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertEqual(section.last, "newer result")
        self.assertEqual(issues, [])

    def test_collect_async_update_section_surfaces_session_error_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "session_id": "agent-1",
                        "role": "update",
                        "phase": None,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": "2026-05-29T09:00:00+00:00",
                        "pid": None,
                        "result": None,
                        "error": "boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [],
                        "next_id": 1,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertEqual(section.last, "error: boom")
        self.assertIn("update: agent-1: error: boom", issues)

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

    def test_collect_async_update_section_reports_invalid_worker_state_shape_locally(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            worker_root = root / ".runtime" / "async" / "update" / "_worker"
            worker_root.mkdir(parents=True)
            (worker_root / "state.json").write_text("{}", encoding="utf-8")

            section, issues = collect_async_update_section(root)

        self.assertIn("worker: state error", section.state)
        self.assertEqual(len(issues), 1)
        self.assertIn("update worker: state error", issues[0])

    def test_collect_async_update_section_reports_stale_for_non_worker_pid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            worker_root = root / ".runtime" / "async" / "update" / "_worker"
            worker_root.mkdir(parents=True)
            (worker_root / "state.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "pid": 4,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "batch_id": None,
                        "session_ids": [],
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root, process_exists=lambda pid: False)

        self.assertEqual(section.state, "worker: stale pid 4")
        self.assertIn("update worker: stale pid 4", issues)

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

    def test_collect_async_update_section_reports_invalid_state_shape_locally(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text("{}", encoding="utf-8")

            section, issues = collect_async_update_section(root)

        self.assertEqual(section.name, "update")
        self.assertIn("state error", section.state)
        self.assertIn("state: .runtime/async/update", section.detail)
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
                insight_collector=lambda memory_root: SectionStatus(
                    name="insight",
                    state="trigger progress",
                    detail="trigger: 2.0/150.0 points",
                ),
                update_collector=lambda memory_root: (SectionStatus(name="update", state="worker: idle"), []),
            )

        self.assertEqual(dashboard.root, root)
        self.assertEqual(len(dashboard.watches), 1)
        self.assertEqual(dashboard.dreamer.name, "dreamer")
        self.assertEqual(dashboard.insight.name, "insight")
        self.assertEqual(dashboard.update.name, "update")
        self.assertIn("pruner: stale pid 42", dashboard.issues)

    def test_collect_status_surfaces_failed_async_session_in_recent_issues(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "session_id": "agent-1",
                        "role": "update",
                        "phase": None,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": "2026-05-29T09:00:00+00:00",
                        "pid": None,
                        "result": None,
                        "error": "boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [],
                        "next_id": 1,
                    }
                ),
                encoding="utf-8",
            )

            dashboard = collect_status(
                root,
                watch_collector=lambda memory_root: ([], []),
                dreamer_collector=lambda memory_root: SectionStatus(name="dreamer", state="trigger progress"),
                insight_collector=lambda memory_root: SectionStatus(name="insight", state="trigger progress"),
            )
            output = format_status_dashboard(dashboard)

        self.assertIn("Async Update", output)
        self.assertIn("last: error: boom", output)
        self.assertIn("Recent Issues", output)
        self.assertIn("  update: agent-1: error: boom", output)

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
