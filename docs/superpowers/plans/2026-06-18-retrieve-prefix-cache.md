# Retrieve Prefix Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `rightmemory retrieve` context-first by supplying a daily cached active-memory snapshot, incremental volatile context, and typed progressive-disclosure tools.

**Architecture:** Add a retrieve-specific context module that owns daily snapshot rendering, same-day diffs, real conversation history, and per-session delivery cursors. Runtime assembles each retrieve request as one text prompt with the snapshot first, rendered real history next, optional diff and pending blocks, and the query last; retrieve no longer relies on provider-native resumed message history. Add only `read_skill(skill_id)` and `read_mf(mf_id)` as retrieve tools.

**Tech Stack:** Python standard library, existing RightMemory runtime/session patterns, Git subprocess calls, `unittest`.

---

## File Structure

- Create `rightmemory/retrieve_context.py`: daily snapshot cache, active-memory file selection, Git diff helper, retrieve turn store, request rendering.
- Modify `rightmemory/runtime.py`: call retrieve context assembly, stop passing provider message history for retrieve, save real turns and delivery cursors on success.
- Modify `rightmemory/agent_cli.py`: add stateless CLI-agent turn support for retrieve so provider sessions do not retain old snapshots.
- Modify `rightmemory/tools.py`: add `read_skill(skill_id)` and `read_mf(mf_id)` and expose only typed retrieve tools.
- Modify `rightmemory/prompt.py`: remove broad retrieve read/search tool guidance.
- Modify `rightmemory/prompts/retrieve.md`: rewrite role prompt around supplied context, `read_skill`, `read_mf`, and `MQ#` recommendation-only behavior.
- Create `tests/test_retrieve_context.py`: focused tests for snapshot, diffs, request rendering, and delivery state.
- Modify `tests/test_config.py`: runtime integration tests for request shape, history persistence, failure behavior, CLI-agent stateless behavior, and retrieve tool surface.
- Modify `tests/test_tools.py`: typed tool tests.

---

### Task 1: Add Retrieve Context Snapshot And Diff Helpers

**Files:**
- Create: `rightmemory/retrieve_context.py`
- Test: `tests/test_retrieve_context.py`

- [ ] **Step 1: Write failing tests for active snapshot rendering**

Add this test file:

```python
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.retrieve_context import (
    active_memory_paths,
    current_memory_head,
    format_memory_diff_block,
    load_daily_snapshot,
    memory_diff_since,
)


class RetrieveContextSnapshotTests(unittest.TestCase):
    def test_daily_snapshot_renders_active_memory_without_skill_or_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nroot body\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("## Detail {#detail}\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("# Skill Body\n", encoding="utf-8")
            (root / ".runtime" / "shared_views" / "imports" / "mf-one").mkdir(parents=True)
            (root / ".runtime" / "shared_views" / "imports" / "mf-one" / "MEMORY.md").write_text(
                "external\n",
                encoding="utf-8",
            )

            snapshot = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))

        self.assertEqual(snapshot.day, "2026-06-18")
        self.assertIn("===== MEMORY.md =====", snapshot.text)
        self.assertIn("# Root {#root}", snapshot.text)
        self.assertIn("===== MEMORY_detail.md =====", snapshot.text)
        self.assertNotIn("MEMORY_SKILL_demo.md", snapshot.text)
        self.assertNotIn(".runtime/shared_views/imports", snapshot.text)
        self.assertNotIn("2026-06-18", snapshot.text)

    def test_daily_snapshot_reuses_same_day_text_even_when_memory_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nfirst\n", encoding="utf-8")

            first = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))
            (root / "MEMORY.md").write_text("# Root {#root}\n\nsecond\n", encoding="utf-8")
            second = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))

        self.assertEqual(first.text, second.text)
        self.assertIn("first", second.text)
        self.assertNotIn("second", second.text)

    def test_active_memory_paths_excludes_memory_skill_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(active_memory_paths(root), ["MEMORY.md", "MEMORY_alpha.md"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextSnapshotTests
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.retrieve_context'`.

- [ ] **Step 3: Implement snapshot helpers**

Create `rightmemory/retrieve_context.py` with this initial content:

```python
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id
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


def _render_snapshot_text(memory_root: Path, paths: list[str]) -> str:
    parts = [SNAPSHOT_HEADER, ""]
    for relative in paths:
        text = (memory_root / relative).read_text(encoding="utf-8")
        parts.append(f"===== {relative} =====")
        parts.append(text.rstrip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


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
```

- [ ] **Step 4: Run snapshot tests to verify they pass**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextSnapshotTests
```

Expected: PASS.

- [ ] **Step 5: Write failing tests for Git diff formatting**

Append to `tests/test_retrieve_context.py`:

```python
class RetrieveContextDiffTests(unittest.TestCase):
    def test_memory_diff_since_returns_active_memory_diff_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Root\n\nfirst\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("old skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "initial memory")
            base = current_memory_head(root)

            (root / "MEMORY.md").write_text("# Root\n\nsecond\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("new skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "update memory")
            head = current_memory_head(root)

            diff = memory_diff_since(root, base, head)

        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", diff)
        self.assertIn("+second", diff)
        self.assertNotIn("MEMORY_SKILL_demo.md", diff)

    def test_format_memory_diff_block_omits_empty_diff(self):
        self.assertEqual(format_memory_diff_block(""), "")
        block = format_memory_diff_block("diff --git a/MEMORY.md b/MEMORY.md\n")
        self.assertIn("Apply this patch mentally", block)
        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", block)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()
```

- [ ] **Step 6: Run diff tests to verify they fail**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextDiffTests
```

Expected: FAIL with `ImportError` or missing `memory_diff_since`.

- [ ] **Step 7: Implement Git diff helpers**

Add to `rightmemory/retrieve_context.py`:

```python
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
```

- [ ] **Step 8: Run all retrieve context tests**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
rtk git add rightmemory/retrieve_context.py tests/test_retrieve_context.py
rtk git commit -m "feat: render retrieve daily snapshot"
```

---

### Task 2: Add Retrieve Turn Store And Request Rendering

**Files:**
- Modify: `rightmemory/retrieve_context.py`
- Test: `tests/test_retrieve_context.py`

- [ ] **Step 1: Write failing tests for retrieve session state and request rendering**

Append to `tests/test_retrieve_context.py`:

```python
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry
from rightmemory.retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    format_recent_submitted_context_block,
)


class RetrieveContextRequestTests(unittest.TestCase):
    def test_request_text_places_snapshot_first_and_query_last(self):
        text = build_retrieve_request_text(
            snapshot_text="Daily memory snapshot\n===== MEMORY.md =====\n# Root\n",
            turns=[("find alpha", "alpha answer")],
            diff_block="# Memory changes since previous retrieve turn\n\n```diff\n+beta\n```",
            recent_block="Recent submitted memory\n\nremember gamma",
            query="find gamma",
        )

        self.assertTrue(text.startswith("Daily memory snapshot\n"))
        self.assertIn("# Prior retrieve conversation\n\nUser: find alpha\nAssistant: alpha answer", text)
        self.assertIn("# Memory changes since previous retrieve turn", text)
        self.assertIn("Recent submitted memory", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind gamma"))

    def test_request_text_omits_empty_diff_and_recent_blocks(self):
        text = build_retrieve_request_text(
            snapshot_text="Daily memory snapshot\n===== MEMORY.md =====\n# Root\n",
            turns=[],
            diff_block="",
            recent_block="",
            query="find root",
        )

        self.assertNotIn("Memory changes since previous retrieve turn", text)
        self.assertNotIn("Recent submitted memory", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind root"))

    def test_recent_submitted_context_block_omits_empty_entries(self):
        self.assertEqual(format_recent_submitted_context_block([]), "")
        block = format_recent_submitted_context_block(
            [
                RecentSubmittedMemoryEntry(
                    update_session_id="update-a",
                    candidate_id=1,
                    submitted_at="2026-06-18T00:00:00+00:00",
                    message="remember delta",
                )
            ]
        )
        self.assertTrue(block.startswith("# Recent submitted memory"))
        self.assertIn("remember delta", block)

    def test_retrieve_context_store_persists_turns_and_commit_cursor(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = RetrieveContextStore(root)

            state = store.load("retrieve-a")
            self.assertEqual(state.turns, [])
            self.assertIsNone(state.delivered_memory_commit)

            store.record_success("retrieve-a", query="find alpha", answer="alpha answer", memory_commit="abc123")
            state = store.load("retrieve-a")

        self.assertEqual(state.delivered_memory_commit, "abc123")
        self.assertEqual([(turn.query, turn.answer) for turn in state.turns], [("find alpha", "alpha answer")])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextRequestTests
```

Expected: FAIL with missing `RetrieveContextStore`.

- [ ] **Step 3: Implement request rendering and session state**

Append to `rightmemory/retrieve_context.py`:

```python
@dataclass(frozen=True)
class RetrieveTurn:
    query: str
    answer: str


@dataclass(frozen=True)
class RetrieveSessionState:
    session_id: str
    delivered_memory_commit: str | None = None
    turns: list[RetrieveTurn] = field(default_factory=list)


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
            if not isinstance(item, dict) or not isinstance(item.get("query"), str) or not isinstance(item.get("answer"), str):
                raise ValueError("retrieve context turn entries must contain query and answer strings")
            turns.append(RetrieveTurn(query=item["query"], answer=item["answer"]))
        return RetrieveSessionState(session_id=session_id, delivered_memory_commit=delivered, turns=turns)

    def record_success(self, session_id: str, *, query: str, answer: str, memory_commit: str | None) -> None:
        state = self.load(session_id)
        next_state = RetrieveSessionState(
            session_id=session_id,
            delivered_memory_commit=memory_commit,
            turns=[*state.turns, RetrieveTurn(query=query, answer=answer)],
        )
        self._write(next_state)

    def _state_path(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.json"

    def _write(self, state: RetrieveSessionState) -> None:
        data = {
            "session_id": state.session_id,
            "delivered_memory_commit": state.delivered_memory_commit,
            "turns": [asdict(turn) for turn in state.turns],
        }
        _write_json(self.memory_root, self._state_path(state.session_id), data)


def build_retrieve_request_text(
    *,
    snapshot_text: str,
    turns: list[tuple[str, str]] | list[RetrieveTurn],
    diff_block: str,
    recent_block: str,
    query: str,
) -> str:
    parts = [snapshot_text.rstrip()]
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
            f"[update session: {entry.update_session_id} | "
            f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
        )
        lines.extend(entry.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


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
```

- [ ] **Step 4: Run request tests**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextRequestTests
```

Expected: PASS.

- [ ] **Step 5: Run all retrieve context tests**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
rtk git add rightmemory/retrieve_context.py tests/test_retrieve_context.py
rtk git commit -m "feat: store retrieve context turns"
```

---

### Task 3: Integrate Context-First Retrieve Into Runtime

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing standalone runtime tests**

Add these tests near the existing retrieve recent-submitted tests in `tests/test_config.py`:

```python
    def test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n\nremembered root\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_session_turn("agent-session", "find root")
            second = runtime.run_session_turn("agent-session", "find again")

        self.assertEqual(first, "reply 1")
        self.assertEqual(second, "reply 2")
        self.assertTrue(runtime.agent.calls[0]["message"].startswith("Daily memory snapshot\n"))
        self.assertIn("===== MEMORY.md =====", runtime.agent.calls[0]["message"])
        self.assertTrue(runtime.agent.calls[0]["message"].rstrip().endswith("# Query\n\nfind root"))
        self.assertIsNone(runtime.agent.calls[0]["message_history"])
        self.assertIsNone(runtime.agent.calls[1]["message_history"])
        self.assertIn("# Prior retrieve conversation", runtime.agent.calls[1]["message"])
        self.assertIn("User: find root", runtime.agent.calls[1]["message"])
        self.assertIn("Assistant: reply 1", runtime.agent.calls[1]["message"])

        state_path = root / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["turns"], [{"query": "find root", "answer": "reply 1"}, {"query": "find again", "answer": "reply 2"}])
        self.assertNotIn("Daily memory snapshot", state_path.read_text(encoding="utf-8"))

    def test_retrieve_turn_does_not_record_context_state_after_failure(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                raise RuntimeError("model failed")

            runtime.agent.run_sync = run_sync
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("agent-session", "find root")

        self.assertFalse((root / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json").exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns \
  tests.test_config.ConfigTests.test_retrieve_turn_does_not_record_context_state_after_failure
```

Expected: FAIL because runtime still passes raw query and Pydantic message history.

- [ ] **Step 3: Implement runtime assembly**

Modify imports in `rightmemory/runtime.py`:

```python
from .retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    current_memory_head,
    format_memory_diff_block,
    format_recent_submitted_context_block,
    load_daily_snapshot,
    memory_diff_since,
)
```

Add a small dataclass near the constants:

```python
@dataclass(frozen=True)
class PreparedRetrieveTurn:
    message: str
    query: str
    recent_submitted_entries: list[RecentSubmittedMemoryEntry]
    memory_commit: str | None
```

Initialize the store in `RightMemoryRuntime.__init__`:

```python
self.retrieve_context = RetrieveContextStore(config.state_root)
```

Replace `_prepare_retrieve_message` with:

```python
    def _prepare_retrieve_turn(self, session_id: str, message: str) -> PreparedRetrieveTurn:
        if self.config.role != "retrieve":
            return PreparedRetrieveTurn(message, message, [], None)
        snapshot = load_daily_snapshot(self.config.memory_root)
        state = self.retrieve_context.load(session_id)
        current_commit = current_memory_head(self.config.memory_root)
        base_commit = state.delivered_memory_commit or snapshot.base_commit
        diff = memory_diff_since(self.config.memory_root, base_commit, current_commit)
        diff_block = format_memory_diff_block(diff)

        entries = collect_recent_submitted_memory(self.config.memory_root)
        if entries:
            entries = self.recent_submitted_delivery.new_entries(session_id, entries)
        recent_block = format_recent_submitted_context_block(entries)
        request = build_retrieve_request_text(
            snapshot_text=snapshot.text,
            turns=state.turns,
            diff_block=diff_block,
            recent_block=recent_block,
            query=message,
        )
        return PreparedRetrieveTurn(request, message, entries, current_commit)
```

Update call sites:

```python
prepared = self._prepare_retrieve_turn(session_id, message)
result = self.agent.run_sync(
    prepared.message,
    message_history=None if self.config.role == "retrieve" else history,
    model_settings=self._model_settings(),
    usage_limits=self._usage_limits(),
)
...
self._record_successful_retrieve_turn(session_id, prepared, output)
```

Add helper:

```python
    def _record_successful_retrieve_turn(self, session_id: str, prepared: PreparedRetrieveTurn, output: str) -> None:
        if self.config.role != "retrieve":
            return
        self.retrieve_context.record_success(
            session_id,
            query=prepared.query,
            answer=output,
            memory_commit=prepared.memory_commit,
        )
        self._record_recent_submitted_delivery(session_id, prepared.recent_submitted_entries)
```

For retrieve role, skip `session.save_json(self._dump_message_history(result))` because retrieve now uses `RetrieveContextStore`. Keep the old `MessageSessionStore` path for non-retrieve roles.

- [ ] **Step 4: Run standalone runtime tests**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns \
  tests.test_config.ConfigTests.test_retrieve_turn_does_not_record_context_state_after_failure
```

Expected: PASS.

- [ ] **Step 5: Update existing retrieve history and recent-submitted tests**

Change `test_run_turn_preserves_message_history` so it uses a non-retrieve role or rename it to assert retrieve renders prior turns in the request and passes `message_history=None`.

Change recent-submitted assertions so they expect the block after the snapshot and before `# Query`, not appended after the raw query. For example:

```python
message = runtime.agent.calls[0]["message"]
self.assertLess(message.index("# Recent submitted memory"), message.index("# Query"))
self.assertTrue(message.rstrip().endswith("# Query\n\nfind one"))
```

- [ ] **Step 6: Run the affected config tests**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_run_turn_preserves_message_history \
  tests.test_config.ConfigTests.test_retrieve_turn_appends_recent_submitted_memory_once_per_session \
  tests.test_config.ConfigTests.test_retrieve_turn_records_recent_submitted_delivery_after_success \
  tests.test_config.ConfigTests.test_retrieve_turn_does_not_record_recent_submitted_delivery_after_failure
```

Expected: PASS after assertion updates.

- [ ] **Step 7: Commit Task 3**

```bash
rtk git add rightmemory/runtime.py tests/test_config.py
rtk git commit -m "feat: assemble retrieve context prefix"
```

---

### Task 4: Add Same-Day Diff Delivery And Prefix Stability Tests

**Files:**
- Modify: `tests/test_config.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/retrieve_context.py`

- [ ] **Step 1: Write failing integration tests for diff cursor and prefix identity**

Add to `tests/test_config.py`:

```python
    def test_retrieve_appends_diff_only_when_memory_head_changes(self):
        root = Path(self.tempdir.name)
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "MEMORY.md").write_text("# Root {#root}\n\nfirst\n", encoding="utf-8")
        self._git(root, "add", "MEMORY.md")
        self._git(root, "commit", "-m", "initial memory")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find first")
            (root / "MEMORY.md").write_text("# Root {#root}\n\nsecond\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "update memory")
            runtime.run_session_turn("agent-session", "find second")
            runtime.run_session_turn("agent-session", "find third")

        second_message = runtime.agent.calls[1]["message"]
        third_message = runtime.agent.calls[2]["message"]
        self.assertIn("# Memory changes since previous retrieve turn", second_message)
        self.assertIn("+second", second_message)
        self.assertNotIn("# Memory changes since previous retrieve turn", third_message)

    def test_retrieve_request_prefix_is_byte_identical_before_first_volatile_block(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n\nstable\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            first_runtime = RightMemoryRuntime(config)
            first_runtime.run_session_turn("session-a", "find alpha")
            second_runtime = RightMemoryRuntime(config)
            second_runtime.run_session_turn("session-b", "find beta")

        first = first_runtime.agent.calls[0]["message"].split("# Query", 1)[0]
        second = second_runtime.agent.calls[0]["message"].split("# Query", 1)[0]
        self.assertEqual(first, second)
```

Add helper to `ConfigTests` if not already present:

```python
    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()
```

- [ ] **Step 2: Run tests to verify failures**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_retrieve_appends_diff_only_when_memory_head_changes \
  tests.test_config.ConfigTests.test_retrieve_request_prefix_is_byte_identical_before_first_volatile_block
```

Expected: FAIL until the runtime advances the memory cursor correctly.

- [ ] **Step 3: Fix cursor advancement if needed**

Ensure `_record_successful_retrieve_turn()` records the `prepared.memory_commit` after successful output and not before model completion. Ensure `_prepare_retrieve_turn()` uses `state.delivered_memory_commit or snapshot.base_commit` as its diff base.

If non-Git memory roots return `None` for both commits, `memory_diff_since()` returns an empty string and the diff block is omitted.

- [ ] **Step 4: Run diff and prefix tests**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_retrieve_appends_diff_only_when_memory_head_changes \
  tests.test_config.ConfigTests.test_retrieve_request_prefix_is_byte_identical_before_first_volatile_block
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
rtk git add rightmemory/runtime.py rightmemory/retrieve_context.py tests/test_config.py
rtk git commit -m "test: cover retrieve context prefix stability"
```

---

### Task 5: Add Typed Retrieve Tools

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for `read_skill` and `read_mf`**

Add to `tests/test_tools.py`:

```python
    def test_retrieve_read_skill_returns_skill_body_by_id(self):
        (self.root / "MEMORY_SKILL_alpha.md").write_text("# Alpha Skill\n\nUse alpha.\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_skill("alpha")

        self.assertIn("# Alpha Skill", result)
        self.assertIn("Use alpha.", result)

    def test_retrieve_read_skill_failure_lists_available_ids_without_paths(self):
        (self.root / "MEMORY_SKILL_beta.md").write_text("# Beta Skill\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_skill("alpha")

        self.assertIn("Skill not found: alpha", result)
        self.assertIn("Available skills:\n- beta", result)
        self.assertNotIn("MEMORY_SKILL_beta.md", result)

    def test_retrieve_read_mf_returns_whole_import_package_by_id(self):
        import_root = self.root / ".runtime" / "shared_views" / "imports" / "auth-api"
        import_root.mkdir(parents=True)
        (import_root / "MEMORY.md").write_text("# Auth API\n\nToken expiry.\n", encoding="utf-8")
        (import_root / "manifest.toml").write_text("view_id = \"auth-api\"\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("auth-api")

        self.assertIn("MF import: auth-api", result)
        self.assertIn("===== MEMORY.md =====", result)
        self.assertIn("Token expiry.", result)
        self.assertIn("===== manifest.toml =====", result)

    def test_retrieve_read_mf_failure_lists_available_ids_without_paths(self):
        (self.root / ".runtime" / "shared_views" / "imports" / "billing-api").mkdir(parents=True)
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("auth-api")

        self.assertIn("MF import not found: auth-api", result)
        self.assertIn("Available MF imports:\n- billing-api", result)
        self.assertNotIn(".runtime", result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
rtk python -m unittest \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_skill_returns_skill_body_by_id \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_skill_failure_lists_available_ids_without_paths \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_mf_returns_whole_import_package_by_id \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_mf_failure_lists_available_ids_without_paths
```

Expected: FAIL because tools are missing.

- [ ] **Step 3: Implement typed tools**

Add methods to `MemoryTools` in `rightmemory/tools.py` near the read helpers:

```python
    def read_skill(self, skill_id: str) -> str:
        """Read a MEMORY_SKILL body by S# id."""
        clean_id = self._validate_memory_reference_id(skill_id)
        relative = f"MEMORY_SKILL_{clean_id}.md"
        path = self.memory_root / relative
        if not path.is_file():
            return self._missing_skill_message(clean_id)
        return self._cap_command_output(self._read_text(path))

    def read_mf(self, mf_id: str) -> str:
        """Read a mirrored MF# import package by id."""
        clean_id = self._validate_memory_reference_id(mf_id)
        root = self.memory_root / RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX / clean_id
        if not root.is_dir():
            return self._missing_mf_message(clean_id)
        files = sorted(path for path in root.rglob("*") if path.is_file())
        if not files:
            return f"MF import is empty: {clean_id}\n\n{self._available_mf_imports_block()}"
        parts = [f"MF import: {clean_id}", ""]
        for path in files:
            relative = path.relative_to(root).as_posix()
            parts.append(f"===== {relative} =====")
            parts.append(self._read_text(path).rstrip())
            parts.append("")
        return self._cap_command_output("\n".join(parts).rstrip())

    def _validate_memory_reference_id(self, value: str) -> str:
        clean = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", clean):
            raise ValueError("id must contain only letters, numbers, dot, underscore, or dash")
        return clean

    def _missing_skill_message(self, skill_id: str) -> str:
        return f"Skill not found: {skill_id}\n\n{self._available_skills_block()}"

    def _available_skills_block(self) -> str:
        ids = []
        for path in sorted(self.memory_root.glob("MEMORY_SKILL_*.md")):
            if path.is_file():
                ids.append(path.stem.removeprefix("MEMORY_SKILL_"))
        if not ids:
            return "Available skills:\n- none"
        return "Available skills:\n" + "\n".join(f"- {item}" for item in ids)

    def _missing_mf_message(self, mf_id: str) -> str:
        return f"MF import not found: {mf_id}\n\n{self._available_mf_imports_block()}"

    def _available_mf_imports_block(self) -> str:
        imports_root = self.memory_root / RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX
        ids = sorted(path.name for path in imports_root.iterdir() if path.is_dir()) if imports_root.is_dir() else []
        if not ids:
            return "Available MF imports:\n- none"
        return "Available MF imports:\n" + "\n".join(f"- {item}" for item in ids)
```

- [ ] **Step 4: Run typed tool tests**

Run:

```bash
rtk python -m unittest \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_skill_returns_skill_body_by_id \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_skill_failure_lists_available_ids_without_paths \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_mf_returns_whole_import_package_by_id \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_mf_failure_lists_available_ids_without_paths
```

Expected: PASS.

- [ ] **Step 5: Write failing runtime tool-surface test**

Update `tests/test_config.py::test_retrieve_runtime_does_not_expose_shared_view_tool`:

```python
        tool_names = {tool.__name__ for tool in runtime._agent_tools()}
        self.assertEqual(tool_names, {"read_skill", "read_mf"})
        self.assertNotIn("retrieve_shared_view", tool_names)
        self.assertNotIn("read_command", tool_names)
        self.assertNotIn("grep", tool_names)
```

- [ ] **Step 6: Run tool-surface test to verify it fails**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool
```

Expected: FAIL because retrieve still exposes broad read/search tools.

- [ ] **Step 7: Expose only typed retrieve tools**

Modify `_agent_tools()` in `rightmemory/runtime.py`:

```python
        if self.config.role == "retrieve":
            return [
                self._agent_tool(self.tools.read_skill),
                self._agent_tool(self.tools.read_mf),
            ]
```

Place this branch before constructing the broad `read_tools` list or before returning it for retrieve.

- [ ] **Step 8: Run retrieve tool-surface test**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool
```

Expected: PASS.

- [ ] **Step 9: Commit Task 5**

```bash
rtk git add rightmemory/tools.py rightmemory/runtime.py tests/test_tools.py tests/test_config.py
rtk git commit -m "feat: add typed retrieve disclosure tools"
```

---

### Task 6: Rewrite Retrieve Prompts

**Files:**
- Modify: `rightmemory/prompts/retrieve.md`
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing prompt tests**

Update or add these assertions in `tests/test_config.py`:

```python
    def test_retrieve_prompt_uses_context_first_contract(self):
        instructions = build_instructions(Path("/memory"), "retrieve")

        self.assertIn("supplied daily memory snapshot", instructions)
        self.assertIn("read_skill", instructions)
        self.assertIn("read_mf", instructions)
        self.assertIn("MQ#", instructions)
        self.assertNotIn("Read `MEMORY.md` before retrieval", instructions)
        self.assertNotIn("read_command", instructions)
        self.assertNotIn("retrieve_shared_view", instructions)
        self.assertNotIn("rightmemory shared-view ask", instructions)
```

- [ ] **Step 2: Run prompt test to verify it fails**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_prompt_uses_context_first_contract
```

Expected: FAIL because current prompt still says to read `MEMORY.md` and mentions broad tools.

- [ ] **Step 3: Rewrite `rightmemory/prompts/retrieve.md`**

Replace the file with:

```markdown
# Retrieve Role

## Supplied Context

- The runtime supplies a daily memory snapshot before the caller query.
- Treat the supplied daily memory snapshot as baseline active memory.
- The runtime may append a memory diff block after the snapshot. Apply diff blocks in order: added lines are newer memory, removed lines are obsolete.
- The runtime may append a `Recent submitted memory` block. Treat those entries as unsettled short-term memory, not settled active memory.
- The current query is last and controls relevance.
- Do not read `MEMORY.md` during ordinary retrieval. Answer from supplied context unless a progressive-disclosure tool is needed.

## Progressive Disclosure

`S#` headings are memory skills backed by `MEMORY_SKILL_<slug>.md`.

When a relevant `S#` heading matches and the caller needs the full instruction body, call `read_skill(skill_id)`. Return skill bodies only when specifically useful.

`MF#` headings are mirrored file shared-view connections.

When a relevant `MF#` heading matches and mirrored provider context is needed, call `read_mf(mf_id)`. Keep external provenance clear in the answer.

`MQ#` headings are provider question shared-view connections.

When a relevant `MQ#` heading matches, report that provider-question context may help, including the local `mq_id` and local relationship context. Do not invent a suggested question and do not call provider ask commands from retrieve.

## Retrieval

- Use judgment to decide which nodes are strongly relevant to the caller's request. Consider direct matches, synonyms, abbreviations, related concepts, nearby heading context, and graph edges present in the supplied context.
- When returning task matches, also include strongly relevant user, workflow, or agent-behavior preferences that may apply to the caller's next action, even if the caller did not ask for preferences.
- There is no fixed hop count or result quota. Stop when more nodes stop adding signal.
- Return matched nodes and matched anchored headings as verbatim addressable lines when available: the whole heading line with `{#id}` / `{F#id}` / `{S#id}` / `{MF#id}` / `{MQ#id}` / edges, or the whole node line. Follow each with a one-line note explaining why it matched.
- After ordinary memory matches, include a separate `Open context questions` block for relevant questions from `# Open Context Questions`. Return question nodes verbatim and label them as questions, not settled memory.
- If a matched heading has direct body paragraphs, include those paragraphs after the heading line. They are part of the heading node. Do not include child nodes unless they independently match.
- If nothing is strongly relevant, reply with `no strong match` plus up to three weak candidates if any exist.
- Do not dump unrelated sections, summarize the whole snapshot, invent node ids, or rewrite memory descriptions in your own words.
```

- [ ] **Step 4: Narrow retrieve tool guidance in `prompt.py`**

Change the retrieve branch in `_tool_guidance()` to:

```python
    if role == "retrieve":
        return (
            "- Use `read_skill(skill_id)` only when a relevant `S#` heading needs its full skill body.\n"
            "- Use `read_mf(mf_id)` only when a relevant `MF#` heading needs mirrored provider context.\n"
            "- Retrieve does not call shared-view endpoints or provider-question ask commands directly."
        )
```

- [ ] **Step 5: Run prompt tests**

Run:

```bash
rtk python -m unittest \
  tests.test_config.ConfigTests.test_retrieve_prompt_uses_context_first_contract \
  tests.test_config.ConfigTests.test_retrieve_prompt_uses_mf_mq_schema_without_endpoint_tool \
  tests.test_config.ConfigTests.test_cli_agent_retrieve_prompt_mentions_mq_recommendation_without_ask_command
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```bash
rtk git add rightmemory/prompts/retrieve.md rightmemory/prompt.py tests/test_config.py
rtk git commit -m "docs: rewrite retrieve context prompt"
```

---

### Task 7: Align CLI-Agent Retrieve With Context-First Requests

**Files:**
- Modify: `rightmemory/agent_cli.py`
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing CLI-agent executor test**

Add to `tests/test_agent_cli.py`:

```python
    def test_retrieve_stateless_turn_does_not_save_provider_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
            )

            with patch("rightmemory.agent_cli._run_cli", return_value='{"type":"thread.started","thread_id":"thread-1"}\n{"item":{"type":"agent_message","text":"reply"}}\n'):
                result = executor.run_stateless_turn("snapshot\n\n# Query\n\nfind root")

            self.assertEqual(result, "reply")
            self.assertFalse((root / ".runtime" / "agent_cli_sessions" / "retrieve").exists())
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
rtk python -m unittest tests.test_agent_cli.AgentCliExecutorTests.test_retrieve_stateless_turn_does_not_save_provider_session
```

Expected: FAIL because `run_stateless_turn` does not exist.

- [ ] **Step 3: Implement stateless CLI-agent turn**

Add to `CliAgentExecutor` in `rightmemory/agent_cli.py`:

```python
    def run_stateless_turn(self, message: str) -> str:
        result = self._run_provider(
            message,
            provider_session_id=None,
            resume=False,
            rightmemory_session_id=NO_SESSION_RIGHTMEMORY_SESSION_ID,
        )
        return result.text
```

- [ ] **Step 4: Route retrieve CLI-agent runtime through stateless turn**

In `RightMemoryRuntime._run_session_cli_agent`, after preparing the retrieve turn:

```python
            if self.config.role == "retrieve":
                result = self.agent.run_stateless_turn(prepared.message)
                self._trace("model_finished", output=str(result))
                self._record_successful_retrieve_turn(session_id, prepared, str(result))
                return result
```

Keep existing `run_session_turn` behavior for non-retrieve roles.

- [ ] **Step 5: Update CLI-agent retrieve runtime test**

Update `test_cli_agent_retrieve_receives_recent_submitted_memory` in `tests/test_config.py`:

```python
        executor_class.return_value.run_stateless_turn.return_value = "cli reply"
        ...
        executor_class.return_value.run_stateless_turn.assert_called_once()
        (message,) = executor_class.return_value.run_stateless_turn.call_args.args
        self.assertTrue(message.startswith("Daily memory snapshot"))
        self.assertIn("# Recent submitted memory", message)
        self.assertLess(message.index("# Recent submitted memory"), message.index("# Query"))
```

- [ ] **Step 6: Run CLI-agent tests**

Run:

```bash
rtk python -m unittest \
  tests.test_agent_cli.AgentCliExecutorTests.test_retrieve_stateless_turn_does_not_save_provider_session \
  tests.test_config.ConfigTests.test_cli_agent_retrieve_receives_recent_submitted_memory
```

Expected: PASS.

- [ ] **Step 7: Commit Task 7**

```bash
rtk git add rightmemory/agent_cli.py rightmemory/runtime.py tests/test_agent_cli.py tests/test_config.py
rtk git commit -m "feat: run cli retrieve with explicit context"
```

---

### Task 8: Final Cleanup And Verification

**Files:**
- Inspect: `rightmemory/retrieve_context.py`
- Inspect: `rightmemory/runtime.py`
- Inspect: `rightmemory/tools.py`
- Inspect: `rightmemory/prompts/retrieve.md`
- Inspect: `rightmemory/prompt.py`
- Inspect: `rightmemory/agent_cli.py`
- Inspect: `tests/test_retrieve_context.py`
- Inspect: `tests/test_config.py`
- Inspect: `tests/test_tools.py`
- Inspect: `tests/test_agent_cli.py`

- [ ] **Step 1: Run focused retrieve-related tests**

Run:

```bash
rtk python -m unittest \
  tests.test_retrieve_context \
  tests.test_recent_submitted \
  tests.test_config.ConfigTests.test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns \
  tests.test_config.ConfigTests.test_retrieve_appends_diff_only_when_memory_head_changes \
  tests.test_config.ConfigTests.test_retrieve_request_prefix_is_byte_identical_before_first_volatile_block \
  tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_skill_returns_skill_body_by_id \
  tests.test_tools.MemoryToolsTests.test_retrieve_read_mf_returns_whole_import_package_by_id
```

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: no output and exit status `0`.

- [ ] **Step 3: Run full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS. Existing skipped tests remain skipped.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
rtk git status --short
rtk git diff --stat
```

Expected: only retrieve prefix-cache files and tests are modified. Pre-existing untracked `.worktree/` and `docs/problems.md` remain untracked and untouched.

- [ ] **Step 5: Commit verification fixes only when there are staged implementation corrections**

Run:

```bash
rtk git status --short
```

Expected after Task 7 with no verification edits: only pre-existing untracked
`.worktree/` and `docs/problems.md`.

When verification required implementation corrections, commit exactly those
corrections:

```bash
rtk git add rightmemory tests
rtk git commit -m "fix: finalize retrieve prefix cache"
```

When there are no implementation corrections, do not create an empty final
commit.

---

## Self-Review Notes

- Spec coverage: snapshot, same-day diffs, pending candidates, session delivery cursors, `read_skill`, `read_mf`, prompt rewrite, `MQ#` non-call behavior, and prefix-stability testing are covered.
- Scope: this plan does not implement structured graph-query tools or old `M#` compatibility.
- Type consistency: the plan uses `DailySnapshot`, `RetrieveContextStore`, `RetrieveSessionState`, `RetrieveTurn`, and `PreparedRetrieveTurn` consistently across tasks.
