import io
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.cli import main
from rightmemory.config import MEMORY_ROOT_ENV
from rightmemory.web.process import (
    MANAGED_WEB_ENV,
    WebServiceStatus,
    _terminate_owned_web_process,
    _wait_for_exit,
    _wait_for_web_registration,
    _wait_for_web_ready,
    register_web_process,
    web_log_path,
    web_pid_path,
    web_ready_path,
    web_launch_path,
    web_settings_path,
    web_stop_path,
)


class WebCliTests(unittest.TestCase):
    def test_web_status_reports_stopped(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.cli.default_memory_root", return_value=root), patch("sys.stdout", stdout):
                result = main(["web", "status"])

        self.assertEqual(result, 0)
        self.assertIn("web: stopped", stdout.getvalue())

    def test_web_start_records_pid_and_settings(self):
        stdout = io.StringIO()

        class FakeProcess:
            pid = 12345

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.web.process.subprocess.Popen", return_value=FakeProcess()) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["web", "start", "--host", "0.0.0.0", "--port", "8766"])

            command = popen.call_args.args[0]
            settings = web_settings_path(root).read_text(encoding="utf-8")
            pid_text = web_pid_path(root).read_text(encoding="utf-8").strip()

        self.assertEqual(result, 0)
        self.assertEqual(pid_text, "12345")
        self.assertIn("0.0.0.0", settings)
        self.assertIn("8766", settings)
        self.assertIn("rightmemory.web.app", command)
        self.assertIn("web: running pid 12345", stdout.getvalue())
        self.assertIn("operator token", stdout.getvalue())

    def test_web_start_prepends_source_checkout_to_pythonpath(self):
        stdout = io.StringIO()

        class FakeProcess:
            pid = 12345

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source_parent = Path(__file__).resolve().parents[1]
            with (
                patch.dict(os.environ, {"PYTHONPATH": "/existing/path"}, clear=False),
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.web.process.subprocess.Popen", return_value=FakeProcess()) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["web", "start"])

            env = popen.call_args.kwargs["env"]

        self.assertEqual(result, 0)
        self.assertEqual(env[MEMORY_ROOT_ENV], str(root))
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(source_parent))
        self.assertIn("/existing/path", env["PYTHONPATH"].split(os.pathsep))

    def test_web_stop_removes_stale_pid(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            web_pid_path(root).parent.mkdir(parents=True)
            web_pid_path(root).write_text("12345\n", encoding="utf-8")
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.web.process._is_web_process", return_value=False),
                patch("sys.stdout", stdout),
            ):
                result = main(["web", "stop"])

        self.assertEqual(result, 0)
        self.assertIn("web: removed stale pid 12345", stdout.getvalue())
        self.assertFalse(web_pid_path(root).exists())

    def test_web_stop_uses_cooperative_request(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 12345, "127.0.0.1", 8766, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.web.process.web_service_status", return_value=running),
                patch("rightmemory.web.process._wait_for_exit", return_value=False),
                patch("sys.stdout", stdout),
            ):
                result = main(["web", "stop", "--timeout", "0"])

            request = web_stop_path(root).read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(request, "12345\n")
        self.assertIn("web: stopping pid 12345", stdout.getvalue())

    def test_web_restart_stops_then_starts(self):
        stdout = io.StringIO()
        events = []

        def fake_stop(memory_root, timeout_seconds=30):
            events.append(("stop", memory_root, timeout_seconds))
            return type("StopResult", (), {"state": "stopped", "pid": None, "log_path": web_log_path(memory_root)})()

        def fake_start(memory_root, host, port, python_executable=None):
            events.append(("start", memory_root, host, port))
            return WebServiceStatus("running", 999, host, port, web_log_path(memory_root))

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.stop_web_service", side_effect=fake_stop),
                patch("rightmemory.cli.start_web_service", side_effect=fake_start),
                patch("sys.stdout", stdout),
            ):
                result = main(["web", "restart", "--host", "127.0.0.1", "--port", "9000"])

        self.assertEqual(result, 0)
        self.assertEqual(events, [("stop", root, 30), ("start", root, "127.0.0.1", 9000)])
        self.assertIn("web: running pid 999", stdout.getvalue())


class PursuitCliTests(unittest.TestCase):
    def test_pursuit_starts_web_service_and_opens_the_map(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            stopped = WebServiceStatus("stopped", None, "127.0.0.1", 9100, web_log_path(root))
            started = WebServiceStatus("running", 1234, "127.0.0.1", 9100, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.web_service_status", return_value=stopped),
                patch("rightmemory.cli.start_web_service", return_value=started) as start,
                patch("rightmemory.cli.webbrowser.open", return_value=True) as browser,
                patch("sys.stdout", stdout),
            ):
                result = main(["pursuit"])
        self.assertEqual(result, 0)
        start.assert_called_once_with(root, host="127.0.0.1", port=9100)
        browser.assert_called_once_with("http://127.0.0.1:9100/#pursuit-map")
        self.assertIn("http://127.0.0.1:9100/#pursuit-map", stdout.getvalue())

    def test_pursuit_reuses_a_running_web_service(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 1234, "0.0.0.0", 9101, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.web_service_status", return_value=running),
                patch("rightmemory.web.process.web_service_status", return_value=running),
                patch("rightmemory.web.process._is_web_process", return_value=True),
                patch("rightmemory.web.process.process_identity", return_value="test-process"),
                patch("rightmemory.web.process.subprocess.Popen") as popen,
                patch("rightmemory.cli.webbrowser.open", return_value=True) as browser,
                patch("sys.stdout", io.StringIO()),
            ):
                register_web_process(root, running.pid, ready=True)
                result = main(["pursuit"])
        self.assertEqual(result, 0)
        popen.assert_not_called()
        browser.assert_called_once_with("http://127.0.0.1:9101/#pursuit-map")

    def test_pursuit_no_open_uses_the_console_entrypoint_and_ipv6_url(self):
        from rightmemory.entrypoint import main as entrypoint_main

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 1234, "::", 9102, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.web_service_status", return_value=running),
                patch("rightmemory.cli.start_web_service", return_value=running),
                patch("rightmemory.cli.webbrowser.open") as browser,
                patch("sys.stdout", stdout),
            ):
                result = entrypoint_main(["pursuit", "--no-open"])
        self.assertEqual(result, 0)
        browser.assert_not_called()
        self.assertIn("http://[::1]:9102/#pursuit-map", stdout.getvalue())

    def test_pursuit_reports_url_when_browser_is_unavailable(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 1234, "127.0.0.1", 9100, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.web_service_status", return_value=running),
                patch("rightmemory.cli.start_web_service", return_value=running),
                patch("rightmemory.cli.webbrowser.open", return_value=False),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", stderr),
            ):
                result = main(["pursuit"])
        self.assertEqual(result, 0)
        self.assertIn("open the Pursuit Map URL", stderr.getvalue())

    def test_pursuit_does_not_open_a_failed_service(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.start_web_service", side_effect=RuntimeError("web service exited with code 1")),
                patch("rightmemory.cli.webbrowser.open") as browser,
                patch("sys.stderr", stderr),
            ):
                result = main(["pursuit"])
        self.assertEqual(result, 1)
        browser.assert_not_called()
        self.assertIn("web service exited with code 1", stderr.getvalue())

    def test_pursuit_does_not_open_an_unknown_ephemeral_port(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 1234, "127.0.0.1", 0, web_log_path(root))
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.start_web_service", return_value=running),
                patch("rightmemory.cli.webbrowser.open") as browser,
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                result = main(["pursuit"])
        self.assertEqual(result, 1)
        browser.assert_not_called()

    def test_pursuit_does_not_accept_crud_subcommands(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch("rightmemory.cli.default_memory_root", return_value=Path(tempdir)),
                patch("rightmemory.cli.start_web_service") as start,
                patch("sys.stderr", io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["pursuit", "create", "direction"])
        self.assertEqual(raised.exception.code, 2)
        start.assert_not_called()


class WebStartupTests(unittest.TestCase):
    def test_exited_process_identity_does_not_keep_shutdown_waiting(self):
        with (
            patch("rightmemory.web.process.process_exists", return_value=False),
            patch("rightmemory.web.process.process_identity", return_value="retained-identity"),
            patch("rightmemory.web.process.os.kill") as kill,
        ):
            self.assertTrue(_wait_for_exit(12345, 0, identity="retained-identity"))
            _terminate_owned_web_process(12345, "retained-identity")
        kill.assert_not_called()

    def test_owned_process_exit_during_signal_is_not_a_shutdown_failure(self):
        with (
            patch("rightmemory.web.process.process_exists", side_effect=[True, False]),
            patch("rightmemory.web.process.process_identity", return_value="owned-identity"),
            patch("rightmemory.web.process.os.kill", side_effect=PermissionError("process exited")),
        ):
            _terminate_owned_web_process(12345, "owned-identity")

    def test_owned_live_process_permission_error_is_not_hidden(self):
        with (
            patch("rightmemory.web.process.process_exists", return_value=True),
            patch("rightmemory.web.process.process_identity", return_value="owned-identity"),
            patch("rightmemory.web.process.os.kill", side_effect=PermissionError("still running")),
            self.assertRaises(PermissionError),
        ):
            _terminate_owned_web_process(12345, "owned-identity")

    def test_registration_accepts_a_launch_bound_redirector_child(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            with patch("rightmemory.web.process.process_identity", return_value="child-identity"):
                register_web_process(root, 54321, ready=True, launch_id="this-launch")
                result = _wait_for_web_registration(root, process, launch_id="this-launch")
        self.assertEqual(result, 54321)
        process.terminate.assert_not_called()

    def test_registration_rejects_a_different_launch_even_with_matching_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            with patch("rightmemory.web.process.process_identity", return_value="child-identity"):
                register_web_process(root, 54321, ready=True, launch_id="other-launch")
                with patch(
                    "rightmemory.web.process.time.sleep",
                    side_effect=lambda _seconds: register_web_process(root, 54321, ready=True, launch_id="this-launch"),
                ) as wait:
                    result = _wait_for_web_registration(root, process, launch_id="this-launch")
        self.assertEqual(result, 54321)
        wait.assert_called_once()
        process.terminate.assert_not_called()

    def test_startup_timeout_waits_for_its_owned_child_before_clearing_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            process.wait.return_value = 0
            with (
                patch("rightmemory.web.process.process_identity", return_value="child-identity"),
                patch("rightmemory.web.process._wait_for_exit", return_value=True) as stopped,
            ):
                register_web_process(root, 54321, launch_id="this-launch")
                with patch("rightmemory.web.process.time.monotonic", side_effect=[0, 1]):
                    with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                        _wait_for_web_registration(root, process, timeout_seconds=0, launch_id="this-launch")
                self.assertFalse(web_pid_path(root).exists())
                self.assertFalse(web_launch_path(root).exists())
        stopped.assert_called_once_with(54321, 5, identity="child-identity")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_startup_timeout_kills_and_reaps_a_child_that_ignores_termination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("test child", 5), 0]
            with (
                patch("rightmemory.web.process.time.monotonic", side_effect=[0, 1]),
                patch("rightmemory.web.process.subprocess.run") as taskkill,
                self.assertRaisesRegex(RuntimeError, "did not become ready"),
            ):
                _wait_for_web_registration(root, process, timeout_seconds=0, launch_id="this-launch")
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        if os.name == "nt":
            self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])

    def test_timeout_preserves_an_unrelated_existing_registration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            with patch("rightmemory.web.process.process_identity", return_value="other-identity"):
                register_web_process(root, 54321, launch_id="other-launch")
                with (
                    patch("rightmemory.web.process.time.monotonic", side_effect=[0, 1]),
                    patch("rightmemory.web.process.subprocess.run"),
                    patch("rightmemory.web.process._wait_for_exit") as stopped,
                    patch("rightmemory.web.process.os.kill") as killed,
                    self.assertRaisesRegex(RuntimeError, "did not become ready"),
                ):
                    _wait_for_web_registration(root, process, timeout_seconds=0, launch_id="this-launch")
                self.assertEqual(web_pid_path(root).read_text(encoding="utf-8").strip(), "54321")
                self.assertFalse(web_stop_path(root).exists())
        stopped.assert_not_called()
        killed.assert_not_called()
        process.wait.assert_called_once_with(timeout=5)

    def test_wait_timeout_does_not_terminate_a_reused_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.web.process.process_identity", return_value="existing-identity"):
                register_web_process(root, 12345)
                with (
                    patch("rightmemory.web.process.time.monotonic", side_effect=[0, 1]),
                    patch("rightmemory.web.process._terminate_web_launcher") as terminate,
                    self.assertRaisesRegex(RuntimeError, "did not become ready"),
                ):
                    _wait_for_web_ready(root, 12345, timeout_seconds=0)
                self.assertTrue(web_pid_path(root).exists())
        terminate.assert_not_called()

    def test_reusing_a_starting_service_waits_without_spawning_another(self):
        from rightmemory.web.process import start_web_service

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            running = WebServiceStatus("running", 12345, "127.0.0.1", 9100, web_log_path(root))
            with (
                patch("rightmemory.web.process.web_service_status", return_value=running),
                patch("rightmemory.web.process._is_web_process", return_value=True),
                patch("rightmemory.web.process.process_identity", return_value="test-process"),
                patch("rightmemory.web.process.subprocess.Popen") as popen,
            ):
                register_web_process(root, running.pid)
                with patch(
                    "rightmemory.web.process.time.sleep",
                    side_effect=lambda _seconds: register_web_process(root, running.pid, ready=True),
                ) as wait:
                    result = start_web_service(root)
        self.assertEqual(result, running)
        wait.assert_called_once()
        popen.assert_not_called()

    def test_registration_waits_for_the_childs_ready_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = None
            with patch("rightmemory.web.process.process_identity", return_value="test-process"):
                register_web_process(root, process.pid)
                web_ready_path(root).write_text("54321\n", encoding="utf-8")
                with patch(
                    "rightmemory.web.process.time.sleep",
                    side_effect=lambda _seconds: register_web_process(root, process.pid, ready=True),
                ) as wait:
                    result = _wait_for_web_registration(root, process, timeout_seconds=1)
        self.assertEqual(result, process.pid)
        wait.assert_called_once()
        process.terminate.assert_not_called()

    def test_registration_reports_child_exit_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            process = Mock(pid=12345)
            process.poll.return_value = 1
            with patch("rightmemory.web.process.process_identity", return_value="test-process"):
                register_web_process(root, process.pid, ready=True)
                with patch("rightmemory.web.process.time.sleep") as wait:
                    with self.assertRaisesRegex(RuntimeError, "exited with code 1"):
                        _wait_for_web_registration(root, process, timeout_seconds=10)
                self.assertFalse(web_ready_path(root).exists())
                self.assertFalse(web_pid_path(root).exists())
        wait.assert_not_called()
        process.terminate.assert_not_called()

    def test_managed_app_announces_ready_after_server_startup(self):
        from rightmemory.web.app import main as web_app_main

        ready_event = threading.Event()
        registrations = []
        test = self

        class Server:
            should_exit = False
            started = False

            def run(server):
                test.assertTrue(web_pid_path(root).exists())
                test.assertFalse(web_ready_path(root).exists())
                server.started = True
                test.assertTrue(ready_event.wait(timeout=2))
                server.should_exit = True

        def record_registration(memory_root, pid, *, ready=False):
            register_web_process(memory_root, pid, ready=ready)
            registrations.append(ready)
            if ready:
                ready_event.set()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch.dict(os.environ, {MANAGED_WEB_ENV: "1"}),
                patch("rightmemory.web.app.create_web_app", return_value=object()),
                patch("rightmemory.web.app.uvicorn.Config"),
                patch("rightmemory.web.app.uvicorn.Server", return_value=Server()),
                patch("rightmemory.web.app.register_web_process", side_effect=record_registration),
            ):
                result = web_app_main(["--serve", "--memory-root", str(root)])
            self.assertFalse(web_ready_path(root).exists())
            self.assertFalse(web_pid_path(root).exists())
        self.assertEqual(result, 0)
        self.assertEqual(registrations, [False, True])

    def test_managed_app_never_announces_ready_after_startup_failure(self):
        from rightmemory.web.app import main as web_app_main

        server = Mock(should_exit=False, started=False)
        server.run.side_effect = RuntimeError("port is occupied")
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch.dict(os.environ, {MANAGED_WEB_ENV: "1"}),
                patch("rightmemory.web.app.create_web_app", return_value=object()),
                patch("rightmemory.web.app.uvicorn.Config"),
                patch("rightmemory.web.app.uvicorn.Server", return_value=server),
                patch("rightmemory.web.app.register_web_process", wraps=register_web_process) as register,
                self.assertRaisesRegex(RuntimeError, "port is occupied"),
            ):
                web_app_main(["--serve", "--memory-root", str(root)])
            self.assertFalse(web_ready_path(root).exists())
            self.assertFalse(web_pid_path(root).exists())
        register.assert_called_once()
        self.assertFalse(register.call_args.kwargs.get("ready", False))
