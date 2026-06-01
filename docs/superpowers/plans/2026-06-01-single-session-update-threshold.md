# Single-Session Update Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single busy async update session become worker-eligible when its pending queue reaches `[update.async].target_batch_candidates`, even before its quiet period expires.

**Architecture:** Keep `submit` lightweight and keep the global worker as the scheduler. Implement the threshold in `AsyncUpdateStore._next_batch`, where normal waiting sessions are classified as eligible or waiting. Preserve recovery priority by leaving the recovery lane ahead of normal work.

**Tech Stack:** Python standard library, `unittest`, existing file-backed async update state.

---

## Scope Check

This is one scheduler change inside async update. It does not change persisted state, config shape, role prompts, README behavior docs, or recovery/manual-recovery commands.

## File Structure

- Modify `tests/test_async_update.py`: add focused scheduler tests near the existing global worker batching tests.
- Modify `rightmemory/async_update.py`: update `_next_batch` normal waiting-session eligibility.
- Modify `DESIGN_NOTES.md`: fold the pressure-threshold rationale into the existing batched command updates note.

## Task 1: Add Focused Scheduler Tests

**Files:**
- Modify: `tests/test_async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Add threshold selection test**

Add this test after `test_global_worker_includes_whole_session_when_it_overshoots_target`:

```python
    def test_single_session_reaching_target_runs_before_quiet_period(self):
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
                    next_flush_at="2026-05-15T01:00:00+00:00",
                    pending=[_job(1, "a1"), _job(2, "a2"), _job(3, "a3")],
                    next_id=4,
                ),
            )

            def fail_sleep(deadline):
                raise AssertionError(f"threshold-ready session should not sleep until {deadline}")

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:00:00+00:00")):
                result = store.run_pending_batches(
                    lambda batch_session_id, message: calls.append(message) or "ok",
                    target_batch_candidates=3,
                    max_wait_seconds=86400,
                    sleep_until=fail_sleep,
                )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.processed, 3)
        self.assertEqual(len(calls), 1)
        self.assertIn("a3", calls[0])
```

- [ ] **Step 2: Add below-threshold waiting test**

Add this test after the threshold selection test:

```python
    def test_single_session_below_target_is_not_eligible_before_quiet_period(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2026-05-15T01:00:00+00:00",
                    pending=[_job(1, "a1"), _job(2, "a2")],
                    next_id=3,
                ),
            )

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:00:00+00:00")):
                batch, deadline = store._next_batch(
                    target_batch_candidates=3,
                    max_wait_seconds=86400,
                )

        self.assertIsNone(batch)
        self.assertEqual(deadline, _dt("2026-05-15T01:00:00+00:00"))
```

- [ ] **Step 3: Strengthen recovery priority test**

Modify `test_retryable_sessions_run_before_normal_batching` so the normal session is threshold-eligible before its quiet period:

```python
            store._write(
                "normal-session",
                AsyncUpdateState(
                    status="running",
                    session_id="normal-session",
                    role="update",
                    phase="waiting",
                    next_flush_at="2026-05-15T01:00:00+00:00",
                    pending=[_job(1, "normal later"), _job(2, "normal also later")],
                    next_id=3,
                ),
            )

            def fail_sleep(deadline):
                raise AssertionError(f"threshold-ready normal work should not sleep until {deadline}")

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:00:00+00:00")):
                result = store.run_pending_batches(
                    lambda batch_session_id, message: calls.append(message) or "ok",
                    target_batch_candidates=2,
                    max_wait_seconds=86400,
                    sleep_until=fail_sleep,
                )
```

Keep the assertions that `"retry first"` appears in `calls[0]` and normal text does not.

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p test_async_update.py -k single_session_reaching
```

Expected before implementation: `test_single_session_reaching_target_runs_before_quiet_period` fails with `threshold-ready session should not sleep`.

## Task 2: Implement Worker Eligibility

**Files:**
- Modify: `rightmemory/async_update.py`
- Test: `tests/test_async_update.py`

- [ ] **Step 1: Change normal waiting-session eligibility**

In `AsyncUpdateStore._next_batch`, replace the normal waiting-session branch:

```python
                ready_at = _required_time(state.next_flush_at, "next_flush_at")
                if ready_at <= now:
                    eligible.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                else:
                    future_deadlines.append(ready_at)
```

with:

```python
                ready_at = _required_time(state.next_flush_at, "next_flush_at")
                pressure_ready = len(state.pending) >= target_batch_candidates
                if ready_at <= now or pressure_ready:
                    eligible.append(AsyncUpdateSessionBatch(state.session_id, ready_at, list(state.pending)))
                else:
                    future_deadlines.append(ready_at)
```

This keeps the original `ready_at` sort key, which is acceptable because threshold-eligible normal work still waits behind recovery work and follows the existing eligible-session ordering.

- [ ] **Step 2: Run focused tests and verify pass**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p test_async_update.py -k single_session -k retryable_sessions_run_before_normal_batching
```

Expected: the new threshold tests and recovery-priority test pass.

## Task 3: Update Durable Design Notes

**Files:**
- Modify: `DESIGN_NOTES.md`

- [ ] **Step 1: Rewrite the batched update paragraph**

Replace the first paragraph under `### Batched command updates` with:

```markdown
Update submissions accumulate as candidate briefs under their original session id. A session normally keeps a one-hour quiet period from its latest submit, while a single busy session can become eligible earlier when its pending queue reaches the configured update batch target. Execution is owned by one global async update worker per memory root. The worker batches eligible session queues by candidate count, keeps each included session queue whole, and runs the update role once for the cross-session batch; per-session state still powers `pull`, `undo`, retry, and recent-submitted retrieval. Async state files keep their own `session_id` and `role` fields instead of inferring them from the read path because submitted candidates are operational state and malformed state should fail visibly.
```

- [ ] **Step 2: Check README is unchanged**

Run:

```bash
git diff -- README.md
```

Expected: no output.

## Task 4: Verification And Commit

**Files:**
- Modify: `rightmemory/async_update.py`
- Modify: `tests/test_async_update.py`
- Modify: `DESIGN_NOTES.md`
- Create: `docs/superpowers/plans/2026-06-01-single-session-update-threshold.md`

- [ ] **Step 1: Run focused async tests**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -p test_async_update.py
```

Expected: pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: pass.

- [ ] **Step 3: Check syntax**

Run:

```bash
.venv/bin/python -m compileall -q rightmemory tests
```

Expected: exit code 0 and no output.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add rightmemory/async_update.py tests/test_async_update.py DESIGN_NOTES.md docs/superpowers/plans/2026-06-01-single-session-update-threshold.md
git commit -m "feat: flush busy update sessions by threshold"
```
