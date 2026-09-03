import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from rightmemory.platform import detached_process_kwargs, process_exists, python_module_child_env
from rightmemory.watch import (
    _read_pid,
    _watch_lock_held,
    install_stamp_path,
    managed_watch_status,
    start_managed_watch,
    stop_managed_watch,
    watch_pid_path,
)
from rightmemory.web.process import (
    _owned_launch_process,
    _wait_for_web_ready,
    _wait_for_web_registration,
    start_web_service,
    stop_web_service,
    web_launch_path,
    web_log_path,
    web_pid_path,
    web_ready_path,
)


@unittest.skipUnless(os.name == "nt", "native Windows process integration")
class WindowsProcessIntegrationTests(unittest.TestCase):
    def test_web_survives_launcher_exit_and_owning_job_close(self):
        # A terminal/task host can close a kill-on-close job after its command
        # returns. Hidden windows and process groups alone do not detach a server.
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        self.assertTrue(job, ctypes.get_last_error())
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000 | 0x0800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            self.assertTrue(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            with tempfile.TemporaryDirectory() as tempdir, socket.socket() as reservation:
                root = Path(tempdir)
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
                reservation.close()
                env = python_module_child_env()
                env["RIGHTMEMORY_ROOT"] = str(root)
                # Use the base interpreter for the gate so the venv redirector
                # cannot create a child before we assign the launcher to our job.
                code = (
                    "import subprocess, sys; sys.stdin.readline(); "
                    "raise SystemExit(subprocess.call(sys.argv[1:]))"
                )
                launcher = subprocess.Popen(
                    [sys._base_executable, "-c", code, sys.executable, "-m", "rightmemory.cli",
                     "web", "start", "--host", "127.0.0.1", "--port", str(port)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                server_pid = None
                try:
                    self.assertTrue(kernel.AssignProcessToJobObject(job, int(launcher._handle)), ctypes.get_last_error())
                    output, _ = launcher.communicate("start\n", timeout=20)
                    self.assertEqual(launcher.returncode, 0, output)
                    server_pid = _read_pid(web_pid_path(root))
                    self.assertIsNotNone(server_pid)
                    self.assertTrue(process_exists(server_pid))
                    self.assertTrue(kernel.CloseHandle(job))
                    job = None
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    self.assertTrue(process_exists(server_pid))
                    stopped = stop_web_service(root, timeout_seconds=10)
                    self.assertEqual(stopped.state, "stopped")
                    self.assertFalse(process_exists(server_pid))
                    server_pid = None
                finally:
                    self._terminate_if_running(server_pid or _read_pid(web_pid_path(root)))
                    if launcher.poll() is None:
                        launcher.kill()
                    launcher.communicate(timeout=5)
                    self._remove_web_log_after_redirector_exit(root)
        finally:
            if job:
                kernel.CloseHandle(job)

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
                self._remove_web_log_after_redirector_exit(root)

    def test_web_startup_timeout_stops_registered_server_and_reaps_redirector(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            launchers = []
            server_pid = None

            def expire_registered_start(memory_root, process, *, launch_id):
                nonlocal server_pid
                launchers.append(process)
                self.assertTrue(self._wait_until(lambda: _owned_launch_process(memory_root, launch_id), timeout=20))
                server_pid, _identity = _owned_launch_process(memory_root, launch_id)
                return _wait_for_web_ready(
                    memory_root, process.pid, process=process, launch_id=launch_id, timeout_seconds=0,
                )

            try:
                with patch("rightmemory.web.process._wait_for_web_registration", side_effect=expire_registered_start):
                    with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                        start_web_service(root, host="127.0.0.1", port=0, python_executable=sys.executable)
                self.assertIsNotNone(server_pid)
                self.assertFalse(process_exists(server_pid))
                self.assertIsNotNone(launchers[0].poll())
                self.assertFalse(web_pid_path(root).exists())
                self.assertFalse(web_ready_path(root).exists())
                self.assertFalse(web_launch_path(root).exists())
                self._remove_web_log_after_redirector_exit(root)
            finally:
                self._terminate_if_running(server_pid)
                for process in launchers:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)

    def test_web_startup_timeout_reaps_unregistered_redirector_tree(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            log_path = web_log_path(root)
            log_path.parent.mkdir(parents=True)
            child_marker = root / "started-child.pid"
            code = (
                "import os, sys, time; from pathlib import Path; "
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); time.sleep(60)"
            )
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    [sys.executable, "-c", code, str(child_marker)],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    **detached_process_kwargs(),
                )
            child_pid = None
            try:
                self.assertTrue(self._wait_until(child_marker.exists, timeout=20))
                child_pid = int(child_marker.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    _wait_for_web_registration(root, process, timeout_seconds=0, launch_id="unregistered-test-launch")
                self.assertFalse(process_exists(child_pid))
                self.assertIsNotNone(process.poll())
                self._remove_web_log_after_redirector_exit(root)
            finally:
                self._terminate_if_running(child_pid)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def _remove_web_log_after_redirector_exit(self, root: Path) -> None:
        # The registered server can exit just before its venv redirector releases
        # inherited log handles. Wait for that release before deleting the fixture.
        def remove() -> bool:
            try:
                web_log_path(root).unlink(missing_ok=True)
                return True
            except PermissionError:
                return False

        self.assertTrue(self._wait_until(remove, timeout=5), "Web log handle was not released")

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
