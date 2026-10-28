import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rightmemory.status as status_module
from rightmemory.semantic_operation import OperationEffect, SemanticOperationStore
from rightmemory.status import (
    DashboardStatus,
    GitStatus,
    SectionStatus,
    SyncStatus,
    collect_curation_section,
    collect_git_status,
    collect_insight_section,
    collect_managed_watch_sections,
    collect_semantic_operation_section,
    collect_status,
    collect_sync_status,
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

    def test_collect_sync_status_reports_enabled_aligned_repository_without_fetching(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            branch = self._init_repo_with_upstream(root)

            with patch("rightmemory.status._run_git", wraps=status_module._run_git) as run_git:
                status = collect_sync_status(
                    root,
                    config_loader=lambda memory_root: type("SyncConfig", (), {"enabled": True})(),
                )

        self.assertTrue(status.enabled)
        self.assertEqual(status.upstream, f"origin/{branch}")
        self.assertEqual((status.ahead, status.behind), (0, 0))
        self.assertEqual(status.issues, ())
        commands = [call.args[1:] for call in run_git.call_args_list]
        self.assertFalse(any(command and command[0] in {"fetch", "pull", "push"} for command in commands))

    def test_collect_sync_status_reports_disabled_configuration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._init_repo_with_upstream(root)

            status = collect_sync_status(
                root,
                config_loader=lambda memory_root: type("SyncConfig", (), {"enabled": False})(),
            )

        self.assertFalse(status.enabled)
        self.assertEqual((status.ahead, status.behind), (0, 0))
        self.assertEqual(status.issues, ())

    def test_collect_sync_status_reports_ahead_behind_and_diverged_repositories(self):
        cases = (
            ("ahead", (1, 0)),
            ("behind", (0, 1)),
            ("diverged", (1, 1)),
        )
        for case, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                branch = self._init_repo_with_upstream(root)
                base = self._git(root, "rev-parse", "HEAD")
                if case in {"behind", "diverged"}:
                    (root / "REMOTE.md").write_text("remote\n", encoding="utf-8")
                    self._git(root, "add", "REMOTE.md")
                    self._git(root, "commit", "-m", "remote state")
                    self._git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
                    self._git(root, "reset", "--hard", base)
                if case in {"ahead", "diverged"}:
                    (root / "LOCAL.md").write_text("local\n", encoding="utf-8")
                    self._git(root, "add", "LOCAL.md")
                    self._git(root, "commit", "-m", "local state")

                status = collect_sync_status(
                    root,
                    config_loader=lambda memory_root: type("SyncConfig", (), {"enabled": True})(),
                )

            self.assertEqual((status.ahead, status.behind), expected)

    def test_collect_sync_status_handles_missing_upstream_missing_ref_and_detached_head(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            branch = self._init_repo(root)
            config_loader = lambda memory_root: type("SyncConfig", (), {"enabled": True})()

            missing_upstream = collect_sync_status(root, config_loader=config_loader)

            self._git(root, "config", f"branch.{branch}.remote", "origin")
            self._git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
            missing_ref = collect_sync_status(root, config_loader=config_loader)

            self._git(root, "checkout", "--detach")
            detached = collect_sync_status(root, config_loader=config_loader)

        self.assertIsNone(missing_upstream.upstream)
        self.assertIsNone(missing_upstream.ahead)
        self.assertTrue(any("upstream" in issue for issue in missing_upstream.issues))
        self.assertEqual(missing_ref.upstream, f"origin/{branch}")
        self.assertIsNone(missing_ref.behind)
        self.assertIn("missing", missing_ref.divergence_error)
        self.assertIsNone(detached.upstream)
        self.assertIn("detached", detached.divergence_error)

    def test_collect_sync_status_reports_missing_successful_failed_and_malformed_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._init_repo_with_upstream(root)
            config_loader = lambda memory_root: type("SyncConfig", (), {"enabled": True})()
            state_path = root / ".runtime" / "sync" / "state.json"

            missing = collect_sync_status(root, config_loader=config_loader)

            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "last_successful_pull_at": "2026-07-24T08:00:00+00:00",
                        "last_status": "synced",
                        "last_message": "local memory is current",
                        "last_files": ["MEMORY.md"],
                    }
                ),
                encoding="utf-8",
            )
            successful = collect_sync_status(root, config_loader=config_loader)

            state_path.write_text(
                json.dumps(
                    {
                        "last_failure_at": "2026-07-24T09:00:00+00:00",
                        "last_status": "offline",
                        "last_message": "sync offline: git fetch failed",
                        "last_files": [],
                    }
                ),
                encoding="utf-8",
            )
            failed = collect_sync_status(root, config_loader=config_loader)

            state_path.write_text("{not json", encoding="utf-8")
            malformed = collect_sync_status(root, config_loader=config_loader)

        self.assertIsNone(missing.last_status)
        self.assertEqual(successful.last_status, "synced")
        self.assertEqual(successful.last_at, "2026-07-24T08:00:00+00:00")
        self.assertEqual(successful.last_message, "local memory is current")
        self.assertEqual(successful.last_files, ("MEMORY.md",))
        self.assertEqual(failed.last_status, "offline")
        self.assertEqual(failed.last_at, "2026-07-24T09:00:00+00:00")
        self.assertIn("fetch failed", failed.last_message)
        self.assertIsNotNone(malformed.last_error)
        self.assertTrue(any("sync state error" in issue for issue in malformed.issues))

    def test_collect_sync_status_surfaces_unreadable_state_without_crashing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._init_repo_with_upstream(root)

            status = collect_sync_status(
                root,
                config_loader=lambda memory_root: type("SyncConfig", (), {"enabled": True})(),
                state_reader=lambda memory_root: (_ for _ in ()).throw(OSError("denied")),
            )

        self.assertIn("OSError: denied", status.last_error)
        self.assertTrue(any("sync state error" in issue for issue in status.issues))

    def test_read_log_preview_prefers_recent_failure_line(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "insight.log"
            log.write_text(
                "[2026-05-29T08:00:00+00:00] rightmemory insight cycle\n"
                "ordinary success message\n"
                "rightmemory insight cycle failed: RuntimeError: boom\n"
                "rightmemory insight watch stopped\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertEqual(preview, "rightmemory insight cycle failed: RuntimeError: boom")

    def test_read_log_preview_ignores_old_failure_before_later_event(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "insight.log"
            log.write_text(
                "[2026-05-29T08:00:00+00:00] rightmemory insight cycle\n"
                "rightmemory insight cycle failed: RuntimeError: old boom\n"
                "[2026-05-29T09:00:00+00:00] rightmemory insight cycle\n"
                "## Insight Cycle Complete\n"
                "Validation: Passed\n"
                "Commit: abc1234\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertEqual(preview, "## Insight Cycle Complete\nValidation: Passed\nCommit: abc1234")

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
            sync=SyncStatus(
                enabled=True,
                upstream="origin/rightmemory-v2",
                ahead=0,
                behind=0,
                last_status="synced",
                last_at="2026-07-24T08:00:00+00:00",
                last_message="local memory is current",
                last_files=("MEMORY.md",),
            ),
            watches=[
                SectionStatus(
                    name="insight",
                    state="running pid 123",
                    log_path=".runtime/watch/insight.log",
                    last="completed one cycle",
                ),
                SectionStatus(
                    name="pruner",
                    state="stopped",
                    log_path=".runtime/watch/pruner.log",
                    last="failed: boom",
                    issue="pruner failed",
                ),
            ],
            insight=SectionStatus(
                name="insight",
                state="trigger progress",
                detail="trigger: 88.0/150.0 points",
            ),
            curation=SectionStatus(
                name="update",
                state="worker: idle",
                detail="pending: 0 candidates across 0 sessions\nstate: .runtime/async/update",
            ),
            issues=["pruner failed"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn(f"RightMemory\n  root: {Path('/memory/root')}\n  git: clean on main @ abc1234", output)
        self.assertIn("Sync\n  enabled: yes", output)
        self.assertIn("upstream: origin/rightmemory-v2", output)
        self.assertIn("divergence: ahead 0, behind 0 (last fetched)", output)
        self.assertIn("last: synced at 2026-07-24T08:00:00+00:00", output)
        self.assertIn("message: local memory is current", output)
        self.assertIn("files: MEMORY.md", output)
        self.assertIn("Managed Watches", output)
        self.assertIn("insight: running pid 123", output)
        self.assertIn("Insight", output)
        self.assertIn("Curation", output)
        self.assertIn("state: .runtime/async/update", output)
        self.assertNotIn("log: .runtime/async/update", output)
        self.assertIn("Recent Issues\n  pruner failed", output)

    def test_format_status_dashboard_marks_missing_logs(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            watches=[
                SectionStatus(
                    name="pruner",
                    state="stopped",
                    log_path=".runtime/watch/pruner.log",
                    log_missing=True,
                ),
            ],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("log: .runtime/watch/pruner.log (missing)", output)

    def test_format_status_dashboard_renders_recovery_hints_for_known_issue_shapes(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="dirty: 1 path", issue="dirty worktree: 1 path"),
            issues=[
                "curation: provider error",
                "git unavailable: not a git repository",
                "pruner: stale pid 456",
                "insight: running outside manager",
                "pruner: status error: ValueError: bad state",
                "pruner: rightmemory pruner check failed: RuntimeError: boom",
                "sync config error: ValueError: bad sync config",
                "insight trigger error: ValueError: insight trigger points must be a number",
                "managed watches: status error: RuntimeError: collector failed",
                "insight: status error: RuntimeError: collector failed",
            ],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recovery", output)
        expected_hints = [
            "curation: inspect `rightmemory curate status`; resume the indicated task or wake the configured worker",
            "git: inspect with `git status --short`; resolve local changes before automatic writes continue",
            "git: inspect the configured memory root and repair Git before retrying",
            "pruner: run `rightmemory watch restart pruner`",
            "insight: stop the foreground process directly, then run `rightmemory watch start insight`",
            "pruner: rerun `rightmemory status`; inspect watch state if it persists",
            "pruner: inspect the shown log path, then run `rightmemory watch restart pruner` when appropriate",
            "sync: fix `rightmemory.toml`, then rerun `rightmemory status`",
            "insight: inspect `.runtime/insight/trigger-state.json`",
            "managed watches: rerun `rightmemory status`; inspect watch state if it persists",
            "insight: rerun `rightmemory status`; inspect insight state if it persists",
        ]
        for hint in expected_hints:
            self.assertIn(f"  {hint}", output)

    def test_format_status_dashboard_omits_recovery_when_no_hints_exist(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            issues=["plain informational issue without known recovery"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("Recent Issues", output)
        self.assertNotIn("Recovery", output)

    def test_collect_managed_watches_keeps_sync_watcher_state_and_log_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "insight.log"
            log.parent.mkdir(parents=True)
            log.write_text("completed one cycle\n", encoding="utf-8")
            statuses = {
                name: type("WatchStatus", (), {
                    "name": name,
                    "state": "stale" if name == "pruner" else "running" if name == "insight" else "stopped",
                    "pid": 456 if name == "pruner" else 123 if name == "insight" else None,
                    "log_path": root / ".runtime" / "watch" / f"{name}.log",
                })()
                for name in ("pruner", "insight", "sync")
            }

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
            )

        self.assertEqual([watch.name for watch in watches], ["pruner", "insight", "sync"])
        insight = next(watch for watch in watches if watch.name == "insight")
        self.assertEqual(insight.state, "running pid 123")
        self.assertEqual(insight.last, "completed one cycle")
        self.assertEqual(next(watch.state for watch in watches if watch.name == "pruner"), "stale pid 456")
        self.assertEqual(next(watch.state for watch in watches if watch.name == "sync"), "stopped")
        self.assertIn("pruner: stale pid 456", issues)

    def test_collect_managed_watches_surfaces_failure_preview_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "pruner.log"
            log.parent.mkdir(parents=True)
            log.write_text("rightmemory pruner check failed: RuntimeError: boom\n", encoding="utf-8")

            statuses = {}
            for name in ("pruner", "insight", "sync"):
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
            )

        pruner = next(watch for watch in watches if watch.name == "pruner")
        insight = next(watch for watch in watches if watch.name == "insight")
        self.assertEqual(pruner.last, "rightmemory pruner check failed: RuntimeError: boom")
        self.assertIn("pruner: rightmemory pruner check failed: RuntimeError: boom", issues)
        self.assertTrue(insight.log_missing)

    def test_collect_managed_watches_does_not_surface_zero_failed_counter_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "pruner.log"
            log.parent.mkdir(parents=True)
            log.write_text(
                "[2026-05-29T15:00:00+00:00] rightmemory pruner scan\n"
                "processed: 0\n"
                "retried: 0\n"
                "failed: 0\n",
                encoding="utf-8",
            )
            statuses = {}
            for name in ("pruner", "insight", "sync"):
                statuses[name] = type(
                    "WatchStatus",
                    (),
                    {
                        "name": name,
                        "state": "running" if name == "pruner" else "stopped",
                        "pid": 123 if name == "pruner" else None,
                        "log_path": root / ".runtime" / "watch" / f"{name}.log",
                    },
                )()
            statuses["pruner"].log_path = log

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
            )

        pruner = next(watch for watch in watches if watch.name == "pruner")
        self.assertIn("failed: 0", pruner.last)
        self.assertNotIn("pruner: failed: 0", issues)

    def test_collect_managed_watches_does_not_surface_graceful_stop_notice_as_issue(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "insight.log"
            log.parent.mkdir(parents=True)
            log.write_text("rightmemory insight watch stopping after current work\n", encoding="utf-8")
            statuses = {}
            for name in ("pruner", "insight", "sync"):
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
            statuses["insight"].log_path = log

            watches, issues = collect_managed_watch_sections(
                root,
                watch_status_reader=lambda memory_root, name: statuses[name],
            )

        insight = next(watch for watch in watches if watch.name == "insight")
        self.assertEqual(insight.last, "rightmemory insight watch stopping after current work")
        self.assertEqual(issues, [])

    def test_collect_managed_watches_ignores_retired_cleanup_records(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_dir = root / ".runtime" / "watch"
            watch_dir.mkdir(parents=True)
            pid_path = watch_dir / "agent-cli-cleanup.pid"
            pid_path.write_text("30944\n", encoding="utf-8")
            (watch_dir / "agent-cli-cleanup.log").write_text(
                "rightmemory agent-cli cleanup failed: old failure\n", encoding="utf-8"
            )
            watches, issues = collect_managed_watch_sections(root)
            self.assertNotIn("agent-cli-cleanup", [watch.name for watch in watches])
            self.assertEqual(issues, [])
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "30944\n")
            self.assertIsNone(status_module._watch_recovery_hint("agent-cli-cleanup: stale pid 30944"))

    def test_collect_managed_watches_default_reader_does_not_create_lock_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watch_dir = root / ".runtime" / "watch"
            watch_dir.mkdir(parents=True)

            watches, issues = collect_managed_watch_sections(
                root,
            )

            self.assertEqual(
                [watch.name for watch in watches],
                ["pruner", "insight", "sync"],
            )
            self.assertEqual(issues, [])
            self.assertFalse((watch_dir / "pruner.lock").exists())
            self.assertFalse((watch_dir / "insight.lock").exists())
            self.assertFalse((watch_dir / "agent-cli-cleanup.lock").exists())

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
                sync_collector=lambda memory_root: SyncStatus(
                    enabled=True,
                    upstream="origin/main",
                    ahead=0,
                    behind=0,
                ),
                watch_collector=lambda memory_root: (
                    [SectionStatus(name="pruner", state="stale pid 42", issue="pruner: stale pid 42")],
                    ["pruner: stale pid 42"],
                ),
                insight_collector=lambda memory_root: SectionStatus(
                    name="insight",
                    state="trigger progress",
                    detail="trigger: 2.0/150.0 points",
                ),
            )

        self.assertEqual(dashboard.root, root)
        self.assertEqual(dashboard.sync.upstream, "origin/main")
        self.assertEqual(len(dashboard.watches), 1)
        self.assertEqual(dashboard.insight.name, "insight")
        self.assertEqual(dashboard.curation.name, "curation")
        self.assertIn("pruner: stale pid 42", dashboard.issues)


    def test_collect_status_does_not_create_runtime_state_or_run_network_git_commands(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._init_repo_with_upstream(root)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            with patch("rightmemory.status._run_git", wraps=status_module._run_git) as run_git:
                collect_status(
                    root,
                    sync_collector=None,
                    watch_collector=lambda memory_root: ([], []),
                    insight_collector=lambda memory_root: None,
                    curation_collector=lambda memory_root: (
                        [],
                    ),
                    operation_collector=lambda memory_root: (None, []),
                )

            after = sorted(path.relative_to(root) for path in root.rglob("*"))

        self.assertEqual(after, before)
        commands = [call.args[1:] for call in run_git.call_args_list]
        self.assertFalse(any(command and command[0] in {"fetch", "pull", "push"} for command in commands))

    def test_status_git_commands_disable_optional_locks_and_prompts(self):
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")
        with patch("rightmemory.status.subprocess.run", return_value=completed) as run:
            status_module._run_git(Path("/memory/root"), "status", "--short")

        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "true")


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

    def _init_repo(self, root: Path) -> str:
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        self._git(root, "add", "MEMORY.md")
        self._git(root, "commit", "-m", "initial memory")
        return self._git(root, "branch", "--show-current")

    def _init_repo_with_upstream(self, root: Path) -> str:
        branch = self._init_repo(root)
        self._git(root, "remote", "add", "origin", "https://example.invalid/rightmemory.git")
        self._git(root, "config", f"branch.{branch}.remote", "origin")
        self._git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
        self._git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        return branch


if __name__ == "__main__":
    unittest.main()
