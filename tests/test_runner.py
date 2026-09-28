from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
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
    _partition_modules,
    _process_group_kwargs,
    _run_module,
    _run_one,
    _run_parallel,
    _schedule_test_modules,
    _terminate_processes,
    _worker_environment,
    _QueuedSuite,
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

    def test_independent_groups_start_before_other_modules(self):
        group = TestModule("test_git.py [1/2]", "tests.test_git", 100, ("tests.test_git.Case.test_one",))
        large = TestModule("test_large.py", "tests.test_large", 10000)
        process_checks = TestModule("test_windows_process_integration.py", "tests.test_windows_process_integration", 1)
        self.assertEqual(_schedule_test_modules([large, group, process_checks]), [process_checks, group, large])

    def test_jobs_default_override_and_validation(self):
        parser = _build_parser()
        self.assertEqual(parser.parse_args([]).jobs, DEFAULT_JOBS)
        self.assertEqual(parser.parse_args(["-j", "3"]).jobs, 3)
        for value in ("0", "-1", "many"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                _positive_jobs(value)

    def test_independent_cases_run_once_with_module_and_class_fixtures(self):
        fixture_name = "_rightmemory_partition_fixture"
        fixture = types.ModuleType(fixture_name)
        events = []
        fixture.setUpModule = lambda: events.append("module setup")
        fixture.tearDownModule = lambda: events.append("module cleanup")

        class FixtureTests(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                events.append("class setup")
                cls.addClassCleanup(events.append, "class cleanup")

            def setUp(self):
                self.addCleanup(events.append, "test cleanup")

        for index in range(7):
            def test_case(self, number=index):
                events.append(number)
            setattr(FixtureTests, f"test_{index}", test_case)
        FixtureTests.__module__ = fixture_name
        FixtureTests.__qualname__ = "FixtureTests"
        fixture.FixtureTests = FixtureTests
        sys.modules[fixture_name] = fixture
        self.addCleanup(sys.modules.pop, fixture_name, None)
        module = TestModule("test_fixture.py", fixture_name, 100)
        other = TestModule("test_other.py", "tests.test_other", 50)
        with patch("tests.runner.INDEPENDENT_TEST_MODULES", {fixture_name: 3}):
            partitions = _partition_modules([module, other], 3)
            self.assertEqual(_partition_modules([module, other], 1), [module, other])
        self.assertEqual(len(partitions), 4)
        self.assertEqual(partitions[-1], other)
        self.assertEqual(len({p.filename for p in partitions}), 4)
        self.assertEqual(events, [])
        reports = []
        with tempfile.TemporaryDirectory() as temp:
            for index, partition in enumerate(partitions[:-1]):
                result_path = Path(temp) / f"{index}.json"
                self.assertEqual(_run_module(partition.module_name, result_path, partition.test_names), 0)
                reports.append(json.loads(result_path.read_text(encoding="utf-8")))
        self.assertEqual(sorted(e for e in events if isinstance(e, int)), list(range(7)))
        self.assertEqual(sum(r["tests"] for r in reports), 7)
        for event in ("module setup", "module cleanup", "class setup", "class cleanup"):
            self.assertEqual(events.count(event), 3)
        self.assertEqual(events.count("test cleanup"), 7)
        events.clear()
        names = [name for part in partitions[:-1] for name in part.test_names]
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp) / "queue"
            queue.mkdir()
            for index, name in enumerate(names):
                (queue / f"{index}.json").write_text(json.dumps(name), encoding="utf-8")
            result = Path(temp) / "result.json"
            self.assertEqual(_run_module(fixture_name, result, test_queue=queue), 0)
            self.assertEqual(json.loads(result.read_text())["tests"], 7)
            self.assertEqual(events.count("class setup"), 1)
            self.assertEqual(events.count("class cleanup"), 1)
            for claim in queue.glob("*.claimed"):
                claim.unlink()
            for index, name in enumerate(names):
                (queue / f"{index}.json").write_text(json.dumps(name), encoding="utf-8")
            with ThreadPoolExecutor(max_workers=3) as executor:
                claimed = list(executor.map(lambda _: [case.id() for case in _QueuedSuite(queue)], range(3)))
            self.assertEqual(sorted(name for group in claimed for name in group), sorted(names))
            self.assertEqual(list(queue.glob("*.json")), [])
            self.assertEqual(len(list(queue.glob("*.claimed"))), 7)

    def test_partition_import_errors_are_left_for_the_worker(self):
        module = TestModule("test_missing.py", "_missing_test_module", 10)
        with patch("tests.runner.INDEPENDENT_TEST_MODULES", {module.module_name: 3}):
            self.assertEqual(_partition_modules([module], 3), [module])

    def test_deadline_sensitive_modules_finish_before_parallel_work_starts(self):
        serial = TestModule("test_serial.py", "tests.test_windows_process_integration", 1)
        other = TestModule("test_other.py", "tests.test_other", 100)
        events = []

        def run_one(_index, module, *_args):
            if module == other:
                self.assertEqual(events, [serial.module_name])
            events.append(module.module_name)
            return ModuleReport(module.filename, 1, 0, 0, 0, 0.1, True, "")

        with patch("tests.runner._run_one", side_effect=run_one):
            reports = _run_parallel([other, serial], 2, Path("temp"), Path("repo"))
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report.successful for report in reports))

    def test_workers_use_an_empty_git_template_with_a_hooks_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = _worker_environment(root)
            self.assertEqual(env["GIT_TEMPLATE_DIR"], str(root / "git-template"))
            self.assertTrue((root / "git-template" / "hooks").is_dir())
            self.assertEqual(list((root / "git-template" / "hooks").iterdir()), [])

    def test_workers_disable_fixture_maintenance_without_replacing_git_config(self):
        with patch.dict(os.environ, {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Fixture User",
        }):
            env = _worker_environment()
            self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "user.name")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Fixture User")
            self.assertEqual(env["GIT_CONFIG_KEY_1"], "maintenance.auto")
            self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")
            self.assertEqual(os.environ["GIT_CONFIG_COUNT"], "1")

    def test_windows_workers_use_native_git_without_changing_parent_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native = root / "mingw64" / "bin"
            native.mkdir(parents=True)
            (native / "git.exe").touch()
            with patch("tests.runner.IS_WINDOWS", True), patch(
                "tests.runner.shutil.which", return_value=str(root / "cmd" / "git.exe")
            ), patch.dict(os.environ, {"PATH": "original-path"}):
                self.assertEqual(_worker_environment()["PATH"], str(native) + os.pathsep + "original-path")
                self.assertEqual(os.environ["PATH"], "original-path")
                (native / "git.exe").unlink()
                self.assertEqual(_worker_environment()["PATH"], "original-path")

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

        FixtureTests.__module__ = fixture_name
        FixtureTests.__qualname__ = "FixtureTests"
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

        with tempfile.TemporaryDirectory() as temp, patch(
            "tests.runner.INDEPENDENT_TEST_MODULES", {fixture_name: 3}
        ):
            reports = []
            partitions = _partition_modules([TestModule("test_fixture.py", fixture_name, 100)], 3)
            for index, partition in enumerate(partitions):
                path = Path(temp) / f"{index}.json"
                _run_module(partition.module_name, path, partition.test_names)
                reports.append(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(
            tuple(sum(r[key] for r in reports) for key in ("tests", "skips", "failures", "errors")),
            (4, 1, 1, 1),
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
