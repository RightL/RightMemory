# Async Update Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic async update recovery with a $1$ hour cooldown, a $2$ attempt limit, explicit manual recovery, and truthful status reporting.

**Architecture:** Keep per-session async update JSON files as the durable queue. Add retry metadata and a recovery lane inside `AsyncUpdateStore`, so failed work can bypass normal batching after cooldown while fresh work keeps the existing batching policy. Add a global `rightmemory update retry` command for manual recovery and update status aggregation to distinguish normal pending, retrying, manual recovery, and in-flight work.

**Tech Stack:** Python standard library, dataclasses, file locks with `fcntl`, subprocess worker startup, existing RightMemory CLI, `unittest`.

---

## Scope Check

This is one subsystem: async update scheduling, recovery, CLI reporting, and status aggregation. It touches the async queue store, CLI dispatch, status dashboard, and tests. It does not change update role prompts, memory file schema, README behavior docs, or isolated-write authority.

## File Structure

- Modify `rightmemory/async_update.py`: add retry constants, state metadata, legacy failed-state normalization, failure transitions, recovery-lane selection, manual retry requeue, and warning formatting.
- Modify `rightmemory/cli.py`: add `rightmemory update retry`, keep `submit` runtime-free, and print the strong manual-recovery warning only when submit targets a manual-recovery session.
- Modify `rightmemory/status.py`: separate async queue counts into normal pending, retrying, manual recovery, and current batch; classify legacy failed pending states as manual recovery.
- Modify `tests/test_async_update.py`: unit tests for retry state, failure transitions, recovery selection, submit semantics, and manual retry requeue.
- Modify `tests/test_cli.py`: CLI tests for manual warning and global retry.
- Modify `tests/test_status.py`: status tests for separated queue counts and legacy manual recovery.
- No README changes.

## Task 1: Add Retry Metadata And Failure Transitions

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Write failing retry state tests**

In `tests/test_async_update.py`, add these tests inside `AsyncUpdateStateTests`, near `test_global_worker_failure_returns_all_current_batches_to_pending`:

```python
    def test_global_worker_failure_schedules_first_retry(self):
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

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T02:00:00+00:00")):
                result = store.run_pending_batches(
                    Mock(side_effect=RuntimeError("isolated failure")),
                    target_batch_candidates=2,
                    max_wait_seconds=86400,
                )
            first = store.read("agent-1")
            second = store.read("agent-2")

        self.assertEqual(result.status, "failed")
        for state in (first, second):
            self.assertEqual(state.status, "failed")
            self.assertIsNone(state.phase)
            self.assertEqual(state.attempts, 1)
            self.assertEqual(state.next_retry_at, "2026-05-15T03:00:00+00:00")
            self.assertEqual(state.current_batch, [])
            self.assertEqual(len(state.pending), 1)
            self.assertEqual(state.error, "isolated failure")
            self.assertEqual(state.last_error, "isolated failure")

    def test_second_failure_moves_to_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    error="first failure",
                    last_error="first failure",
                    pending=[_job(1, "retry me")],
                    next_id=2,
                ),
            )

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T02:00:00+00:00")):
                result = store.run_pending_batches(
                    Mock(side_effect=RuntimeError("second failure")),
                    target_batch_candidates=15,
                    max_wait_seconds=86400,
                )
            state = store.read("agent-1")

        self.assertEqual(result.status, "failed")
        self.assertEqual(state.status, "needs_manual_recovery")
        self.assertEqual(state.attempts, 2)
        self.assertIsNone(state.next_retry_at)
        self.assertEqual(state.current_batch, [])
        self.assertEqual([job.message for job in state.pending], ["retry me"])
        self.assertEqual(state.error, "second failure")
        self.assertEqual(state.last_error, "second failure")

    def test_successful_retry_clears_retry_metadata(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    error="first failure",
                    last_error="first failure",
                    pending=[_job(1, "retry me")],
                    next_id=2,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_session_id, message: calls.append(message) or "ok",
                target_batch_candidates=15,
                max_wait_seconds=86400,
            )
            state = store.read("agent-1")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(state.attempts, 0)
        self.assertIsNone(state.next_retry_at)
        self.assertIsNone(state.last_error)
        self.assertIsNone(state.error)
        self.assertEqual(state.pending, [])
```

Add this compatibility test near `test_dead_worker_running_batch_returns_batch_to_pending`:

```python
    def test_legacy_failed_pending_state_becomes_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    finished_at="2026-05-15T00:00:00+00:00",
                    error="old failure",
                    pending=[_job(1, "old pending")],
                    next_id=2,
                ),
            )

            state = store.read("agent-1")

        self.assertEqual(state.status, "needs_manual_recovery")
        self.assertEqual(state.attempts, 2)
        self.assertEqual(state.current_batch, [])
        self.assertEqual([job.message for job in state.pending], ["old pending"])
        self.assertEqual(state.error, "old failure")
        self.assertEqual(state.last_error, "old failure")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_global_worker_failure_schedules_first_retry \
  tests.test_async_update.AsyncUpdateStateTests.test_second_failure_moves_to_manual_recovery \
  tests.test_async_update.AsyncUpdateStateTests.test_successful_retry_clears_retry_metadata \
  tests.test_async_update.AsyncUpdateStateTests.test_legacy_failed_pending_state_becomes_manual_recovery
```

Expected: failures mentioning missing `attempts`, `next_retry_at`, or `last_error` fields, and failed sessions not entering manual recovery.

- [ ] **Step 3: Add retry constants and state fields**

In `rightmemory/async_update.py`, add constants near the existing timing constants:

```python
UPDATE_DEBOUNCE_SECONDS = 60 * 60
UPDATE_RETRY_COOLDOWN_SECONDS = 60 * 60
UPDATE_MAX_AUTOMATIC_ATTEMPTS = 2
WORKER_IDLE_POLL_SECONDS = 30
STATUS_MANUAL_RECOVERY = "needs_manual_recovery"
```

Extend `AsyncUpdateState`:

```python
@dataclass(frozen=True)
class AsyncUpdateState:
    status: str
    session_id: str
    role: str
    phase: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    result: str | None = None
    error: str | None = None
    attempts: int = 0
    next_retry_at: str | None = None
    last_error: str | None = None
    next_flush_at: str | None = None
    current_batch: list[AsyncUpdateJob] = field(default_factory=list)
    pending: list[AsyncUpdateJob] = field(default_factory=list)
    next_id: int = 1
```

Add helpers near `_optional_str`:

```python
def _optional_nonnegative_int(value: object, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"async update state must contain nonnegative integer field: {field}")
    return value


def _is_legacy_failed_pending_state(state: AsyncUpdateState) -> bool:
    return (
        state.status == "failed"
        and state.attempts == 0
        and state.next_retry_at is None
        and bool(state.current_batch or state.pending)
    )
```

Update `_state_from_json()` to parse the new fields:

```python
    return AsyncUpdateState(
        status=status,
        session_id=session_id,
        role=role,
        phase=_optional_str(data.get("phase")),
        started_at=_optional_str(data.get("started_at")),
        finished_at=_optional_str(data.get("finished_at")),
        pid=data.get("pid") if isinstance(data.get("pid"), int) else None,
        result=_optional_str(data.get("result")),
        error=_optional_str(data.get("error")),
        attempts=_optional_nonnegative_int(data.get("attempts"), "attempts"),
        next_retry_at=_optional_str(data.get("next_retry_at")),
        last_error=_optional_str(data.get("last_error")),
        next_flush_at=_optional_str(data.get("next_flush_at")),
        current_batch=_required_job_list(data, "current_batch"),
        pending=_required_job_list(data, "pending"),
        next_id=next_id,
    )
```

- [ ] **Step 4: Implement failure and success retry transitions**

In `AsyncUpdateStore`, add this helper near `_fail_locked()`:

```python
    def _manual_recovery_locked(self, session_id: str, state: AsyncUpdateState) -> AsyncUpdateState:
        retry_pending = [*state.current_batch, *state.pending]
        next_state = replace(
            state,
            status=STATUS_MANUAL_RECOVERY,
            phase=None,
            finished_at=state.finished_at or _now(),
            pid=os.getpid(),
            attempts=max(state.attempts, UPDATE_MAX_AUTOMATIC_ATTEMPTS),
            next_retry_at=None,
            last_error=state.last_error or state.error,
            current_batch=[],
            pending=retry_pending,
        )
        self._write(session_id, next_state)
        return next_state
```

Update `_read_checked_locked()` so legacy failed pending states normalize on read:

```python
    def _read_checked_locked(self, session_id: str) -> AsyncUpdateState:
        state = self._read_raw(session_id)
        if _is_legacy_failed_pending_state(state):
            return self._manual_recovery_locked(session_id, state)
        if state.status != "running":
            return state
        worker = self._worker_snapshot()
        if state.phase == "running" and state.current_batch:
            if worker.pid is not None and state.pid == worker.pid:
                return state
            return self._fail_locked(session_id, "worker process exited before writing result")
        if worker.pid is None and (state.pid is not None or worker.dead_pid is not None):
            return self._fail_locked(session_id, "worker process exited before writing result")
        return state
```

Replace `_fail_locked()` with:

```python
    def _fail_locked(self, session_id: str, error: str) -> AsyncUpdateState:
        current = self._read_raw(session_id)
        retry_pending = [*current.current_batch, *current.pending]
        attempts = current.attempts + 1
        manual = attempts >= UPDATE_MAX_AUTOMATIC_ATTEMPTS
        now = _now_dt()
        state = replace(
            current,
            status=STATUS_MANUAL_RECOVERY if manual else "failed",
            phase=None,
            finished_at=_format_time(now),
            pid=os.getpid(),
            error=error,
            attempts=attempts,
            next_retry_at=None if manual else _format_time(now + timedelta(seconds=UPDATE_RETRY_COOLDOWN_SECONDS)),
            last_error=error,
            next_flush_at=None,
            current_batch=[],
            pending=retry_pending,
        )
        self._write(session_id, state)
        return state
```

In `_finish_cross_session_batch()`, clear retry metadata in both success branches:

```python
                    next_state = replace(
                        state,
                        status="running",
                        phase="waiting",
                        started_at=_now(),
                        finished_at=None,
                        pid=os.getpid(),
                        current_batch=[],
                        next_flush_at=next_flush_at,
                        result=result,
                        error=None,
                        attempts=0,
                        next_retry_at=None,
                        last_error=None,
                    )
```

```python
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
                        attempts=0,
                        next_retry_at=None,
                        last_error=None,
                    )
```

In `_start_cross_session_batch()`, set `status="running"` so recovery states become in-flight cleanly:

```python
                next_state = replace(
                    state,
                    status="running",
                    phase="running",
                    started_at=_now(),
                    finished_at=None,
                    current_batch=current_batch,
                    pending=pending,
                    next_flush_at=state.next_flush_at if pending else None,
                    result=None,
                    error=None,
                    pid=os.getpid(),
                )
```

- [ ] **Step 5: Run retry state tests**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_global_worker_failure_schedules_first_retry \
  tests.test_async_update.AsyncUpdateStateTests.test_second_failure_moves_to_manual_recovery \
  tests.test_async_update.AsyncUpdateStateTests.test_successful_retry_clears_retry_metadata \
  tests.test_async_update.AsyncUpdateStateTests.test_legacy_failed_pending_state_becomes_manual_recovery
```

Expected: all selected tests pass.

- [ ] **Step 6: Update old failure test expectations**

Update `test_global_worker_failure_returns_all_current_batches_to_pending` so it now expects first-attempt retry metadata:

```python
        self.assertEqual(result.status, "failed")
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        self.assertEqual(first.attempts, 1)
        self.assertEqual(second.attempts, 1)
        self.assertIsNotNone(first.next_retry_at)
        self.assertIsNotNone(second.next_retry_at)
        self.assertEqual([job.message for job in first.pending], ["a1"])
        self.assertEqual([job.message for job in second.pending], ["b1"])
        self.assertEqual(first.current_batch, [])
        self.assertEqual(second.current_batch, [])
        self.assertEqual(first.error, "isolated failure")
        self.assertEqual(second.error, "isolated failure")
```

Update `test_dead_worker_running_batch_returns_batch_to_pending` and `test_stale_running_batch_is_recovered_when_another_worker_is_active` to assert `attempts == 1` and `next_retry_at is not None`.

- [ ] **Step 7: Run async update tests**

Run:

```bash
python -m unittest tests.test_async_update
```

Expected: all tests in `tests.test_async_update` pass.

- [ ] **Step 8: Commit Task 1**

Run:

```bash
rtk git add rightmemory/async_update.py tests/test_async_update.py
rtk git commit -m "Add async update retry state"
```

## Task 2: Preserve Submit Semantics And Add Manual Warning

**Files:**
- Modify: `rightmemory/async_update.py`
- Modify: `rightmemory/cli.py`
- Test: `tests/test_async_update.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing submit behavior tests**

In `tests/test_async_update.py`, replace `test_submit_after_failed_state_preserves_pending_order_and_starts_worker` with:

```python
    def test_submit_during_retry_cooldown_appends_without_recovering(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2026-05-15T01:00:00+00:00",
                    error="previous failure",
                    last_error="previous failure",
                    pending=[_job(1, "retry first")],
                    next_id=2,
                ),
            )
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen,
                patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:10:00+00:00")),
            ):
                state = store.submit("agent-1", "new update")

        popen.assert_called_once()
        self.assertEqual(state.status, "failed")
        self.assertIsNone(state.phase)
        self.assertEqual(state.attempts, 1)
        self.assertEqual(state.next_retry_at, "2026-05-15T01:00:00+00:00")
        self.assertEqual(state.error, "previous failure")
        self.assertEqual([job.message for job in state.pending], ["retry first", "new update"])
        self.assertEqual([job.id for job in state.pending], [1, 2])
        self.assertEqual(state.current_batch, [])
```

Replace `test_submit_after_failed_state_recovers_leftover_current_batch` with:

```python
    def test_submit_during_manual_recovery_appends_without_recovering(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="needs_manual_recovery",
                    session_id="agent-1",
                    role="update",
                    attempts=2,
                    error="previous failure",
                    last_error="previous failure",
                    current_batch=[_job(1, "interrupted first"), _job(2, "interrupted second")],
                    pending=[_job(3, "already pending")],
                    next_id=4,
                ),
            )
            process = Mock(pid=4242)

            with patch("rightmemory.async_update.subprocess.Popen", return_value=process):
                state = store.submit("agent-1", "new update")

        self.assertEqual(state.status, "needs_manual_recovery")
        self.assertIsNone(state.phase)
        self.assertEqual(state.attempts, 2)
        self.assertEqual(state.current_batch, [])
        self.assertEqual(
            [job.message for job in state.pending],
            ["interrupted first", "interrupted second", "already pending", "new update"],
        )
        self.assertEqual([job.id for job in state.pending], [1, 2, 3, 4])
```

In `tests/test_cli.py`, add `AsyncUpdateState` to the async update import:

```python
from rightmemory.async_update import AsyncUpdateState, AsyncUpdateStore
```

Add this test near `test_main_submits_async_update_without_building_runtime`:

```python
    def test_submit_into_manual_recovery_prints_critical_warning(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            store = AsyncUpdateStore(memory_root, "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="needs_manual_recovery",
                    session_id="agent-1",
                    role="update",
                    attempts=2,
                    error="previous failure",
                    last_error="previous failure",
                    pending=[],
                    next_id=1,
                ),
            )

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "submit", "--session", "agent-1", "new", "candidate"])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("status: needs_manual_recovery", output)
        self.assertIn("pending: 1", output)
        self.assertIn("CRITICAL: this async memory update session is blocked after 2 failed attempts.", output)
        self.assertIn("Agent: report this issue to the user", output)
```

- [ ] **Step 2: Run submit tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_submit_during_retry_cooldown_appends_without_recovering \
  tests.test_async_update.AsyncUpdateStateTests.test_submit_during_manual_recovery_appends_without_recovering \
  tests.test_cli.JsonRequestTests.test_submit_into_manual_recovery_prints_critical_warning
```

Expected: failures showing submit recovers failed state or no critical warning is printed.

- [ ] **Step 3: Implement non-recovering enqueue**

In `rightmemory/async_update.py`, add a warning constant near the status constants:

```python
MANUAL_RECOVERY_WARNING = (
    "CRITICAL: this async memory update session is blocked after "
    f"{UPDATE_MAX_AUTOMATIC_ATTEMPTS} failed attempts.\n"
    "The new candidate was saved, but this session will not be processed until manual recovery runs.\n"
    "Agent: report this issue to the user and suggest `rightmemory update retry`."
)
```

Add:

```python
def manual_recovery_warning(state: AsyncUpdateState) -> str | None:
    if state.status != STATUS_MANUAL_RECOVERY:
        return None
    return MANUAL_RECOVERY_WARNING
```

Update the import in `rightmemory/cli.py`:

```python
from .async_update import AsyncUpdateStore, format_state, manual_recovery_warning
```

Update `_submit()`:

```python
def _submit(memory_root, role: str, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    state = AsyncUpdateStore(memory_root, role).submit(session_id, message)
    print(format_state(state))
    warning = manual_recovery_warning(state)
    if warning:
        print()
        print(warning)
    return 0
```

Update `_enqueue_locked()` so failed/manual states keep their status and retry metadata:

```python
        if state.status in {"failed", STATUS_MANUAL_RECOVERY}:
            pending = [*state.current_batch, *state.pending, job]
            return replace(
                state,
                phase=None,
                pid=worker_pid,
                current_batch=[],
                pending=pending,
                next_id=next_id,
            )
```

Place that branch before the existing `state.status == "running" and state.phase == "running"` branch.

- [ ] **Step 4: Run submit tests**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_submit_during_retry_cooldown_appends_without_recovering \
  tests.test_async_update.AsyncUpdateStateTests.test_submit_during_manual_recovery_appends_without_recovering \
  tests.test_cli.JsonRequestTests.test_submit_into_manual_recovery_prints_critical_warning
```

Expected: all selected tests pass.

- [ ] **Step 5: Run CLI and async update tests**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli
```

Expected: both test modules pass.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
rtk git add rightmemory/async_update.py rightmemory/cli.py tests/test_async_update.py tests/test_cli.py
rtk git commit -m "Preserve async submit during recovery"
```

## Task 3: Add Recovery-Lane Worker Selection

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Write failing recovery selection tests**

In `tests/test_async_update.py`, add these tests near the global worker batching tests:

```python
    def test_retryable_failed_session_runs_below_target_without_waiting(self):
        calls = []
        slept = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    error="previous failure",
                    last_error="previous failure",
                    pending=[_job(1, "retry me")],
                    next_id=2,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_session_id, message: calls.append((batch_session_id, message)) or "ok",
                target_batch_candidates=15,
                max_wait_seconds=86400,
                sleep_until=slept.append,
            )
            state = store.read("agent-1")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(slept, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("retry me", calls[0][1])
        self.assertEqual(state.status, "succeeded")

    def test_failed_session_in_cooldown_waits_until_retry_deadline(self):
        slept = []
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2026-05-15T01:00:00+00:00",
                    error="previous failure",
                    last_error="previous failure",
                    pending=[_job(1, "retry later")],
                    next_id=2,
                ),
            )

            def fake_now():
                if slept:
                    return _dt("2026-05-15T01:00:00+00:00")
                return _dt("2026-05-15T00:00:00+00:00")

            with patch("rightmemory.async_update._now_dt", side_effect=fake_now):
                result = store.run_pending_batches(
                    lambda batch_session_id, message: calls.append(message) or "ok",
                    target_batch_candidates=15,
                    max_wait_seconds=86400,
                    sleep_until=slept.append,
                )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(slept, [_dt("2026-05-15T00:00:30+00:00")])
        self.assertEqual(len(calls), 1)
        self.assertIn("retry later", calls[0])

    def test_retryable_sessions_run_before_normal_batching(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "retry-session",
                AsyncUpdateState(
                    status="failed",
                    session_id="retry-session",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    error="previous failure",
                    last_error="previous failure",
                    pending=[_job(1, "retry first")],
                    next_id=2,
                ),
            )
            store._write(
                "normal-session",
                AsyncUpdateState(
                    status="running",
                    session_id="normal-session",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[_job(1, "normal later")],
                    next_id=2,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_session_id, message: calls.append(message) or "ok",
                target_batch_candidates=15,
                max_wait_seconds=86400,
            )

        self.assertEqual(result.status, "succeeded")
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn("retry first", calls[0])
        self.assertNotIn("normal later", calls[0])
```

- [ ] **Step 2: Run recovery selection tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_retryable_failed_session_runs_below_target_without_waiting \
  tests.test_async_update.AsyncUpdateStateTests.test_failed_session_in_cooldown_waits_until_retry_deadline \
  tests.test_async_update.AsyncUpdateStateTests.test_retryable_sessions_run_before_normal_batching
```

Expected: failures showing failed states are ignored by `_next_batch()`.

- [ ] **Step 3: Implement recovery-aware `_next_batch()`**

Replace `_next_batch()` in `rightmemory/async_update.py` with:

```python
    def _next_batch(
        self,
        target_batch_candidates: int,
        max_wait_seconds: int,
    ) -> tuple[list[AsyncUpdateSessionBatch] | None, datetime | None]:
        now = _now_dt()
        recovery: list[AsyncUpdateSessionBatch] = []
        eligible: list[AsyncUpdateSessionBatch] = []
        future_deadlines: list[datetime] = []

        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                if state.role != self.role:
                    continue
                if state.current_batch or not state.pending:
                    continue
                if state.status == "failed":
                    ready_at = _required_time(state.next_retry_at, "next_retry_at")
                    if ready_at <= now:
                        recovery.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                    else:
                        future_deadlines.append(ready_at)
                    continue
                if state.status != "running" or state.phase != "waiting":
                    continue
                ready_at = _required_time(state.next_flush_at, "next_flush_at")
                if ready_at <= now:
                    eligible.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                else:
                    future_deadlines.append(ready_at)

        recovery.sort(key=lambda item: (item.ready_at, item.session_id))
        if recovery:
            return recovery, None

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
```

- [ ] **Step 4: Run recovery selection tests**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_retryable_failed_session_runs_below_target_without_waiting \
  tests.test_async_update.AsyncUpdateStateTests.test_failed_session_in_cooldown_waits_until_retry_deadline \
  tests.test_async_update.AsyncUpdateStateTests.test_retryable_sessions_run_before_normal_batching
```

Expected: all selected tests pass.

- [ ] **Step 5: Run async update tests**

Run:

```bash
python -m unittest tests.test_async_update
```

Expected: all async update tests pass.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
rtk git add rightmemory/async_update.py tests/test_async_update.py
rtk git commit -m "Add async update recovery lane"
```

## Task 4: Add Global Manual Retry Command

**Files:**
- Modify: `rightmemory/async_update.py`
- Modify: `rightmemory/cli.py`
- Test: `tests/test_async_update.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing store retry test**

In `tests/test_async_update.py`, add:

```python
    def test_retry_manual_recovery_requeues_all_manual_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="needs_manual_recovery",
                    session_id="agent-1",
                    role="update",
                    attempts=2,
                    error="boom",
                    last_error="boom",
                    pending=[_job(1, "first")],
                    next_id=2,
                ),
            )
            store._write(
                "agent-2",
                AsyncUpdateState(
                    status="needs_manual_recovery",
                    session_id="agent-2",
                    role="update",
                    attempts=2,
                    error="boom",
                    last_error="boom",
                    pending=[_job(1, "second"), _job(2, "third")],
                    next_id=3,
                ),
            )
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen,
                patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T04:00:00+00:00")),
            ):
                result = store.retry_manual_recovery()
            first = store.read("agent-1")
            second = store.read("agent-2")

        popen.assert_called_once()
        self.assertEqual(result.requeued_sessions, 2)
        self.assertEqual(result.requeued_candidates, 3)
        self.assertEqual(result.skipped_sessions, 0)
        self.assertEqual(result.worker_pid, 4242)
        self.assertEqual(result.worker_action, "started")
        for state in (first, second):
            self.assertEqual(state.status, "failed")
            self.assertEqual(state.attempts, 0)
            self.assertEqual(state.next_retry_at, "2026-05-15T04:00:00+00:00")
            self.assertIsNone(state.error)
            self.assertIsNone(state.last_error)
```

- [ ] **Step 2: Write failing CLI retry tests**

In `tests/test_cli.py`, add these tests near the async update submit and pull tests:

```python
    def test_update_retry_requeues_manual_recovery_without_session(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            async_root = memory_root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "agent-1.json").write_text(
                json.dumps(
                    {
                        "status": "needs_manual_recovery",
                        "session_id": "agent-1",
                        "role": "update",
                        "phase": None,
                        "started_at": None,
                        "finished_at": None,
                        "pid": None,
                        "result": None,
                        "error": "boom",
                        "attempts": 2,
                        "next_retry_at": None,
                        "last_error": "boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "message": "manual item",
                                "submitted_at": "2026-05-15T00:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "retry"])

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("requeued sessions: 1", output)
        self.assertIn("requeued candidates: 1", output)
        self.assertIn("worker: started pid 123", output)

    def test_retry_is_only_supported_for_update_role(self):
        with patch("rightmemory.cli.load_config", return_value=object()):
            with self.assertRaises(ValueError):
                main(["retrieve", "retry"])
```

- [ ] **Step 3: Run retry tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_retry_manual_recovery_requeues_all_manual_sessions \
  tests.test_cli.JsonRequestTests.test_update_retry_requeues_manual_recovery_without_session \
  tests.test_cli.JsonRequestTests.test_retry_is_only_supported_for_update_role
```

Expected: failures because `retry_manual_recovery()` and `rightmemory update retry` do not exist.

- [ ] **Step 4: Implement retry result and store method**

In `rightmemory/async_update.py`, add this dataclass near `AsyncUpdateWorkerResult`:

```python
@dataclass(frozen=True)
class AsyncUpdateRetryResult:
    requeued_sessions: int = 0
    requeued_candidates: int = 0
    skipped_sessions: int = 0
    worker_pid: int | None = None
    worker_action: str = "not started"
```

Add this method to `AsyncUpdateStore` after `cancel_pending()`:

```python
    def retry_manual_recovery(self) -> AsyncUpdateRetryResult:
        now = _now_dt()
        requeued_sessions = 0
        requeued_candidates = 0
        skipped_sessions = 0
        first_session_id: str | None = None

        for path in self._session_state_paths():
            session_id = path.stem
            with self._locked(session_id):
                state = self._read_checked_locked(session_id)
                if state.status != STATUS_MANUAL_RECOVERY:
                    continue
                retry_pending = [*state.current_batch, *state.pending]
                if not retry_pending:
                    skipped_sessions += 1
                    continue
                next_state = replace(
                    state,
                    status="failed",
                    phase=None,
                    started_at=state.started_at,
                    finished_at=None,
                    pid=None,
                    error=None,
                    attempts=0,
                    next_retry_at=_format_time(now),
                    last_error=None,
                    current_batch=[],
                    pending=retry_pending,
                )
                self._write(session_id, next_state)
                requeued_sessions += 1
                requeued_candidates += len(retry_pending)
                if first_session_id is None:
                    first_session_id = session_id

        worker_pid = None
        worker_action = "not started"
        if first_session_id is not None:
            before_pid = self._active_worker_pid()
            self._start_worker_if_needed(first_session_id)
            worker_pid = self._active_worker_pid()
            if worker_pid is not None:
                worker_action = "woken" if before_pid is not None else "started"
        return AsyncUpdateRetryResult(
            requeued_sessions=requeued_sessions,
            requeued_candidates=requeued_candidates,
            skipped_sessions=skipped_sessions,
            worker_pid=worker_pid,
            worker_action=worker_action,
        )
```

Add a formatter near `format_state()`:

```python
def format_retry_result(result: AsyncUpdateRetryResult) -> str:
    lines = [
        f"requeued sessions: {result.requeued_sessions}",
        f"requeued candidates: {result.requeued_candidates}",
        f"skipped sessions: {result.skipped_sessions}",
    ]
    if result.worker_pid is None:
        lines.append("worker: not started")
    else:
        lines.append(f"worker: {result.worker_action} pid {result.worker_pid}")
    return "\n".join(lines)
```

- [ ] **Step 5: Implement CLI retry dispatch**

Update the import in `rightmemory/cli.py`:

```python
from .async_update import AsyncUpdateStore, format_retry_result, format_state, manual_recovery_warning
```

Add a help branch in `main()` after `undo`:

```python
    if remaining and remaining[0] == "retry":
        if args.role != "update":
            raise ValueError("retry is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _retry_parser(args.role).parse_args(remaining[1:])
            return 0
```

Add command dispatch before `runtime = RightMemoryRuntime(config)`:

```python
    if remaining and remaining[0] == "retry":
        _retry_parser(args.role).parse_args(remaining[1:])
        return _retry(config.memory_root, args.role)
```

Add parser and handler near `_undo_parser()` and `_undo()`:

```python
def _retry_parser(role: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"rightmemory {role} retry")
```

```python
def _retry(memory_root, role: str) -> int:
    result = AsyncUpdateStore(memory_root, role).retry_manual_recovery()
    print(format_retry_result(result))
    return 0
```

- [ ] **Step 6: Run retry tests**

Run:

```bash
python -m unittest \
  tests.test_async_update.AsyncUpdateStateTests.test_retry_manual_recovery_requeues_all_manual_sessions \
  tests.test_cli.JsonRequestTests.test_update_retry_requeues_manual_recovery_without_session \
  tests.test_cli.JsonRequestTests.test_retry_is_only_supported_for_update_role
```

Expected: all selected tests pass.

- [ ] **Step 7: Run async and CLI tests**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli
```

Expected: both modules pass.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
rtk git add rightmemory/async_update.py rightmemory/cli.py tests/test_async_update.py tests/test_cli.py
rtk git commit -m "Add async update manual retry"
```

## Task 5: Separate Async Update Status Counts

**Files:**
- Modify: `rightmemory/status.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Write failing status tests**

In `tests/test_status.py`, update `test_collect_async_update_section_counts_pending_and_current_batches` expected detail to include zero retry/manual counts:

```python
        self.assertIn("pending: 2 candidates across 1 session", section.detail)
        self.assertIn("retrying: 0 candidates across 0 sessions", section.detail)
        self.assertIn("manual recovery: 0 candidates across 0 sessions", section.detail)
        self.assertIn("current batch: 1 candidate across 1 session", section.detail)
```

Add this test near the async update section tests:

```python
    def test_collect_async_update_section_separates_retrying_and_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            base = {
                "role": "update",
                "phase": None,
                "started_at": "2026-05-29T08:00:00+00:00",
                "finished_at": None,
                "pid": None,
                "result": None,
                "next_flush_at": None,
                "current_batch": [],
                "next_id": 2,
            }
            (async_root / "retrying.json").write_text(
                json.dumps(
                    {
                        **base,
                        "status": "failed",
                        "session_id": "retrying",
                        "error": "temporary boom",
                        "attempts": 1,
                        "next_retry_at": "2026-05-29T09:00:00+00:00",
                        "last_error": "temporary boom",
                        "pending": [
                            {
                                "id": 1,
                                "message": "retrying",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (async_root / "manual.json").write_text(
                json.dumps(
                    {
                        **base,
                        "status": "needs_manual_recovery",
                        "session_id": "manual",
                        "error": "permanent boom",
                        "attempts": 2,
                        "next_retry_at": None,
                        "last_error": "permanent boom",
                        "pending": [
                            {
                                "id": 1,
                                "message": "manual",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            },
                            {
                                "id": 2,
                                "message": "manual two",
                                "submitted_at": "2026-05-29T08:01:00+00:00",
                            },
                        ],
                        "next_id": 3,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertIn("pending: 0 candidates across 0 sessions", section.detail)
        self.assertIn("retrying: 1 candidate across 1 session", section.detail)
        self.assertIn("manual recovery: 2 candidates across 1 session", section.detail)
        self.assertIn("current batch: 0 candidates across 0 sessions", section.detail)
        self.assertIn("update: retrying: retrying after error: temporary boom", issues)
        self.assertIn("update: manual: manual recovery required: permanent boom", issues)

    def test_collect_async_update_section_counts_legacy_failed_pending_as_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            async_root = root / ".runtime" / "async" / "update"
            async_root.mkdir(parents=True)
            (async_root / "legacy.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "session_id": "legacy",
                        "role": "update",
                        "phase": None,
                        "started_at": "2026-05-29T08:00:00+00:00",
                        "finished_at": "2026-05-29T09:00:00+00:00",
                        "pid": None,
                        "result": None,
                        "error": "old boom",
                        "next_flush_at": None,
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "message": "legacy",
                                "submitted_at": "2026-05-29T08:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            section, issues = collect_async_update_section(root)

        self.assertIn("manual recovery: 1 candidate across 1 session", section.detail)
        self.assertIn("update: legacy: manual recovery required: old boom", issues)
```

- [ ] **Step 2: Run status tests to verify they fail**

Run:

```bash
python -m unittest \
  tests.test_status.StatusDashboardTests.test_collect_async_update_section_counts_pending_and_current_batches \
  tests.test_status.StatusDashboardTests.test_collect_async_update_section_separates_retrying_and_manual_recovery \
  tests.test_status.StatusDashboardTests.test_collect_async_update_section_counts_legacy_failed_pending_as_manual_recovery
```

Expected: failures because status does not emit retrying/manual recovery counts.

- [ ] **Step 3: Implement status classification**

Update the import in `rightmemory/status.py`:

```python
from .async_update import STATUS_MANUAL_RECOVERY, _is_async_worker_process, _is_legacy_failed_pending_state, _state_from_json
```

In `collect_async_update_section()`, replace the count variables:

```python
    pending_candidates = 0
    pending_sessions = 0
    retrying_candidates = 0
    retrying_sessions = 0
    manual_candidates = 0
    manual_sessions = 0
    current_candidates = 0
    current_sessions = 0
```

Replace the per-state pending classification block with:

```python
        pending = state.pending
        current = state.current_batch
        manual_recovery = state.status == STATUS_MANUAL_RECOVERY or _is_legacy_failed_pending_state(state)
        retrying = state.status == "failed" and not manual_recovery
        normal_pending = state.status == "running" and bool(pending)
        if pending and manual_recovery:
            manual_candidates += len(pending)
            manual_sessions += 1
        elif pending and retrying:
            retrying_candidates += len(pending)
            retrying_sessions += 1
        elif pending and normal_pending:
            pending_candidates += len(pending)
            pending_sessions += 1
        elif pending:
            pending_candidates += len(pending)
            pending_sessions += 1
        if current:
            current_candidates += len(current)
            current_sessions += 1
```

Replace the issue block with:

```python
        if state.error:
            error_preview = _cap_preview(str(state.error)).splitlines()[0]
            if manual_recovery:
                issues.append(f"update: {state.session_id}: manual recovery required: {error_preview}")
            elif retrying:
                issues.append(f"update: {state.session_id}: retrying after error: {error_preview}")
            else:
                issues.append(f"update: {state.session_id}: error: {error_preview}")
            last_values.append((_async_outcome_time(path, state), path.name, f"error: {state.error}"))
        elif state.result:
            last_values.append((_async_outcome_time(path, state), path.name, state.result))
```

Update `detail_lines`:

```python
    detail_lines = [
        (
            f"pending: {pending_candidates} {_plural('candidate', pending_candidates)} "
            f"across {pending_sessions} {_plural('session', pending_sessions)}"
        ),
        (
            f"retrying: {retrying_candidates} {_plural('candidate', retrying_candidates)} "
            f"across {retrying_sessions} {_plural('session', retrying_sessions)}"
        ),
        (
            f"manual recovery: {manual_candidates} {_plural('candidate', manual_candidates)} "
            f"across {manual_sessions} {_plural('session', manual_sessions)}"
        ),
        (
            f"current batch: {current_candidates} {_plural('candidate', current_candidates)} "
            f"across {current_sessions} {_plural('session', current_sessions)}"
        ),
        f"state: {_display_path(Path(memory_root), async_root)}",
    ]
```

- [ ] **Step 4: Run status tests**

Run:

```bash
python -m unittest tests.test_status
```

Expected: all status tests pass after updating any older assertions that expected only the previous pending/current detail lines.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
rtk git add rightmemory/status.py tests/test_status.py
rtk git commit -m "Separate async update recovery status"
```

## Task 6: Full Verification And Runtime Smoke

**Files:**
- Verify: `rightmemory/async_update.py`
- Verify: `rightmemory/cli.py`
- Verify: `rightmemory/status.py`
- Verify: `tests/test_async_update.py`
- Verify: `tests/test_cli.py`
- Verify: `tests/test_status.py`

- [ ] **Step 1: Run focused tests**

Run:

```bash
python -m unittest tests.test_async_update tests.test_cli tests.test_status
```

Expected: all focused tests pass.

- [ ] **Step 2: Run full unit suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: full suite passes.

- [ ] **Step 3: Run compile check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: command exits with status `$0` and no output.

- [ ] **Step 4: Inspect status output manually**

Run:

```bash
rightmemory status
```

Expected: async update section shows separate `pending`, `retrying`, `manual recovery`, and `current batch` lines. Existing legacy failed pending sessions in the real memory root should appear under `manual recovery`.

- [ ] **Step 5: Commit final verification adjustments**

If verification required small fixes, commit them:

```bash
rtk git add rightmemory tests
rtk git commit -m "Verify async update recovery"
```

If no fixes were needed after Task $5$, skip this commit and keep the working tree clean.

## Spec Coverage Review

- Automatic recovery after failure: Tasks $1$ and $3$.
- $1$ hour cooldown: Task $1$.
- Stop after $2$ failed attempts: Task $1$.
- Candidate durability and order: Tasks $1$ and $3$.
- Submit output unchanged except manual recovery warning: Task $2$.
- Global `rightmemory update retry`: Task $4$.
- Status separation: Task $5$.
- Legacy failed pending state as manual recovery: Tasks $1$ and $5$.
- No README changes: file structure and Task $6$ verification.
