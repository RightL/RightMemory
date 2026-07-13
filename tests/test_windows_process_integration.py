import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

from rightmemory.platform import process_exists
from rightmemory.watch import (
    _read_pid,
    _watch_lock_held,
    install_stamp_path,
    managed_watch_status,
    start_managed_watch,
    stop_managed_watch,
    watch_pid_path,
)
from rightmemory.web.process import start_web_service, stop_web_service, web_pid_path


@unittest.skipUnless(os.name == "nt", "native Windows process integration")
class WindowsProcessIntegrationTests(unittest.TestCase):
    def test_managed_watch_restarts_with_new_registered_pid_and_stops(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._seed_sync_memory(root)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                started = start_managed_watch(root, "sync", python_executable=sys.executable)
            active_pid = started.pid
            try:
                self.assertTrue(self._wait_until(lambda: _watch_lock_held(root, "sync")))
                stamp = install_stamp_path(root)
                stamp.write_text("changed\n", encoding="utf-8")
                pid_path = watch_pid_path(root, "sync")
                self.assertTrue(
                    self._wait_until(
                        lambda: (replacement := _read_pid(pid_path)) is not None and replacement != active_pid,
                        timeout=20,
                    )
                )
                replacement_pid = _read_pid(pid_path)
                self.assertIsNotNone(replacement_pid)
                active_pid = replacement_pid
                status = managed_watch_status(root, "sync")
                self.assertEqual(status.state, "running")
                self.assertEqual(status.pid, replacement_pid)

                stopped = stop_managed_watch(root, "sync", timeout_seconds=20)

                self.assertEqual(stopped.state, "stopped")
                self.assertFalse(pid_path.exists())
                active_pid = None
            finally:
                self._terminate_if_running(active_pid)

    def test_web_service_honors_cooperative_stop_request(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                started = start_web_service(root, host="127.0.0.1", port=0, python_executable=sys.executable)
            active_pid = started.pid
            try:
                self.assertIsNotNone(active_pid)
                self.assertTrue(self._wait_until(lambda: process_exists(active_pid)))

                stopped = stop_web_service(root, timeout_seconds=20)

                self.assertEqual(stopped.state, "stopped")
                self.assertFalse(web_pid_path(root).exists())
                active_pid = None
            finally:
                self._terminate_if_running(active_pid)

    def _seed_sync_memory(self, root: Path) -> None:
        (root / "MEMORY.md").write_text("# Test Memory\n", encoding="utf-8")
        (root / "rightmemory.toml").write_text(
            "[sync]\nenabled = true\nstale_pull_after_hours = 24\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "RightMemory Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@rightmemory.local"], cwd=root, check=True)
        subprocess.run(["git", "add", "MEMORY.md", "rightmemory.toml"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "memory: seed"], cwd=root, check=True)

    def _wait_until(self, predicate, *, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return bool(predicate())

    def _terminate_if_running(self, pid: int | None) -> None:
        if pid is None or not process_exists(pid):
            return
        os.kill(pid, signal.SIGTERM)
        self._wait_until(lambda: not process_exists(pid), timeout=5)


if __name__ == "__main__":
    unittest.main()
