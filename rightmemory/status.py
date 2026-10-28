from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_insight_watch_config, load_sync_config
from .platform import lock_file_nonblocking, process_identity, unlock_file
from .semantic_operation import FINAL_PHASES, SemanticOperationStore
from .sync import read_sync_state
from .watch import MANAGED_WATCH_TARGETS, ManagedWatchStatus, _is_managed_watch_process, watch_log_path, watch_pid_path


MAX_PREVIEW_CHARS = 300
MAX_PREVIEW_LINES = 3
MAX_LOG_TAIL_BYTES = 32 * 1024
FAILURE_MARKERS = ("failed", "error")
LOG_EVENT_PATTERN = re.compile(r"^\[\d{4}-\d{2}-\d{2}T")
NEUTRAL_FAILURE_COUNTER_PATTERN = re.compile(r"^(failed|errors?)\s*[:=]\s*0\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class GitStatus:
    summary: str
    issue: str | None = None


@dataclass(frozen=True)
class SyncStatus:
    enabled: bool | None
    upstream: str | None
    ahead: int | None
    behind: int | None
    divergence_error: str | None = None
    last_status: str | None = None
    last_at: str | None = None
    last_message: str | None = None
    last_files: tuple[str, ...] = ()
    last_error: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionStatus:
    name: str
    state: str
    log_path: str | None = None
    log_missing: bool = False
    detail: str | None = None
    last: str | None = None
    issue: str | None = None


@dataclass(frozen=True)
class DashboardStatus:
    root: Path
    git: GitStatus
    sync: SyncStatus | None = None
    watches: list[SectionStatus] = field(default_factory=list)
    insight: SectionStatus | None = None
    curation: SectionStatus | None = None
    operations: SectionStatus | None = None
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _InsightTriggerSnapshot:
    points: float = 0.0
    updated_at: str | None = None
    last_successful_insight_at: str | None = None
    last_successful_insight_result: str | None = None
    last_recovery_at: str | None = None


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


def collect_sync_status(
    memory_root: Path,
    *,
    config_loader: Callable[[Path], object] = load_sync_config,
    state_reader: Callable[[Path], dict[str, object]] = read_sync_state,
) -> SyncStatus:
    root = Path(memory_root)
    issues: list[str] = []
    enabled: bool | None = None
    try:
        configured = getattr(config_loader(root), "enabled")
        if not isinstance(configured, bool):
            raise ValueError("[sync].enabled must be a boolean")
        enabled = configured
    except Exception as exc:
        issues.append(f"sync config error: {type(exc).__name__}: {exc}")

    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    divergence_error: str | None = None
    branch = _run_git(root, "branch", "--show-current")
    if branch.returncode != 0:
        detail = _command_detail(branch) or "git command failed"
        divergence_error = f"Git unavailable: {detail}"
    else:
        branch_name = branch.stdout.strip()
        if not branch_name:
            divergence_error = "detached HEAD has no upstream tracking reference"
        else:
            tracking = _run_git(
                root,
                "for-each-ref",
                "--format=%(upstream:short)",
                "--count=1",
                f"refs/heads/{branch_name}",
            )
            if tracking.returncode != 0:
                detail = _command_detail(tracking) or "git command failed"
                divergence_error = f"could not read upstream tracking reference: {detail}"
            else:
                upstream = tracking.stdout.strip() or _configured_upstream(root, branch_name)
                if upstream is None:
                    divergence_error = "no upstream tracking reference"
                else:
                    resolved = _run_git(
                        root,
                        "rev-parse",
                        "--verify",
                        "--quiet",
                        f"{upstream}^{{commit}}",
                    )
                    if resolved.returncode != 0:
                        divergence_error = "upstream ref is missing from the local repository"
                    else:
                        divergence = _run_git(
                            root,
                            "rev-list",
                            "--left-right",
                            "--count",
                            f"HEAD...{upstream}",
                        )
                        if divergence.returncode != 0:
                            detail = _command_detail(divergence) or "git command failed"
                            divergence_error = f"could not compare upstream: {detail}"
                        else:
                            try:
                                ahead_text, behind_text = divergence.stdout.split()
                                ahead = int(ahead_text)
                                behind = int(behind_text)
                            except (TypeError, ValueError):
                                divergence_error = "Git returned an invalid ahead/behind count"

    if divergence_error and enabled is not False:
        issues.append(f"sync upstream unavailable: {divergence_error}")

    last_status: str | None = None
    last_at: str | None = None
    last_message: str | None = None
    last_files: tuple[str, ...] = ()
    last_error: str | None = None
    try:
        state = state_reader(root)
        last_status, last_at, last_message, last_files = _last_sync_outcome(state)
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        issues.append(f"sync state error: {last_error}")

    return SyncStatus(
        enabled=enabled,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        divergence_error=divergence_error,
        last_status=last_status,
        last_at=last_at,
        last_message=last_message,
        last_files=last_files,
        last_error=last_error,
        issues=tuple(issues),
    )


def _configured_upstream(root: Path, branch_name: str) -> str | None:
    remote = _run_git(root, "config", "--get", f"branch.{branch_name}.remote")
    merge = _run_git(root, "config", "--get", f"branch.{branch_name}.merge")
    if remote.returncode != 0 or merge.returncode != 0:
        return None
    remote_name = remote.stdout.strip()
    merge_ref = merge.stdout.strip()
    if not remote_name or not merge_ref.startswith("refs/heads/"):
        return None
    branch_ref = merge_ref.removeprefix("refs/heads/")
    return branch_ref if remote_name == "." else f"{remote_name}/{branch_ref}"


def read_log_preview(path: Path) -> str | None:
    try:
        lines = _read_text_tail(path, MAX_LOG_TAIL_BYTES).splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"error reading log: {type(exc).__name__}: {exc}"

    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return None
    recent = _latest_log_event_lines(meaningful)
    for line in reversed(recent):
        if _looks_like_failure(line):
            return _cap_preview(line)
    return _cap_preview("\n".join(recent[-MAX_PREVIEW_LINES:]))


def collect_managed_watch_sections(
    memory_root: Path,
    *,
    watch_status_reader: Callable[[Path, str], object] | None = None,
) -> tuple[list[SectionStatus], list[str]]:
    sections: list[SectionStatus] = []
    issues: list[str] = []
    if watch_status_reader is None:
        watch_status_reader = read_only_managed_watch_status

    for name in MANAGED_WATCH_TARGETS:
        try:
            status = watch_status_reader(memory_root, name)
            log_path = Path(getattr(status, "log_path"))
            log_missing = not log_path.exists()
            state = str(getattr(status, "state"))
            pid = getattr(status, "pid", None)
            section_issues: list[str] = []
            if state == "running" and pid is not None:
                section_state = f"running pid {pid}"
            elif state == "stale" and pid is not None:
                section_state = f"stale pid {pid}"
                section_issues.append(f"{name}: stale pid {pid}")
            elif state == "external":
                section_state = "running outside manager"
                section_issues.append(f"{name}: running outside manager")
            else:
                section_state = state
            last = read_log_preview(log_path)
            if last and _looks_like_failure(last):
                section_issues.append(f"{name}: {last.splitlines()[0]}")
            issues.extend(section_issues)
            sections.append(
                SectionStatus(
                    name=name,
                    state=section_state,
                    log_path=_display_path(memory_root, log_path),
                    log_missing=log_missing,
                    last=last,
                    issue=section_issues[0] if section_issues else None,
                )
            )
        except Exception as exc:
            message = f"{name}: status error: {type(exc).__name__}: {exc}"
            sections.append(SectionStatus(name=name, state=message, issue=message))
            issues.append(message)
    return sections, issues


def read_only_managed_watch_status(memory_root: Path, name: str) -> ManagedWatchStatus:
    log_path = watch_log_path(memory_root, name)
    pid = _read_watch_pid(watch_pid_path(memory_root, name))
    if pid is not None:
        if _is_managed_watch_process(pid, name, memory_root=memory_root):
            return ManagedWatchStatus(name=name, state="running", pid=pid, log_path=log_path)
        if _watch_lock_held_read_only(memory_root, name):
            return ManagedWatchStatus(name=name, state="external", pid=None, log_path=log_path)
        return ManagedWatchStatus(name=name, state="stale", pid=pid, log_path=log_path)
    if _watch_lock_held_read_only(memory_root, name):
        return ManagedWatchStatus(name=name, state="external", pid=None, log_path=log_path)
    return ManagedWatchStatus(name=name, state="stopped", pid=None, log_path=log_path)


def collect_insight_section(
    memory_root: Path,
    *,
    trigger_reader: Callable[[Path], object] | None = None,
    config_loader: Callable[[], object] = load_insight_watch_config,
) -> SectionStatus:
    if trigger_reader is None:
        trigger_reader = _read_insight_trigger_snapshot
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
        last_result = getattr(state, "last_successful_insight_result", None)
        if last_result:
            detail += f"\nlast result: {last_result}"
        last = getattr(state, "last_successful_insight_at", None)
        return SectionStatus(name="insight", state="trigger progress", detail=detail, last=last)
    except Exception as exc:
        return SectionStatus(
            name="insight",
            state=f"error: {type(exc).__name__}: {exc}",
            issue=f"insight trigger error: {type(exc).__name__}: {exc}",
        )


def collect_curation_section(memory_root: Path) -> tuple[SectionStatus, list[str]]:
    from .curation_worker import CurationWorker
    from .learning import list_learning_candidates
    from .learning_service import LearningService

    root = Path(memory_root)
    worker = CurationWorker(root).status()
    notices = LearningService(root).notices()
    candidates = list_learning_candidates(root)
    issues = [f"curation: {worker['error']}"] if worker.get("error") else []
    detail = [f"pending sources: {len(candidates)}"]
    for notice in notices:
        detail.append(f"{notice['domain']}: {notice['status']}; {notice['candidate_count']} pending")
        if notice.get("task_id"):
            detail.append(f"  task: {notice['task_id']}")
    detail.append("state: learning/ and .runtime/learning/")
    state = "running" if worker["live"] else "needs recovery" if issues else "idle"
    return SectionStatus(
        name="curation", state=state, detail="\n".join(detail),
        log_path=_display_path(root, Path(worker["log_path"])),
        log_missing=not Path(worker["log_path"]).is_file(),
        issue=issues[0] if issues else None,
    ), issues


def collect_semantic_operation_section(memory_root: Path) -> tuple[SectionStatus, list[str]]:
    records = SemanticOperationStore(memory_root).list_outstanding_records()
    active = sum(record.phase not in FINAL_PHASES for record in records)
    pending = []
    issues: list[str] = []
    for record in records:
        if record.phase not in FINAL_PHASES:
            continue
        for effect in record.effects:
            if effect.status == "done":
                continue
            pending.append((record.operation_id, effect))
            if effect.status == "failed":
                issues.append(
                    f"semantic effect: {record.operation_id}: {effect.name}: {effect.error or 'failed'}"
                )
    if pending:
        state = f"{len(pending)} {_plural('effect', len(pending))} pending"
    elif active:
        state = f"{active} active"
    else:
        state = "settled"
    detail = "\n".join(
        (
            f"active operations: {active}",
            f"pending effects: {len(pending)}",
            "state: .runtime/operations/records",
        )
    )
    return SectionStatus(name="operations", state=state, detail=detail), issues


def collect_status(
    memory_root: Path,
    *,
    sync_collector: Callable[[Path], SyncStatus] | None = None,
    watch_collector: Callable[[Path], tuple[list[SectionStatus], list[str]]] | None = None,
    insight_collector: Callable[[Path], SectionStatus] = collect_insight_section,
    curation_collector: Callable[[Path], tuple[SectionStatus, list[str]]] = collect_curation_section,
    operation_collector: Callable[[Path], tuple[SectionStatus, list[str]]] = collect_semantic_operation_section,
) -> DashboardStatus:
    root = Path(memory_root)
    git = collect_git_status(root)
    if sync_collector is None:
        sync_collector = collect_sync_status
    if watch_collector is None:
        watch_collector = collect_managed_watch_sections

    issues: list[str] = []
    try:
        sync = sync_collector(root)
        issues.extend(sync.issues)
    except Exception as exc:
        message = f"sync: status error: {type(exc).__name__}: {exc}"
        sync = SyncStatus(
            enabled=None,
            upstream=None,
            ahead=None,
            behind=None,
            divergence_error="status collection failed",
            last_error=f"{type(exc).__name__}: {exc}",
            issues=(message,),
        )
        issues.append(message)

    try:
        watches, watch_issues = watch_collector(root)
        issues.extend(watch_issues)
    except Exception as exc:
        message = f"managed watches: status error: {type(exc).__name__}: {exc}"
        watches = [SectionStatus(name="watches", state=message, issue=message)]
        issues.append(message)

    try:
        insight = insight_collector(root)
        if insight.issue:
            issues.append(insight.issue)
    except Exception as exc:
        message = f"insight: status error: {type(exc).__name__}: {exc}"
        insight = SectionStatus(name="insight", state=message, issue=message)
        issues.append(message)

    try:
        curation, curation_issues = curation_collector(root)
        issues.extend(curation_issues)
    except Exception as exc:
        message = f"curation: status error: {type(exc).__name__}: {exc}"
        curation = SectionStatus(name="curation", state=message, issue=message)
        issues.append(message)

    try:
        operations, operation_issues = operation_collector(root)
        issues.extend(operation_issues)
    except Exception as exc:
        message = f"semantic operations: status error: {type(exc).__name__}: {exc}"
        operations = SectionStatus(name="operations", state=message, issue=message)
        issues.append(message)

    return DashboardStatus(
        root=root,
        git=git,
        sync=sync,
        watches=watches,
        insight=insight,
        curation=curation,
        operations=operations,
        issues=issues,
    )


def format_status_dashboard(status: DashboardStatus) -> str:
    lines: list[str] = [
        "RightMemory",
        f"  root: {status.root}",
        f"  git: {status.git.summary}",
    ]
    if status.sync is not None:
        lines.append("")
        lines.append("Sync")
        lines.extend(_format_sync_status(status.sync))
    lines.extend(("", "Managed Watches"))
    if status.watches:
        for watch in status.watches:
            lines.extend(_format_section(watch))
    else:
        lines.append("  (none)")

    if status.insight is not None:
        lines.append("")
        lines.append("Insight")
        lines.extend(_format_section(status.insight))

    if status.curation is not None:
        lines.append("")
        lines.append("Curation")
        lines.extend(_format_section(status.curation))

    if status.operations is not None:
        lines.append("")
        lines.append("Semantic Operations")
        lines.extend(_format_section(status.operations))

    issues = _dashboard_issues(status)
    if issues:
        lines.append("")
        lines.append("Recent Issues")
        lines.extend(f"  {issue}" for issue in issues)
    hints = _recovery_hints(status)
    if hints:
        lines.append("")
        lines.append("Recovery")
        lines.extend(f"  {hint}" for hint in hints)
    return "\n".join(lines)


def _dashboard_issues(status: DashboardStatus) -> list[str]:
    issues = list(status.issues)
    if status.sync is not None:
        for issue in status.sync.issues:
            if issue not in issues:
                issues.append(issue)
    if status.git.issue:
        issues.insert(0, status.git.issue)
    return issues


def _recovery_hints(status: DashboardStatus) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for issue in _dashboard_issues(status):
        hint = _recovery_hint_for_issue(issue)
        if hint and hint not in seen:
            hints.append(hint)
            seen.add(hint)
    return hints


def _recovery_hint_for_issue(issue: str) -> str | None:
    # Keep this ordered so broad subsystem patterns do not catch specific cases first.
    if issue.startswith("dirty worktree:"):
        return "git: inspect with `git status --short`; resolve local changes before automatic writes continue"
    if issue.startswith("git unavailable:"):
        return "git: inspect the configured memory root and repair Git before retrying"
    if issue.startswith("sync config error:"):
        return "sync: fix `rightmemory.toml`, then rerun `rightmemory status`"
    if issue.startswith("sync state error:"):
        return "sync: inspect `.runtime/sync/state.json` for malformed or unreadable state"
    if issue.startswith("sync upstream unavailable:"):
        return "sync: inspect the local branch upstream and last-fetched remote-tracking ref"
    if issue.startswith("sync: status error:"):
        return "sync: rerun `rightmemory status`; inspect Git and sync configuration if it persists"
    if issue.startswith("insight trigger error:"):
        return "insight: inspect `.runtime/insight/trigger-state.json`"
    if issue.startswith("managed watches: status error:"):
        return "managed watches: rerun `rightmemory status`; inspect watch state if it persists"
    if issue.startswith("insight: status error:"):
        return "insight: rerun `rightmemory status`; inspect insight state if it persists"
    if issue.startswith("semantic operations: status error:"):
        return "semantic operations: inspect `.runtime/operations/records/`"
    if issue.startswith("semantic effect:"):
        return "semantic effects: inspect `.runtime/operations/records/`; later semantic turns retry pending effects"
    if issue.startswith("curation:"):
        return "curation: inspect `rightmemory curate status`; resume the indicated task or wake the configured worker"
    return _watch_recovery_hint(issue)


def _watch_recovery_hint(issue: str) -> str | None:
    for name in MANAGED_WATCH_TARGETS:
        prefix = f"{name}: "
        if not issue.startswith(prefix):
            continue
        detail = issue[len(prefix) :]
        if detail.startswith("stale pid "):
            return f"{name}: run `rightmemory watch restart {name}`"
        if detail == "running outside manager":
            return f"{name}: stop the foreground process directly, then run `rightmemory watch start {name}`"
        if detail.startswith("status error:"):
            return f"{name}: rerun `rightmemory status`; inspect watch state if it persists"
        if _looks_like_failure(detail):
            return (
                f"{name}: inspect the shown log path, then run "
                f"`rightmemory watch restart {name}` when appropriate"
            )
    return None


def _format_section(section: SectionStatus) -> list[str]:
    lines = [f"  {section.name}: {section.state}"]
    if section.log_path:
        suffix = " (missing)" if section.log_missing else ""
        lines.append(f"    log: {section.log_path}{suffix}")
    if section.detail:
        lines.extend(f"    {line}" for line in section.detail.splitlines())
    if section.last:
        for index, line in enumerate(section.last.splitlines()):
            prefix = "last: " if index == 0 else "      "
            lines.append(f"    {prefix}{line}")
    return lines


def _format_sync_status(status: SyncStatus) -> list[str]:
    enabled = "unavailable" if status.enabled is None else ("yes" if status.enabled else "no")
    upstream = status.upstream or "none"
    if status.ahead is not None and status.behind is not None:
        divergence = f"ahead {status.ahead}, behind {status.behind} (last fetched)"
    else:
        detail = status.divergence_error or "upstream comparison unavailable"
        divergence = f"unavailable ({detail})"
    lines = [
        f"  enabled: {enabled}",
        f"  upstream: {upstream}",
        f"  divergence: {divergence}",
    ]
    if status.last_error:
        lines.append(f"  last: unavailable ({status.last_error})")
    elif status.last_status is None:
        lines.append("  last: none")
    else:
        lines.append(f"  last: {status.last_status} at {status.last_at}")
        message = _cap_preview(status.last_message or "")
        for index, line in enumerate(message.splitlines()):
            prefix = "message: " if index == 0 else "         "
            lines.append(f"    {prefix}{line}")
        if status.last_files:
            lines.append(f"    files: {', '.join(status.last_files)}")
    return lines


def _cap_preview(text: str) -> str:
    lines = text.splitlines()[:MAX_PREVIEW_LINES]
    preview = "\n".join(lines)
    if len(preview) > MAX_PREVIEW_CHARS:
        return preview[:MAX_PREVIEW_CHARS]
    return preview


def _read_text_tail(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def _looks_like_failure(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not NEUTRAL_FAILURE_COUNTER_PATTERN.match(line)]
    if not lines:
        return False
    lower = "\n".join(lines).lower()
    return any(marker in lower for marker in FAILURE_MARKERS)


def _latest_log_event_lines(lines: list[str]) -> list[str]:
    for index in range(len(lines) - 1, -1, -1):
        if LOG_EVENT_PATTERN.match(lines[index]):
            return lines[index:]
    return lines


def _display_path(memory_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(memory_root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)


def _read_watch_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _watch_lock_held_read_only(memory_root: Path, name: str) -> bool:
    path = memory_root / ".runtime" / "watch" / f"{name}.lock"
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            locked = False
            try:
                lock_file_nonblocking(handle)
                locked = True
            except BlockingIOError:
                return True
            finally:
                if locked:
                    unlock_file(handle)
    except OSError:
        return False
    return False


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected in {path}")
    return data


def _last_sync_outcome(
    state: dict[str, object],
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    if not isinstance(state, dict):
        raise ValueError("sync state must be a JSON object")
    outcome_keys = ("last_status", "last_message", "last_files")
    if not any(key in state for key in outcome_keys):
        return None, None, None, ()

    status = state.get("last_status")
    message = state.get("last_message")
    files = state.get("last_files")
    if not isinstance(status, str) or not status:
        raise ValueError("sync state must contain non-empty string field: last_status")
    if not isinstance(message, str) or not message:
        raise ValueError("sync state must contain non-empty string field: last_message")
    if not isinstance(files, list) or any(not isinstance(path, str) for path in files):
        raise ValueError("sync state must contain string list field: last_files")

    if "last_failure_at" in state:
        timestamp = _sync_timestamp(state.get("last_failure_at"), "last_failure_at")
    else:
        candidates = [
            _sync_timestamp(state[key], key)
            for key in ("last_successful_pull_at", "last_successful_push_at")
            if key in state
        ]
        if not candidates:
            raise ValueError("sync state with a recorded result must contain its timestamp")
        timestamp = max(candidates, key=lambda item: item[0])
    return status, timestamp[1], message, tuple(files)


def _sync_timestamp(value: object, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise ValueError(f"sync state field {field} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"sync state field {field} must be an ISO datetime string") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"sync state field {field} must include a timezone")
    return parsed, value


def _read_insight_trigger_snapshot(memory_root: Path) -> _InsightTriggerSnapshot:
    path = Path(memory_root) / ".runtime" / "insight" / "trigger-state.json"
    if not path.exists():
        return _InsightTriggerSnapshot()
    data = _read_json(path)
    points = data.get("points", 0.0)
    if isinstance(points, bool) or not isinstance(points, (int, float)):
        raise ValueError("insight trigger points must be a number")
    points = float(points)
    if not math.isfinite(points) or points < 0:
        raise ValueError("insight trigger points must be a nonnegative finite number")
    return _InsightTriggerSnapshot(
        points=points,
        updated_at=_optional_iso_datetime_str(data.get("updated_at"), "updated_at"),
        last_successful_insight_at=_optional_iso_datetime_str(
            data.get("last_successful_insight_at"),
            "last_successful_insight_at",
        ),
        last_successful_insight_result=_optional_insight_result_str(
            data.get("last_successful_insight_result"),
            "last_successful_insight_result",
        ),
        last_recovery_at=_optional_iso_datetime_str(data.get("last_recovery_at"), "last_recovery_at"),
    )


def _optional_iso_datetime_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime string or null") from exc
    return value


def _optional_insight_result_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if value not in {"artifact", "noop"}:
        raise ValueError(f"{field} must be artifact, noop, or null")
    return value


def _plural(word: str, count: int) -> str:
    return word if count == 1 else f"{word}s"


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    env = os.environ.copy()
    env["GIT_ASKPASS"] = "true"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            command,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    output = result.stderr.strip() or result.stdout.strip()
    return output.splitlines()[0] if output else ""
