# Cross-Session Update Batching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Batch async update candidates across multiple session ids with one global worker while keeping the public per-session `submit`, `pull`, `undo`, and retrieve behavior unchanged.

**Architecture:** Keep per-session async update JSON files as the durable queue and caller-facing status. Add `[update.async]` config, one global worker state/lock, cross-session batch selection by eligible candidate count, and a private `rightmemory update _async-worker` entry point that runs update turns with synthetic batch session ids.

**Tech Stack:** Python standard library, dataclasses, file locks with `fcntl`, subprocess worker startup, `unittest`, existing RightMemory runtime and isolated-write path.

---

## Scope Check

This is one subsystem: async update scheduling and worker execution. It touches config parsing, async update state management, CLI dispatch, retrieval compatibility, docs, and tests, but all changes serve the same runtime behavior.

## File Structure

- Modify `rightmemory/config.py`: add `AsyncUpdateConfig`, defaults, `load_async_update_config()`, and allow `[update.async]`.
- Modify `rightmemory/async_update.py`: keep per-session state, add global worker state/lock, cross-session eligibility, batch selection, synthetic batch ids, and global worker loop.
- Modify `rightmemory/cli.py`: load async update config for submit and worker, replace `_submitted-worker --session <id>` with `_async-worker`.
- Modify `rightmemory/recent_submitted.py`: use a store helper for session state paths so worker state files are never treated as update sessions.
- Modify `README.md`: document `[update.async]` and cross-session internal batching.
- Modify `DESIGN_NOTES.md`: update the batched command updates note.
- Modify `tests/test_config.py`: config tests.
- Modify `tests/test_async_update.py`: unit tests for global worker state, selection, success, failure, and recovery.
- Modify `tests/test_cli.py`: CLI integration tests for `_async-worker`, single global worker startup, trigger increments, and synthetic sessions.
- Modify `tests/test_recent_submitted.py`: compatibility test proving recent submitted memory ignores worker metadata and still includes pending/in-flight candidates.

## Task 1: Add Async Update Config

**Files:**
- Modify: `rightmemory/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

In `tests/test_config.py`, add `load_async_update_config` to the existing `rightmemory.config` import list:

```python
from rightmemory.config import (
    AgentCliConfig,
    PrunerConfig,
    RuntimeConfig,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
```

Add these tests in `ConfigTests` near the review and dreamer config tests:

```python
    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_defaults(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_async_update_config()

        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))
        self.assertEqual(config.target_batch_candidates, 15)
        self.assertEqual(config.max_wait_seconds, 86400)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_parses_custom_values(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "openai/update"

            [update.async]
            target_batch_candidates = 22
            max_wait_seconds = 7200
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            async_config = load_async_update_config()
            runtime_config = load_config("update")

        self.assertEqual(async_config.target_batch_candidates, 22)
        self.assertEqual(async_config.max_wait_seconds, 7200)
        self.assertEqual(runtime_config.model_id, "openai/update")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_rejects_invalid_values(self):
        cases = [
            ("target_batch_candidates = 0", "[update.async].target_batch_candidates must be a positive integer"),
            ("target_batch_candidates = true", "[update.async].target_batch_candidates must be a positive integer"),
            ("max_wait_seconds = 0", "[update.async].max_wait_seconds must be a positive integer"),
            ("max_wait_seconds = true", "[update.async].max_wait_seconds must be a positive integer"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                config_path = self._write_config(
                    f"""
                    [update.async]
                    {body}
                    """
                )

                with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
                    with self.assertRaises(ValueError) as caught:
                        load_async_update_config()

                self.assertIn(message, str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_rejects_unknown_key(self):
        config_path = self._write_config(
            """
            [update.async]
            target_batch_candidates = 15
            extra = 1
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_async_update_config()

        self.assertIn("unsupported [update.async] config key(s): extra", str(caught.exception))
```

- [ ] **Step 2: Run config tests and verify failure**

Run:

```bash
python -m unittest tests.test_config
```

Expected: FAIL because `load_async_update_config` does not exist and `[update.async]` is not accepted.

- [ ] **Step 3: Implement async update config parsing**

In `rightmemory/config.py`, add defaults near the other default constants:

```python
DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES = 15
DEFAULT_UPDATE_MAX_WAIT_SECONDS = 24 * 60 * 60
```

Add this dataclass after `ReviewConfig`:

```python
@dataclass(frozen=True)
class AsyncUpdateConfig:
    memory_root: Path = MEMORY_ROOT
    target_batch_candidates: int = DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES
    max_wait_seconds: int = DEFAULT_UPDATE_MAX_WAIT_SECONDS
```

Add this loader after `load_review_config()`:

```python
def load_async_update_config() -> AsyncUpdateConfig:
    data = _load_raw_config()

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("update", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[update] must be a TOML table")
    _reject_unknown_keys(section, {"model", "agent_cli", "async"}, "[update]")

    async_section = section.get("async", {})
    if async_section is None:
        async_section = {}
    if not isinstance(async_section, dict):
        raise ValueError("[update.async] must be a TOML table")
    _reject_unknown_keys(async_section, {"target_batch_candidates", "max_wait_seconds"}, "[update.async]")

    return AsyncUpdateConfig(
        memory_root=MEMORY_ROOT,
        target_batch_candidates=_positive_integer(
            async_section,
            "target_batch_candidates",
            DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES,
            "[update.async]",
        ),
        max_wait_seconds=_positive_integer(
            async_section,
            "max_wait_seconds",
            DEFAULT_UPDATE_MAX_WAIT_SECONDS,
            "[update.async]",
        ),
    )
```

Update `_allowed_role_keys()` so `[update.async]` is accepted by normal runtime config parsing:

```python
def _allowed_role_keys(role: str) -> set[str]:
    allowed = {"model", "agent_cli"}
    if role == "dreamer":
        allowed.add("watch")
    if role == "pruner":
        allowed.update({"generation_commits", "revival_grace_checkpoints"})
    if role == "update":
        allowed.add("async")
    return allowed
```

- [ ] **Step 4: Run config tests and verify pass**

Run:

```bash
python -m unittest tests.test_config
```

Expected: PASS.

- [ ] **Step 5: Commit config support**

```bash
git add rightmemory/config.py tests/test_config.py
git commit -m "feat: add async update batching config"
```

## Task 2: Add Global Worker State And Session-State Helpers

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Write failing worker-state tests**

In `tests/test_async_update.py`, add these tests to `AsyncUpdateStateTests`:

```python
    def test_worker_command_uses_global_async_worker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")

            command = store._worker_command()

        self.assertEqual(command[-2:], ["update", "_async-worker"])
        self.assertNotIn("--session", command)

    def test_worker_state_round_trips_and_detects_live_pid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=4242,
                    batch_id="update-batch-test",
                    session_ids=["agent-1", "agent-2"],
                    error=None,
                )

            with patch("rightmemory.async_update._process_exists", return_value=True):
                active = store._active_worker_pid()

        self.assertEqual(active, 4242)

    def test_session_state_paths_exclude_worker_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2026-05-15T00:00:00+00:00",
                    pending=[_job(1, "first")],
                    next_id=2,
                ),
            )
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=4242,
                    batch_id=None,
                    session_ids=[],
                    error=None,
                )

            paths = [path.name for path in store._session_state_paths()]

        self.assertEqual(paths, ["agent-1.json"])
```

- [ ] **Step 2: Run async update tests and verify failure**

Run:

```bash
python -m unittest tests.test_async_update
```

Expected: FAIL because `_worker_command()` still requires a session id and worker helpers do not exist.

- [ ] **Step 3: Add worker state paths and helpers**

In `rightmemory/async_update.py`, change the `AsyncUpdateStore.__init__()` body to add a worker directory:

```python
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "async" / role
        self.worker_root = self.root / "_worker"
```

Replace `_worker_command(self, session_id: str)` with:

```python
    def _worker_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "rightmemory.cli",
            self.role,
            "_async-worker",
        ]
```

Add these helpers inside `AsyncUpdateStore` after `_worker_command()`:

```python
    def _worker_state_path(self) -> Path:
        return self.worker_root / "state.json"

    def _worker_lock_path(self) -> Path:
        return self.worker_root / "state.lock"

    @contextmanager
    def _worker_locked(self):
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.worker_root.mkdir(parents=True, exist_ok=True)
        lock_path = self._worker_lock_path()
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_worker_locked(self) -> dict[str, object]:
        path = self._worker_state_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("async update worker state must be a JSON object")
        return data

    def _write_worker_locked(
        self,
        *,
        status: str,
        pid: int | None,
        batch_id: str | None,
        session_ids: list[str],
        error: str | None,
    ) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.worker_root.mkdir(parents=True, exist_ok=True)
        state_path = self._worker_state_path()
        tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            {
                "status": status,
                "pid": pid,
                "started_at": _now(),
                "batch_id": batch_id,
                "session_ids": session_ids,
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, state_path)
        _fsync_directory(state_path.parent)

    def _clear_worker_locked(self) -> None:
        self._write_worker_locked(status="idle", pid=None, batch_id=None, session_ids=[], error=None)

    def _active_worker_pid(self) -> int | None:
        with self._worker_locked():
            state = self._read_worker_locked()
            pid = state.get("pid")
            if not isinstance(pid, int):
                return None
            if _process_exists(pid):
                return pid
            self._clear_worker_locked()
            return None

    def _session_state_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(path for path in self.root.glob("*.json") if path.is_file())
```

- [ ] **Step 4: Run the new worker helper tests and verify pass**

Run:

```bash
python -m unittest tests.test_async_update.AsyncUpdateStateTests.test_worker_command_uses_global_async_worker
python -m unittest tests.test_async_update.AsyncUpdateStateTests.test_worker_state_round_trips_and_detects_live_pid
python -m unittest tests.test_async_update.AsyncUpdateStateTests.test_session_state_paths_exclude_worker_state
```

Expected: PASS for each targeted helper test.

- [ ] **Step 5: Commit helper work**

Run:

```bash
git add rightmemory/async_update.py tests/test_async_update.py
git commit -m "feat: add async update global worker state"
```

## Task 3: Start One Global Worker From Submit

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Update submit tests for one global worker**

In `tests/test_async_update.py`, add this test:

```python
    def test_submit_starts_only_one_global_worker_for_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            process = Mock(pid=4242)

            with patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen:
                first = store.submit("agent-1", "first")
                second = store.submit("agent-2", "second")

        popen.assert_called_once()
        self.assertEqual(first.status, "running")
        self.assertEqual(second.status, "running")
        self.assertEqual(first.phase, "waiting")
        self.assertEqual(second.phase, "waiting")
        self.assertEqual([job.message for job in first.pending], ["first"])
        self.assertEqual([job.message for job in second.pending], ["second"])
```

In the existing `test_submit_after_failed_state_preserves_pending_order_and_starts_worker`, replace the per-session
pid assertion:

```python
        self.assertEqual(state.pid, 4242)
```

with a global worker-state assertion:

```python
        with store._worker_locked():
            worker = store._read_worker_locked()
        self.assertEqual(worker["pid"], 4242)
```

In `tests/test_cli.py`, update `test_main_cancels_pending_update_without_building_runtime` so the two submits use two different sessions and still assert one worker:

```python
                first = main(["update", "submit", "--session", "agent-1", "first"])
                second = main(["update", "submit", "--session", "agent-2", "second"])
                undo = main(["update", "undo", "--session", "agent-1", "1"])
                state = AsyncUpdateStore(memory_root, "update").read("agent-1")
```

Keep the existing assertions that `first == 0`, `second == 0`, `undo == 0`, and
`popen.call_count == 1`. Replace the final pending assertion with:

```python
        self.assertEqual([job.id for job in state.pending], [])
```

Update the stdout assertions to match the canceled candidate:

```python
        output = stdout.getvalue()
        self.assertIn("canceled pending candidate: 1", output)
        self.assertIn("pending: 0", output)
```

In `test_pull_marks_dead_worker_failed_and_keeps_pending_updates`, replace the exact pid error assertion:

```python
        self.assertIn("error: worker process exited before writing result: pid 123", output)
```

with:

```python
        self.assertIn("error: worker process exited before writing result", output)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli
```

Expected: FAIL because `submit()` still uses per-session worker liveness and per-session `_worker_command()`.

- [ ] **Step 3: Replace per-session worker startup with global startup**

In `rightmemory/async_update.py`, replace `submit()` with:

```python
    def submit(self, session_id: str, message: str) -> AsyncUpdateState:
        now = _now_dt()
        with self._locked(session_id):
            current = self._read_checked_locked(session_id)
            job = AsyncUpdateJob(id=current.next_id, message=message, submitted_at=_format_time(now))
            worker_pid = self._active_worker_pid()
            state = self._enqueue_locked(current, job, now=now, worker_pid=worker_pid)
            self._write(session_id, state)

        self._start_worker_if_needed(session_id)
        return self.read(session_id)
```

Replace `_enqueue_locked()` with:

```python
    def _enqueue_locked(
        self,
        state: AsyncUpdateState,
        job: AsyncUpdateJob,
        *,
        now: datetime,
        worker_pid: int | None,
    ) -> AsyncUpdateState:
        pending = [*state.current_batch, *state.pending, job]
        next_id = max(state.next_id, job.id + 1)
        next_flush_at = _format_time(now + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS))
        return AsyncUpdateState(
            status="running",
            session_id=state.session_id,
            role=self.role,
            phase="waiting",
            started_at=state.started_at or _format_time(now),
            pid=worker_pid,
            next_flush_at=next_flush_at,
            pending=pending,
            next_id=next_id,
        )
```

Add this method after `_active_worker_pid()`:

```python
    def _start_worker_if_needed(self, session_id: str) -> None:
        with self._worker_locked():
            state = self._read_worker_locked()
            pid = state.get("pid")
            if isinstance(pid, int) and _process_exists(pid):
                return
            try:
                process = subprocess.Popen(
                    self._worker_command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=self.memory_root,
                    env=os.environ.copy(),
                )
            except Exception as exc:
                self._fail(session_id, str(exc))
                raise
            self._write_worker_locked(
                status="running",
                pid=process.pid,
                batch_id=None,
                session_ids=[],
                error=None,
            )
```

Replace `_has_active_worker()` calls by removing `_has_active_worker()` entirely if nothing uses it after this change.

Update `_read_checked_locked()` so dead global workers recover running states:

```python
    def _read_checked_locked(self, session_id: str) -> AsyncUpdateState:
        state = self._read_raw(session_id)
        if state.status == "running" and self._active_worker_pid() is None:
            return self._fail_locked(session_id, "worker process exited before writing result")
        return state
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli
```

Expected: PASS for existing submit behavior updated to one global worker. If a test expects per-session pid text, update it to assert `status: running` and avoid requiring a per-session worker pid.

- [ ] **Step 5: Commit global submit behavior**

```bash
git add rightmemory/async_update.py tests/test_async_update.py tests/test_cli.py
git commit -m "feat: start one async update worker"
```

## Task 4: Implement Cross-Session Batch Selection And Execution

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Write failing batch selection and execution tests**

In `tests/test_async_update.py`, add these tests:

```python
    def test_global_worker_batches_multiple_eligible_sessions_by_candidate_count(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    started_at="2026-05-15T00:00:00+00:00",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[_job(1, "a1"), _job(2, "a2")],
                    next_id=3,
                ),
            )
            store._write(
                "agent-2",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-2",
                    role="update",
                    phase="waiting",
                    started_at="2026-05-15T00:00:00+00:00",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[_job(1, "b1")],
                    next_id=2,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_session_id, message: calls.append((batch_session_id, message)) or "ok",
                target_batch_candidates=3,
                max_wait_seconds=86400,
                on_batch_success=calls.append,
            )
            first = store.read("agent-1")
            second = store.read("agent-2")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(first.pending, [])
        self.assertEqual(second.pending, [])
        self.assertEqual(len([call for call in calls if isinstance(call, tuple)]), 1)
        batch_session_id, message = [call for call in calls if isinstance(call, tuple)][0]
        self.assertTrue(batch_session_id.startswith("update-batch-"))
        self.assertIn("[update session: agent-1 | candidate: 1", message)
        self.assertIn("[update session: agent-1 | candidate: 2", message)
        self.assertIn("[update session: agent-2 | candidate: 1", message)
        self.assertIn("a1", message)
        self.assertIn("b1", message)
        self.assertIn(3, calls)

    def test_global_worker_includes_whole_session_when_it_overshoots_target(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[_job(1, "a1"), _job(2, "a2")],
                    next_id=3,
                ),
            )
            store._write(
                "agent-2",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-2",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[_job(1, "b1"), _job(2, "b2")],
                    next_id=3,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_session_id, message: calls.append(message) or "ok",
                target_batch_candidates=3,
                max_wait_seconds=86400,
            )

        self.assertEqual(result.processed, 4)
        self.assertEqual(len(calls), 1)
        self.assertIn("b2", calls[0])

    def test_global_worker_waits_below_target_until_max_wait_fallback(self):
        slept = []
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2026-05-15T00:00:00+00:00",
                    pending=[_job(1, "a1")],
                    next_id=2,
                ),
            )

            with patch("rightmemory.async_update._now_dt", side_effect=[
                _dt("2026-05-15T00:10:00+00:00"),
                _dt("2026-05-16T00:00:00+00:00"),
            ]):
                result = store.run_pending_batches(
                    lambda batch_session_id, message: calls.append(message) or "ok",
                    target_batch_candidates=15,
                    max_wait_seconds=86400,
                    sleep_until=slept.append,
                )

        self.assertEqual(len(slept), 1)
        self.assertEqual(slept[0], _dt("2026-05-16T00:00:00+00:00"))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(calls), 1)

    def test_global_worker_failure_returns_all_current_batches_to_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            for session_id, message in (("agent-1", "a1"), ("agent-2", "b1")):
                store._write(
                    session_id,
                    AsyncUpdateState(
                        status="running",
                        session_id=session_id,
                        role="update",
                        phase="waiting",
                        next_flush_at="2000-01-01T00:00:00+00:00",
                        pending=[_job(1, message)],
                        next_id=2,
                    ),
                )

            result = store.run_pending_batches(
                Mock(side_effect=RuntimeError("isolated failure")),
                target_batch_candidates=2,
                max_wait_seconds=86400,
            )
            first = store.read("agent-1")
            second = store.read("agent-2")

        self.assertEqual(result.status, "failed")
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        self.assertEqual([job.message for job in first.pending], ["a1"])
        self.assertEqual([job.message for job in second.pending], ["b1"])
        self.assertEqual(first.current_batch, [])
        self.assertEqual(second.current_batch, [])
        self.assertEqual(first.error, "isolated failure")
        self.assertEqual(second.error, "isolated failure")
```

Add this helper near `_job()` at the bottom of `tests/test_async_update.py`:

```python
def _dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
```

Replace the existing per-session `test_run_pending_batches_failure_returns_current_batch_to_pending` and
`test_run_pending_batches_success_calls_callback_with_candidate_count` tests with the cross-session tests above.
Those old tests exercise the removed one-session worker signature and should not remain after this task.

- [ ] **Step 2: Run async update tests and verify failure**

Run:

```bash
python -m unittest tests.test_async_update
```

Expected: FAIL because `run_pending_batches()` still accepts one session id and runs one queue.

- [ ] **Step 3: Add batch result and origin formatting helpers**

In `rightmemory/async_update.py`, add `hashlib` to imports:

```python
import hashlib
```

Add these dataclasses after `AsyncUpdateState`:

```python
@dataclass(frozen=True)
class AsyncUpdateSessionBatch:
    session_id: str
    ready_at: datetime
    jobs: list[AsyncUpdateJob]


@dataclass(frozen=True)
class AsyncUpdateWorkerResult:
    status: str
    processed: int = 0
    failed: bool = False
```

Replace `_format_batch_message()` with:

```python
def _format_batch_message(batches: list[AsyncUpdateSessionBatch]) -> str:
    lines = [
        "Process the following submitted memory update candidates as one batch.",
        "Use the standalone update instructions to decide what should become durable memory.",
        "",
        "Candidates:",
    ]
    for batch in batches:
        for job in batch.jobs:
            lines.append(
                f"[update session: {batch.session_id} | "
                f"candidate: {job.id} | submitted_at: {job.submitted_at}]"
            )
            lines.extend(job.message.splitlines() or [""])
            lines.append("")
    return "\n".join(lines).rstrip()
```

Add synthetic batch id helpers near `_format_batch_message()`:

```python
def _batch_session_id(batches: list[AsyncUpdateSessionBatch]) -> str:
    parts = []
    for batch in batches:
        for job in batch.jobs:
            parts.append(f"{batch.session_id}:{job.id}:{job.submitted_at}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"update-batch-{digest}"


def _candidate_count(batches: list[AsyncUpdateSessionBatch]) -> int:
    return sum(len(batch.jobs) for batch in batches)
```

- [ ] **Step 4: Replace `run_pending_batches()` with global batch loop**

In `rightmemory/async_update.py`, replace `run_pending_batches()` with this version:

```python
    def run_pending_batches(
        self,
        run_message: Callable[[str, str], str],
        *,
        target_batch_candidates: int,
        max_wait_seconds: int,
        sleep_until: Callable[[datetime], None] | None = None,
        on_batch_success: Callable[[int], None] | None = None,
    ) -> AsyncUpdateWorkerResult:
        sleep_until = _sleep_until if sleep_until is None else sleep_until
        with self._worker_locked():
            self._write_worker_locked(
                status="running",
                pid=os.getpid(),
                batch_id=None,
                session_ids=[],
                error=None,
            )

        processed = 0
        try:
            while True:
                batch, deadline = self._next_batch(target_batch_candidates, max_wait_seconds)
                if batch is None:
                    if deadline is None:
                        return AsyncUpdateWorkerResult(status="succeeded" if processed else "idle", processed=processed)
                    sleep_until(deadline)
                    continue

                started = self._start_cross_session_batch(batch)
                if not started:
                    continue
                batch = started

                batch_id = _batch_session_id(batch)
                session_ids = [item.session_id for item in batch]
                with self._worker_locked():
                    self._write_worker_locked(
                        status="running",
                        pid=os.getpid(),
                        batch_id=batch_id,
                        session_ids=session_ids,
                        error=None,
                    )

                try:
                    result = run_message(batch_id, _format_batch_message(batch))
                except Exception as exc:
                    self._fail_cross_session_batch(batch, str(exc))
                    return AsyncUpdateWorkerResult(status="failed", processed=processed, failed=True)

                accepted_count = self._finish_cross_session_batch(batch, result)
                if accepted_count:
                    processed += accepted_count
                    if on_batch_success is not None:
                        on_batch_success(accepted_count)
        finally:
            with self._worker_locked():
                self._clear_worker_locked()
```

- [ ] **Step 5: Add selection and state transition helpers**

Add these methods inside `AsyncUpdateStore` below `run_pending_batches()`:

```python
    def _next_batch(
        self,
        target_batch_candidates: int,
        max_wait_seconds: int,
    ) -> tuple[list[AsyncUpdateSessionBatch] | None, datetime | None]:
        now = _now_dt()
        eligible: list[AsyncUpdateSessionBatch] = []
        future_deadlines: list[datetime] = []

        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                state = self._read_raw(session_id)
                if state.role != self.role:
                    continue
                if state.status != "running" or state.phase != "waiting":
                    continue
                if state.current_batch or not state.pending:
                    continue
                ready_at = _required_time(state.next_flush_at, "next_flush_at")
                if ready_at <= now:
                    eligible.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                else:
                    future_deadlines.append(ready_at)

        eligible.sort(key=lambda item: (item.ready_at, item.session_id))
        if not eligible:
            return None, min(future_deadlines) if future_deadlines else None

        selected: list[AsyncUpdateSessionBatch] = []
        total = 0
        for item in eligible:
            selected.append(item)
            total += len(item.jobs)
            if total >= target_batch_candidates:
                return selected, None

        fallback_at = eligible[0].ready_at + timedelta(seconds=max_wait_seconds)
        if now >= fallback_at:
            return selected, None

        deadlines = [fallback_at, *future_deadlines]
        return None, min(deadlines)

    def _start_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch]) -> list[AsyncUpdateSessionBatch]:
        started: list[AsyncUpdateSessionBatch] = []
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.pending[: len(expected_ids)]] != expected_ids:
                    continue
                current_batch = state.pending[: len(expected_ids)]
                pending = state.pending[len(expected_ids):]
                next_state = replace(
                    state,
                    phase="running",
                    started_at=_now(),
                    finished_at=None,
                    current_batch=current_batch,
                    pending=pending,
                    next_flush_at=None,
                    result=None,
                    error=None,
                    pid=os.getpid(),
                )
                self._write(item.session_id, next_state)
                started.append(AsyncUpdateSessionBatch(item.session_id, item.ready_at, current_batch))
        return started

    def _finish_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch], result: str) -> int:
        accepted = 0
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.current_batch] != expected_ids:
                    continue
                accepted += len(state.current_batch)
                if state.pending:
                    next_flush_at = state.next_flush_at or _format_time(
                        _now_dt() + timedelta(seconds=UPDATE_DEBOUNCE_SECONDS)
                    )
                    next_state = replace(
                        state,
                        phase="waiting",
                        started_at=_now(),
                        finished_at=None,
                        pid=os.getpid(),
                        current_batch=[],
                        next_flush_at=next_flush_at,
                        result=result,
                        error=None,
                    )
                else:
                    next_state = replace(
                        state,
                        status="succeeded",
                        phase=None,
                        finished_at=_now(),
                        pid=os.getpid(),
                        current_batch=[],
                        pending=[],
                        next_flush_at=None,
                        result=result,
                        error=None,
                    )
                self._write(item.session_id, next_state)
        return accepted

    def _fail_cross_session_batch(self, batch: list[AsyncUpdateSessionBatch], error: str) -> None:
        for item in sorted(batch, key=lambda entry: entry.session_id):
            expected_ids = [job.id for job in item.jobs]
            with self._locked(item.session_id):
                state = self._read_raw(item.session_id)
                if [job.id for job in state.current_batch] != expected_ids:
                    continue
                self._fail_locked(item.session_id, error)
```

Keep `_start_pending_batch_locked()`, `_finish_current()`, and `_fail_if_current_batch()` until all callers are removed. Remove them in the cleanup task after CLI integration proves unused.

- [ ] **Step 6: Run async update tests and verify pass**

Run:

```bash
python -m unittest tests.test_async_update
```

Expected: PASS.

- [ ] **Step 7: Commit cross-session batch loop**

```bash
git add rightmemory/async_update.py tests/test_async_update.py
git commit -m "feat: batch async updates across sessions"
```

## Task 5: Wire The Global Worker Into The CLI

**Files:**
- Modify: `rightmemory/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

In `tests/test_cli.py`, replace `test_submitted_worker_processes_pending_updates_as_one_batch` with:

```python
    def test_async_worker_processes_multiple_sessions_as_one_batch(self):
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message))
                return f"session {session_id}: {message}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-2", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_async_update_config", return_value=_async_update_config(memory_root, target=2)),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config()),
            ):
                result = main(["update", "_async-worker"])

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].startswith("update-batch-"))
        self.assertIn("[update session: agent-1 | candidate: 1", calls[0][1])
        self.assertIn("[update session: agent-2 | candidate: 1", calls[0][1])
```

Add this helper near `_dreamer_watch_config()` in `tests/test_cli.py`:

```python
def _async_update_config(memory_root: Path, *, target: int = 15, max_wait: int = 86400):
    return type(
        "AsyncUpdateConfig",
        (),
        {
            "memory_root": memory_root,
            "target_batch_candidates": target,
            "max_wait_seconds": max_wait,
        },
    )()
```

Update trigger tests that call `_submitted-worker` so they call `_async-worker` and patch `load_async_update_config` with `_async_update_config(memory_root, target=1)`.

Add this CLI rejection test:

```python
    def test_submitted_worker_private_command_is_removed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            ):
                with self.assertRaises(SystemExit):
                    main(["update", "_submitted-worker", "--session", "agent-1"])
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: FAIL because `_async-worker` is not routed and `load_async_update_config` is not imported.

- [ ] **Step 3: Import async update config and update CLI routing**

In `rightmemory/cli.py`, add `load_async_update_config` to the config import list:

```python
from .config import (
    MEMORY_ROOT,
    ROLES,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
```

Replace the private command validation around line `$118$` with:

```python
    if remaining and remaining[0] == "_async-worker" and args.role != "update":
        raise ValueError("_async-worker is only supported for the update role")
```

Replace the runtime branch that handles `_submitted-worker` with:

```python
        if remaining and remaining[0] == "_async-worker":
            return _async_worker(runtime, config.memory_root, args.role)
```

Update `_submit()` so submit loads config before calling `submit()` only if the final `AsyncUpdateStore.submit()` signature needs config. If Task $3$ kept config out of submit, leave `_submit()` as:

```python
def _submit(memory_root, role: str, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    state = AsyncUpdateStore(memory_root, role).submit(session_id, message)
    print(format_state(state))
    return 0
```

Replace `_submitted_worker()` with `_async_worker()`:

```python
def _async_worker(
    runtime: RightMemoryRuntime,
    memory_root,
    role: str,
) -> int:
    dreamer_watch_config = load_dreamer_watch_config()
    async_update_config = load_async_update_config()
    store = AsyncUpdateStore(memory_root, role)
    result = store.run_pending_batches(
        lambda batch_session_id, message: runtime.run_session_turn(batch_session_id, message),
        target_batch_candidates=async_update_config.target_batch_candidates,
        max_wait_seconds=async_update_config.max_wait_seconds,
        on_batch_success=_dreamer_trigger_incrementer(
            memory_root,
            dreamer_watch_config.update_candidate_points,
        ),
    )
    if result.status == "failed":
        return 1
    return 0
```

- [ ] **Step 4: Run CLI tests and verify pass**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: PASS.

- [ ] **Step 5: Commit CLI worker integration**

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: wire async update global worker"
```

## Task 6: Preserve Recent Submitted Memory Compatibility

**Files:**
- Modify: `rightmemory/recent_submitted.py`
- Test: `tests/test_recent_submitted.py`

- [ ] **Step 1: Write failing recent submitted worker-state test**

In `tests/test_recent_submitted.py`, add:

```python
    def test_collect_ignores_async_worker_state_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store_root = root / ".runtime" / "async" / "update"
            worker_root = store_root / "_worker"
            worker_root.mkdir(parents=True)
            (worker_root / "state.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "pid": 123,
                        "started_at": "2026-05-29T00:00:00+00:00",
                        "batch_id": "update-batch-test",
                        "session_ids": ["agent-1"],
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            self._write_state(
                root,
                "agent-1",
                pending=[
                    {
                        "id": 1,
                        "message": "remember real pending item",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    }
                ],
            )

            entries = collect_recent_submitted_memory(root)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].update_session_id, "agent-1")
        self.assertEqual(entries[0].message, "remember real pending item")
```

- [ ] **Step 2: Run recent submitted tests and verify behavior**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: PASS if worker state is under `_worker/` and `glob("*.json")` already excludes it. If FAIL, update collection in Step $3$.

- [ ] **Step 3: Use the store path helper**

Change `collect_recent_submitted_memory()` in `rightmemory/recent_submitted.py` from:

```python
    for state_path in sorted(store.root.glob("*.json")):
```

to:

```python
    for state_path in store._session_state_paths():
```

- [ ] **Step 4: Run recent submitted tests and verify pass**

Run:

```bash
python -m unittest tests.test_recent_submitted
```

Expected: PASS.

- [ ] **Step 5: Commit recent submitted compatibility**

```bash
git add rightmemory/recent_submitted.py tests/test_recent_submitted.py
git commit -m "test: preserve recent submitted update collection"
```

## Task 7: Update Docs And Remove Dead Per-Session Worker Code

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Remove unused per-session worker helpers**

In `rightmemory/async_update.py`, remove the old one-session worker methods named
`_start_pending_batch_locked`, `_start_wait_or_idle_locked`, `_finish_current`,
`_record_worker_pid`, and `_fail_if_current_batch` after confirming no callers
remain.

Keep `_fail()`, `_fail_locked()`, `_read_checked_locked()`, and `_locked()` because submit, pull, and recovery still use them.

- [ ] **Step 2: Update README async update text**

In `README.md`, replace the existing async update bullet with:

```markdown
- Async `update submit` calls for the same `--session` still accumulate as pending candidates and reset that session's one-hour quiet period. A single global async update worker scans all eligible session queues, batches whole session queues until it reaches `[update.async].target_batch_candidates` candidates by default, and falls back after `[update.async].max_wait_seconds`. `pull` and `undo` remain per-session. While submissions are waiting or being processed, retrieve can see newly submitted unconsolidated memory as `Recent submitted memory` so fresh context is available before the updater writes it.
```

Add this config block near the review and dreamer watch config examples:

```toml
[update.async]
target_batch_candidates = 15
max_wait_seconds = 86400
```

In prose immediately after the block, add:

```markdown
`target_batch_candidates` is a fill threshold, not a hard cap. The worker keeps eligible session queues whole, so a batch may overshoot the target. `max_wait_seconds` is measured from the oldest eligible queue's quiet-period deadline.
```

- [ ] **Step 3: Update DESIGN_NOTES batched command updates paragraph**

In `DESIGN_NOTES.md`, update the "Batched command updates" note to say:

```markdown
Update submissions accumulate as candidate briefs under their original session id. Each session keeps a one-hour quiet period from its latest submit, but execution is owned by one global async update worker per memory root. The worker batches eligible session queues by candidate count, keeps each included session queue whole, and runs the update role once for the cross-session batch; per-session state still powers `pull`, `undo`, retry, and recent-submitted retrieval. Async state files keep their own `session_id` and `role` fields instead of inferring them from the read path because submitted candidates are operational state and malformed state should fail visibly.
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli tests.test_recent_submitted
```

Expected: PASS.

- [ ] **Step 5: Commit cleanup and docs**

```bash
git add rightmemory/async_update.py README.md DESIGN_NOTES.md tests/test_async_update.py tests/test_cli.py tests/test_recent_submitted.py
git commit -m "docs: describe cross-session async updates"
```

## Task 8: Full Verification

**Files:**
- Verify repository only

- [ ] **Step 1: Run full unit test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 2: Run compile check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: no output and exit code $0$.

- [ ] **Step 3: Check git status**

Run:

```bash
git status --short
```

Expected: no unstaged or untracked files except intentional generated artifacts that are explained before completion.

- [ ] **Step 4: Final review of commits**

Run:

```bash
git log --oneline -8
```

Expected: the latest commits are the task commits from this plan, and no unrelated files were changed.
