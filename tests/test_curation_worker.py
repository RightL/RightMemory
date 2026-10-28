from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
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

    def notices(self):
        return [
            {"domain": domain, "ready": count > 0, "mode": "update", "scope": "",
             "task_id": self.task_id if domain == "memory" else domain,
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

    def test_failure_budget_survives_restarts_and_new_task_gets_its_own_budget(self):
        service = FakeService()
        service.error = RuntimeError("provider unavailable")
        self.assertEqual(self.worker.run(service), 1)
        self.assertEqual(service.calls.count("memory"), 2)
        self.assertFalse(self.worker.has_work(service.notices()))
        restarted = CurationWorker(self.root)
        self.assertEqual(restarted.run(service), 1)
        self.assertEqual(service.calls.count("memory"), 2)
        service.task_id = "memory-explicit-retry"
        service.error = None
        self.assertTrue(restarted.has_work(service.notices()))
        self.assertEqual(restarted.run(service), 0)
        self.assertEqual(service.pending["memory"], 0)
        self.assertIsNone(restarted.status()["error"])

    def test_explicit_wake_resets_failures_without_replacing_live_worker(self):
        self.worker._write({"pid": 123, "wake": 1, "failures": {"memory": {
            "task_id": "failed", "attempts": 2, "error": "provider failed",
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
        self.assertEqual(self.worker.run(service), 1)
        self.assertEqual(service.calls, ["memory", "memory"])
        self.assertEqual(self.worker.status()["failures"]["memory"]["attempts"], 2)
        self.assertFalse(self.worker.has_work(service.notices()))
        self.assertEqual(CurationWorker(self.root).run(service), 1)
        self.assertEqual(len(service.calls), 2)

    def test_normal_wake_preserves_the_failed_task_budget(self):
        failures = {"memory": {"task_id": "failed", "attempts": 2, "error": "provider failed"}}
        self.worker._write({"wake": 1, "failures": failures})
        with patch.object(self.worker, "_start", return_value={"status": "started"}):
            self.worker.wake()
        self.assertEqual(self.worker.status()["failures"], failures)

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
        self.worker._write({"failures": {"memory": {"task_id": "t", "attempts": 2, "error": "failed"}}})
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
