from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory
from .tools import MEMORY_DETAIL_FILE_RE, MEMORY_SKILL_FILE_RE


SNAPSHOT_HEADER = "Daily memory snapshot"
DIFF_HEADER = "Memory changes since previous retrieve turn"
RECENT_SUBMITTED_CONTEXT_HEADER = "Recent submitted memory"
QUERY_HEADER = "Query"
HISTORY_HEADER = "Prior retrieve conversation"
SNAPSHOT_STATE = ".runtime/retrieve_context/daily-snapshot.json"
SESSION_STATE_DIR = ".runtime/retrieve_context/sessions"


@dataclass(frozen=True)
class DailySnapshot:
    day: str
    base_commit: str | None
    content_hash: str
    text: str
    paths: list[str] = field(default_factory=list)


def active_memory_paths(memory_root: Path) -> list[str]:
    root = Path(memory_root)
    paths: list[str] = []
    for path in root.glob("MEMORY*.md"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "MEMORY.md" or MEMORY_DETAIL_FILE_RE.fullmatch(relative):
            if not MEMORY_SKILL_FILE_RE.fullmatch(relative):
                paths.append(relative)
    return sorted(paths, key=lambda item: (item != "MEMORY.md", item))


def load_daily_snapshot(memory_root: Path, *, now: datetime | None = None) -> DailySnapshot:
    root = Path(memory_root)
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    day = now.date().isoformat()
    state_path = root / SNAPSHOT_STATE
    if state_path.exists():
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("day") == day:
            return _snapshot_from_dict(data)

    paths = active_memory_paths(root)
    text = _render_snapshot_text(root, paths)
    snapshot = DailySnapshot(
        day=day,
        base_commit=current_memory_head(root),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        paths=paths,
    )
    _write_json(root, state_path, asdict(snapshot))
    return snapshot


def current_memory_head(memory_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=memory_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def memory_diff_since(memory_root: Path, old_commit: str | None, new_commit: str | None) -> str:
    if not old_commit or not new_commit or old_commit == new_commit:
        return ""
    changed = _changed_active_memory_paths(memory_root, old_commit, new_commit)
    if not changed:
        return ""
    result = subprocess.run(
        ["git", "diff", old_commit, new_commit, "--", *changed],
        cwd=memory_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout.strip()


def format_memory_diff_block(diff: str) -> str:
    clean = diff.strip()
    if not clean:
        return ""
    return (
        f"# {DIFF_HEADER}\n\n"
        "Apply this patch mentally to the daily memory snapshot. "
        "Added lines are newer memory. Removed lines are obsolete.\n\n"
        "```diff\n"
        f"{clean}\n"
        "```"
    )


def _render_snapshot_text(memory_root: Path, paths: list[str]) -> str:
    parts = [SNAPSHOT_HEADER, ""]
    for relative in paths:
        text = (memory_root / relative).read_text(encoding="utf-8")
        parts.append(f"===== {relative} =====")
        parts.append(text.rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _changed_active_memory_paths(memory_root: Path, old_commit: str, new_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", old_commit, new_commit, "--", "MEMORY.md", "MEMORY_*.md"],
        cwd=memory_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {result.stderr.strip()}")
    paths = []
    for raw in result.stdout.splitlines():
        path = raw.strip()
        if path == "MEMORY.md" or MEMORY_DETAIL_FILE_RE.fullmatch(path):
            if not MEMORY_SKILL_FILE_RE.fullmatch(path):
                paths.append(path)
    return sorted(set(paths))


def _snapshot_from_dict(data: dict[str, object]) -> DailySnapshot:
    paths = data.get("paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("daily snapshot paths must be a list of strings")
    day = data.get("day")
    content_hash = data.get("content_hash")
    text = data.get("text")
    base_commit = data.get("base_commit")
    if not isinstance(day, str) or not isinstance(content_hash, str) or not isinstance(text, str):
        raise ValueError("daily snapshot state is malformed")
    if base_commit is not None and not isinstance(base_commit, str):
        raise ValueError("daily snapshot base_commit must be a string or null")
    return DailySnapshot(day=day, base_commit=base_commit, content_hash=content_hash, text=text, paths=paths)


def _write_json(memory_root: Path, path: Path, data: dict[str, object]) -> None:
    _ensure_runtime_gitignore(memory_root / ".runtime")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
