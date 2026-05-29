import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore


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
        self.assertIsNone(recovered.next_flush_at)
        self.assertEqual(recovered.current_batch, [])
        self.assertEqual([job.id for job in recovered.pending], [1, 2])
        self.assertEqual(recovered.pending[0].message, "interrupted")
        self.assertIn("worker process exited before writing result", recovered.error or "")

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

    def test_submit_after_failed_state_preserves_pending_order_and_starts_worker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    finished_at="2026-05-15T00:00:00+00:00",
                    error="previous failure",
                    pending=[_job(1, "retry first")],
                    next_id=2,
                ),
            )
            process = Mock(pid=4242)

            with patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen:
                state = store.submit("agent-1", "new update")
                with store._worker_locked():
                    worker = store._read_worker_locked()

        popen.assert_called_once()
        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "waiting")
        self.assertEqual(worker["pid"], 4242)
        self.assertEqual([job.message for job in state.pending], ["retry first", "new update"])
        self.assertEqual([job.id for job in state.pending], [1, 2])
        self.assertEqual(state.current_batch, [])

    def test_submit_after_failed_state_recovers_leftover_current_batch(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    phase=None,
                    finished_at="2026-05-15T00:00:00+00:00",
                    error="previous failure",
                    current_batch=[_job(1, "interrupted first"), _job(2, "interrupted second")],
                    pending=[_job(3, "already pending")],
                    next_id=4,
                ),
            )
            process = Mock(pid=4242)

            with patch("rightmemory.async_update.subprocess.Popen", return_value=process):
                state = store.submit("agent-1", "new update")

        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "waiting")
        self.assertEqual(state.current_batch, [])
        self.assertEqual(
            [job.message for job in state.pending],
            ["interrupted first", "interrupted second", "already pending", "new update"],
        )
        self.assertEqual([job.id for job in state.pending], [1, 2, 3, 4])

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

    def test_run_pending_batches_failure_returns_current_batch_to_pending(self):
        callback = Mock()
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
                    pending=[_job(1, "first"), _job(2, "second")],
                    next_id=3,
                ),
            )

            state = store.run_pending_batches(
                "agent-1",
                Mock(side_effect=RuntimeError("isolated failure")),
                on_batch_success=callback,
            )

        self.assertEqual(state.status, "failed")
        self.assertIsNone(state.phase)
        self.assertIsNone(state.next_flush_at)
        self.assertEqual(state.current_batch, [])
        self.assertEqual([job.id for job in state.pending], [1, 2])
        self.assertEqual(state.error, "isolated failure")
        callback.assert_not_called()

    def test_run_pending_batches_success_calls_callback_with_candidate_count(self):
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
                    pending=[_job(1, "first"), _job(2, "second")],
                    next_id=3,
                ),
            )

            state = store.run_pending_batches(
                "agent-1",
                lambda message: "ok",
                on_batch_success=calls.append,
            )

        self.assertEqual(state.status, "succeeded")
        self.assertEqual(calls, [2])

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


if __name__ == "__main__":
    unittest.main()
