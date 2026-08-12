from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .corrections import AGENT_CORRECTION_SOURCE_PATHS, annotate_agent_correction_entries
from .retrieve_selection import DeliveredRange, RetrieveDeliveryCoverage
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


SNAPSHOT_HEADER = "Daily RightMemory root snapshot"
SNAPSHOT_SCOPE = "rightmemory-roots-v3"
DIFF_HEADER = "RightMemory root changes since previous retrieve turn"
RECENT_SUBMITTED_CONTEXT_HEADER = "Recent submitted RightMemory candidates"
UPDATED_MATERIAL_HEADER = "Updated retrieval material"
CURRENT_MATERIAL_HEADER = "Current retrieval material"
QUERY_HEADER = "Query"
SNAPSHOT_STATE = ".runtime/retrieve_context/daily-snapshot.json"
SESSION_STATE_DIR = ".runtime/retrieve_context/sessions"


@dataclass(frozen=True)
class DailySnapshot:
    day: str
    base_commit: str | None
    content_hash: str
    text: str
    paths: list[str] = field(default_factory=list)
    scope: str = SNAPSHOT_SCOPE


@dataclass(frozen=True)
class RetrieveSessionState:
    session_id: str
    delivered_memory_commit: str | None = None
    model_history_json: bytes | None = field(default=None, repr=False)
    visible_recent_candidates: dict[str, str] = field(default_factory=dict)
    delivery_coverage: RetrieveDeliveryCoverage = field(default_factory=RetrieveDeliveryCoverage)


class RetrieveContextStore:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.root = self.memory_root / SESSION_STATE_DIR

    def load(self, session_id: str) -> RetrieveSessionState:
        path = self._state_path(session_id)
        if not path.exists():
            return RetrieveSessionState(session_id=session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("session_id") != session_id:
            raise ValueError("retrieve context session state is malformed")
        allowed = {
            "session_id",
            "delivered_memory_commit",
            "model_history",
            "visible_recent_candidates",
            "delivery_coverage",
        }
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(
                "retrieve context session state has unsupported field(s): "
                + ", ".join(sorted(unknown))
            )
        delivered = data.get("delivered_memory_commit")
        if delivered is not None and not isinstance(delivered, str):
            raise ValueError("retrieve context delivered_memory_commit must be a string or null")
        raw_history = data.get("model_history")
        if raw_history is not None and not isinstance(raw_history, list):
            raise ValueError("retrieve context model_history must be a list or null")
        model_history_json = (
            None
            if raw_history is None
            else json.dumps(raw_history, ensure_ascii=False, separators=(",", ":")).encode()
        )
        raw_visible = data.get("visible_recent_candidates", {})
        if not isinstance(raw_visible, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_visible.items()
        ):
            raise ValueError("retrieve context visible_recent_candidates must be a string map")
        coverage = _coverage_from_dict(data.get("delivery_coverage", {}))
        return RetrieveSessionState(
            session_id=session_id,
            delivered_memory_commit=delivered,
            model_history_json=model_history_json,
            visible_recent_candidates=dict(raw_visible),
            delivery_coverage=coverage,
        )

    def record_success(
        self,
        session_id: str,
        *,
        memory_commit: str | None,
        model_history_json: bytes | None,
        visible_recent_candidates: dict[str, str],
        delivery: RetrieveDeliveryCoverage | None = None,
    ) -> None:
        state = self.load(session_id)
        next_state = RetrieveSessionState(
            session_id=session_id,
            delivered_memory_commit=memory_commit,
            model_history_json=model_history_json,
            visible_recent_candidates=dict(visible_recent_candidates),
            delivery_coverage=state.delivery_coverage.merged(delivery or RetrieveDeliveryCoverage()),
        )
        self._write(next_state)

    def reset(self, session_id: str) -> bool:
        path = self._state_path(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(path.parent)
        return True

    def _state_path(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.json"

    def _write(self, state: RetrieveSessionState) -> None:
        data = {
            "session_id": state.session_id,
            "delivered_memory_commit": state.delivered_memory_commit,
            "model_history": _model_history_value(state.model_history_json),
            "visible_recent_candidates": dict(state.visible_recent_candidates),
            "delivery_coverage": _coverage_to_dict(state.delivery_coverage),
        }
        _write_json(self.memory_root, self._state_path(state.session_id), data)


def root_memory_paths(memory_root: Path) -> list[str]:
    root = Path(memory_root)
    names = [
        "MEMORY.md",
        "PURSUITS.md",
        *AGENT_CORRECTION_SOURCE_PATHS.values(),
    ]
    return [name for name in names if (root / name).is_file()]


def load_daily_snapshot(memory_root: Path, *, now: datetime | None = None) -> DailySnapshot:
    root = Path(memory_root)
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    day = now.date().isoformat()
    state_path = root / SNAPSHOT_STATE
    if state_path.exists():
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("day") == day and data.get("scope") == SNAPSHOT_SCOPE:
            return _snapshot_from_dict(data)

    paths = root_memory_paths(root)
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
        encoding="utf-8",
        errors="replace",
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
    changed = _changed_root_memory_paths(memory_root, old_commit, new_commit)
    if not changed:
        return ""
    correction_sources = {
        path: source_id
        for source_id, path in AGENT_CORRECTION_SOURCE_PATHS.items()
    }
    ordinary_paths = [path for path in changed if path not in correction_sources]
    parts: list[str] = []
    if ordinary_paths:
        result = subprocess.run(
            ["git", "diff", old_commit, new_commit, "--", *ordinary_paths],
            cwd=memory_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
        if result.stdout.strip():
            parts.append(result.stdout.strip())

    for path in changed:
        source_id = correction_sources.get(path)
        if source_id is None:
            continue
        old_text = _git_file_text(memory_root, old_commit, path)
        new_text = _git_file_text(memory_root, new_commit, path)
        old_annotated = (
            "" if old_text is None else annotate_agent_correction_entries(old_text, source_id)
        )
        new_annotated = (
            "" if new_text is None else annotate_agent_correction_entries(new_text, source_id)
        )
        rendered = "\n".join(
            difflib.unified_diff(
                old_annotated.splitlines(),
                new_annotated.splitlines(),
                fromfile="/dev/null" if old_text is None else f"a/{path}",
                tofile="/dev/null" if new_text is None else f"b/{path}",
                lineterm="",
            )
        )
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts)


def format_memory_diff_block(diff: str) -> str:
    clean = diff.strip()
    if not clean:
        return ""
    return (
        f"# {DIFF_HEADER}\n\n"
        "Apply this patch mentally to the daily RightMemory root snapshot. "
        "Added lines are current. Removed lines are obsolete.\n\n"
        "```diff\n"
        f"{clean}\n"
        "```"
    )


def build_retrieve_request_text(
    *,
    context_parts: list[str] | tuple[str, ...],
    query: str,
) -> str:
    parts = [part.rstrip() for part in context_parts if part.strip()]
    parts.append(format_query_block(query))
    return "\n\n".join(parts).rstrip() + "\n"


def format_query_block(query: str) -> str:
    return f"# {QUERY_HEADER}\n\n{query.strip()}"


def format_recent_submitted_context_block(
    entries: list[object],
    *,
    no_longer_pending: list[str] | tuple[str, ...] = (),
) -> str:
    if not entries and not no_longer_pending:
        return ""
    lines = [f"# {RECENT_SUBMITTED_CONTEXT_HEADER}", ""]
    if entries:
        if no_longer_pending:
            lines.extend(["New pending:", ""])
        for entry in entries:
            lines.append(
                f"[selection_id: {entry.update_session_id}:{entry.candidate_id} | "
                f"update session: {entry.update_session_id} | "
                f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
            )
            lines.extend(entry.message.splitlines() or [""])
            lines.append("")
    if no_longer_pending:
        lines.extend(["No longer pending:", ""])
        lines.extend(f"- `{selection_id}`" for selection_id in no_longer_pending)
        lines.append("")
    return "\n".join(lines).rstrip()


def format_updated_material_block(labels: list[str]) -> str:
    if not labels:
        return ""
    lines = [
        f"# {UPDATED_MATERIAL_HEADER}",
        "",
        "These sources changed since they were last returned. Read them again if relevant:",
        "",
        *(f"- {label}" for label in labels),
    ]
    return "\n".join(lines)


def format_current_material_block(text: str) -> str:
    clean = text.strip()
    if not clean:
        return ""
    return f"# {CURRENT_MATERIAL_HEADER}\n\n{clean}"


def _render_snapshot_text(memory_root: Path, paths: list[str]) -> str:
    parts = [SNAPSHOT_HEADER, ""]
    correction_sources = {
        path: source_id
        for source_id, path in AGENT_CORRECTION_SOURCE_PATHS.items()
    }
    for relative in paths:
        text = (memory_root / relative).read_text(encoding="utf-8")
        source_id = correction_sources.get(relative)
        if source_id is not None:
            text = annotate_agent_correction_entries(text, source_id)
        parts.append(f"===== {relative} =====")
        parts.append(text.rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _changed_root_memory_paths(memory_root: Path, old_commit: str, new_commit: str) -> list[str]:
    root_names = {
        "MEMORY.md",
        "PURSUITS.md",
        *AGENT_CORRECTION_SOURCE_PATHS.values(),
    }
    result = subprocess.run(
        ["git", "diff", "--name-only", old_commit, new_commit, "--", *sorted(root_names)],
        cwd=memory_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {result.stderr.strip()}")
    paths = [raw.strip() for raw in result.stdout.splitlines() if raw.strip() in root_names]
    return sorted(set(paths))


def _git_file_text(memory_root: Path, commit: str, path: str) -> str | None:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=memory_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if exists.returncode != 0:
        return None
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=memory_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git show failed for {path}: {result.stderr.strip()}")
    return result.stdout


def _snapshot_from_dict(data: dict[str, object]) -> DailySnapshot:
    paths = data.get("paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("daily snapshot paths must be a list of strings")
    day = data.get("day")
    content_hash = data.get("content_hash")
    text = data.get("text")
    base_commit = data.get("base_commit")
    scope = data.get("scope")
    if not isinstance(day, str) or not isinstance(content_hash, str) or not isinstance(text, str):
        raise ValueError("daily snapshot state is malformed")
    if base_commit is not None and not isinstance(base_commit, str):
        raise ValueError("daily snapshot base_commit must be a string or null")
    if scope != SNAPSHOT_SCOPE:
        raise ValueError("daily snapshot scope is unsupported")
    return DailySnapshot(day=day, base_commit=base_commit, content_hash=content_hash, text=text, paths=paths)


def _coverage_from_dict(value: object) -> RetrieveDeliveryCoverage:
    if value is None:
        return RetrieveDeliveryCoverage()
    if not isinstance(value, dict):
        raise ValueError("retrieve delivery_coverage must be an object")
    allowed = {"local_items", "source_items", "complete_sources", "ranges", "recent_candidates"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"retrieve delivery_coverage has unknown field(s): {', '.join(sorted(unknown))}")

    def string_map(name: str) -> dict[str, str]:
        raw = value.get(name, {})
        if not isinstance(raw, dict) or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in raw.items()
        ):
            raise ValueError(f"retrieve delivery_coverage.{name} must be a string map")
        return dict(raw)

    raw_ranges = value.get("ranges", [])
    if not isinstance(raw_ranges, list):
        raise ValueError("retrieve delivery_coverage.ranges must be a list")
    ranges: list[DeliveredRange] = []
    for raw in raw_ranges:
        if not isinstance(raw, dict) or set(raw) != {"source_id", "start", "end", "source_hash"}:
            raise ValueError("retrieve delivery_coverage range entries are malformed")
        source_id = raw.get("source_id")
        start = raw.get("start")
        end = raw.get("end")
        source_hash = raw.get("source_hash")
        if (
            not isinstance(source_id, str)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
            or not isinstance(source_hash, str)
        ):
            raise ValueError("retrieve delivery_coverage range entries are malformed")
        ranges.append(DeliveredRange(source_id, start, end, source_hash))

    recent = value.get("recent_candidates", {})
    if not isinstance(recent, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in recent.items()
    ):
        raise ValueError("retrieve delivery_coverage.recent_candidates must be a string map")
    return RetrieveDeliveryCoverage(
        local_items=string_map("local_items"),
        source_items=string_map("source_items"),
        complete_sources=string_map("complete_sources"),
        ranges=ranges,
        recent_candidates=dict(recent),
    )


def _coverage_to_dict(coverage: RetrieveDeliveryCoverage) -> dict[str, object]:
    return {
        "local_items": dict(coverage.local_items),
        "source_items": dict(coverage.source_items),
        "complete_sources": dict(coverage.complete_sources),
        "ranges": [asdict(item) for item in coverage.ranges],
        "recent_candidates": dict(coverage.recent_candidates),
    }


def _model_history_value(data: bytes | None) -> list[object] | None:
    if data is None:
        return None
    value = json.loads(data)
    if not isinstance(value, list):
        raise ValueError("retrieve model history must encode a list")
    return value


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
