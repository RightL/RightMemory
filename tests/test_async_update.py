import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.async_update import (
    AsyncUpdateJob,
    AsyncUpdateSessionBatch,
    AsyncUpdateState,
    AsyncUpdateStore,
    _is_async_worker_process,
)


class AsyncUpdateStateTests(unittest.TestCase):
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

    def test_async_worker_process_check_rejects_unrelated_pid(self):
        with (
            patch("rightmemory.async_update._process_exists", return_value=True),
            patch("rightmemory.async_update._read_process_cmdline", return_value=["[kworker/R-rcu_g]"]),
        ):
            active = _is_async_worker_process(4, "update")

        self.assertFalse(active)

    def test_submit_replaces_stale_worker_pid_that_belongs_to_another_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=4,
                    batch_id=None,
                    session_ids=[],
                    error=None,
                )
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.async_update._read_process_cmdline", return_value=["[kworker/R-rcu_g]"]),
            ):
                state = store.submit("agent-1", "first")

        popen.assert_called_once()
        self.assertEqual(state.status, "running")

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

    def test_dead_worker_running_batch_returns_batch_to_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            interrupted = _job(1, "interrupted")
            already_pending = _job(2, "already pending")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="running",
                    started_at="2026-05-15T00:00:00+00:00",
                    pid=12345,
                    current_batch=[interrupted],
                    pending=[already_pending],
                    next_id=3,
                ),
            )

            with patch("rightmemory.async_update._process_exists", return_value=False):
                recovered = store.read("agent-1")

        self.assertEqual(recovered.status, "failed")
        self.assertIsNone(recovered.phase)
        self.assertEqual(recovered.attempts, 1)
        self.assertIsNotNone(recovered.next_retry_at)
        self.assertIsNone(recovered.next_flush_at)
        self.assertEqual(recovered.current_batch, [])
        self.assertEqual([job.id for job in recovered.pending], [1, 2])
        self.assertEqual(recovered.pending[0].message, "interrupted")
        self.assertIn("worker process exited before writing result", recovered.error or "")

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

    def test_submit_starts_only_one_global_worker_for_multiple_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
            ):
                first = store.submit("agent-1", "first")
                second = store.submit("agent-2", "second")

        popen.assert_called_once()
        self.assertEqual(first.status, "running")
        self.assertEqual(second.status, "running")
        self.assertEqual(first.phase, "waiting")
        self.assertEqual(second.phase, "waiting")
        self.assertEqual([job.message for job in first.pending], ["first"])
        self.assertEqual([job.message for job in second.pending], ["second"])

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

            with patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen:
                state = store.submit("agent-1", "new update")

        popen.assert_not_called()
        self.assertEqual(state.status, "needs_manual_recovery")
        self.assertIsNone(state.phase)
        self.assertEqual(state.attempts, 2)
        self.assertEqual(state.current_batch, [])
        self.assertEqual(
            [job.message for job in state.pending],
            ["interrupted first", "interrupted second", "already pending", "new update"],
        )
        self.assertEqual([job.id for job in state.pending], [1, 2, 3, 4])

    def test_submit_to_empty_failed_state_starts_fresh_pending_work(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=0,
                    error="old empty failure",
                    next_id=1,
                ),
            )
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process),
                patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:00:00+00:00")),
            ):
                state = store.submit("agent-1", "new update")

        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "waiting")
        self.assertEqual(state.attempts, 0)
        self.assertIsNone(state.next_retry_at)
        self.assertIsNone(state.error)
        self.assertEqual(state.next_flush_at, "2026-05-15T01:00:00+00:00")
        self.assertEqual([job.message for job in state.pending], ["new update"])

    def test_submit_during_running_batch_keeps_current_batch_in_flight(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            original = _job(1, "running")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[original],
                    next_id=2,
                ),
            )
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=os.getpid(),
                    batch_id="update-batch-test",
                    session_ids=["agent-1"],
                    error=None,
                )
            started = store._start_cross_session_batch(
                [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [original])]
            )

            with patch("rightmemory.async_update.subprocess.Popen") as popen:
                submitted = store.submit("agent-1", "new while running")
            accepted = store._finish_cross_session_batch(started, "ok")
            final = store.read("agent-1")

        popen.assert_not_called()
        self.assertEqual([job.id for job in submitted.current_batch], [1])
        self.assertEqual([job.id for job in submitted.pending], [2])
        self.assertEqual(accepted, 1)
        self.assertEqual(final.status, "running")
        self.assertEqual(final.phase, "waiting")
        self.assertEqual(final.current_batch, [])
        self.assertEqual([job.message for job in final.pending], ["new while running"])
        self.assertEqual(final.result, "ok")

    def test_submit_between_selection_and_start_keeps_new_pending_quiet_period(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            original = _job(1, "selected")
            selected = [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [original])]
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[original],
                    next_id=2,
                ),
            )
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=os.getpid(),
                    batch_id="update-batch-test",
                    session_ids=["agent-1"],
                    error=None,
                )

            with patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T00:00:00+00:00")):
                submitted = store.submit("agent-1", "new while selected")
            started = store._start_cross_session_batch(selected)
            accepted = store._finish_cross_session_batch(started, "ok")
            final = store.read("agent-1")

        self.assertEqual([job.id for job in submitted.pending], [1, 2])
        self.assertEqual(accepted, 1)
        self.assertEqual([job.message for job in final.pending], ["new while selected"])
        self.assertEqual(final.next_flush_at, "2026-05-15T01:00:00+00:00")

    def test_waiting_state_without_worker_record_is_not_failed_during_startup_window(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    pending=[_job(1, "queued")],
                    next_flush_at="2026-05-15T00:00:00+00:00",
                    next_id=2,
                ),
            )

            state = store.read("agent-1")

        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "waiting")
        self.assertIsNone(state.error)
        self.assertEqual([job.message for job in state.pending], ["queued"])

    def test_stale_running_batch_is_recovered_when_another_worker_is_active(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="running",
                    pid=999999,
                    current_batch=[_job(1, "interrupted")],
                    pending=[_job(2, "already pending")],
                    next_id=3,
                ),
            )
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=os.getpid(),
                    batch_id="update-batch-other",
                    session_ids=["agent-2"],
                    error=None,
                )

            recovered = store.read("agent-1")

        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.current_batch, [])
        self.assertEqual(recovered.attempts, 1)
        self.assertIsNotNone(recovered.next_retry_at)
        self.assertEqual([job.id for job in recovered.pending], [1, 2])
        self.assertIn("worker process exited before writing result", recovered.error or "")

    def test_cancel_pending_removes_matching_candidate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    pending=[_job(1, "first"), _job(2, "second"), _job(3, "third")],
                    next_id=4,
                ),
            )

            state, canceled = store.cancel_pending("agent-1", 2)

        self.assertTrue(canceled)
        self.assertEqual([job.id for job in state.pending], [1, 3])
        self.assertEqual([job.message for job in state.pending], ["first", "third"])

    def test_cancel_pending_leaves_current_batch_unchanged(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="running",
                    pid=12345,
                    current_batch=[_job(1, "running")],
                    pending=[_job(2, "pending")],
                    next_id=3,
                ),
            )
            with store._worker_locked():
                store._write_worker_locked(
                    status="running",
                    pid=12345,
                    batch_id="update-batch-test",
                    session_ids=["agent-1"],
                    error=None,
                )

            with patch("rightmemory.async_update._process_exists", return_value=True):
                state, canceled = store.cancel_pending("agent-1", 1)

        self.assertFalse(canceled)
        self.assertEqual([job.id for job in state.current_batch], [1])
        self.assertEqual([job.id for job in state.pending], [2])

    def test_cancel_pending_missing_candidate_leaves_state_unchanged(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    pending=[_job(1, "first")],
                    next_id=2,
                ),
            )

            state, canceled = store.cancel_pending("agent-1", 99)

        self.assertFalse(canceled)
        self.assertEqual([job.id for job in state.pending], [1])

    def test_cancel_pending_rejects_invalid_candidate_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")

            with self.assertRaisesRegex(ValueError, "candidate id must be a positive integer"):
                store.cancel_pending("agent-1", "1")

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

            def fake_now():
                if slept:
                    return _dt("2026-05-16T00:00:00+00:00")
                return _dt("2026-05-15T00:10:00+00:00")

            with patch("rightmemory.async_update._now_dt", side_effect=fake_now):
                result = store.run_pending_batches(
                    lambda batch_session_id, message: calls.append(message) or "ok",
                    target_batch_candidates=15,
                    max_wait_seconds=86400,
                    sleep_until=slept.append,
                )

        self.assertEqual(len(slept), 1)
        self.assertEqual(slept[0], _dt("2026-05-15T00:10:30+00:00"))
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(calls), 1)

    def test_global_worker_rechecks_no_work_exit_when_submit_wakes_it(self):
        calls = 0
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")

            def fake_next_batch(target_batch_candidates, max_wait_seconds):
                nonlocal calls
                calls += 1
                if calls == 1:
                    with store._worker_locked():
                        store._increment_wake_counter_locked()
                return None, None

            with patch.object(store, "_next_batch", side_effect=fake_next_batch):
                result = store.run_pending_batches(
                    Mock(side_effect=AssertionError("no batch should run")),
                    target_batch_candidates=15,
                    max_wait_seconds=86400,
                )

        self.assertEqual(result.status, "idle")
        self.assertEqual(calls, 2)

    def test_worker_exits_without_processing_when_leader_lock_is_held(self):
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
                    pending=[_job(1, "queued")],
                    next_id=2,
                ),
            )
            store.worker_root.mkdir(parents=True, exist_ok=True)
            lock_path = store.worker_root / "leader.lock"
            calls = []

            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    result = store.run_pending_batches(
                        lambda batch_session_id, message: calls.append((batch_session_id, message)) or "processed",
                        target_batch_candidates=15,
                        max_wait_seconds=86400,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            state = store.read("agent-1")

        self.assertEqual(result.status, "idle")
        self.assertEqual(result.processed, 0)
        self.assertFalse(result.failed)
        self.assertEqual(calls, [])
        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "waiting")
        self.assertEqual([job.id for job in state.pending], [1])
        self.assertEqual(state.current_batch, [])
        self.assertIsNone(state.error)

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

        self.assertEqual(result.status, "succeeded")
        self.assertGreaterEqual(len(calls), 1)
        self.assertIn("retry first", calls[0])
        self.assertNotIn("normal later", calls[0])

    def test_global_worker_failure_returns_all_current_batches_to_pending(self):
        retry_ready = False
        slept = []
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

            def fake_now():
                if retry_ready:
                    return _dt("2026-05-15T01:00:00+00:00")
                return _dt("2026-05-15T00:00:00+00:00")

            def fake_sleep(deadline):
                nonlocal retry_ready
                slept.append(deadline)
                retry_ready = True

            run_message = Mock(side_effect=[RuntimeError("isolated failure"), "ok"])
            with patch("rightmemory.async_update._now_dt", side_effect=fake_now):
                result = store.run_pending_batches(
                    run_message,
                    target_batch_candidates=2,
                    max_wait_seconds=86400,
                    sleep_until=fake_sleep,
                )
            first = store.read("agent-1")
            second = store.read("agent-2")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(run_message.call_count, 2)
        self.assertEqual(slept, [_dt("2026-05-15T00:00:30+00:00")])
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(first.attempts, 0)
        self.assertEqual(second.attempts, 0)
        self.assertIsNone(first.next_retry_at)
        self.assertIsNone(second.next_retry_at)
        self.assertEqual(first.pending, [])
        self.assertEqual(second.pending, [])
        self.assertEqual(first.current_batch, [])
        self.assertEqual(second.current_batch, [])
        self.assertIsNone(first.error)
        self.assertIsNone(second.error)

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
                patch("rightmemory.async_update._process_exists", return_value=True),
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

    def test_retry_manual_recovery_skips_malformed_state_files(self):
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
            bad_path = store._state_path("broken-agent")
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_path.write_text("{not json", encoding="utf-8")
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process),
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T04:00:00+00:00")),
            ):
                result = store.retry_manual_recovery()
            state = store.read("agent-1")

        self.assertEqual(result.requeued_sessions, 1)
        self.assertEqual(result.requeued_candidates, 1)
        self.assertEqual(result.skipped_sessions, 1)
        self.assertEqual(result.worker_pid, 4242)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.next_retry_at, "2026-05-15T04:00:00+00:00")

    def test_retry_manual_recovery_restores_manual_state_when_worker_start_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            for session_id, message in (("agent-1", "first"), ("agent-2", "second")):
                store._write(
                    session_id,
                    AsyncUpdateState(
                        status="needs_manual_recovery",
                        session_id=session_id,
                        role="update",
                        attempts=2,
                        error="boom",
                        last_error="boom",
                        pending=[_job(1, message)],
                        next_id=2,
                    ),
                )

            with (
                patch("rightmemory.async_update.subprocess.Popen", side_effect=OSError("spawn failed")),
                patch("rightmemory.async_update._now_dt", return_value=_dt("2026-05-15T04:00:00+00:00")),
            ):
                result = store.retry_manual_recovery()
            first = store.read("agent-1")
            second = store.read("agent-2")

        self.assertEqual(result.requeued_sessions, 0)
        self.assertEqual(result.requeued_candidates, 0)
        self.assertEqual(result.skipped_sessions, 0)
        self.assertEqual(result.worker_action, "failed")
        self.assertEqual(result.worker_error, "OSError: spawn failed")
        for state, message in ((first, "first"), (second, "second")):
            self.assertEqual(state.status, "needs_manual_recovery")
            self.assertEqual(state.attempts, 2)
            self.assertIsNone(state.next_retry_at)
            self.assertEqual(state.error, "OSError: spawn failed")
            self.assertEqual(state.last_error, "OSError: spawn failed")
            self.assertEqual([job.message for job in state.pending], [message])

    def test_retry_manual_recovery_restores_manual_state_when_worker_state_is_malformed(self):
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
            worker_state = store._worker_state_path()
            worker_state.parent.mkdir(parents=True, exist_ok=True)
            worker_state.write_text("{not json", encoding="utf-8")

            result = store.retry_manual_recovery()
            state = store.read("agent-1")

        self.assertEqual(result.requeued_sessions, 0)
        self.assertEqual(result.requeued_candidates, 0)
        self.assertEqual(result.worker_action, "failed")
        self.assertTrue(result.worker_error.startswith("JSONDecodeError:"))
        self.assertEqual(state.status, "needs_manual_recovery")
        self.assertIsNone(state.next_retry_at)
        self.assertTrue(state.error.startswith("JSONDecodeError:"))
        self.assertEqual([job.message for job in state.pending], ["first"])

    def test_read_rejects_state_missing_required_identity_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"status": "succeeded", "result": "done"}), encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                store.read("agent-1")

        self.assertIn("async update state must contain string field: session_id", str(caught.exception))

    def test_read_rejects_legacy_queue_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "agent-1",
                        "role": "update",
                        "current": {"id": 1, "message": "first", "submitted_at": "2026-05-15T00:00:00+00:00"},
                        "queued": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                store.read("agent-1")

        self.assertIn("unsupported legacy job fields", str(caught.exception))


def _job(job_id: int, message: str) -> AsyncUpdateJob:
    return AsyncUpdateJob(id=job_id, message=message, submitted_at="2026-05-15T00:00:00+00:00")


def _dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


if __name__ == "__main__":
    unittest.main()
