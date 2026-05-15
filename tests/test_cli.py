import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.cli import _daemon_stdio_json, _handle_json_request, main


class FakeRuntime:
    def __init__(self, config=None):
        self.config = config
        self.session_turns = []

    def run_turn(self, message: str) -> str:
        return f"handled: {message}"

    def run_session_turn(self, session_id: str, message: str) -> str:
        self.session_turns.append((session_id, message))
        return f"session {session_id}: {message}"

    def cleanup(self):
        pass


class JsonRequestTests(unittest.TestCase):
    def test_handle_json_request(self):
        response = _handle_json_request(FakeRuntime(), {"message": "hello"})

        self.assertEqual(response, {"type": "assistant", "message": "handled: hello"})

    def test_handle_json_request_requires_message(self):
        with self.assertRaises(ValueError):
            _handle_json_request(FakeRuntime(), {})

    def test_daemon_stdio_json_handles_json_lines(self):
        stdin = io.StringIO('{"message":"hello"}\n\n{"bad":true}\n')
        stdout = io.StringIO()

        with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            result = _daemon_stdio_json(FakeRuntime())

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual(lines[0], {"type": "assistant", "message": "handled: hello"})
        self.assertEqual(lines[1]["type"], "error")

    def test_main_loads_retrieve_role(self):
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["retrieve", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["retrieve"])

    def test_main_loads_dreamer_role(self):
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["dreamer", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["dreamer"])

    def test_main_rejects_old_curator_role(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["curator", "--session", "agent-1", "hello"])

        self.assertEqual(caught.exception.code, 2)

    def test_main_runs_one_shot_session_turn(self):
        roles = []
        stdout = io.StringIO()

        def fake_load_config(role):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["retrieve", "--session", "agent-1", "hello", "there"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["retrieve"])
        self.assertEqual(stdout.getvalue().strip(), "session agent-1: hello there")

    def test_main_submits_async_update_without_building_runtime(self):
        roles = []
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                roles.append(role)
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                result = main(["update", "submit", "--session", "agent-1", "remember", "this"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["update"])
        self.assertIn("status: running", stdout.getvalue())
        self.assertIn("session: agent-1", stdout.getvalue())
        self.assertIn("current_id: 1", stdout.getvalue())
        self.assertIn("queued: 0", stdout.getvalue())

    def test_submit_is_only_supported_for_update_role(self):
        with patch("rightmemory.cli.load_config", return_value=object()):
            with self.assertRaises(ValueError):
                main(["retrieve", "submit", "--session", "agent-1", "remember", "this"])

    def test_subcommand_help_does_not_load_config(self):
        stdout = io.StringIO()

        with (
            patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
            patch("sys.stdout", stdout),
        ):
            with self.assertRaises(SystemExit) as caught:
                main(["update", "submit", "--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("rightmemory update submit", stdout.getvalue())

    def test_main_queues_async_update_while_worker_is_running(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", stdout),
            ):
                popen.return_value.pid = 123
                first = main(["update", "submit", "--session", "agent-1", "first"])
                second = main(["update", "submit", "--session", "agent-1", "second"])
                pull = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(pull, 0)
        self.assertEqual(popen.call_count, 1)
        output = stdout.getvalue()
        self.assertIn("status: running", output)
        self.assertIn("current_id: 1", output)
        self.assertIn("queued: 1", output)
        self.assertIn("queued_ids: 2", output)

    def test_pull_marks_dead_worker_failed_and_keeps_queued_updates(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "second"]), 0)

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update._process_exists", return_value=False),
                patch("sys.stdout", stdout),
            ):
                pull = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(pull, 0)
        output = stdout.getvalue()
        self.assertIn("status: failed", output)
        self.assertIn("current_id: 1", output)
        self.assertIn("queued: 1", output)
        self.assertIn("queued_ids: 2", output)
        self.assertIn("error: worker process exited before writing result: pid 123", output)

    def test_main_pulls_async_update_state(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                result = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertIn("status: idle", stdout.getvalue())

    def test_submitted_worker_processes_queue_in_order(self):
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message))
                return f"session {session_id}: {message}"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.async_update.subprocess.Popen") as popen,
                patch("rightmemory.async_update._process_exists", return_value=True),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("sys.stdout", io.StringIO()),
            ):
                popen.return_value.pid = 123
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "first"]), 0)
                self.assertEqual(main(["update", "submit", "--session", "agent-1", "second"]), 0)

            with patch("rightmemory.cli.load_config", fake_load_config), patch(
                "rightmemory.cli.RightMemoryRuntime",
                RecordingRuntime,
            ):
                result = main(["update", "_submitted-worker", "--session", "agent-1"])

            stdout = io.StringIO()
            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                pull_result = main(["update", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertEqual(pull_result, 0)
        self.assertEqual(calls, [("agent-1", "first"), ("agent-1", "second")])
        self.assertIn("status: succeeded", stdout.getvalue())
        self.assertIn("queued: 0", stdout.getvalue())
        self.assertIn("result: session agent-1: second", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
