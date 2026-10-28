from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.curation_worker import CurationWorker, default_executor, wake_configured_curation


class FakeService:
    def __init__(self):
        self.pending = {domain: 2 for domain in ("memory", "guidance", "skills")}
        self.calls = []
        self.error = None
        self.task_id = "memory-first"
        self.native = False
        self.batch_id = "batch-first"

    def notices(self):
        return [
            {"domain": domain, "ready": count > 0, "mode": "update", "scope": "",
             "task_id": self.task_id if domain == "memory" else domain,
             "batch_id": self.batch_id,
             "executor": "native" if self.native else "configured"}
            for domain, count in self.pending.items()
        ]

    def run(self, domain, **kwargs):
        self.calls.append(domain)
        if self.error and domain == "memory":
            raise self.error
        self.pending[domain] -= 1
        return {"status": "completed"}


class CurationWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worker = CurationWorker(self.root)

    @contextmanager
    def retry_clock(self, worker=None):
        worker = worker or self.worker
        clock = [datetime(2026, 10, 28, tzinfo=UTC)]

        def advance(deadline, observed_wake):
            self.assertGreater(deadline, clock[0])
            self.assertEqual(worker.status()["next_ready_at"], deadline.isoformat())
            clock[0] = deadline

        with patch("rightmemory.curation_worker.datetime", wraps=datetime) as dates, \
             patch.object(worker, "_wait_for_work", side_effect=advance):
            dates.now.side_effect = lambda zone: clock[0]
            yield clock

    def test_drains_all_domains_without_a_second_candidate_store(self):
        service = FakeService()
        self.assertEqual(self.worker.run(service), 0)
        self.assertEqual(service.calls, ["memory", "guidance", "skills"] * 2)
        self.assertFalse(self.worker.status()["live"])
        state = json.loads((self.worker.root / "state.json").read_text())
        self.assertEqual(state["failures"], {})
        self.assertNotIn("pid", state)

    def test_native_tasks_are_never_run_by_configured_worker(self):
        service = FakeService()
        service.native = True
        self.assertEqual(self.worker.run(service), 0)
        self.assertEqual(service.calls, [])

    def test_waiting_batch_runs_at_deadline_without_another_submission(self):
        start = datetime(2026, 10, 28, tzinfo=UTC)
        clock = [start]
        deadline = start + timedelta(seconds=3)
        service = FakeService()
        service.pending = {"memory": 1}

        def notices():
            pending = bool(service.pending["memory"])
            return [{"domain": "memory", "ready": pending and clock[0] >= deadline,
                     "next_ready_at": deadline.isoformat() if pending and clock[0] < deadline else None}]

        def sleep(seconds):
            self.assertFalse(service.calls)
            self.assertEqual(self.worker.status()["next_ready_at"], deadline.isoformat())
            clock[0] += timedelta(seconds=seconds)

        service.notices = notices
        with patch("rightmemory.curation_worker.datetime", wraps=datetime) as dates, \
             patch("rightmemory.curation_worker.time.sleep", side_effect=sleep):
            dates.now.side_effect = lambda zone: clock[0]
            self.assertEqual(self.worker.run(service), 0)
        self.assertEqual(service.calls, ["memory"])
        self.assertEqual(clock[0], deadline)
        self.assertIsNone(self.worker.status()["next_ready_at"])

    def test_new_submission_interrupts_the_wait_and_rechecks_readiness(self):
        deadline = datetime.now(UTC) + timedelta(hours=25)
        service = FakeService()
        service.pending = {"memory": 1}
        ready = [False]

        def notices():
            pending = bool(service.pending["memory"])
            return [{"domain": "memory", "ready": pending and ready[0],
                     "next_ready_at": deadline.isoformat() if pending and not ready[0] else None}]

        def submit(seconds):
            self.assertFalse(service.calls)
            ready[0] = True
            self.assertEqual(self.worker.wake()["status"], "running")

        service.notices = notices
        with patch("rightmemory.curation_worker.time.sleep", side_effect=submit) as sleep:
            self.assertEqual(self.worker.run(service), 0)
        sleep.assert_called_once()
        self.assertEqual(service.calls, ["memory"])

    def test_stop_interrupts_a_waiting_batch_without_running_it(self):
        deadline = datetime.now(UTC) + timedelta(hours=25)
        service = FakeService()
        service.notices = lambda: [{"domain": "memory", "ready": False,
                                   "next_ready_at": deadline.isoformat()}]

        def request_stop(seconds):
            with self.worker._locked("control.lock"):
                state = self.worker._read()
                state["stop"] = {"pid": state["pid"], "identity": state["identity"]}
                self.worker._write(state)

        with patch("rightmemory.curation_worker.time.sleep", side_effect=request_stop) as sleep:
            self.assertEqual(self.worker.run(service), 0)
        sleep.assert_called_once()
        self.assertFalse(service.calls)
        self.assertFalse(self.worker.status()["live"])

    def test_sync_wakes_a_future_batch_but_not_native_or_failed_work(self):
        waiting = {"domain": "memory", "ready": False,
                   "batch_id": "waiting-batch",
                   "next_ready_at": (datetime.now(UTC) + timedelta(hours=25)).isoformat()}
        self.assertTrue(self.worker.has_work([waiting]))
        self.assertFalse(self.worker.has_work([{**waiting, "executor": "native"}]))
        with patch("rightmemory.learning_service.LearningService") as service, \
             patch.object(CurationWorker, "wake", return_value={"status": "started"}) as wake:
            service.return_value.notices.return_value = [waiting]
            self.assertEqual(wake_configured_curation(self.root), {"status": "started"})
            wake.assert_called_once()
        self.worker._write({"failures": {"memory": {
            "task_id": None, "batch_id": "waiting-batch", "attempts": 2,
            "error": "failed", "next_retry_at": None,
        }}})
        self.assertFalse(self.worker.has_work([waiting]))

    def test_failure_budget_survives_restarts_and_new_task_gets_its_own_budget(self):
        service = FakeService()
        service.error = RuntimeError("provider unavailable")
        with self.retry_clock():
            self.assertEqual(self.worker.run(service), 1)
        self.assertEqual(service.calls.count("memory"), 2)
        self.assertFalse(self.worker.has_work(service.notices()))
        restarted = CurationWorker(self.root)
        self.assertEqual(restarted.run(service), 1)
        self.assertEqual(service.calls.count("memory"), 2)
        service.batch_id = "pending-evidence-added-behind-failed-task"
        self.assertFalse(restarted.has_work(service.notices()))
        service.task_id = "memory-explicit-retry"
        service.error = None
        self.assertTrue(restarted.has_work(service.notices()))
        self.assertEqual(restarted.run(service), 0)
        self.assertEqual(service.pending["memory"], 0)
        self.assertIsNone(restarted.status()["error"])

    def test_explicit_wake_resets_failures_without_replacing_live_worker(self):
        self.worker._write({"pid": 123, "wake": 1, "failures": {"memory": {
            "task_id": "failed", "batch_id": None, "attempts": 2,
            "error": "provider failed", "next_retry_at": None,
        }}, "error": "provider failed"})
        with patch.object(self.worker, "_live", return_value=True), patch.object(self.worker, "_start") as start:
            result = self.worker.wake(retry_failed=True)
        self.assertEqual(result, {"status": "running", "pid": 123})
        start.assert_not_called()
        self.assertEqual(self.worker.status()["failures"], {})

    def test_automatic_task_replacement_carries_the_retry_budget(self):
        service = FakeService()
        service.pending = {"memory": 1, "guidance": 0, "skills": 0}

        def stale(domain, **kwargs):
            service.calls.append(domain)
            service.task_id = f"memory-replacement-{len(service.calls)}"
            return {"status": "retry_ready", "task_id": service.task_id, "reason": "domain context changed"}

        service.run = stale
        with self.retry_clock():
            self.assertEqual(self.worker.run(service), 1)
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(self.worker.status()["failures"]["memory"]["attempts"], 2)
        self.assertFalse(self.worker.has_work(service.notices()))
        self.assertEqual(CurationWorker(self.root).run(service), 1)
        self.assertEqual(len(service.calls), 2)

    def test_normal_wake_preserves_the_failed_task_budget(self):
        failures = {"memory": {"task_id": "failed", "batch_id": None, "attempts": 2,
                               "error": "provider failed", "next_retry_at": None}}
        self.worker._write({"wake": 1, "failures": failures})
        with patch.object(self.worker, "_start", return_value={"status": "started"}):
            self.worker.wake()
        self.assertEqual(self.worker.status()["failures"], failures)

    def test_retry_cooldown_survives_restart_and_runs_at_the_boundary(self):
        service = FakeService()
        service.pending = {"memory": 1}
        service.error = RuntimeError("temporary outage")
        start = datetime(2026, 10, 28, tzinfo=UTC)
        deadline = start + timedelta(seconds=3600)

        def stop_at_first_wait(until, observed_wake):
            self.assertEqual(until, deadline)
            self.assertEqual(service.calls, ["memory"])
            failure = self.worker.status()["failures"]["memory"]
            self.assertEqual(failure["attempts"], 1)
            self.assertEqual(failure["next_retry_at"], deadline.isoformat())
            self.assertEqual(self.worker.status()["next_ready_at"], deadline.isoformat())
            state = self.worker._read()
            state["stop"] = {"pid": state["pid"], "identity": state["identity"]}
            self.worker._write(state)

        with patch("rightmemory.curation_worker.datetime", wraps=datetime) as dates, \
             patch.object(self.worker, "_wait_for_work", side_effect=stop_at_first_wait):
            dates.now.return_value = start
            self.worker.run(service)
        restarted = CurationWorker(self.root)
        self.assertTrue(restarted.has_work(service.notices()))
        clock = [deadline - timedelta(seconds=1)]

        def reach_boundary(seconds):
            self.assertEqual(service.calls, ["memory"])
            self.assertEqual(restarted.status()["next_ready_at"], deadline.isoformat())
            self.assertEqual(seconds, 1)
            clock[0] += timedelta(seconds=seconds)

        with patch("rightmemory.curation_worker.datetime", wraps=datetime) as dates, \
             patch("rightmemory.curation_worker.time.sleep", side_effect=reach_boundary) as sleep:
            dates.now.side_effect = lambda zone: clock[0]
            self.assertEqual(restarted.run(service), 1)
        sleep.assert_called_once()
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(clock[0], deadline)
        self.assertIsNone(restarted.status()["failures"]["memory"]["next_retry_at"])

    def test_new_pending_batch_does_not_inherit_exhausted_unclaimed_failure(self):
        service = FakeService()
        service.pending = {"memory": 1}
        service.task_id = None
        service.error = RuntimeError("claim unavailable")
        with self.retry_clock():
            self.assertEqual(self.worker.run(service), 1)
        self.assertFalse(self.worker.has_work(service.notices()))
        self.assertEqual(service.calls, ["memory", "memory"])
        service.batch_id = "new-session-new-evidence"
        service.error = None
        self.assertTrue(self.worker.has_work(service.notices()))
        self.assertEqual(CurationWorker(self.root).run(service), 0)
        self.assertEqual(service.calls, ["memory"] * 3)
        self.assertEqual(self.worker.status()["failures"], {})

    def test_successful_retry_clears_failure(self):
        service = FakeService()
        service.pending = {"memory": 1}
        run = service.run

        def transient(domain, **kwargs):
            service.error = RuntimeError("temporary outage") if not service.calls else None
            return run(domain, **kwargs)

        service.run = transient
        with self.retry_clock():
            self.assertEqual(self.worker.run(service), 0)
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(self.worker.status()["failures"], {})
        self.assertIsNone(self.worker.status()["error"])

    def test_error_after_finished_task_does_not_charge_the_next_batch(self):
        service = FakeService()
        service.pending = {"memory": 1}
        run = service.run

        def finish_then_fail(domain, **kwargs):
            if not service.calls:
                service.calls.append(domain)
                service.task_id = None
                service.batch_id = "next-unrelated-batch"
                raise OSError("post-completion state write failed")
            return run(domain, **kwargs)

        service.run = finish_then_fail
        with patch.object(self.worker, "_wait_for_work", side_effect=AssertionError("new batch delayed")):
            self.assertEqual(self.worker.run(service), 0)
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(self.worker.status()["failures"], {})

    def test_retry_cooldown_does_not_delay_other_domains(self):
        service = FakeService()
        service.pending = {"memory": 1, "guidance": 2}
        service.error = RuntimeError("memory unavailable")
        call_times = []
        run = service.run
        with self.retry_clock() as clock:
            start = clock[0]

            def record_time(domain, **kwargs):
                call_times.append((domain, clock[0]))
                return run(domain, **kwargs)

            service.run = record_time
            self.assertEqual(self.worker.run(service), 1)
        self.assertEqual([when for domain, when in call_times if domain == "guidance"], [start, start])
        self.assertEqual([when for domain, when in call_times if domain == "memory"],
                         [start, start + timedelta(seconds=3600)])

    def test_new_submission_during_retry_wait_runs_a_new_unclaimed_batch(self):
        service = FakeService()
        service.pending = {"memory": 1}
        service.task_id = None
        service.error = RuntimeError("claim unavailable")

        def submit_during_cooldown(seconds):
            self.assertEqual(service.calls, ["memory"])
            service.batch_id = "new-batch"
            service.error = None
            self.assertEqual(self.worker.wake()["status"], "running")

        with patch("rightmemory.curation_worker.time.sleep", side_effect=submit_during_cooldown) as sleep:
            self.assertEqual(self.worker.run(service), 0)
        sleep.assert_called_once()
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(self.worker.status()["failures"], {})

    def test_claim_creation_does_not_reset_the_batch_retry_budget(self):
        service = FakeService()
        service.pending = {"memory": 1}
        service.task_id = None

        def claim_then_fail(domain, **kwargs):
            service.calls.append(domain)
            if len(service.calls) == 2:
                service.task_id = "memory-created-on-retry"
            raise RuntimeError("provider unavailable")

        service.run = claim_then_fail
        with self.retry_clock():
            self.assertEqual(self.worker.run(service), 1)
        self.assertEqual(service.calls, ["memory", "memory"])
        failure = self.worker.status()["failures"]["memory"]
        self.assertEqual(failure["task_id"], service.task_id)
        self.assertEqual(failure["attempts"], 2)
        self.assertFalse(self.worker.has_work(service.notices()))

    def test_worker_launch_uses_memory_root_to_avoid_shadow_checkout_import(self):
        shadow = self.root / "shadow-checkout"
        package = shadow / "rightmemory"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("raise RuntimeError('shadow checkout imported')\n")
        actual_popen = subprocess.Popen
        children = []

        def start_probe(command, **kwargs):
            self.assertEqual(Path(kwargs["cwd"]), self.root)
            probe = [sys.executable, "-c",
                     "import pathlib, rightmemory; print(pathlib.Path(rightmemory.__file__).resolve())"]
            child = actual_popen(probe, cwd=kwargs["cwd"], env=kwargs["env"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, error.decode())
            self.assertEqual(Path(output.decode().strip()),
                             Path(__file__).resolve().parents[1] / "rightmemory" / "__init__.py")
            children.append(child)
            return child

        before = Path.cwd()
        try:
            os.chdir(shadow)
            with patch("rightmemory.curation_worker.subprocess.Popen", side_effect=start_probe):
                self.worker.wake()
        finally:
            os.chdir(before)
        self.assertEqual(len(children), 1)

    def test_process_identity_is_authoritative_without_a_command_line(self):
        with patch("rightmemory.curation_worker.process_exists", return_value=True), \
             patch("rightmemory.curation_worker.process_identity", return_value="current"), \
             patch("rightmemory.curation_worker.process_command") as command:
            self.assertTrue(self.worker._live({"pid": 123, "identity": "current"}))
            self.assertFalse(self.worker._live({"pid": 123, "identity": "reused"}))
            command.assert_not_called()
            command.return_value = "python -m rightmemory.curation_worker --root somewhere"
            self.assertTrue(self.worker._live({"pid": 123, "identity": None}))
            command.return_value = None
            self.assertFalse(self.worker._live({"pid": 123, "identity": None}))

    def test_install_boundary_defers_new_worker_and_does_not_restart_implicitly(self):
        with patch.object(self.worker, "_start") as start:
            with self.worker.install_boundary():
                self.assertEqual(self.worker.wake()["status"], "deferred")
            start.assert_not_called()
            self.worker.wake()
            start.assert_called_once()

    def test_invalid_retry_state_does_not_reset_automatic_budget(self):
        self.worker.root.mkdir(parents=True)
        (self.worker.root / "state.json").write_text('{"failures":{"memory":{"attempts":-1}}}')
        with self.assertRaises(ValueError):
            self.worker.status()

    def test_sync_wake_respects_native_mode_and_failed_task_budget(self):
        stamp = self.root / ".runtime/install.stamp"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("mode=assistant\n")
        self.assertEqual(default_executor(self.root), "native")
        with patch("rightmemory.learning_service.LearningService", side_effect=AssertionError("native worker")):
            self.assertIsNone(wake_configured_curation(self.root))
        stamp.write_text("mode=standalone\n")
        self.worker._write({"failures": {"memory": {"task_id": "t", "batch_id": None,
                                                   "attempts": 2, "error": "failed", "next_retry_at": None}}})
        service = Mock()
        service.notices.return_value = [{"domain": "memory", "task_id": "t", "ready": True}]
        with patch("rightmemory.learning_service.LearningService", return_value=service), patch.object(CurationWorker, "wake") as wake:
            self.assertIsNone(wake_configured_curation(self.root))
            wake.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX subprocess lock boundary")
    def test_stop_waits_for_the_inflight_batch_and_prevents_the_next(self):
        script = '''
import sys, time
from pathlib import Path
from rightmemory.curation_worker import CurationWorker
root=Path(sys.argv[1])
class Service:
    def notices(self):
        return [{"domain":"memory","ready":True,"mode":"update","scope":""}]
    def run(self, domain, **kwargs):
        (root/"entered").touch()
        while not (root/"release").exists(): time.sleep(.01)
        (root/"finished").touch()
        return {"status":"completed"}
CurationWorker(root).run(Service())
'''
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not (self.root / "entered").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / "entered").exists())
        self.assertTrue(self.worker.status()["live"])
        with self.assertRaises(TimeoutError):
            self.worker.stop(timeout_seconds=.05)
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "finished").exists())
        (self.root / "release").touch()
        process.wait(timeout=10)
        self.assertTrue((self.root / "finished").exists())
        self.assertFalse(self.worker.status()["live"])
        self.assertEqual(process.returncode, 0, process.stderr.read().decode())
        process.stderr.close()


if __name__ == "__main__":
    unittest.main()
