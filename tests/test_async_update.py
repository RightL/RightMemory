import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.async_update import (
    AsyncUpdateJob,
    AsyncUpdateSessionBatch,
    AsyncUpdateState,
    AsyncUpdateStore,
    _batch_session_id,
    _is_async_worker_process,
)
from rightmemory.platform import lock_file_nonblocking, unlock_file
from rightmemory.semantic_operation import OperationEffect, SemanticOperationStore
from rightmemory.update_queue import (
    UpdateCandidate,
    UpdateQueueStore,
    update_candidate_batch_id,
)


class AsyncUpdateStateTests(unittest.TestCase):
    def test_local_and_synchronized_batches_share_one_operation_identity(self):
        job = _job(1, "same evidence")
        batch = [
            AsyncUpdateSessionBatch(
                "agent-1",
                _dt("2000-01-01T00:00:00+00:00"),
                [job],
            )
        ]
        candidate = UpdateCandidate(
            uid=job.candidate_uid,
            session_id="agent-1",
            display_id=job.id,
            message=job.message,
            submitted_at=job.submitted_at,
        )

        self.assertEqual(
            _batch_session_id(batch),
            update_candidate_batch_id((candidate,)),
        )

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
            patch("rightmemory.async_update.process_command", return_value="[kworker/R-rcu_g]"),
        ):
            active = _is_async_worker_process(4, "update")

        self.assertFalse(active)

    def test_submit_replaces_stale_worker_pid_that_belongs_to_another_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            with patch("rightmemory.async_update.process_identity", return_value="proc:original"):
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
                patch("rightmemory.async_update.process_identity", return_value="proc:replacement"),
            ):
                state = store.submit("agent-1", "first")

        popen.assert_called_once()
        self.assertEqual(state.status, "running")

    def test_submit_creates_and_persists_candidate_uid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            with patch.object(store, "_start_worker_if_needed"):
                submitted = store.submit("agent-1", "first")
            loaded = store.read("agent-1")
            persisted = json.loads(store._state_path("agent-1").read_text(encoding="utf-8"))

        candidate_uid = submitted.pending[0].candidate_uid
        self.assertRegex(candidate_uid, r"^[0-9a-f]{32}$")
        self.assertEqual(loaded.pending[0].candidate_uid, candidate_uid)
        self.assertEqual(persisted["pending"][0]["candidate_uid"], candidate_uid)

    def test_submit_with_caller_candidate_uid_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            candidate_uid = "a" * 32
            with patch.object(store, "_start_worker_if_needed"):
                first = store.submit("agent-1", "first", candidate_uid=candidate_uid)
                second = store.submit("agent-1", "first", candidate_uid=candidate_uid)

        self.assertEqual(first.pending, second.pending)
        self.assertEqual([job.id for job in second.pending], [1])
        self.assertEqual(second.pending[0].candidate_uid, candidate_uid)
        self.assertEqual(second.next_id, 2)

    def test_candidate_uid_remains_idempotent_after_processing_finishes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            candidate_uid = "a" * 32
            with patch.object(store, "_start_worker_if_needed"):
                store.submit("agent-1", "first", candidate_uid=candidate_uid)
            store.run_pending_batches(
                lambda _session_id, _message: "updated",
                target_batch_candidates=1,
                max_wait_seconds=0,
            )

            with patch.object(store, "_start_worker_if_needed"):
                repeated = store.submit(
                    "agent-1",
                    "first",
                    candidate_uid=candidate_uid,
                )

        self.assertEqual(repeated.pending, [])
        self.assertEqual(repeated.current_batch, [])
        self.assertEqual(repeated.next_id, 2)
        self.assertEqual(repeated.accepted_candidate_uids, [candidate_uid])

    def test_submit_rejects_candidate_uid_reuse_for_different_evidence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            candidate_uid = "b" * 32
            with patch.object(store, "_start_worker_if_needed"):
                store.submit("agent-1", "first", candidate_uid=candidate_uid)
                with self.assertRaisesRegex(ValueError, "different update evidence"):
                    store.submit("agent-1", "second", candidate_uid=candidate_uid)

            state = store.read("agent-1")

        self.assertEqual([job.message for job in state.pending], ["first"])

    def test_submit_rejects_noncanonical_candidate_uid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            with self.assertRaisesRegex(ValueError, "32 lowercase hexadecimal"):
                store.submit("agent-1", "first", candidate_uid="NOT-A-UUID")

            self.assertFalse(store._state_path("agent-1").exists())

    def test_outbox_only_submit_crash_is_reconciled_and_publishable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            candidate = UpdateCandidate(
                uid="a" * 32,
                session_id="agent-1",
                display_id=1,
                message="survived submit crash",
                submitted_at="2026-05-15T00:00:00+00:00",
            )
            UpdateQueueStore(root).write_outbox(candidate)

            publishable = store.publishable_candidate_uids()
            state = store.read("agent-1")

        self.assertEqual(publishable, frozenset({candidate.uid}))
        self.assertEqual(
            state.pending,
            [
                AsyncUpdateJob(
                    id=1,
                    candidate_uid=candidate.uid,
                    message=candidate.message,
                    submitted_at=candidate.submitted_at,
                )
            ],
        )
        self.assertEqual(state.next_id, 2)

    def test_attempted_pending_is_publishable_even_after_scheduler_failure(self):
        for status in ("failed", "needs_manual_recovery"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                store = AsyncUpdateStore(root, "update")
                job = _job(1, "must finish publication")
                store._write(
                    "agent-1",
                    AsyncUpdateState(
                        status=status,
                        session_id="agent-1",
                        role="update",
                        attempts=2,
                        pending=[job],
                        next_id=2,
                    ),
                )
                queue = UpdateQueueStore(root)
                queue.write_outbox(_candidate_from_job("agent-1", job))
                queue.begin_publication(job.candidate_uid, attempted_at=job.submitted_at)

                self.assertEqual(
                    store.publishable_candidate_uids(),
                    frozenset({job.candidate_uid}),
                )

    def test_acknowledge_synchronized_normalizes_an_empty_local_lane(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            job = _job(1, "published")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    pid=4242,
                    next_flush_at="2099-01-01T00:00:00+00:00",
                    pending=[job],
                    next_id=2,
                ),
            )

            removed = store.acknowledge_synchronized(frozenset({job.candidate_uid}))
            with patch.object(store, "_worker_snapshot", side_effect=AssertionError("not needed")):
                state = store.read("agent-1")

        self.assertEqual(removed, 1)
        self.assertEqual(state.status, "succeeded")
        self.assertIsNone(state.phase)
        self.assertIsNone(state.pid)
        self.assertIsNone(state.next_flush_at)
        self.assertEqual(state.pending, [])
        self.assertEqual(state.next_id, 2)

    def test_reserved_and_current_candidates_are_not_publishable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            current = _job(1, "current")
            reserved = _job(2, "reserved")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="running",
                    current_batch=[current],
                    next_id=2,
                ),
            )
            store._write(
                "agent-2",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-2",
                    role="update",
                    phase="waiting",
                    pending=[reserved],
                    next_id=3,
                ),
            )
            queue = UpdateQueueStore(root)
            queue.write_outbox(_candidate_from_job("agent-1", current))
            queue.write_outbox(_candidate_from_job("agent-2", reserved))
            batch = [
                AsyncUpdateSessionBatch(
                    "agent-2",
                    _dt("2000-01-01T00:00:00+00:00"),
                    [reserved],
                )
            ]
            store._reserve_cross_session_batch(batch, _batch_session_id(batch))

            publishable = store.publishable_candidate_uids()

        self.assertEqual(publishable, frozenset())

    def test_begin_publication_serializes_with_cancel(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                state = store.submit("agent-1", "race candidate")
            job = state.pending[0]
            candidate = _candidate_from_job("agent-1", job)
            publication_started = threading.Event()
            allow_publication = threading.Event()
            cancel_finished = threading.Event()
            results: dict[str, object] = {}
            errors: list[BaseException] = []
            original = UpdateQueueStore.begin_publication

            def delayed_begin(queue, uid, *, attempted_at, attempt_id=None):
                publication_started.set()
                if not allow_publication.wait(2):
                    raise TimeoutError("publication test gate timed out")
                return original(
                    queue,
                    uid,
                    attempted_at=attempted_at,
                    attempt_id=attempt_id,
                )

            def publish():
                try:
                    results["marker"] = store.begin_publication(
                        candidate,
                        attempted_at=job.submitted_at,
                    )
                except BaseException as exc:  # Capture thread failures for the assertion thread.
                    errors.append(exc)

            def cancel():
                try:
                    results["cancel"] = store.cancel_pending("agent-1", job.id)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    cancel_finished.set()

            with patch.object(UpdateQueueStore, "begin_publication", new=delayed_begin):
                publishing = threading.Thread(target=publish)
                canceling = threading.Thread(target=cancel)
                publishing.start()
                self.assertTrue(publication_started.wait(2))
                canceling.start()
                self.assertFalse(cancel_finished.wait(0.05))
                allow_publication.set()
                publishing.join(2)
                canceling.join(2)

            self.assertFalse(publishing.is_alive())
            self.assertFalse(canceling.is_alive())
            self.assertEqual(errors, [])
            canceled_state, canceled = results["cancel"]

        self.assertIsNotNone(results["marker"])
        self.assertFalse(canceled)
        self.assertEqual(canceled_state.pending, [job])

    def test_cancel_result_retains_git_authority_after_marker_cleanup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                submitted = store.submit("agent-1", "publication race")
            job = submitted.pending[0]
            queue_store = UpdateQueueStore(root)
            queue_store.begin_publication(
                job.candidate_uid,
                attempted_at=job.submitted_at,
            )

            result = store.cancel_pending_candidate("agent-1", job.id)
            queue_store.clear_publication_marker(job.candidate_uid)

        self.assertEqual(result.outcome, "publication_started")
        self.assertEqual(result.candidate, job)
        self.assertEqual(result.state.pending, [job])

    def test_stale_publication_snapshot_cannot_resurrect_canceled_candidate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                submitted = store.submit("agent-1", "cancel before publication")
            job = submitted.pending[0]
            stale_candidate = _candidate_from_job("agent-1", job)

            _state, canceled = store.cancel_pending("agent-1", job.id)
            marker = store.begin_publication(
                stale_candidate,
                attempted_at=job.submitted_at,
            )

        self.assertTrue(canceled)
        self.assertIsNone(marker)

    def test_reserved_candidate_cannot_cross_publication_boundary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                submitted = store.submit("agent-1", "reserve before publication")
            job = submitted.pending[0]
            batch = [
                AsyncUpdateSessionBatch(
                    "agent-1",
                    _dt("2000-01-01T00:00:00+00:00"),
                    [job],
                )
            ]
            store._reserve_cross_session_batch(batch, _batch_session_id(batch))

            marker = store.begin_publication(
                _candidate_from_job("agent-1", job),
                attempted_at=job.submitted_at,
            )

        self.assertIsNone(marker)

    def test_before_batches_runs_again_after_worker_wait(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            job = _job(1, "publish before local selection")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2099-01-01T00:00:00+00:00",
                    pending=[job],
                    next_id=2,
                ),
            )
            candidate = _candidate_from_job("agent-1", job)
            UpdateQueueStore(root).write_outbox(candidate)
            before_calls = 0
            run_message = Mock(side_effect=AssertionError("attempted candidate must not run locally"))

            def before_batches():
                nonlocal before_calls
                before_calls += 1
                if before_calls == 2:
                    self.assertIsNotNone(
                        store.begin_publication(candidate, attempted_at=job.submitted_at)
                    )
                return True

            def finish_wait(_deadline):
                state = store._read_raw("agent-1")
                store._write(
                    "agent-1",
                    replace(state, next_flush_at="2000-01-01T00:00:00+00:00"),
                )

            result = store.run_pending_batches(
                run_message,
                target_batch_candidates=15,
                max_wait_seconds=0,
                sleep_until=finish_wait,
                before_batches=before_batches,
            )

        self.assertEqual(result.status, "idle")
        self.assertEqual(before_calls, 2)
        run_message.assert_not_called()

    def test_cancel_write_failure_restores_candidate_for_publication(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            with patch.object(store, "_start_worker_if_needed"):
                submitted = store.submit("agent-1", "keep after interrupted cancel")
            job = submitted.pending[0]

            with patch.object(store, "_write", side_effect=OSError("interrupted cancel")):
                with self.assertRaisesRegex(OSError, "interrupted cancel"):
                    store.cancel_pending("agent-1", job.id)

            self.assertIsNone(UpdateQueueStore(root).read_outbox(job.candidate_uid))
            recovered = store.read("agent-1")
            restored = UpdateQueueStore(root).read_outbox(job.candidate_uid)
            batch, _deadline = store._next_batch(1, 0)

        self.assertEqual(recovered.pending, [job])
        self.assertEqual(restored, _candidate_from_job("agent-1", job))
        self.assertIsNotNone(batch)
        self.assertEqual(batch[0].jobs, [job])

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

    def test_dead_worker_running_batch_preserves_reserved_batch_for_retry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            interrupted = _job(1, "interrupted")
            already_pending = _job(2, "already pending")
            operation_id = _batch_session_id(
                [AsyncUpdateSessionBatch("agent-1", _dt("2026-05-15T00:00:00+00:00"), [interrupted])]
            )
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="running",
                    started_at="2026-05-15T00:00:00+00:00",
                    pid=12345,
                    current_operation_id=operation_id,
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
        self.assertEqual([job.id for job in recovered.current_batch], [1])
        self.assertEqual([job.id for job in recovered.pending], [2])
        self.assertEqual(recovered.current_batch[0].message, "interrupted")
        self.assertEqual(recovered.current_operation_id, operation_id)
        self.assertIn("worker process exited before writing result", recovered.error or "")

    def test_terminal_operation_acknowledges_dead_batch_without_callback(self):
        for phase in ("committed", "no_change"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                store = AsyncUpdateStore(root, "update")
                job = _job(1, f"{phase} candidate")
                batch = [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [job])]
                operation_id = _batch_session_id(batch)
                store._write(
                    "agent-1",
                    AsyncUpdateState(
                        status="running",
                        session_id="agent-1",
                        role="update",
                        phase="running",
                        pid=12345,
                        current_operation_id=operation_id,
                        current_batch=[job],
                        next_id=2,
                    ),
                )
                _record_terminal_operation(root, operation_id, phase=phase, output=f"{phase} output")
                callback = Mock(side_effect=AssertionError("terminal operation must not rerun"))

                result = store.run_pending_batches(
                    callback,
                    target_batch_candidates=1,
                    max_wait_seconds=0,
                )
                state = store.read("agent-1")

            callback.assert_not_called()
            self.assertEqual(result.status, "idle")
            self.assertEqual(state.status, "succeeded")
            self.assertEqual(state.result, f"{phase} output")
            self.assertEqual(state.current_batch, [])
            self.assertIsNone(state.current_operation_id)
            self.assertEqual(state.last_operation_id, operation_id)

    def test_terminal_operation_completed_during_start_skips_callback(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            job = _job(1, "already handled during start")
            batch = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [job])
            ]
            operation_id = _batch_session_id(batch)
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    next_flush_at="2000-01-01T00:00:00+00:00",
                    pending=[job],
                    next_id=2,
                ),
            )
            original_start = store._start_cross_session_batch

            def start_and_complete(start_batch, batch_id):
                started = original_start(start_batch, batch_id)
                _record_terminal_operation(
                    root,
                    batch_id,
                    phase="committed",
                    output="completed during start",
                )
                return started

            callback = Mock(side_effect=AssertionError("terminal operation must not rerun"))
            with patch.object(store, "_start_cross_session_batch", side_effect=start_and_complete):
                result = store.run_pending_batches(
                    callback,
                    target_batch_candidates=1,
                    max_wait_seconds=0,
                )
            state = store.read("agent-1")

        callback.assert_not_called()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.processed, 1)
        self.assertEqual(state.result, "completed during start")
        self.assertEqual(state.last_operation_id, operation_id)

    def test_terminal_operation_recovers_partial_ack_and_preserves_new_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            first_job = _job(1, "first")
            second_job = _job(1, "second")
            new_job = _job(2, "newer")
            batch = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [first_job]),
                AsyncUpdateSessionBatch("agent-2", _dt("2000-01-01T00:00:00+00:00"), [second_job]),
            ]
            operation_id = _batch_session_id(batch)
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="succeeded",
                    session_id="agent-1",
                    role="update",
                    result="saved output",
                    last_operation_id=operation_id,
                    next_id=2,
                ),
            )
            store._write(
                "agent-2",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-2",
                    role="update",
                    phase="running",
                    pid=12345,
                    next_flush_at="2099-01-01T00:00:00+00:00",
                    current_operation_id=operation_id,
                    current_batch=[second_job],
                    pending=[new_job],
                    next_id=3,
                ),
            )
            store._reserve_cross_session_batch(batch, operation_id)
            _record_terminal_operation(root, operation_id, phase="committed", output="saved output")
            reservation_path = store._reservation_path(operation_id)
            self.assertTrue(reservation_path.exists())

            recovered = store.read("agent-2")
            already_finished = store.read("agent-1")
            self.assertFalse(reservation_path.exists())

        self.assertEqual(already_finished.last_operation_id, operation_id)
        self.assertEqual(recovered.status, "running")
        self.assertEqual(recovered.phase, "waiting")
        self.assertEqual(recovered.current_batch, [])
        self.assertIsNone(recovered.current_operation_id)
        self.assertEqual(recovered.last_operation_id, operation_id)
        self.assertEqual([job.message for job in recovered.pending], ["newer"])

    def test_batch_identity_includes_full_message_content(self):
        first = [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [_job(1, "alpha")])]
        second = [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [_job(1, "beta")])]

        first_id = _batch_session_id(first)
        second_id = _batch_session_id(second)

        self.assertNotEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("update-batch-"))
        self.assertEqual(len(first_id), len("update-batch-") + 64)

    def test_missing_operation_receipt_retries_same_reserved_batch(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = AsyncUpdateStore(root, "update")
            job = _job(1, "retry only this")
            batch = [AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [job])]
            operation_id = _batch_session_id(batch)
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    current_operation_id=operation_id,
                    current_batch=[job],
                    next_id=2,
                ),
            )

            result = store.run_pending_batches(
                lambda batch_id, message: calls.append((batch_id, message)) or "retried output",
                target_batch_candidates=15,
                max_wait_seconds=86400,
            )
            state = store.read("agent-1")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], operation_id)
        self.assertEqual(state.status, "succeeded")
        self.assertEqual(state.last_operation_id, operation_id)

    def test_cross_session_operation_waits_for_every_participant_before_retry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            first_job = _job(1, "first")
            second_job = _job(1, "second")
            batch = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [first_job]),
                AsyncUpdateSessionBatch("agent-2", _dt("2000-01-01T00:00:00+00:00"), [second_job]),
            ]
            operation_id = _batch_session_id(batch)
            for session_id, job, retry_at in (
                ("agent-1", first_job, "2000-01-01T00:00:00+00:00"),
                ("agent-2", second_job, "2099-01-01T00:00:00+00:00"),
            ):
                store._write(
                    session_id,
                    AsyncUpdateState(
                        status="failed",
                        session_id=session_id,
                        role="update",
                        attempts=1,
                        next_retry_at=retry_at,
                        current_operation_id=operation_id,
                        current_batch=[job],
                        next_id=2,
                    ),
                )
            store._reserve_cross_session_batch(batch, operation_id)

            selected, deadline = store._next_batch(target_batch_candidates=15, max_wait_seconds=86400)

        self.assertIsNone(selected)
        self.assertEqual(deadline, _dt("2099-01-01T00:00:00+00:00"))

    def test_reserved_batch_recovers_exact_participants_after_crash_mid_start(self):
        class StopAfterRecovery(Exception):
            pass

        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            first_job = _job(1, "first original")
            second_job = _job(1, "second original")
            newer_job = _job(2, "newer candidate")
            batch = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [first_job]),
                AsyncUpdateSessionBatch("agent-2", _dt("2000-01-01T00:00:00+00:00"), [second_job]),
            ]
            operation_id = _batch_session_id(batch)
            for session_id, job in (("agent-1", first_job), ("agent-2", second_job)):
                store._write(
                    session_id,
                    AsyncUpdateState(
                        status="running",
                        session_id=session_id,
                        role="update",
                        phase="waiting",
                        next_flush_at="2000-01-01T00:00:00+00:00",
                        pending=[job],
                        next_id=2,
                    ),
                )

            original_write = store._write

            def crash_on_second_start(session_id, state):
                if session_id == "agent-2" and state.current_operation_id == operation_id:
                    raise RuntimeError("simulated crash during batch start")
                original_write(session_id, state)

            with patch.object(store, "_write", side_effect=crash_on_second_start):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    store.run_pending_batches(
                        lambda _batch_id, _message: "must not run",
                        target_batch_candidates=2,
                        max_wait_seconds=0,
                    )

            reservation_path = store._reservation_path(operation_id)
            first_after_crash = store._read_raw("agent-1")
            second_after_crash = store._read_raw("agent-2")
            self.assertTrue(reservation_path.exists())
            self.assertEqual(first_after_crash.current_batch, [first_job])
            self.assertEqual(first_after_crash.current_operation_id, operation_id)
            self.assertEqual(second_after_crash.pending, [second_job])

            original_write(
                "agent-1",
                replace(
                    first_after_crash,
                    pending=[newer_job],
                    next_flush_at="2099-01-01T00:00:00+00:00",
                    next_id=3,
                ),
            )

            def run_message(batch_id, message):
                calls.append((batch_id, message))
                return "recovered"

            def stop_after_recovery(_deadline):
                raise StopAfterRecovery

            with self.assertRaises(StopAfterRecovery):
                store.run_pending_batches(
                    run_message,
                    target_batch_candidates=2,
                    max_wait_seconds=0,
                    sleep_until=stop_after_recovery,
                )

            first = store._read_raw("agent-1")
            second = store._read_raw("agent-2")
            reservation_cleared = not reservation_path.exists()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], operation_id)
        self.assertEqual(first.last_operation_id, operation_id)
        self.assertEqual(first.pending, [newer_job])
        self.assertEqual(second.last_operation_id, operation_id)
        self.assertTrue(reservation_cleared)

    def test_reserved_batch_recovers_legacy_operation_id_assignment_gap(self):
        calls = []
        callback_states = []
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            first_job = _job(1, "already moved")
            second_job = _job(1, "not moved yet")
            batch = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [first_job]),
                AsyncUpdateSessionBatch("agent-2", _dt("2000-01-01T00:00:00+00:00"), [second_job]),
            ]
            operation_id = _batch_session_id(batch)
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="failed",
                    session_id="agent-1",
                    role="update",
                    attempts=1,
                    next_retry_at="2000-01-01T00:00:00+00:00",
                    current_batch=[first_job],
                    next_id=2,
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
                    pending=[second_job],
                    next_id=2,
                ),
            )
            store._reserve_cross_session_batch(batch, operation_id)

            def run_message(batch_id, message):
                calls.append((batch_id, message))
                callback_states.extend((store._read_raw("agent-1"), store._read_raw("agent-2")))
                return "recovered"

            result = store.run_pending_batches(
                run_message,
                target_batch_candidates=2,
                max_wait_seconds=0,
            )
            first = store.read("agent-1")
            second = store.read("agent-2")
            reservation_cleared = not store._reservation_path(operation_id).exists()

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.processed, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], operation_id)
        self.assertEqual(
            [state.current_operation_id for state in callback_states],
            [operation_id, operation_id],
        )
        self.assertEqual(first.last_operation_id, operation_id)
        self.assertEqual(second.last_operation_id, operation_id)
        self.assertTrue(reservation_cleared)

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
                patch("rightmemory.async_update.process_command", return_value="python -m rightmemory.cli update _async-worker"),
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
        self.assertEqual([job.id for job in state.current_batch], [1, 2])
        self.assertEqual(
            [job.message for job in state.pending],
            ["already pending", "new update"],
        )
        self.assertEqual([job.id for job in state.pending], [3, 4])

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
            selected = [
                AsyncUpdateSessionBatch("agent-1", _dt("2000-01-01T00:00:00+00:00"), [original])
            ]
            operation_id = _batch_session_id(selected)
            reserved = store._reserve_cross_session_batch(selected, operation_id)
            started = store._start_cross_session_batch(reserved.participants, operation_id)

            with patch("rightmemory.async_update.subprocess.Popen") as popen:
                submitted = store.submit("agent-1", "new while running")
            accepted = store._finish_cross_session_batch(started, operation_id, "ok")
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
            operation_id = _batch_session_id(selected)
            reserved = store._reserve_cross_session_batch(selected, operation_id)
            started = store._start_cross_session_batch(reserved.participants, operation_id)
            accepted = store._finish_cross_session_batch(started, operation_id, "ok")
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
        self.assertEqual([job.id for job in recovered.current_batch], [1])
        self.assertEqual(recovered.attempts, 1)
        self.assertIsNotNone(recovered.next_retry_at)
        self.assertEqual([job.id for job in recovered.pending], [2])
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

    def test_cancel_pending_does_not_remove_a_reserved_candidate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            reserved_job = _job(1, "reserved")
            newer_job = _job(2, "not reserved")
            store._write(
                "agent-1",
                AsyncUpdateState(
                    status="running",
                    session_id="agent-1",
                    role="update",
                    phase="waiting",
                    pending=[reserved_job, newer_job],
                    next_id=3,
                ),
            )
            batch = [
                AsyncUpdateSessionBatch(
                    "agent-1",
                    _dt("2000-01-01T00:00:00+00:00"),
                    [reserved_job],
                )
            ]
            operation_id = _batch_session_id(batch)
            store._reserve_cross_session_batch(batch, operation_id)

            reserved_state, reserved_canceled = store.cancel_pending("agent-1", 1)
            final_state, newer_canceled = store.cancel_pending("agent-1", 2)

        self.assertFalse(reserved_canceled)
        self.assertEqual(reserved_state.pending, [reserved_job, newer_job])
        self.assertTrue(newer_canceled)
        self.assertEqual(final_state.pending, [reserved_job])

    def test_cancel_between_selection_and_reservation_reselects_remaining_candidate(self):
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
                    pending=[_job(1, "cancel me"), _job(2, "keep me")],
                    next_id=3,
                ),
            )
            original_reserve = store._reserve_cross_session_batch
            canceled = False

            def cancel_before_first_reservation(batch, operation_id):
                nonlocal canceled
                if not canceled:
                    canceled = True
                    _, did_cancel = store.cancel_pending("agent-1", 1)
                    self.assertTrue(did_cancel)
                return original_reserve(batch, operation_id)

            with patch.object(
                store,
                "_reserve_cross_session_batch",
                side_effect=cancel_before_first_reservation,
            ):
                result = store.run_pending_batches(
                    lambda operation_id, message: calls.append((operation_id, message)) or "processed",
                    target_batch_candidates=2,
                    max_wait_seconds=0,
                )
            state = store.read("agent-1")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.processed, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state.current_batch, [])
        self.assertEqual(state.pending, [])

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
                lock_file_nonblocking(handle)
                try:
                    result = store.run_pending_batches(
                        lambda batch_session_id, message: calls.append((batch_session_id, message)) or "processed",
                        target_batch_candidates=15,
                        max_wait_seconds=86400,
                    )
                finally:
                    unlock_file(handle)
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
        self.assertEqual([job.message for job in state.current_batch], ["retry me"])
        self.assertEqual(state.pending, [])
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
                    pending=[
                        replace(_job(1, "second"), candidate_uid="a" * 32),
                        _job(2, "third"),
                    ],
                    next_id=3,
                ),
            )
            process = Mock(pid=4242)

            with (
                patch("rightmemory.async_update.subprocess.Popen", return_value=process) as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.async_update.process_command", return_value="python -m rightmemory.cli update _async-worker"),
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
                patch("rightmemory.async_update.process_command", return_value="python -m rightmemory.cli update _async-worker"),
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
                job = _job(1, message)
                if session_id == "agent-2":
                    job = replace(job, candidate_uid="a" * 32)
                store._write(
                    session_id,
                    AsyncUpdateState(
                        status="needs_manual_recovery",
                        session_id=session_id,
                        role="update",
                        attempts=2,
                        error="boom",
                        last_error="boom",
                        pending=[job],
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

    def test_read_rejects_nonpositive_or_boolean_candidate_ids(self):
        job = {
            "id": 1,
            "candidate_uid": f"{1:032x}",
            "message": "first",
            "submitted_at": "2026-05-15T00:00:00+00:00",
        }
        cases = (
            ("boolean next id", True, [job]),
            ("zero next id", 0, []),
            ("negative next id", -1, []),
            ("boolean job id", 2, [{**job, "id": True}]),
            ("zero job id", 2, [{**job, "id": 0}]),
            ("negative job id", 2, [{**job, "id": -1}]),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            for name, next_id, pending in cases:
                with self.subTest(name=name):
                    state_path.write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "session_id": "agent-1",
                                "role": "update",
                                "current_batch": [],
                                "pending": pending,
                                "next_id": next_id,
                            }
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaises(ValueError):
                        store.read("agent-1")

    def test_read_rejects_duplicate_or_out_of_order_candidate_ids(self):
        def job(job_id: int) -> dict[str, object]:
            return {
                "id": job_id,
                "candidate_uid": f"{job_id:032x}",
                "message": f"candidate {job_id}",
                "submitted_at": "2026-05-15T00:00:00+00:00",
            }

        cases = (
            ("duplicate", [job(1)], [job(1)]),
            ("out of order in pending", [], [job(2), job(1)]),
            ("out of order across lists", [job(2)], [job(1)]),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            for name, current_batch, pending in cases:
                with self.subTest(name=name):
                    state_path.write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "session_id": "agent-1",
                                "role": "update",
                                "current_batch": current_batch,
                                "pending": pending,
                                "next_id": 3,
                            }
                        ),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, "unique and strictly increasing"):
                        store.read("agent-1")

    def test_read_rejects_next_id_not_above_every_live_job(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "session_id": "agent-1",
                        "role": "update",
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 2,
                                "candidate_uid": f"{2:032x}",
                                "message": "candidate",
                                "submitted_at": "2026-05-15T00:00:00+00:00",
                            }
                        ],
                        "next_id": 2,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "greater than every live job id"):
                store.read("agent-1")

    def test_read_rejects_duplicate_candidate_uids(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            duplicate_uid = "c" * 32
            state_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "agent-1",
                        "role": "update",
                        "current_batch": [],
                        "pending": [
                            {
                                "id": 1,
                                "candidate_uid": duplicate_uid,
                                "message": "first",
                                "submitted_at": "2026-05-15T00:00:00+00:00",
                            },
                            {
                                "id": 2,
                                "candidate_uid": duplicate_uid,
                                "message": "second",
                                "submitted_at": "2026-05-15T00:01:00+00:00",
                            },
                        ],
                        "next_id": 3,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "candidate uids must be unique"):
                store.read("agent-1")

    def test_read_rejects_non_object_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            state_path.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                store.read("agent-1")


def _job(job_id: int, message: str) -> AsyncUpdateJob:
    return AsyncUpdateJob(
        id=job_id,
        candidate_uid=f"{job_id:032x}",
        message=message,
        submitted_at="2026-05-15T00:00:00+00:00",
    )


def _candidate_from_job(session_id: str, job: AsyncUpdateJob) -> UpdateCandidate:
    return UpdateCandidate(
        uid=job.candidate_uid,
        session_id=session_id,
        display_id=job.id,
        message=job.message,
        submitted_at=job.submitted_at,
    )


def _record_terminal_operation(root: Path, operation_id: str, *, phase: str, output: str) -> None:
    operation_store = SemanticOperationStore(root)
    effects = (OperationEffect("pending-test-effect"),)
    operation_store.begin(operation_id, {"test_operation": operation_id}, effects=effects)
    changed_paths = ("MEMORY.md",) if phase == "committed" else ()
    operation_store.prepare_outcome(
        operation_id,
        output=output,
        start_commit="base-commit",
        changed_paths=changed_paths,
    )
    if phase == "committed":
        operation_store.complete_commit(operation_id, "landed-commit")
    else:
        operation_store.complete_no_change(operation_id)


def _dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


if __name__ == "__main__":
    unittest.main()
