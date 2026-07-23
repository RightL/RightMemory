from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch

from tests.runner import (
    DEFAULT_JOBS,
    ModuleReport,
    TestModule,
    _build_parser,
    _discover_test_modules,
    _print_reports,
    _positive_jobs,
    _process_group_kwargs,
    _run_module,
    _run_one,
    _run_parallel,
    _schedule_test_modules,
    _terminate_processes,
)


class TestRunnerTests(unittest.TestCase):
    def test_discovery_and_largest_first_scheduling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "test_small.py").write_text("x", encoding="utf-8")
            (root / "test_large.py").write_text("xxx", encoding="utf-8")
            (root / "helper.py").touch()
            (root / "test_directory.py").mkdir()
            modules = _discover_test_modules(root, "example")

        self.assertEqual(
            [(item.filename, item.module_name) for item in modules],
            [
                ("test_large.py", "example.test_large"),
                ("test_small.py", "example.test_small"),
            ],
        )
        self.assertEqual(
            [item.filename for item in _schedule_test_modules(modules)],
            ["test_large.py", "test_small.py"],
        )

    def test_jobs_default_override_and_validation(self):
        parser = _build_parser()
        self.assertEqual(parser.parse_args([]).jobs, DEFAULT_JOBS)
        self.assertEqual(parser.parse_args(["-j", "3"]).jobs, 3)
        for value in ("0", "-1", "many"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _positive_jobs(value)

    def test_process_groups_and_tree_cleanup_cover_both_platforms(self):
        with patch("tests.runner.IS_WINDOWS", True):
            self.assertIn("creationflags", _process_group_kwargs())
        with patch("tests.runner.IS_WINDOWS", False):
            self.assertEqual(_process_group_kwargs(), {"start_new_session": True})

        process = Mock(pid=123)
        process.poll.return_value = 0
        with (
            patch("tests.runner.IS_WINDOWS", False),
            patch("tests.runner.signal.SIGKILL", 9, create=True),
            patch("tests.runner._signal_group") as signal_group,
        ):
            _terminate_processes([process])
        self.assertEqual(
            signal_group.call_args_list,
            [
                call(process, signal.SIGTERM),
                call(process, 9),
            ],
        )

        with (
            patch("tests.runner.IS_WINDOWS", True),
            patch("tests.runner._taskkill_tree") as taskkill,
        ):
            _terminate_processes([process])
        taskkill.assert_called_once_with(123)

    def test_cancellation_between_popen_and_registration_kills_tree(self):
        module = TestModule("test_example.py", "tests.test_example", 1)
        process = Mock(pid=123, returncode=-1)
        process.communicate.return_value = ("", "")
        active, lock, stop = {}, threading.Lock(), threading.Event()

        def launch(*_args, **_kwargs):
            stop.set()
            return process

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("tests.runner.subprocess.Popen", side_effect=launch),
            patch("tests.runner._terminate_processes") as terminate,
        ):
            report = _run_one(0, module, Path(temp), Path(temp), active, lock, stop)

        terminate.assert_called_once_with([process])
        self.assertFalse(report.successful)
        self.assertEqual(active, {})

    def test_communicate_exception_kills_registered_process_tree(self):
        module = TestModule("test_example.py", "tests.test_example", 1)
        process = Mock(pid=123)
        process.communicate.side_effect = RuntimeError("pipe failed")
        active, lock, stop = {}, threading.Lock(), threading.Event()
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("tests.runner.subprocess.Popen", return_value=process),
            patch("tests.runner._terminate_processes") as terminate,
            self.assertRaisesRegex(RuntimeError, "pipe failed"),
        ):
            _run_one(0, module, Path(temp), Path(temp), active, lock, stop)
        terminate.assert_called_once_with([process])
        self.assertEqual(active, {})

    def test_coordinator_exception_also_cleans_up(self):
        executor = Mock()
        executor.submit.side_effect = RuntimeError("submit failed")
        module = TestModule("test_example.py", "tests.test_example", 1)
        with (
            patch("tests.runner.ThreadPoolExecutor", return_value=executor),
            patch("tests.runner._terminate_processes") as terminate,
            self.assertRaisesRegex(RuntimeError, "submit failed"),
        ):
            _run_parallel([module], 1, Path("temp"), Path("repo"))
        terminate.assert_called_once_with([])
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_module_result_aggregates_failures_errors_and_skips(self):
        fixture_name = "_rightmemory_runner_fixture"
        fixture = types.ModuleType(fixture_name)

        class FixtureTests(unittest.TestCase):
            def test_passes(self):
                pass

            @unittest.skip("fixture skip")
            def test_skips(self):
                pass

            def test_fails(self):
                self.fail("fixture failure")

            def test_errors(self):
                raise RuntimeError("fixture error")

        fixture.FixtureTests = FixtureTests
        sys.modules[fixture_name] = fixture
        self.addCleanup(sys.modules.pop, fixture_name, None)
        with tempfile.TemporaryDirectory() as temp:
            result_path = Path(temp) / "result.json"
            status = _run_module(fixture_name, result_path)
            result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (
                status,
                result["tests"],
                result["skips"],
                result["failures"],
                result["errors"],
            ),
            (1, 4, 1, 1, 1),
        )

    def test_report_is_sorted_aggregated_and_keeps_diagnostics(self):
        reports = [
            ModuleReport("test_z.py", 2, 1, 0, 0, 0.1, True, ""),
            ModuleReport("test_a.py", 1, 0, 1, 0, 0.2, False, "trace", "out", "err"),
        ]
        output = StringIO()
        with redirect_stdout(output):
            successful = _print_reports(reports, jobs=2, wall_seconds=0.3)
        text = output.getvalue()
        self.assertLess(text.index("test_a.py"), text.index("test_z.py"))
        self.assertIn("Ran 3 tests", text)
        self.assertIn("skips=1, failures=1, errors=0", text)
        self.assertIn("trace", text)
        self.assertFalse(successful)
