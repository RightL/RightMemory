from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .async_update import _process_exists
from .config import load_dreamer_watch_config, load_sync_config
from .dreamer_trigger import DreamerTriggerStore
from .watch import MANAGED_WATCH_TARGETS, managed_watch_status


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
        (
            f"pending: {pending_candidates} {_plural('candidate', pending_candidates)} "
            f"across {pending_sessions} {_plural('session', pending_sessions)}"
        ),
        (
            f"current batch: {current_candidates} {_plural('candidate', current_candidates)} "
            f"across {current_sessions} {_plural('session', current_sessions)}"
        ),
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


def _display_path(memory_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(memory_root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)


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
    output = result.stderr.strip() or result.stdout.strip()
    return output.splitlines()[0] if output else ""
