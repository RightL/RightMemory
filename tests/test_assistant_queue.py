from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.async_update import AsyncUpdateStore
from rightmemory.update_queue import UpdateQueueStore


class AssistantQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.assistant = AsyncUpdateStore(self.root, "update", executor="assistant")
        self.worker = AsyncUpdateStore(self.root, "update")

    def submit(self, session: str, uid_number: int):
        return self.assistant.submit(session, "test evidence", candidate_uid=_uid(uid_number))

    def ready(self, store=None, *, trigger=1, target=1):
        return (store or self.assistant).ready_batch(
            trigger_candidates=trigger, target_batch_candidates=target, max_wait_seconds=0
        )

    def reserve(self, *, trigger=1, target=1):
        batch, deadline = self.ready(trigger=trigger, target=target)
        self.assertIsNone(deadline)
        self.assertIsNotNone(batch)
        reservation = self.assistant.reserve_batch(batch)
        self.assertIsNotNone(reservation)
        return reservation

    def test_assistant_submit_persists_ownership_without_starting_or_inspecting_a_worker(self) -> None:
        with (
            patch.object(AsyncUpdateStore, "_start_worker_if_needed", side_effect=AssertionError("worker started")),
            patch.object(AsyncUpdateStore, "_active_worker_pid", side_effect=AssertionError("worker inspected")),
        ):
            submitted = self.submit("assistant-session", 1)
            loaded = self.assistant.read("assistant-session")

        self.assertEqual(submitted.executor, "assistant")
        self.assertEqual(loaded.executor, "assistant")
        self.assertEqual([job.candidate_uid for job in loaded.pending], [_uid(1)])
        self.assertIsNone(loaded.pid)
        self.assertFalse(self.assistant._worker_state_path().exists())

    def test_default_worker_submit_and_execution_remain_available(self) -> None:
        with patch.object(self.worker, "_start_worker_if_needed") as start:
            submitted = self.worker.submit("worker-session", "test evidence", candidate_uid=_uid(1))
        start.assert_called_once_with("worker-session")
        self.assertEqual(submitted.executor, "worker")
        callback = Mock(return_value="completed")

        result = self.worker.run_pending_batches(
            callback, trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0
        )

        callback.assert_called_once()
        self.assertEqual(result.processed, 1)
        self.assertEqual(self.worker.read("worker-session").pending, [])

    def test_cross_session_batch_uses_the_existing_aggregate_trigger(self) -> None:
        self.submit("first", 1)
        batch, deadline = self.ready(trigger=2, target=2)
        self.assertIsNone(batch)
        self.assertIsNotNone(deadline)
        self.submit("second", 2)

        reservation = self.reserve(trigger=2, target=2)
        candidates = self.assistant.start_reserved_batch(reservation.operation_id)

        self.assertEqual(reservation.executor, "assistant")
        self.assertEqual({item.session_id for item in reservation.participants}, {"first", "second"})
        self.assertEqual({candidate.uid for candidate in candidates}, {_uid(1), _uid(2)})
        for session in ("first", "second"):
            state = self.assistant.read(session)
            self.assertEqual(state.current_operation_id, reservation.operation_id)
            self.assertIsNone(state.pid)

    def test_worker_never_selects_or_runs_an_assistant_reservation(self) -> None:
        self.submit("assistant-session", 1)
        reservation = self.reserve()
        self.assistant.start_reserved_batch(reservation.operation_id)
        with patch.object(self.worker, "_start_worker_if_needed"):
            self.worker.submit("worker-session", "test evidence", candidate_uid=_uid(2))

        selected, _ = self.ready(self.worker)
        self.assertEqual([item.session_id for item in selected], ["worker-session"])
        callback = Mock(return_value="completed")
        result = self.worker.run_pending_batches(
            callback, trigger_candidates=1, target_batch_candidates=1, max_wait_seconds=0
        )

        self.assertEqual(result.processed, 1)
        self.assertNotEqual(callback.call_args.args[0], reservation.operation_id)
        state = self.assistant.read("assistant-session")
        self.assertEqual(state.current_operation_id, reservation.operation_id)
        self.assertEqual([job.candidate_uid for job in state.current_batch], [_uid(1)])

    def test_assistant_ready_does_not_recover_an_unrelated_dead_worker(self) -> None:
        with patch.object(self.worker, "_start_worker_if_needed"):
            submitted = self.worker.submit("worker-session", "test evidence", candidate_uid=_uid(1))
        self.worker._write(
            "worker-session",
            replace(submitted, phase="running", current_batch=submitted.pending, pending=[], pid=999999),
        )
        before = self.worker._state_path("worker-session").read_bytes()
        self.submit("assistant-session", 2)

        with patch.object(AsyncUpdateStore, "_worker_snapshot", side_effect=AssertionError("foreign worker inspected")):
            selected, _ = self.ready()

        self.assertEqual([item.session_id for item in selected], ["assistant-session"])
        self.assertEqual(self.worker._state_path("worker-session").read_bytes(), before)

    def test_assistant_running_state_does_not_depend_on_the_invoking_process(self) -> None:
        self.submit("assistant-session", 1)
        reservation = self.reserve()
        self.assistant.start_reserved_batch(reservation.operation_id)
        fresh_process_store = AsyncUpdateStore(self.root, "update", executor="assistant")

        with patch.object(AsyncUpdateStore, "_worker_snapshot", side_effect=AssertionError("worker inspected")):
            state = fresh_process_store.read("assistant-session")
            default_reader_state = self.worker.read("assistant-session")

        self.assertEqual(state.status, "running")
        self.assertEqual(state.phase, "running")
        self.assertIsNone(state.error)
        self.assertEqual(default_reader_state.current_operation_id, reservation.operation_id)

    def test_new_candidates_do_not_join_the_reserved_or_running_batch(self) -> None:
        self.submit("first", 1)
        self.submit("second", 2)
        reservation = self.reserve(trigger=2, target=2)
        self.submit("first", 3)
        candidates = self.assistant.start_reserved_batch(reservation.operation_id)
        self.submit("first", 4)

        self.assertEqual({candidate.uid for candidate in candidates}, {_uid(1), _uid(2)})
        state = self.assistant.read("first")
        self.assertEqual([job.candidate_uid for job in state.current_batch], [_uid(1)])
        self.assertEqual([job.candidate_uid for job in state.pending], [_uid(3), _uid(4)])
        self.assistant.finish_reserved_batch(reservation.operation_id, "completed")
        state = self.assistant.read("first")

        self.assertEqual(state.current_batch, [])
        self.assertEqual([job.candidate_uid for job in state.pending], [_uid(3), _uid(4)])
        self.assertEqual(state.phase, "waiting")
        self.assertEqual(state.executor, "assistant")
        self.assertIsNone(state.pid)
        self.assertIsNone(self.assistant._read_reservation(reservation.operation_id))

    def test_active_session_cannot_switch_executor_on_submit(self) -> None:
        self.submit("shared-session", 1)

        with self.assertRaises(ValueError):
            self.worker.submit("shared-session", "test evidence", candidate_uid=_uid(2))

        self.assertEqual([job.candidate_uid for job in self.assistant.read("shared-session").pending], [_uid(1)])
        self.assertIsNone(UpdateQueueStore(self.root).read_outbox(_uid(2)))

    def test_wrong_executor_cannot_reserve_start_or_finish_a_batch(self) -> None:
        self.submit("assistant-session", 1)
        batch, _ = self.ready()
        with self.assertRaises(RuntimeError):
            self.worker.reserve_batch(batch)
        reservation = self.assistant.reserve_batch(batch)
        self.assertIsNotNone(reservation)
        with self.assertRaises(RuntimeError):
            self.worker.start_reserved_batch(reservation.operation_id)
        with self.assertRaises(RuntimeError):
            self.worker.finish_reserved_batch(reservation.operation_id, "completed")

    def test_interrupted_outbox_submit_retains_assistant_ownership(self) -> None:
        original_write = self.assistant._write

        def interrupt_after_outbox(session_id, state):
            if state.pending:
                raise RuntimeError("interrupted after outbox write")
            original_write(session_id, state)

        with patch.object(self.assistant, "_write", side_effect=interrupt_after_outbox):
            with self.assertRaises(RuntimeError):
                self.submit("assistant-session", 1)

        self.assertIsNotNone(UpdateQueueStore(self.root).read_outbox(_uid(1)))
        self.assertEqual(self.assistant._read_raw("assistant-session").executor, "assistant")
        loaded = self.worker.read("assistant-session")
        self.assertEqual(loaded.executor, "assistant")
        self.assertEqual([job.candidate_uid for job in loaded.pending], [_uid(1)])
        self.assertIsNone(self.ready(self.worker)[0])


def _uid(number: int) -> str:
    return f"{number:032x}"


if __name__ == "__main__":
    unittest.main()
