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

    def test_main_loads_curator_role(self):
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["curator", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["curator"])

    def test_main_loads_dreamer_role(self):
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["dreamer", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["dreamer"])

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
            result = main(["curator", "--session", "agent-1", "hello", "there"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["curator"])
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
                result = main(["curator", "submit", "--session", "agent-1", "remember", "this"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["curator"])
        self.assertIn("status: running", stdout.getvalue())
        self.assertIn("session: agent-1", stdout.getvalue())

    def test_main_pulls_async_update_state(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                result = main(["curator", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertIn("status: idle", stdout.getvalue())

    def test_submitted_worker_records_success(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            with patch("rightmemory.cli.load_config", fake_load_config), patch(
                "rightmemory.cli.RightMemoryRuntime",
                FakeRuntime,
            ):
                result = main(["curator", "_submitted-worker", "--session", "agent-1", "hello"])

            stdout = io.StringIO()
            with patch("rightmemory.cli.load_config", fake_load_config), patch("sys.stdout", stdout):
                pull_result = main(["curator", "pull", "--session", "agent-1"])

        self.assertEqual(result, 0)
        self.assertEqual(pull_result, 0)
        self.assertIn("status: succeeded", stdout.getvalue())
        self.assertIn("result: session agent-1: hello", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
