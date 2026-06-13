import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.cli import main
from rightmemory.web.process import (
    WebServiceStatus,
    web_log_path,
    web_pid_path,
    web_settings_path,
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
