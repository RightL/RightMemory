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
    output = result.stderr.strip() or result.stdout.strip()
    return output.splitlines()[0] if output else ""
