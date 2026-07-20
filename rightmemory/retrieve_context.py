from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .retrieve_selection import DeliveredRange, RetrieveDeliveryCoverage
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


SNAPSHOT_HEADER = "Daily RightMemory root snapshot"
SNAPSHOT_SCOPE = "rightmemory-roots-v2"
DIFF_HEADER = "RightMemory root changes since previous retrieve turn"
RECENT_SUBMITTED_CONTEXT_HEADER = "Recent submitted RightMemory candidates"
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
    scope: str = SNAPSHOT_SCOPE


@dataclass(frozen=True)
class RetrieveTurn:
    query: str
    answer: str


@dataclass(frozen=True)
class RetrieveSessionState:
    session_id: str
    delivered_memory_commit: str | None = None
    turns: list[RetrieveTurn] = field(default_factory=list)
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
        delivered = data.get("delivered_memory_commit")
        if delivered is not None and not isinstance(delivered, str):
            raise ValueError("retrieve context delivered_memory_commit must be a string or null")
        raw_turns = data.get("turns", [])
        if not isinstance(raw_turns, list):
            raise ValueError("retrieve context turns must be a list")
        turns: list[RetrieveTurn] = []
        for item in raw_turns:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("query"), str)
                or not isinstance(item.get("answer"), str)
            ):
                raise ValueError("retrieve context turn entries must contain query and answer strings")
            turns.append(RetrieveTurn(query=item["query"], answer=item["answer"]))
        coverage = _coverage_from_dict(data.get("delivery_coverage", {}))
        return RetrieveSessionState(
            session_id=session_id,
            delivered_memory_commit=delivered,
            turns=turns,
            delivery_coverage=coverage,
        )

    def record_success(
        self,
        session_id: str,
        *,
        query: str,
        answer: str,
        memory_commit: str | None,
        delivery: RetrieveDeliveryCoverage | None = None,
    ) -> None:
        state = self.load(session_id)
        next_state = RetrieveSessionState(
            session_id=session_id,
            delivered_memory_commit=memory_commit,
            turns=[*state.turns, RetrieveTurn(query=query, answer=answer)],
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
            "turns": [asdict(turn) for turn in state.turns],
            "delivery_coverage": _coverage_to_dict(state.delivery_coverage),
        }
        _write_json(self.memory_root, self._state_path(state.session_id), data)


def root_memory_paths(memory_root: Path) -> list[str]:
    root = Path(memory_root)
    return [name for name in ("MEMORY.md", "PURSUITS.md") if (root / name).is_file()]


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
    result = subprocess.run(
        ["git", "diff", old_commit, new_commit, "--", *changed],
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
    return result.stdout.strip()


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
    snapshot_text: str,
    turns: list[tuple[str, str]] | list[RetrieveTurn],
    diff_block: str,
    recent_block: str,
    query: str,
) -> str:
    parts: list[str] = []
    if snapshot_text.strip():
        parts.append(snapshot_text.rstrip())
    history = _format_turn_history(turns)
    if history:
        parts.append(history)
    if diff_block.strip():
        parts.append(diff_block.strip())
    if recent_block.strip():
        parts.append(recent_block.strip())
    parts.append(f"# {QUERY_HEADER}\n\n{query.strip()}")
    return "\n\n".join(parts).rstrip() + "\n"


def format_recent_submitted_context_block(entries: list[object]) -> str:
    if not entries:
        return ""
    lines = [f"# {RECENT_SUBMITTED_CONTEXT_HEADER}", ""]
    for entry in entries:
        lines.append(
            f"[selection_id: {entry.update_session_id}:{entry.candidate_id} | "
            f"update session: {entry.update_session_id} | "
            f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
        )
        lines.extend(entry.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_snapshot_text(memory_root: Path, paths: list[str]) -> str:
    parts = [SNAPSHOT_HEADER, ""]
    for relative in paths:
        text = (memory_root / relative).read_text(encoding="utf-8")
        parts.append(f"===== {relative} =====")
        parts.append(text.rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _changed_root_memory_paths(memory_root: Path, old_commit: str, new_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", old_commit, new_commit, "--", "MEMORY.md", "PURSUITS.md"],
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
    root_names = {"MEMORY.md", "PURSUITS.md"}
    paths = [raw.strip() for raw in result.stdout.splitlines() if raw.strip() in root_names]
    return sorted(set(paths))


def _format_turn_history(turns: list[tuple[str, str]] | list[RetrieveTurn]) -> str:
    if not turns:
        return ""
    lines = [f"# {HISTORY_HEADER}", ""]
    for turn in turns:
        if isinstance(turn, RetrieveTurn):
            query, answer = turn.query, turn.answer
        else:
            query, answer = turn
        lines.append(f"User: {query}")
        lines.append(f"Assistant: {answer}")
        lines.append("")
    return "\n".join(lines).rstrip()


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

    recent = value.get("recent_candidates", [])
    if not isinstance(recent, list) or any(not isinstance(item, str) for item in recent):
        raise ValueError("retrieve delivery_coverage.recent_candidates must be a string list")
    return RetrieveDeliveryCoverage(
        local_items=string_map("local_items"),
        source_items=string_map("source_items"),
        complete_sources=string_map("complete_sources"),
        ranges=ranges,
        recent_candidates=list(recent),
    )


def _coverage_to_dict(coverage: RetrieveDeliveryCoverage) -> dict[str, object]:
    return {
        "local_items": dict(coverage.local_items),
        "source_items": dict(coverage.source_items),
        "complete_sources": dict(coverage.complete_sources),
        "ranges": [asdict(item) for item in coverage.ranges],
        "recent_candidates": list(coverage.recent_candidates),
    }


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
