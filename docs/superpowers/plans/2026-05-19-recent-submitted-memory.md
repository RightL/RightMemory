# Recent Submitted Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `rightmemory retrieve` see recent update submissions before the update role consolidates them into `MEMORY.md`.

**Architecture:** Add a focused runtime helper that collects submitted update candidates from `.runtime/async/update/`, formats them as `Recent submitted memory`, and tracks which candidate keys have been delivered to each retrieve session. Integrate that helper at the runtime turn boundary so standalone and cli-agent retrieve modes receive the same short-term memory overlay.

**Tech Stack:** Python standard library, `unittest`, existing RightMemory async update JSON state, existing runtime/session locking helpers.

---

## File Structure

- Create `rightmemory/recent_submitted.py`: collect recent update submissions, format the retriever-facing block, and track delivered candidate keys per retrieve session.
- Create `tests/test_recent_submitted.py`: unit tests for collection, formatting, delivery-delta tracking, malformed state errors, and memory-file safety.
- Modify `rightmemory/runtime.py`: append new recent-submitted candidates to retrieve turn input and record delivered keys after successful turns.
- Modify `tests/test_config.py`: runtime tests for standalone retrieve, failure behavior, and cli-agent retrieve.
- Modify `rightmemory/prompts/retrieve.md`: tell the retriever how to use and label `Recent submitted memory`.
- Modify `README.md`: document the runtime behavior near the async update command description.

---

### Task 1: Collect And Format Recent Submitted Memory

**Files:**
- Create: `rightmemory/recent_submitted.py`
- Create: `tests/test_recent_submitted.py`

- [ ] **Step 1: Write failing collection and formatting tests**

Create `tests/test_recent_submitted.py` with:

```python
import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.recent_submitted import (
    RecentSubmittedMemoryEntry,
    collect_recent_submitted_memory,
    format_recent_submitted_block,
)


class RecentSubmittedMemoryCollectionTests(unittest.TestCase):
    def test_collects_pending_and_current_batch_from_all_update_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_state(
                root,
                "update-a",
                pending=[
                    {
                        "id": 2,
                        "message": "remember second submitted item",
                        "submitted_at": "2026-05-19T00:02:00+00:00",
                    }
                ],
            )
            self._write_state(
                root,
                "update-b",
                current_batch=[
                    {
                        "id": 3,
                        "message": "remember active batch item",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    }
                ],
                pending=[
                    {
                        "id": 4,
                        "message": "remember later submitted item",
                        "submitted_at": "2026-05-19T00:01:00+00:00",
                    }
                ],
            )

            entries = collect_recent_submitted_memory(root)

        self.assertEqual(
            [entry.key for entry in entries],
            [
                "update-b:3:2026-05-19T00:00:00+00:00",
                "update-b:4:2026-05-19T00:01:00+00:00",
                "update-a:2:2026-05-19T00:02:00+00:00",
            ],
        )
        self.assertEqual(entries[0].update_session_id, "update-b")
        self.assertEqual(entries[0].candidate_id, 3)
        self.assertEqual(entries[0].message, "remember active batch item")

    def test_formats_recent_submitted_block_for_retriever(self):
        entries = [
            RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="remember that retriever sees submitted memory",
            )
        ]

        block = format_recent_submitted_block(entries)

        self.assertIn("Recent submitted memory", block)
        self.assertIn("not been consolidated into MEMORY.md yet", block)
        self.assertIn("[update session: update-a | candidate: 1 | submitted_at: 2026-05-19T00:00:00+00:00]", block)
        self.assertIn("remember that retriever sees submitted memory", block)

    def test_format_returns_empty_string_when_there_are_no_entries(self):
        self.assertEqual(format_recent_submitted_block([]), "")

    def test_collect_raises_for_malformed_update_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "async" / "update" / "broken.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "broken",
                        "role": "update",
                        "pending": [],
                        "current_batch": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                collect_recent_submitted_memory(root)

        self.assertIn("async update state must contain integer field: next_id", str(caught.exception))

    def _write_state(self, root: Path, session_id: str, *, pending=None, current_batch=None):
        state_path = root / ".runtime" / "async" / "update" / f"{session_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "session_id": session_id,
                    "role": "update",
                    "phase": "waiting",
                    "started_at": "2026-05-19T00:00:00+00:00",
                    "finished_at": None,
                    "pid": None,
                    "result": None,
                    "error": None,
                    "next_flush_at": "2026-05-19T01:00:00+00:00",
                    "current_batch": current_batch or [],
                    "pending": pending or [],
                    "next_id": 10,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify the missing module failure**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: FAIL with an import error for `rightmemory.recent_submitted`.

- [ ] **Step 3: Implement collection and formatting**

Create `rightmemory/recent_submitted.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore


RECENT_SUBMITTED_HEADER = "Recent submitted memory"
RECENT_SUBMITTED_INTRO = (
    "These are memory update submissions that have not been consolidated into MEMORY.md yet. "
    "Use them as short-term working memory when relevant."
)


@dataclass(frozen=True)
class RecentSubmittedMemoryEntry:
    update_session_id: str
    candidate_id: int
    submitted_at: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.update_session_id}:{self.candidate_id}:{self.submitted_at}"


def collect_recent_submitted_memory(memory_root: Path) -> list[RecentSubmittedMemoryEntry]:
    store = AsyncUpdateStore(memory_root, "update")
    if not store.root.exists():
        return []

    entries: list[RecentSubmittedMemoryEntry] = []
    for state_path in sorted(store.root.glob("*.json")):
        session_hint = state_path.stem
        with store._locked(session_hint):
            state = store._read_checked_locked(session_hint)
        if state.role != "update":
            raise ValueError(f"async update state role mismatch: expected update, got {state.role}")
        entries.extend(_entries_from_jobs(state, state.current_batch))
        entries.extend(_entries_from_jobs(state, state.pending))
    return sorted(
        entries,
        key=lambda entry: (entry.submitted_at, entry.update_session_id, entry.candidate_id),
    )


def format_recent_submitted_block(entries: list[RecentSubmittedMemoryEntry]) -> str:
    if not entries:
        return ""

    lines = [RECENT_SUBMITTED_HEADER, "", RECENT_SUBMITTED_INTRO, ""]
    for entry in entries:
        lines.append(
            f"[update session: {entry.update_session_id} | "
            f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
        )
        lines.extend(entry.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


def append_recent_submitted_memory(message: str, entries: list[RecentSubmittedMemoryEntry]) -> str:
    block = format_recent_submitted_block(entries)
    if not block:
        return message
    return f"{message.rstrip()}\n\n{block}"


def _entries_from_jobs(
    state: AsyncUpdateState,
    jobs: list[AsyncUpdateJob],
) -> list[RecentSubmittedMemoryEntry]:
    return [
        RecentSubmittedMemoryEntry(
            update_session_id=state.session_id,
            candidate_id=job.id,
            submitted_at=job.submitted_at,
            message=job.message,
        )
        for job in jobs
    ]
```

- [ ] **Step 4: Run collection tests**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/recent_submitted.py tests/test_recent_submitted.py
git commit -m "feat: collect recent submitted memory"
```

---

### Task 2: Track Delivered Candidates Per Retrieve Session

**Files:**
- Modify: `rightmemory/recent_submitted.py`
- Modify: `tests/test_recent_submitted.py`

- [ ] **Step 1: Add failing delivery-delta tests**

Append these tests to `RecentSubmittedMemoryCollectionTests` in `tests/test_recent_submitted.py`:

```python
    def test_delivery_store_returns_all_entries_then_session_delta(self):
        from rightmemory.recent_submitted import RecentSubmittedMemoryDeliveryStore

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = RecentSubmittedMemoryDeliveryStore(root)
            first = RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="first",
            )
            second = RecentSubmittedMemoryEntry(
                update_session_id="update-b",
                candidate_id=2,
                submitted_at="2026-05-19T00:01:00+00:00",
                message="second",
            )
            third = RecentSubmittedMemoryEntry(
                update_session_id="update-c",
                candidate_id=3,
                submitted_at="2026-05-19T00:02:00+00:00",
                message="third",
            )

            self.assertEqual(store.new_entries("retrieve-a", [first, second]), [first, second])
            store.record_delivered("retrieve-a", [first, second])

            self.assertEqual(store.new_entries("retrieve-a", [first, second, third]), [third])
            self.assertEqual(store.new_entries("retrieve-b", [first, second, third]), [first, second, third])

            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["session_id"], "retrieve-a")
        self.assertEqual(state["delivered"], [first.key, second.key])

    def test_delivery_store_does_not_touch_memory_files(self):
        from rightmemory.recent_submitted import RecentSubmittedMemoryDeliveryStore

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_path = root / "MEMORY.md"
            memory_path.write_text("# Memory\n", encoding="utf-8")
            store = RecentSubmittedMemoryDeliveryStore(root)
            entry = RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="first",
            )

            store.record_delivered("retrieve-a", [entry])

            self.assertEqual(memory_path.read_text(encoding="utf-8"), "# Memory\n")
            self.assertTrue((root / ".runtime" / ".gitignore").exists())

    def test_delivery_store_rejects_malformed_delivered_state(self):
        from rightmemory.recent_submitted import RecentSubmittedMemoryDeliveryStore

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"session_id": "retrieve-a", "delivered": [1]}), encoding="utf-8")
            store = RecentSubmittedMemoryDeliveryStore(root)

            with self.assertRaises(ValueError) as caught:
                store.new_entries("retrieve-a", [])

        self.assertIn("recent submitted delivery state must contain string delivered keys", str(caught.exception))
```

- [ ] **Step 2: Run tests to verify missing delivery store failure**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: FAIL with an import error for `RecentSubmittedMemoryDeliveryStore`.

- [ ] **Step 3: Replace `rightmemory/recent_submitted.py` with collector plus delivery store**

Replace the full file with:

```python
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore
from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


RECENT_SUBMITTED_HEADER = "Recent submitted memory"
RECENT_SUBMITTED_INTRO = (
    "These are memory update submissions that have not been consolidated into MEMORY.md yet. "
    "Use them as short-term working memory when relevant."
)


@dataclass(frozen=True)
class RecentSubmittedMemoryEntry:
    update_session_id: str
    candidate_id: int
    submitted_at: str
    message: str

    @property
    def key(self) -> str:
        return f"{self.update_session_id}:{self.candidate_id}:{self.submitted_at}"


class RecentSubmittedMemoryDeliveryStore:
    def __init__(self, memory_root: Path):
        self.root = memory_root / ".runtime" / "recent_submitted" / "retrieve"

    def new_entries(
        self,
        retrieve_session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> list[RecentSubmittedMemoryEntry]:
        with self._locked(retrieve_session_id):
            delivered = self._read_delivered_locked(retrieve_session_id)
        return [entry for entry in entries if entry.key not in delivered]

    def record_delivered(
        self,
        retrieve_session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> None:
        if not entries:
            return
        with self._locked(retrieve_session_id):
            delivered = self._read_delivered_locked(retrieve_session_id)
            delivered.update(entry.key for entry in entries)
            self._write_delivered_locked(retrieve_session_id, delivered)

    def _state_path(self, retrieve_session_id: str) -> Path:
        safe_id = _safe_session_id(retrieve_session_id)
        return self.root / f"{safe_id}.json"

    def _lock_path(self, retrieve_session_id: str) -> Path:
        safe_id = _safe_session_id(retrieve_session_id)
        return self.root / f"{safe_id}.lock"

    @contextmanager
    def _locked(self, retrieve_session_id: str):
        runtime_root = self.root.parent.parent
        _ensure_runtime_gitignore(runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(retrieve_session_id)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_delivered_locked(self, retrieve_session_id: str) -> set[str]:
        state_path = self._state_path(retrieve_session_id)
        if not state_path.exists():
            return set()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("session_id") != retrieve_session_id:
            raise ValueError("recent submitted delivery state session_id mismatch")
        delivered = data.get("delivered")
        if not isinstance(delivered, list) or any(not isinstance(key, str) for key in delivered):
            raise ValueError("recent submitted delivery state must contain string delivered keys")
        return set(delivered)

    def _write_delivered_locked(self, retrieve_session_id: str, delivered: set[str]) -> None:
        state_path = self._state_path(retrieve_session_id)
        tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {"session_id": retrieve_session_id, "delivered": sorted(delivered)},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        _fsync_directory(state_path.parent)


def collect_recent_submitted_memory(memory_root: Path) -> list[RecentSubmittedMemoryEntry]:
    store = AsyncUpdateStore(memory_root, "update")
    if not store.root.exists():
        return []

    entries: list[RecentSubmittedMemoryEntry] = []
    for state_path in sorted(store.root.glob("*.json")):
        session_hint = state_path.stem
        with store._locked(session_hint):
            state = store._read_checked_locked(session_hint)
        if state.role != "update":
            raise ValueError(f"async update state role mismatch: expected update, got {state.role}")
        entries.extend(_entries_from_jobs(state, state.current_batch))
        entries.extend(_entries_from_jobs(state, state.pending))
    return sorted(
        entries,
        key=lambda entry: (entry.submitted_at, entry.update_session_id, entry.candidate_id),
    )


def format_recent_submitted_block(entries: list[RecentSubmittedMemoryEntry]) -> str:
    if not entries:
        return ""

    lines = [RECENT_SUBMITTED_HEADER, "", RECENT_SUBMITTED_INTRO, ""]
    for entry in entries:
        lines.append(
            f"[update session: {entry.update_session_id} | "
            f"candidate: {entry.candidate_id} | submitted_at: {entry.submitted_at}]"
        )
        lines.extend(entry.message.splitlines() or [""])
        lines.append("")
    return "\n".join(lines).rstrip()


def append_recent_submitted_memory(message: str, entries: list[RecentSubmittedMemoryEntry]) -> str:
    block = format_recent_submitted_block(entries)
    if not block:
        return message
    return f"{message.rstrip()}\n\n{block}"


def _entries_from_jobs(
    state: AsyncUpdateState,
    jobs: list[AsyncUpdateJob],
) -> list[RecentSubmittedMemoryEntry]:
    return [
        RecentSubmittedMemoryEntry(
            update_session_id=state.session_id,
            candidate_id=job.id,
            submitted_at=job.submitted_at,
            message=job.message,
        )
        for job in jobs
    ]
```

- [ ] **Step 4: Run delivery tests**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/recent_submitted.py tests/test_recent_submitted.py
git commit -m "feat: track recent submitted memory delivery"
```

---

### Task 3: Inject Recent Submitted Memory Into Retrieve Runtime Turns

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing standalone and cli-agent runtime tests**

In `tests/test_config.py`, add this helper inside `RuntimeTests` before `_fake_pydantic_modules`:

```python
    def _write_async_update_state(self, session_id: str, *, pending=None, current_batch=None):
        state_path = Path(self.tempdir.name) / ".runtime" / "async" / "update" / f"{session_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "session_id": session_id,
                    "role": "update",
                    "phase": "waiting",
                    "started_at": "2026-05-19T00:00:00+00:00",
                    "finished_at": None,
                    "pid": None,
                    "result": None,
                    "error": None,
                    "next_flush_at": "2026-05-19T01:00:00+00:00",
                    "current_batch": current_batch or [],
                    "pending": pending or [],
                    "next_id": 10,
                }
            ),
            encoding="utf-8",
        )
```

Add these tests inside `RuntimeTests` near `test_run_session_turn_preserves_message_history_on_disk`:

```python
    def test_retrieve_turn_appends_recent_submitted_memory_once_per_session(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember first submitted item",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("retrieve-a", "find submitted memory")
            self._write_async_update_state(
                "update-a",
                pending=[
                    {
                        "id": 1,
                        "message": "remember first submitted item",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    },
                    {
                        "id": 2,
                        "message": "remember second submitted item",
                        "submitted_at": "2026-05-19T00:01:00+00:00",
                    },
                ],
            )
            runtime.run_session_turn("retrieve-a", "find submitted memory again")

        first_message = runtime.agent.calls[0]["message"]
        second_message = runtime.agent.calls[1]["message"]
        self.assertIn("find submitted memory", first_message)
        self.assertIn("Recent submitted memory", first_message)
        self.assertIn("remember first submitted item", first_message)
        self.assertNotIn("remember second submitted item", first_message)
        self.assertIn("find submitted memory again", second_message)
        self.assertIn("Recent submitted memory", second_message)
        self.assertNotIn("remember first submitted item", second_message)
        self.assertIn("remember second submitted item", second_message)

    def test_retrieve_turn_records_recent_submitted_delivery_after_success(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember delivered item",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("retrieve-a", "find delivered item")

        state_path = Path(self.tempdir.name) / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["delivered"], ["update-a:1:2026-05-19T00:00:00+00:00"])

    def test_retrieve_turn_does_not_record_recent_submitted_delivery_after_failure(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember retry item",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )
        failing_modules = self._fake_pydantic_modules()
        failing_modules["pydantic_ai"].Agent = self._failing_agent()

        with patch.dict("sys.modules", failing_modules):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("retrieve-a", "find retry item")

        state_path = Path(self.tempdir.name) / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
        self.assertFalse(state_path.exists())

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            retry_runtime = RightMemoryRuntime(config)
            retry_runtime.run_session_turn("retrieve-a", "find retry item")

        self.assertIn("remember retry item", retry_runtime.agent.calls[0]["message"])

    def test_cli_agent_retrieve_receives_recent_submitted_memory(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember cli submitted item",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_session_turn.return_value = "cli reply"
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("retrieve-a", "find cli item")

        self.assertEqual(result, "cli reply")
        executor_class.return_value.run_session_turn.assert_called_once()
        session_id, message = executor_class.return_value.run_session_turn.call_args.args
        self.assertEqual(session_id, "retrieve-a")
        self.assertIn("find cli item", message)
        self.assertIn("Recent submitted memory", message)
        self.assertIn("remember cli submitted item", message)
```

Also update existing retrieve runtime tests that call `run_turn` without a
temporary memory root, so the new collector reads the test sandbox rather than a
real user memory root:

```python
    def test_run_turn_preserves_message_history(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            model_kwargs={"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_turn("remember one")
            second = runtime.run_turn("what was that?")

        self.assertEqual(first, "reply 1")
        self.assertEqual(second, "reply 2")
        self.assertIsNone(runtime.agent.calls[0]["message_history"])
        self.assertEqual(runtime.agent.calls[1]["message_history"], ["message 1"])
        self.assertEqual(
            runtime.agent.calls[0]["model_settings"],
            {"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )
        self.assertEqual(runtime.agent.calls[0]["usage_limits"].request_limit, 100)

    def test_rejects_unsupported_model_kwargs(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            model_kwargs={"api_version": "2026-01-01"},
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with self.assertRaises(ValueError):
            runtime.run_turn("hello")
```

- [ ] **Step 2: Run runtime tests to verify missing integration**

Run:

```bash
python -m unittest \
  tests.test_config.RuntimeTests.test_retrieve_turn_appends_recent_submitted_memory_once_per_session \
  tests.test_config.RuntimeTests.test_retrieve_turn_records_recent_submitted_delivery_after_success \
  tests.test_config.RuntimeTests.test_retrieve_turn_does_not_record_recent_submitted_delivery_after_failure \
  tests.test_config.RuntimeTests.test_cli_agent_retrieve_receives_recent_submitted_memory
```

Expected: FAIL because retrieve messages do not include `Recent submitted memory` and no delivery state is written.

- [ ] **Step 3: Import recent-submitted helpers in `rightmemory/runtime.py`**

Add this import near the existing imports:

```python
from .recent_submitted import (
    RecentSubmittedMemoryDeliveryStore,
    RecentSubmittedMemoryEntry,
    append_recent_submitted_memory,
    collect_recent_submitted_memory,
)
```

- [ ] **Step 4: Initialize the delivery store**

In `RightMemoryRuntime.__init__`, after `self.sessions = MessageSessionStore(config.memory_root, config.role)`, add:

```python
        self.recent_submitted_delivery = RecentSubmittedMemoryDeliveryStore(config.memory_root)
```

- [ ] **Step 5: Add helper methods to `RightMemoryRuntime`**

Add these methods before `_run_session_model`:

```python
    def _prepare_retrieve_message(
        self,
        session_id: str,
        message: str,
    ) -> tuple[str, list[RecentSubmittedMemoryEntry]]:
        if self.config.role != "retrieve":
            return message, []
        entries = collect_recent_submitted_memory(self.config.memory_root)
        if not entries:
            return message, []
        new_entries = self.recent_submitted_delivery.new_entries(session_id, entries)
        if not new_entries:
            return message, []
        return append_recent_submitted_memory(message, new_entries), new_entries

    def _record_recent_submitted_delivery(
        self,
        session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> None:
        if self.config.role != "retrieve" or not entries:
            return
        self.recent_submitted_delivery.record_delivered(session_id, entries)
```

- [ ] **Step 6: Apply the helper in standalone `run_turn`**

In the standalone branch of `run_turn`, replace the `agent.run_sync` call setup with this shape:

```python
        prepared_message, recent_entries = self._prepare_retrieve_message(
            NO_SESSION_RIGHTMEMORY_SESSION_ID,
            message,
        )
        result, post_sync = self._run_locked_turn(
            lambda: self.agent.run_sync(
                prepared_message,
                message_history=self._message_history or None,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
        )
        self._store_message_history_from_result(result)
        self._record_recent_submitted_delivery(NO_SESSION_RIGHTMEMORY_SESSION_ID, recent_entries)
        if post_sync is not None:
            self._run_sync_reconciler(post_sync)
        return self._result_output(result)
```

Keep the existing cli-agent `run_turn` branch as it is; it flows through `_run_session_cli_agent`.

- [ ] **Step 7: Apply the helper in `_run_session_model`**

Inside `_run_session_model`, after `self._trace("history_loaded", message_count=len(history or []))`, add:

```python
            prepared_message, recent_entries = self._prepare_retrieve_message(session_id, message)
```

Then change the model call to use `prepared_message`:

```python
            result = self.agent.run_sync(
                prepared_message,
                message_history=history,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
```

After `session.save_json(self._dump_message_history(result))`, add:

```python
            self._record_recent_submitted_delivery(session_id, recent_entries)
```

- [ ] **Step 8: Apply the helper in `_run_session_cli_agent`**

Inside `_run_session_cli_agent`, after `with self.sessions.locked(session_id):`, add:

```python
            prepared_message, recent_entries = self._prepare_retrieve_message(session_id, message)
```

Then change the agent call and record delivery:

```python
            result = self.agent.run_session_turn(session_id, prepared_message)
            self._trace("model_finished", output=str(result))
            self._record_recent_submitted_delivery(session_id, recent_entries)
            return result
```

- [ ] **Step 9: Run runtime tests**

Run:

```bash
python -m unittest \
  tests.test_config.RuntimeTests.test_cli_agent_runtime_uses_executor_without_pydantic_agent \
  tests.test_config.RuntimeTests.test_cli_agent_run_turn_uses_reserved_session_lock \
  tests.test_config.RuntimeTests.test_run_turn_preserves_message_history \
  tests.test_config.RuntimeTests.test_run_session_turn_preserves_message_history_on_disk \
  tests.test_config.RuntimeTests.test_retrieve_turn_appends_recent_submitted_memory_once_per_session \
  tests.test_config.RuntimeTests.test_retrieve_turn_records_recent_submitted_delivery_after_success \
  tests.test_config.RuntimeTests.test_retrieve_turn_does_not_record_recent_submitted_delivery_after_failure \
  tests.test_config.RuntimeTests.test_cli_agent_retrieve_receives_recent_submitted_memory
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add rightmemory/runtime.py tests/test_config.py
git commit -m "feat: inject recent submitted memory into retrieval"
```

---

### Task 4: Align Retriever Prompt And README

**Files:**
- Modify: `rightmemory/prompts/retrieve.md`
- Modify: `README.md`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing prompt assertions**

In `PromptTests.test_retrieve_prompt_has_role_prompt_and_retrieve_command_behavior`, after `self.assertIn("Retrieve Role", prompt)`, add:

```python
        self.assertIn("Recent submitted memory", prompt)
        self.assertIn("short-term working memory", prompt)
```

- [ ] **Step 2: Run prompt test to verify missing guidance**

Run:

```bash
python -m unittest tests.test_config.PromptTests.test_retrieve_prompt_has_role_prompt_and_retrieve_command_behavior
```

Expected: FAIL because the retrieve prompt does not mention `Recent submitted memory`.

- [ ] **Step 3: Update `rightmemory/prompts/retrieve.md`**

In `rightmemory/prompts/retrieve.md`, add this section between `## Sources And Schema` and `## Retrieval`:

```markdown
## Recent Submitted Memory

- The runtime may append a `Recent submitted memory` block to the caller message.
- These entries are memory update submissions that have not been consolidated into `MEMORY.md` yet.
- Use them as short-term working memory when they are relevant to the retrieval request.
- When returning one of these entries, label it as recent submitted memory instead of inventing a graph node id or treating it as settled memory content.
```

- [ ] **Step 4: Update README command runtime notes**

In `README.md`, replace the async update bullet under `## Command Runtime` with:

```markdown
- Async `update submit` calls for the same `--session` accumulate as pending candidates. The worker waits one hour from the latest submit, then sends the pending candidates to the update role as one batch; `pull` reports phase, pending candidates, current batch, and timing. Retrieval receives not-yet-consolidated submissions as `Recent submitted memory` and uses a per-retrieve-session delta so repeated retrieve turns do not keep re-sending the same short-term entries.
```

- [ ] **Step 5: Run prompt test**

Run:

```bash
python -m unittest tests.test_config.PromptTests.test_retrieve_prompt_has_role_prompt_and_retrieve_command_behavior
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/prompts/retrieve.md README.md tests/test_config.py
git commit -m "docs: explain recent submitted memory retrieval"
```

---

### Task 5: Final Verification

**Files:**
- No new code changes expected.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
python -m unittest tests.test_recent_submitted tests.test_async_update tests.test_config.RuntimeTests tests.test_config.PromptTests
```

Expected: PASS.

- [ ] **Step 2: Run syntax checks**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: exits with status 0 and no output.

- [ ] **Step 3: Run full test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 4: Inspect final git status**

Run:

```bash
git status --short
```

Expected: shows no tracked implementation changes left unstaged. Existing unrelated untracked notes may remain:

```text
?? docs/path-location-memory-note.md
?? docs/superpowers/plans/2026-05-19-user-context-memory.md
?? docs/user-profile-goal-memory-note.md
```

- [ ] **Step 5: Summarize outcome**

Report:

```text
Implemented recent submitted memory retrieval.
Verified with:
- python -m unittest tests.test_recent_submitted tests.test_async_update tests.test_config.RuntimeTests tests.test_config.PromptTests
- python -m compileall -q rightmemory tests
- python -m unittest discover -s tests
```
