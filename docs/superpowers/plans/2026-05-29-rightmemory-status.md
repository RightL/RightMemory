# RightMemory Status Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `rightmemory status` dashboard that summarizes repo state, managed watches, dreamer trigger progress, async update queues, recent previews, and issue hints in one command.

**Architecture:** Create `rightmemory/status.py` as a small aggregation and formatting module with section-local collectors. Keep `rightmemory/cli.py` as a thin dispatcher for the top-level command, and keep existing `watch status` and `update pull` behavior unchanged.

**Tech Stack:** Python standard library, dataclasses, JSON file reads, bounded Git subprocess calls, existing RightMemory watch/config/dreamer-trigger helpers, `unittest`.

---

## Scope Check

This plan implements one subsystem: a read-only operational dashboard. It does not add a daily report, full-message dumping command, JSON output, update watch process, repair behavior, or new agent turns. The plan is intentionally split into collectors and formatter first, then subsystem integration, then CLI wiring and docs.

## File Structure

- Create `rightmemory/status.py`: dataclasses, Git collector, log preview helper, managed watch collector, dreamer trigger collector, async update collector, issue aggregation, and text formatter.
- Create `tests/test_status.py`: unit coverage for collectors and formatted output without invoking real agents or long-running watches.
- Modify `rightmemory/cli.py`: route `rightmemory status` before role parsing and print `format_status_dashboard(collect_status(MEMORY_ROOT))`.
- Modify `tests/test_cli.py`: CLI coverage for the new top-level command and proof that `watch status` remains process-manager-only.
- Modify `README.md`: document `rightmemory status` as the daily operational dashboard and clarify how it differs from `watch status`.

## Task $1$: Add Status Core, Git State, Log Preview, And Formatter

**Files:**
- Create: `rightmemory/status.py`
- Create: `tests/test_status.py`

- [ ] **Step $1$: Write failing tests for Git state, log previews, and basic formatting**

Create `tests/test_status.py` with the initial tests:

```python
import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.status import (
    DashboardStatus,
    GitStatus,
    SectionStatus,
    collect_git_status,
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
                SectionStatus(name="review", state="running pid 123", log_path=".runtime/watch/review.log", last="reviewed 3 sessions"),
                SectionStatus(name="pruner", state="stopped", log_path=".runtime/watch/pruner.log", last="failed: boom", issue="pruner failed"),
            ],
            dreamer=SectionStatus(name="dreamer", state="running pid 456", log_path=".runtime/watch/dreamer.log", detail="trigger: 12.5/50.0 points"),
            update=SectionStatus(name="update", state="worker: idle", log_path=".runtime/async/update/", detail="pending: 0 candidates across 0 sessions"),
            issues=["pruner failed"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("RightMemory\n  root: /memory/root\n  git: clean on main @ abc1234", output)
        self.assertIn("Managed Watches", output)
        self.assertIn("review: running pid 123", output)
        self.assertIn("Async Update", output)
        self.assertIn("Recent Issues\n  pruner failed", output)

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
```

- [ ] **Step $2$: Run the new tests and verify they fail**

Run:

```bash
rtk python -m unittest tests.test_status
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.status'`.

- [ ] **Step $3$: Implement the first status module slice**

Create `rightmemory/status.py`:

```python
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


MAX_PREVIEW_CHARS = 300
MAX_PREVIEW_LINES = 3
FAILURE_MARKERS = ("failed", "error", "stopping after")


@dataclass(frozen=True)
class GitStatus:
    summary: str
    issue: str | None = None


@dataclass(frozen=True)
class SectionStatus:
    name: str
    state: str
    log_path: str | None = None
    detail: str | None = None
    last: str | None = None
    issue: str | None = None


@dataclass(frozen=True)
class DashboardStatus:
    root: Path
    git: GitStatus
    watches: list[SectionStatus] = field(default_factory=list)
    dreamer: SectionStatus | None = None
    update: SectionStatus | None = None
    issues: list[str] = field(default_factory=list)


def collect_git_status(memory_root: Path) -> GitStatus:
    root = Path(memory_root)
    inside = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        detail = _command_detail(inside) or "not a git repository"
        return GitStatus(summary=f"unavailable: {detail}", issue=f"git unavailable: {detail}")

    branch = _run_git(root, "branch", "--show-current")
    head = _run_git(root, "rev-parse", "--short", "HEAD")
    status = _run_git(root, "status", "--short")
    for result in (branch, head, status):
        if result.returncode != 0:
            detail = _command_detail(result) or "git command failed"
            return GitStatus(summary=f"unavailable: {detail}", issue=f"git unavailable: {detail}")

    branch_name = branch.stdout.strip() or "detached"
    head_name = head.stdout.strip()
    dirty_paths = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty_paths:
        count = len(dirty_paths)
        noun = "path" if count == 1 else "paths"
        return GitStatus(
            summary=f"dirty: {count} {noun} on {branch_name} @ {head_name}",
            issue=f"dirty worktree: {count} {noun}",
        )
    return GitStatus(summary=f"clean on {branch_name} @ {head_name}")


def read_log_preview(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"error reading log: {type(exc).__name__}: {exc}"

    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return None
    for line in reversed(meaningful):
        lower = line.lower()
        if any(marker in lower for marker in FAILURE_MARKERS):
            return _cap_preview(line)
    return _cap_preview("\n".join(meaningful[-MAX_PREVIEW_LINES:]))


def format_status_dashboard(status: DashboardStatus) -> str:
    lines: list[str] = [
        "RightMemory",
        f"  root: {status.root}",
        f"  git: {status.git.summary}",
        "",
        "Managed Watches",
    ]
    if status.watches:
        for watch in status.watches:
            lines.extend(_format_section(watch))
    else:
        lines.append("  (none)")

    if status.dreamer is not None:
        lines.append("")
        lines.append("Dreamer")
        lines.extend(_format_section(status.dreamer))

    if status.update is not None:
        lines.append("")
        lines.append("Async Update")
        lines.extend(_format_section(status.update))

    issues = list(status.issues)
    if status.git.issue:
        issues.insert(0, status.git.issue)
    if issues:
        lines.append("")
        lines.append("Recent Issues")
        lines.extend(f"  {issue}" for issue in issues)
    return "\n".join(lines)


def _format_section(section: SectionStatus) -> list[str]:
    lines = [f"  {section.name}: {section.state}"]
    if section.log_path:
        lines.append(f"    log: {section.log_path}")
    if section.detail:
        lines.append(f"    {section.detail}")
    if section.last:
        for index, line in enumerate(section.last.splitlines()):
            prefix = "last: " if index == 0 else "      "
            lines.append(f"    {prefix}{line}")
    return lines


def _cap_preview(text: str) -> str:
    lines = text.splitlines()[:MAX_PREVIEW_LINES]
    preview = "\n".join(lines)
    if len(preview) > MAX_PREVIEW_CHARS:
        return preview[:MAX_PREVIEW_CHARS]
    return preview


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip()).splitlines()[0] if (result.stderr.strip() or result.stdout.strip()) else ""
```

- [ ] **Step $4$: Run the new status tests and verify they pass**

Run:

```bash
rtk python -m unittest tests.test_status
```

Expected: PASS.

- [ ] **Step $5$: Commit Task $1$**

Run:

```bash
rtk git add rightmemory/status.py tests/test_status.py
rtk git commit -m "feat: add status dashboard core"
```

## Task $2$: Add Managed Watch And Dreamer Trigger Collection

**Files:**
- Modify: `rightmemory/status.py`
- Modify: `tests/test_status.py`

- [ ] **Step $1$: Write failing tests for watches, sync disabled, and dreamer progress**

Append these tests to `StatusDashboardTests` in `tests/test_status.py`:

```python
    def test_collect_managed_watches_includes_sync_disabled_and_log_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log = root / ".runtime" / "watch" / "review.log"
            log.parent.mkdir(parents=True)
            log.write_text("reviewed 3 sessions\n", encoding="utf-8")

            statuses = {
                "review": type("WatchStatus", (), {"name": "review", "state": "running", "pid": 123, "log_path": log})(),
                "dreamer": type("WatchStatus", (), {"name": "dreamer", "state": "stopped", "pid": None, "log_path": root / ".runtime" / "watch" / "dreamer.log"})(),
                "pruner": type("WatchStatus", (), {"name": "pruner", "state": "stale", "pid": 456, "log_path": root / ".runtime" / "watch" / "pruner.log"})(),
                "sync": type("WatchStatus", (), {"name": "sync", "state": "stopped", "pid": None, "log_path": root / ".runtime" / "watch" / "sync.log"})(),
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
```

Add these imports near the existing `rightmemory.status` import:

```python
    collect_dreamer_section,
    collect_managed_watch_sections,
```

- [ ] **Step $2$: Run the focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_status.StatusDashboardTests.test_collect_managed_watches_includes_sync_disabled_and_log_preview tests.test_status.StatusDashboardTests.test_collect_dreamer_section_reports_trigger_progress
```

Expected: FAIL because the collector functions do not exist.

- [ ] **Step $3$: Implement managed watch and dreamer collectors**

Modify imports in `rightmemory/status.py`:

```python
from collections.abc import Callable

from .config import load_dreamer_watch_config, load_sync_config
from .dreamer_trigger import DreamerTriggerStore
from .watch import MANAGED_WATCH_TARGETS, managed_watch_status
```

Add these functions after `read_log_preview()`:

```python
def collect_managed_watch_sections(
    memory_root: Path,
    *,
    watch_status_reader: Callable[[Path, str], object] = managed_watch_status,
    sync_config_loader: Callable[[], object] = load_sync_config,
) -> tuple[list[SectionStatus], list[str]]:
    sections: list[SectionStatus] = []
    issues: list[str] = []
    sync_disabled = False
    try:
        sync_config = sync_config_loader()
        sync_disabled = not bool(getattr(sync_config, "enabled", False))
    except Exception as exc:
        sync_error = f"sync config error: {type(exc).__name__}: {exc}"
        issues.append(sync_error)
        sync_disabled = False

    for name in MANAGED_WATCH_TARGETS:
        try:
            status = watch_status_reader(memory_root, name)
            log_path = Path(getattr(status, "log_path"))
            state = str(getattr(status, "state"))
            pid = getattr(status, "pid", None)
            if name == "sync" and sync_disabled:
                section_state = "disabled"
            elif state == "running" and pid is not None:
                section_state = f"running pid {pid}"
            elif state == "stale" and pid is not None:
                section_state = f"stale pid {pid}"
                issues.append(f"{name}: stale pid {pid}")
            elif state == "external":
                section_state = "running outside manager"
                issues.append(f"{name}: running outside manager")
            else:
                section_state = state
            last = None if name == "sync" and sync_disabled else read_log_preview(log_path)
            sections.append(
                SectionStatus(
                    name=name,
                    state=section_state,
                    log_path=_display_path(memory_root, log_path),
                    last=last,
                    issue=issues[-1] if issues and issues[-1].startswith(f"{name}:") else None,
                )
            )
        except Exception as exc:
            message = f"{name}: status error: {type(exc).__name__}: {exc}"
            sections.append(SectionStatus(name=name, state=message, issue=message))
            issues.append(message)
    return sections, issues


def collect_dreamer_section(
    memory_root: Path,
    *,
    trigger_reader: Callable[[Path], object] | None = None,
    config_loader: Callable[[], object] = load_dreamer_watch_config,
) -> SectionStatus:
    if trigger_reader is None:
        trigger_reader = lambda root: DreamerTriggerStore(root).read()
    try:
        state = trigger_reader(memory_root)
        config = config_loader()
        detail = (
            f"trigger: {getattr(state, 'points')}/{getattr(config, 'trigger_points')} points\n"
            f"check interval: {getattr(config, 'check_interval_seconds')} seconds"
        )
        updated_at = getattr(state, "updated_at", None)
        if updated_at:
            detail += f"\nupdated: {updated_at}"
        return SectionStatus(name="dreamer", state="trigger progress", detail=detail)
    except Exception as exc:
        return SectionStatus(
            name="dreamer",
            state=f"error: {type(exc).__name__}: {exc}",
            issue=f"dreamer trigger error: {type(exc).__name__}: {exc}",
        )
```

Add the path formatter helper near the private helpers:

```python
def _display_path(memory_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(memory_root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)
```

- [ ] **Step $4$: Run the status tests and verify pass**

Run:

```bash
rtk python -m unittest tests.test_status
```

Expected: PASS.

- [ ] **Step $5$: Commit Task $2$**

Run:

```bash
rtk git add rightmemory/status.py tests/test_status.py
rtk git commit -m "feat: collect watch and dreamer status"
```

## Task $3$: Add Async Update Worker And Queue Collection

**Files:**
- Modify: `rightmemory/status.py`
- Modify: `tests/test_status.py`

- [ ] **Step $1$: Write failing async update status tests**

Append these tests to `StatusDashboardTests`:

```python
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
                            {"id": 1, "message": "first", "submitted_at": "2026-05-29T08:00:00+00:00"},
                            {"id": 2, "message": "second", "submitted_at": "2026-05-29T08:05:00+00:00"},
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
                            {"id": 1, "message": "running", "submitted_at": "2026-05-29T08:30:00+00:00"}
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
```

Add imports:

```python
import json

from rightmemory.status import collect_async_update_section
```

- [ ] **Step $2$: Run async status tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_status.StatusDashboardTests.test_collect_async_update_section_reports_idle_when_state_missing tests.test_status.StatusDashboardTests.test_collect_async_update_section_counts_pending_and_current_batches tests.test_status.StatusDashboardTests.test_collect_async_update_section_reports_running_worker tests.test_status.StatusDashboardTests.test_collect_async_update_section_reports_malformed_state_locally
```

Expected: FAIL because `collect_async_update_section` does not exist.

- [ ] **Step $3$: Implement async update collector**

Modify imports in `rightmemory/status.py`:

```python
import json
from typing import Any

from .async_update import _process_exists
```

Add the collector after `collect_dreamer_section()`:

```python
def collect_async_update_section(
    memory_root: Path,
    *,
    process_exists: Callable[[int], bool] = _process_exists,
) -> tuple[SectionStatus, list[str]]:
    async_root = Path(memory_root) / ".runtime" / "async" / "update"
    worker_state, worker_issue = _read_worker_state(async_root, process_exists)
    try:
        session_states = [_read_json(path) for path in sorted(async_root.glob("*.json")) if path.is_file()]
    except Exception as exc:
        issue = f"update: state error: {type(exc).__name__}: {exc}"
        return (
            SectionStatus(
                name="update",
                state=f"state error: {type(exc).__name__}: {exc}",
                log_path=_display_path(Path(memory_root), async_root),
                issue=issue,
            ),
            [issue],
        )

    pending_candidates = 0
    pending_sessions = 0
    current_candidates = 0
    current_sessions = 0
    flush_times: list[str] = []
    last_values: list[str] = []

    for state in session_states:
        pending = _list_field(state, "pending")
        current = _list_field(state, "current_batch")
        if pending:
            pending_candidates += len(pending)
            pending_sessions += 1
        if current:
            current_candidates += len(current)
            current_sessions += 1
        next_flush_at = state.get("next_flush_at")
        if isinstance(next_flush_at, str) and next_flush_at:
            flush_times.append(next_flush_at)
        result = state.get("result")
        error = state.get("error")
        if isinstance(error, str) and error:
            last_values.append(f"error: {error}")
        elif isinstance(result, str) and result:
            last_values.append(result)

    detail_lines = [
        f"pending: {pending_candidates} {_plural('candidate', pending_candidates)} across {pending_sessions} {_plural('session', pending_sessions)}",
        f"current batch: {current_candidates} {_plural('candidate', current_candidates)} across {current_sessions} {_plural('session', current_sessions)}",
        f"state: {_display_path(Path(memory_root), async_root)}",
    ]
    if flush_times:
        detail_lines.insert(1, f"next flush: {min(flush_times)}")
    if worker_state.detail:
        detail_lines.insert(0, worker_state.detail)
    issues = [worker_issue] if worker_issue else []
    return (
        SectionStatus(
            name="update",
            state=worker_state.state,
            log_path=_display_path(Path(memory_root), async_root),
            detail="\n".join(detail_lines),
            last=_cap_preview(last_values[-1]) if last_values else None,
            issue=worker_issue,
        ),
        issues,
    )
```

Add private helpers near the bottom:

```python
@dataclass(frozen=True)
class _WorkerSummary:
    state: str
    detail: str | None = None


def _read_worker_state(async_root: Path, process_exists: Callable[[int], bool]) -> tuple[_WorkerSummary, str | None]:
    path = async_root / "_worker" / "state.json"
    if not path.exists():
        return _WorkerSummary(state="worker: idle"), None
    try:
        data = _read_json(path)
    except Exception as exc:
        issue = f"update worker: state error: {type(exc).__name__}: {exc}"
        return _WorkerSummary(state=f"worker: state error: {type(exc).__name__}: {exc}"), issue
    pid = data.get("pid")
    status = data.get("status")
    if not isinstance(pid, int):
        return _WorkerSummary(state="worker: idle"), None
    if not process_exists(pid):
        issue = f"update worker: stale pid {pid}"
        return _WorkerSummary(state=f"worker: stale pid {pid}"), issue
    detail_parts = []
    batch_id = data.get("batch_id")
    if isinstance(batch_id, str) and batch_id:
        detail_parts.append(f"batch: {batch_id}")
    session_ids = data.get("session_ids")
    if isinstance(session_ids, list):
        visible = ", ".join(item for item in session_ids if isinstance(item, str))
        if visible:
            detail_parts.append(f"sessions: {visible}")
    state = f"worker: {status} pid {pid}" if isinstance(status, str) and status else f"worker: running pid {pid}"
    return _WorkerSummary(state=state, detail="\n".join(detail_parts) if detail_parts else None), None


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected in {path}")
    return data


def _list_field(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"
```

- [ ] **Step $4$: Run all status tests and verify pass**

Run:

```bash
rtk python -m unittest tests.test_status
```

Expected: PASS.

- [ ] **Step $5$: Commit Task $3$**

Run:

```bash
rtk git add rightmemory/status.py tests/test_status.py
rtk git commit -m "feat: collect async update status"
```

## Task $4$: Add Full Dashboard Collection And CLI Command

**Files:**
- Modify: `rightmemory/status.py`
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_status.py`
- Modify: `tests/test_cli.py`

- [ ] **Step $1$: Write failing dashboard aggregation test**

Append this test to `StatusDashboardTests`:

```python
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
                watch_collector=lambda memory_root: ([SectionStatus(name="pruner", state="stale pid 42", issue="pruner: stale pid 42")], ["pruner: stale pid 42"]),
                dreamer_collector=lambda memory_root: SectionStatus(name="dreamer", state="trigger progress", detail="trigger: 1.0/50.0 points"),
                update_collector=lambda memory_root: (SectionStatus(name="update", state="worker: idle"), []),
            )

        self.assertEqual(dashboard.root, root)
        self.assertEqual(len(dashboard.watches), 1)
        self.assertEqual(dashboard.dreamer.name, "dreamer")
        self.assertEqual(dashboard.update.name, "update")
        self.assertIn("pruner: stale pid 42", dashboard.issues)
```

Add import:

```python
    collect_status,
```

- [ ] **Step $2$: Write failing CLI tests**

Add these tests near the other watch manager tests in `tests/test_cli.py`:

```python
    def test_main_status_prints_operational_dashboard(self):
        stdout = io.StringIO()
        dashboard = "RightMemory\n  root: /memory/root\n  git: clean on main @ abc1234"

        with (
            patch("rightmemory.cli.MEMORY_ROOT", Path("/memory/root")),
            patch("rightmemory.cli.collect_status", return_value=object()) as collect_status,
            patch("rightmemory.cli.format_status_dashboard", return_value=dashboard),
            patch("sys.stdout", stdout),
        ):
            result = main(["status"])

        self.assertEqual(result, 0)
        collect_status.assert_called_once_with(Path("/memory/root"))
        self.assertEqual(stdout.getvalue().strip(), dashboard)

    def test_watch_status_remains_managed_watch_process_view(self):
        stdout = io.StringIO()
        status = type("WatchStatus", (), {"name": "review", "state": "running", "pid": 123, "log_path": Path("/memory/.runtime/watch/review.log")})()

        with (
            patch("rightmemory.cli.MEMORY_ROOT", Path("/memory")),
            patch("rightmemory.cli.managed_watch_status", return_value=status),
            patch("sys.stdout", stdout),
        ):
            result = main(["watch", "status", "review"])

        self.assertEqual(result, 0)
        self.assertIn("review: running pid 123, log /memory/.runtime/watch/review.log", stdout.getvalue())
        self.assertNotIn("Async Update", stdout.getvalue())
```

- [ ] **Step $3$: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_status.StatusDashboardTests.test_collect_status_aggregates_sections_and_issues tests.test_cli.JsonRequestTests.test_main_status_prints_operational_dashboard tests.test_cli.JsonRequestTests.test_watch_status_remains_managed_watch_process_view
```

Expected: FAIL because `collect_status` is missing from `rightmemory.status` and the CLI does not route `status`.

- [ ] **Step $4$: Implement dashboard aggregation**

Modify imports in `rightmemory/status.py`:

```python
from collections.abc import Callable
```

Add the public aggregator near the collector functions:

```python
def collect_status(
    memory_root: Path,
    *,
    watch_collector: Callable[[Path], tuple[list[SectionStatus], list[str]]] | None = None,
    dreamer_collector: Callable[[Path], SectionStatus] = collect_dreamer_section,
    update_collector: Callable[[Path], tuple[SectionStatus, list[str]]] = collect_async_update_section,
) -> DashboardStatus:
    root = Path(memory_root)
    git = collect_git_status(root)
    if watch_collector is None:
        watch_collector = collect_managed_watch_sections

    issues: list[str] = []
    try:
        watches, watch_issues = watch_collector(root)
        issues.extend(watch_issues)
    except Exception as exc:
        message = f"managed watches: status error: {type(exc).__name__}: {exc}"
        watches = [SectionStatus(name="watches", state=message, issue=message)]
        issues.append(message)

    try:
        dreamer = dreamer_collector(root)
        if dreamer.issue:
            issues.append(dreamer.issue)
    except Exception as exc:
        message = f"dreamer: status error: {type(exc).__name__}: {exc}"
        dreamer = SectionStatus(name="dreamer", state=message, issue=message)
        issues.append(message)

    try:
        update, update_issues = update_collector(root)
        issues.extend(update_issues)
    except Exception as exc:
        message = f"update: status error: {type(exc).__name__}: {exc}"
        update = SectionStatus(name="update", state=message, issue=message)
        issues.append(message)

    return DashboardStatus(root=root, git=git, watches=watches, dreamer=dreamer, update=update, issues=issues)
```

- [ ] **Step $5$: Wire the CLI command**

Modify imports in `rightmemory/cli.py`:

```python
from .status import collect_status, format_status_dashboard
```

In `main()`, add the top-level route before the role parser is built:

```python
    if argv and argv[0] == "status":
        return _status_main(argv[1:])
```

Place it with the other top-level command checks, after `doctor` or before `prune`.

Add this helper near the other top-level command helpers:

```python
def _status_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory status")
    parser.parse_args(argv)
    print(format_status_dashboard(collect_status(MEMORY_ROOT)))
    return 0
```

This keeps `rightmemory status --help` useful while still rejecting unsupported detail flags in the first version.

- [ ] **Step $6$: Run focused tests and verify pass**

Run:

```bash
rtk python -m unittest tests.test_status tests.test_cli.JsonRequestTests.test_main_status_prints_operational_dashboard tests.test_cli.JsonRequestTests.test_watch_status_remains_managed_watch_process_view
```

Expected: PASS.

- [ ] **Step $7$: Commit Task $4$**

Run:

```bash
rtk git add rightmemory/status.py rightmemory/cli.py tests/test_status.py tests/test_cli.py
rtk git commit -m "feat: add rightmemory status command"
```

## Task $5$: Document The Dashboard And Verify The Full Suite

**Files:**
- Modify: `README.md`
- Test: full test suite and compile check

- [ ] **Step $1$: Update README command examples**

In `README.md`, add `rightmemory status` near the existing operational commands list. Use this wording:

```markdown
Use `rightmemory status` for a read-only operational dashboard across the configured memory root. It summarizes Git state, managed watches, dreamer trigger progress, async update queues, bounded last-message previews, and file paths for full logs or state. Use `rightmemory watch status` only when you need the lower-level managed-watch process view.
```

In the async update section, add:

```markdown
`rightmemory status` includes aggregate async update worker and queue state without requiring a session id. For one session's detailed pending, running, result, or error state, continue to use `rightmemory update pull --session <id>`.
```

- [ ] **Step $2$: Run focused documentation-adjacent tests**

Run:

```bash
rtk python -m unittest tests.test_status tests.test_cli
```

Expected: PASS.

- [ ] **Step $3$: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: exit code $0$ and no output.

- [ ] **Step $4$: Run full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS for the full suite.

- [ ] **Step $5$: Manually inspect dashboard output in a temporary memory root**

Run:

```bash
rtk python -m rightmemory.cli status
```

Expected: grouped output beginning with `RightMemory`, followed by `Managed Watches`, `Dreamer`, `Async Update`, and optional `Recent Issues`. If the developer's default memory root is absent, this command may report section-local errors; it must not start any watch, worker, or agent.

- [ ] **Step $6$: Commit Task $5$**

Run:

```bash
rtk git add README.md
rtk git commit -m "docs: document rightmemory status"
```

## Final Verification

- [ ] **Step $1$: Confirm branch status**

Run:

```bash
rtk git status --short
```

Expected: no output or `ok` from `rtk`.

- [ ] **Step $2$: Confirm recent commits**

Run:

```bash
rtk git log --oneline -5
```

Expected: includes:

```text
docs: document rightmemory status
feat: add rightmemory status command
feat: collect async update status
feat: collect watch and dreamer status
feat: add status dashboard core
```

- [ ] **Step $3$: Summarize behavior for the user**

Report that `rightmemory status` is read-only, summarizes all operational state in one command, leaves `watch status` unchanged, and links to full log/state files instead of dumping long messages.
