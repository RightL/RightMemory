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

    def test_main_loads_reviewer_role(self):
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with patch("rightmemory.cli.load_config", fake_load_config), patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime):
            result = main(["reviewer", "daemon", "--stdio-json"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer"])

    def test_main_rejects_old_curator_role(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["curator", "--session", "agent-1", "hello"])

        self.assertEqual(caught.exception.code, 2)

    def test_review_scan_once_runs_scanner(self):
        roles = []
        stdout = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self):
                return type("Result", (), {"format": lambda self: "reviewed: 1"})()

        def fake_load_config(role):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.load_review_config", return_value=object()),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("rightmemory.cli.ReviewScanner", FakeScanner),
            patch("sys.stdout", stdout),
        ):
            result = main(["review", "scan", "--once"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer"])
        self.assertEqual(stdout.getvalue().strip(), "reviewed: 1")

    def test_review_normalize_prints_normalized_session_without_loading_config(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "session.jsonl"
            rows = [
                {"type": "session_meta", "timestamp": "t0", "payload": {"id": "s1", "cwd": "/repo"}},
                {"type": "event_msg", "timestamp": "t1", "payload": {"type": "user_message", "message": "hello"}},
                {"type": "event_msg", "timestamp": "t2", "payload": {"type": "agent_message", "message": "hi"}},
                {"type": "event_msg", "timestamp": "t3", "payload": {"type": "task_complete"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            with (
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("sys.stdout", stdout),
            ):
                result = main(["review", "normalize", "--source", "codex", "--path", str(path), "--already-reviewed-turns", "1"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(payload["already_reviewed_turns"], 1)
        self.assertEqual(payload["turns"][0]["user"], "hello")

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
        self.assertIn("phase: waiting", stdout.getvalue())
        self.assertIn("session: agent-1", stdout.getvalue())
        self.assertIn("current_batch: 0", stdout.getvalue())
        self.assertIn("pending: 1", stdout.getvalue())
        self.assertIn("pending_ids: 1", stdout.getvalue())

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

    def test_main_accumulates_pending_update_while_worker_is_waiting(self):
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
        self.assertIn("phase: waiting", output)
        self.assertIn("current_batch: 0", output)
        self.assertIn("pending: 2", output)
        self.assertIn("pending_ids: 1, 2", output)

    def test_pull_marks_dead_worker_failed_and_keeps_pending_updates(self):
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
        self.assertIn("current_batch: 0", output)
        self.assertIn("pending: 2", output)
        self.assertIn("pending_ids: 1, 2", output)
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

    def test_submitted_worker_processes_pending_updates_as_one_batch(self):
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
                patch("rightmemory.async_update.UPDATE_DEBOUNCE_SECONDS", 0),
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
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "agent-1")
        self.assertIn("Process the following submitted memory update candidates as one batch.", calls[0][1])
        self.assertIn("first", calls[0][1])
        self.assertIn("second", calls[0][1])
        self.assertIn("status: succeeded", stdout.getvalue())
        self.assertIn("pending: 0", stdout.getvalue())
        self.assertIn("result: session agent-1: Process the following", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
