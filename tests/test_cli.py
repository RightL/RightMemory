import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
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


class FakeReviewResult:
    def __init__(self, text: str, reviewed: int = 0, failed: int = 0):
        self.text = text
        self.reviewed = reviewed
        self.failed = failed

    def format(self):
        return self.text


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
                return FakeReviewResult("reviewed: 1", reviewed=1)

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

    def test_review_watch_runs_scans_until_interrupted(self):
        roles = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        results = [
            FakeReviewResult("reviewed: 1", reviewed=1),
            FakeReviewResult("reviewed: 0", reviewed=0),
        ]

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self):
                return results.pop(0)

        def fake_load_config(role):
            roles.append(role)
            return type("Config", (), {"memory_root": memory_root})()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch", "--interval", "5"])

        self.assertEqual(result, 130)
        self.assertEqual(roles, ["reviewer", "reviewer"])
        self.assertIn("rightmemory review scan", stdout.getvalue())
        self.assertIn("reviewed: 1", stdout.getvalue())
        self.assertIn("reviewed: 0", stdout.getvalue())
        self.assertIn("rightmemory review watch stopped", stderr.getvalue())

    def test_review_watch_retries_failed_scan_without_sleeping(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        results = [
            FakeReviewResult("failed: 1", failed=1),
            FakeReviewResult("reviewed: 0", reviewed=0),
        ]

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self):
                return results.pop(0)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch", "--interval", "5"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(5)
        self.assertIn("failed: 1", stdout.getvalue())
        self.assertIn("reviewed: 0", stdout.getvalue())

    def test_review_watch_default_interval_is_two_hours(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        class FakeScanner:
            def __init__(self, config, run_reviewer):
                self.config = config
                self.run_reviewer = run_reviewer

            def scan_once(self):
                return FakeReviewResult("reviewed: 0", reviewed=0)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.load_review_config", return_value=object()),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("rightmemory.cli.ReviewScanner", FakeScanner),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["review", "watch"])

        self.assertEqual(result, 130)
        sleep.assert_called_once_with(7200)

    def test_review_watch_rejects_non_positive_interval(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["review", "watch", "--interval", "0"])

    def test_watch_start_starts_review_and_dreamer_managed_processes(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            roles = []

            def fake_load_config(role):
                roles.append(role)
                return type("Config", (), {"memory_root": memory_root})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.watch.subprocess.Popen", side_effect=[FakeProcess(101), FakeProcess(102)]) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            review_pid = (memory_root / ".runtime" / "watch" / "review.pid").read_text(encoding="utf-8")
            dreamer_pid = (memory_root / ".runtime" / "watch" / "dreamer.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["reviewer", "dreamer"])
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(review_pid, "101\n")
        self.assertEqual(dreamer_pid, "102\n")
        self.assertIn("review: running pid 101", stdout.getvalue())
        self.assertIn("dreamer: running pid 102", stdout.getvalue())

    def test_watch_status_reports_stopped_without_config(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch("rightmemory.cli.MEMORY_ROOT", Path(tempdir)),
                patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")),
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "status"])

        self.assertEqual(result, 0)
        self.assertIn("review: stopped", stdout.getvalue())
        self.assertIn("dreamer: stopped", stdout.getvalue())

    def test_watch_stop_sends_graceful_term_and_removes_pid(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            pid_path = memory_root / ".runtime" / "watch" / "dreamer.pid"
            pid_path.parent.mkdir(parents=True)
            pid_path.write_text("123\n", encoding="utf-8")
            with (
                patch("rightmemory.cli.MEMORY_ROOT", memory_root),
                patch("rightmemory.watch._is_managed_watch_process", side_effect=[True, False]),
                patch("rightmemory.watch.os.kill") as kill,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "stop", "dreamer"])
            pid_exists = pid_path.exists()

        self.assertEqual(result, 0)
        kill.assert_called_once()
        self.assertFalse(pid_exists)
        self.assertIn("dreamer: stopped pid 123", stdout.getvalue())

    def test_dreamer_watch_runs_cycle_and_defaults_to_three_days(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message))
                return super().run_session_turn(session_id, message)

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("Config", (), {"memory_root": Path(tempdir)})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch"])

            state_path = Path(tempdir) / ".runtime" / "dreamer" / "watch-state.json"
            state_exists = state_path.exists()

        self.assertEqual(result, 130)
        self.assertEqual(calls, [("dreamer-watch", "Run a scheduled dream cycle.")])
        sleep.assert_called_once_with(259200)
        self.assertIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("session dreamer-watch: Run a scheduled dream cycle.", stdout.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())
        self.assertTrue(state_exists)

    def test_dreamer_watch_waits_when_recent_cycle_was_recorded(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            state_path = memory_root / ".runtime" / "dreamer" / "watch-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"last_run_at": datetime.now(timezone.utc).isoformat(), "last_status": "succeeded"}),
                encoding="utf-8",
            )
            config = type("Config", (), {"memory_root": memory_root})()
            with (
                patch("rightmemory.cli.load_config", return_value=config),
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("dreamer should wait")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                result = main(["dreamer", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertGreaterEqual(sleep.call_args.args[0], 59)
        self.assertNotIn("rightmemory dreamer cycle", stdout.getvalue())
        self.assertIn("rightmemory dreamer watch stopped", stderr.getvalue())

    def test_dreamer_watch_rejects_non_positive_interval(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["dreamer", "watch", "--interval", "0"])

    def test_watch_is_only_supported_for_dreamer_role(self):
        with patch("rightmemory.cli.load_config", side_effect=AssertionError("config should not load")):
            with self.assertRaises(ValueError):
                main(["retrieve", "watch"])

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
                result = main(["review", "normalize", "--source", "codex", "--path", str(path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["source"], "codex")
        self.assertEqual(payload["session_id"], "s1")
        self.assertNotIn("already_reviewed_turns", payload)
        self.assertNotIn("i", payload["turns"][0])
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
