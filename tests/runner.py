from __future__ import annotations

import argparse
import io
import inspect
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

DEFAULT_JOBS = min(24, os.cpu_count() or 1)
SERIAL_MODULES = {"tests.test_windows_process_integration"}
# These modules own independent fixtures for each test method.
INDEPENDENT_TEST_MODULES = {
    "tests.test_config": 4,
    "tests.test_tools": 4,
    "tests.test_guidance": 4,
    "tests.test_status": 4,
    "tests.test_shares": 4,
    "tests.test_async_update": 4,
    "tests.test_isolated_write_candidate_validation": 4,
    "tests.test_isolated_write_execution": 4,
    "tests.test_sync_publication": 4,
    "tests.test_sync_preflight": 4,
    "tests.test_pursuit_store": 24,
    "tests.test_pursuit_web": 6,
    "tests.test_update_queue_git_claims": 4,
    "tests.test_update_queue_git_publication": 4,
    "tests.test_update_queue_git_recovery": 4,
}
# Measured long integration cases go first so a late history operation does not
# leave the rest of the machine idle at the end of the run.
LONG_RUNNING_CASES = (
    "test_undo_redo_cross_batch_boundaries_preserves_action_order",
    "test_all_operations_validate_and_leave_the_root_clean",
    "test_operational_commit_does_not_interrupt_pending_actions_or_history",
    "test_pending_create_parent_child_rename_move_and_history_keep_ids",
    "test_retry_manual_recovers_ambiguous_push_success",
    "test_delete_undo_redo_restore_exact_edges_focus_and_bytes",
    "test_pending_delete_restores_subtree_backing_files_edges_focus_and_bytes",
    "test_expired_lease_takeover_fences_the_old_token",
    "test_unrelated_memory_commit_is_preserved_through_checkpoint_and_undo",
    "test_delete_undo_restores_executable_backing_file_mode",
    "test_undo_and_redo_add_commits_in_the_same_session",
    "test_rename_many_history_returns_ordered_id_remaps_in_both_directions",
    "test_flush_batches_distinct_actions_and_next_edit_makes_another_commit",
    "test_finalization_preserves_candidates_published_after_the_claim",
    "test_multiple_undo_redo_use_new_commits_without_rewriting_history",
)
TERMINATE_GRACE_SECONDS = 3.0
IS_WINDOWS = os.name == "nt"
WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


@dataclass(frozen=True)
class TestModule:
    filename: str
    module_name: str
    source_bytes: int
    test_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleReport:
    filename: str
    tests: int
    skips: int
    failures: int
    errors: int
    seconds: float
    successful: bool
    details: str
    stdout: str = ""
    stderr: str = ""


def _positive_jobs(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("jobs must be a positive integer") from exc
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be a positive integer")
    return jobs


def _discover_test_modules(
    test_dir: Path, package_name: str = "tests"
) -> list[TestModule]:
    paths = sorted(
        (path for path in test_dir.glob("test_*.py") if path.is_file()),
        key=lambda path: path.name,
    )
    return [
        TestModule(path.name, f"{package_name}.{path.stem}", path.stat().st_size)
        for path in paths
    ]


def _schedule_test_modules(modules: Sequence[TestModule]) -> list[TestModule]:
    # Start deadline-sensitive process checks before the late wave of app imports.
    # Independent groups include the expensive Git suites and large unit modules.
    return sorted(modules, key=lambda module: (
        module.module_name not in SERIAL_MODULES,
        not module.test_names, -module.source_bytes, module.filename,
    ))


def _partition_modules(modules: Sequence[TestModule], jobs: int) -> list[TestModule]:
    partitions = []
    for module in modules:
        if jobs == 1 or module.module_name not in INDEPENDENT_TEST_MODULES:
            partitions.append(module)
            continue
        loader = unittest.TestLoader()
        cases = list(_test_cases(loader.loadTestsFromName(module.module_name)))
        if loader.errors or not cases:
            # Let the normal worker report discovery errors with their traceback.
            partitions.append(module)
            continue
        weighted = []
        for case in cases:
            method = getattr(case, case._testMethodName)
            try:
                weight = len(inspect.getsource(method).encode("utf-8"))
            except (OSError, TypeError):
                weight = max(1, module.source_bytes // len(cases))
            weighted.append((weight, case.id()))
        count = min(jobs, len(cases), INDEPENDENT_TEST_MODULES[module.module_name])
        groups: list[list[str]] = [[] for _ in range(count)]
        weights = [0] * count
        for weight, name in sorted(weighted, key=lambda item: (-item[0], item[1])):
            index = min(range(count), key=lambda index: (weights[index], index))
            groups[index].append(name)
            weights[index] += weight
        for index, names in enumerate(groups):
            partitions.append(TestModule(
                f"{module.filename} [{index + 1}/{count}]", module.module_name,
                weights[index], tuple(sorted(names)),
            ))
    return partitions


def _worker_environment(temp_dir: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if temp_dir is not None:
        template = temp_dir / "git-template"
        (template / "hooks").mkdir(parents=True, exist_ok=True)
        env["GIT_TEMPLATE_DIR"] = str(template)
    # Fixture repositories are disposable; do not spawn housekeeping after writes.
    config_count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env.update({
        "GIT_CONFIG_COUNT": str(config_count + 1),
        f"GIT_CONFIG_KEY_{config_count}": "maintenance.auto",
        f"GIT_CONFIG_VALUE_{config_count}": "false",
    })
    if IS_WINDOWS:
        git = shutil.which("git")
        if git and Path(git).parent.name.lower() == "cmd":
            # Git for Windows' cmd launcher starts another process for every call.
            native_dir = Path(git).parent.parent / "mingw64" / "bin"
            if (native_dir / "git.exe").is_file():
                env["PATH"] = str(native_dir) + os.pathsep + env.get("PATH", "")
    return env


def _test_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _test_cases(test)
        else:
            yield test


def _process_group_kwargs() -> dict[str, object]:
    if IS_WINDOWS:
        return {"creationflags": WINDOWS_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


@contextmanager
def _ignore_cancel_signals() -> Iterator[None]:
    signals = (signal.SIGINT,) if IS_WINDOWS else (signal.SIGINT, signal.SIGTERM)
    previous = [signal.signal(sig, signal.SIG_IGN) for sig in signals]
    try:
        yield
    finally:
        for sig, handler in zip(signals, previous):
            signal.signal(sig, handler)


@contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    if IS_WINDOWS:
        yield
        return
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class _QueuedSuite(unittest.TestSuite):
    _cleanup = False

    def __init__(self, queue: Path):
        super().__init__()
        self.queue = queue

    def __iter__(self):
        # Exclusive creation gives each case to exactly one worker. Keeping one suite
        # open lets unittest retain its normal module/class fixture lifecycle.
        for pending in sorted(self.queue.glob("*.json")):
            claimed = pending.with_suffix(".claimed")
            try:
                claimed.touch(exist_ok=False)
            except FileExistsError:
                continue
            name = json.loads(pending.read_text(encoding="utf-8"))
            pending.unlink()
            yield from _test_cases(unittest.defaultTestLoader.loadTestsFromName(name))


def _run_module(
    module_name: str, result_path: Path, test_names: Sequence[str] = (),
    test_queue: Path | None = None,
) -> int:
    started = time.perf_counter()
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, buffer=True)
    suite = (_QueuedSuite(test_queue) if test_queue is not None else
             unittest.defaultTestLoader.loadTestsFromNames(test_names or [module_name]))
    result = runner.run(suite)
    payload = {
        "tests": result.testsRun,
        "skips": len(result.skipped),
        "failures": len(result.failures) + len(result.unexpectedSuccesses),
        "errors": len(result.errors),
        "seconds": time.perf_counter() - started,
        "successful": result.wasSuccessful(),
        "details": stream.getvalue(),
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def _taskkill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False, shell=False,
            timeout=TERMINATE_GRACE_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except OSError:
        pass


def _terminate_processes(processes: Sequence[subprocess.Popen[str]]) -> None:
    if IS_WINDOWS:
        for process in processes:
            _taskkill_tree(process.pid)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
    else:
        for process in processes:
            _signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while (
            any(process.poll() is None for process in processes)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        for process in processes:
            _signal_group(process, signal.SIGKILL)
    for process in processes:
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


def _error_report(
    module: TestModule, message: str, stdout: str = "", stderr: str = ""
) -> ModuleReport:
    return ModuleReport(
        module.filename, 0, 0, 0, 1, 0.0, False, message, stdout, stderr
    )


def _collect_report(
    module: TestModule,
    returncode: int,
    result_path: Path,
    stdout: str,
    stderr: str,
) -> ModuleReport:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("result is not an object")
        counts = [payload.get(key) for key in ("tests", "skips", "failures", "errors")]
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("invalid test counts")
        tests, skips, failures, errors = counts
        seconds = payload.get("seconds")
        successful = payload.get("successful")
        details = payload.get("details")
        if type(seconds) not in (int, float) or seconds < 0:
            raise ValueError("invalid elapsed time")
        if type(successful) is not bool or not isinstance(details, str):
            raise ValueError("invalid result fields")
        if successful != (failures == 0 and errors == 0):
            raise ValueError("success status does not match counts")
        if returncode != (0 if successful else 1):
            raise ValueError(f"child exited with status {returncode}")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        message = f"Test process did not produce a valid result: {exc}"
        return _error_report(module, message, stdout, stderr)
    return ModuleReport(
        module.filename,
        tests,
        skips,
        failures,
        errors,
        float(seconds),
        successful,
        details,
        stdout,
        stderr,
    )


def _run_one(
    index: int,
    module: TestModule,
    temp_dir: Path,
    repo_root: Path,
    active: dict[int, subprocess.Popen[str]],
    active_lock: threading.Lock,
    stop: threading.Event,
) -> ModuleReport:
    if stop.is_set():
        return _error_report(module, "Test run cancelled.")
    result_path = temp_dir / f"{index:04d}.json"
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "tests",
        "--_run-module",
        module.module_name,
        "--_result-file",
        str(result_path),
    ]
    if module.test_names:
        queue = temp_dir / module.module_name
        if not any(queue.glob("*.json")):
            return ModuleReport(module.filename, 0, 0, 0, 0, 0.0, True, "")
        command.extend(("--_test-queue", str(queue)))
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=_worker_environment(temp_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **_process_group_kwargs(),
        )
    except OSError as exc:
        return _error_report(module, f"Could not start test process: {exc}")
    with active_lock:
        cancelled = stop.is_set()
        if not cancelled:
            active[index] = process
    if cancelled:  # Ctrl-C landed after Popen and before registration.
        _terminate_processes([process])
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_processes([process])
        raise
    finally:
        with active_lock:
            active.pop(index, None)
    if cancelled:
        return _error_report(module, "Test run cancelled.", stdout, stderr)
    return _collect_report(module, process.returncode, result_path, stdout, stderr)


def _run_parallel(
    modules: Sequence[TestModule], jobs: int, temp_dir: Path, repo_root: Path
) -> list[ModuleReport] | None:
    scheduled = _schedule_test_modules(modules)
    queues: dict[str, list[str]] = {}
    for module in modules:
        if module.test_names:
            queues.setdefault(module.module_name, []).extend(module.test_names)
    for module_name, names in queues.items():
        queue = temp_dir / module_name
        queue.mkdir()
        # Keep each class together so workers can reuse its fixture.
        priorities = {name: index for index, name in enumerate(LONG_RUNNING_CASES)}
        def case_order(name):
            class_name, method = name.rsplit(".", 1)
            return class_name, priorities.get(method, len(priorities)), name
        for index, name in enumerate(sorted(names, key=case_order)):
            (queue / f"{index:04d}.json").write_text(json.dumps(name), encoding="utf-8")
    active: dict[int, subprocess.Popen[str]] = {}
    active_lock, stop = threading.Lock(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=jobs)
    futures, reports = {}, []
    try:
        for index, module in enumerate(scheduled):
            if module.module_name in SERIAL_MODULES:
                reports.append(_run_one(index, module, temp_dir, repo_root, active, active_lock, stop))
                continue
            future = executor.submit(
                _run_one, index, module, temp_dir, repo_root, active, active_lock, stop
            )
            futures[future] = module
        for future in as_completed(futures):
            try:
                reports.append(future.result())
            except Exception as exc:
                message = f"Runner worker failed: {exc}"
                reports.append(_error_report(futures[future], message))
        executor.shutdown(wait=True)
    except BaseException as exc:
        with active_lock:
            stop.set()
            running = list(active.values())
        with _ignore_cancel_signals():
            _terminate_processes(running)
            executor.shutdown(wait=True, cancel_futures=True)
        if isinstance(exc, KeyboardInterrupt):
            return None
        raise
    return sorted(reports, key=lambda report: report.filename)


def _print_reports(
    reports: Sequence[ModuleReport], *, jobs: int, wall_seconds: float
) -> bool:
    for report in sorted(reports, key=lambda item: item.filename):
        status = "PASS" if report.successful else "FAIL"
        counts = (
            f"{report.tests} tests, {report.skips} skips, "
            f"{report.failures} failures, {report.errors} errors"
        )
        print(f"{status} {report.filename} ({counts}, {report.seconds:.2f}s)")
        if not report.successful:
            for heading, text in (
                ("", report.details),
                ("Captured process stdout:", report.stdout),
                ("Captured process stderr:", report.stderr),
            ):
                if text.strip():
                    if heading:
                        print(heading)
                    print(text.rstrip())
    tests = sum(report.tests for report in reports)
    skips = sum(report.skips for report in reports)
    failures = sum(report.failures for report in reports)
    errors = sum(report.errors for report in reports)
    successful = failures == 0 and errors == 0
    print("-" * 70)
    print(f"Ran {tests} tests in {wall_seconds:.2f}s with {jobs} jobs")
    print(
        f"{'OK' if successful else 'FAILED'} "
        f"(skips={skips}, failures={failures}, errors={errors})"
    )
    return successful


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests",
        description="Run tests in isolated worker processes.",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=_positive_jobs,
        default=DEFAULT_JOBS,
        help=f"maximum concurrent test processes (default: {DEFAULT_JOBS})",
    )
    parser.add_argument("--_run-module", help=argparse.SUPPRESS)
    parser.add_argument("--_result-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_test-name", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--_test-queue", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args._run_module is not None or args._result_file is not None:
        if args._run_module is None or args._result_file is None:
            raise SystemExit(
                "internal module and result arguments must be used together"
            )
        return _run_module(args._run_module, args._result_file, args._test_name, args._test_queue)
    test_dir = Path(__file__).resolve().parent
    modules = _discover_test_modules(test_dir)
    if not modules:
        print("No test_*.py modules found.", file=sys.stderr)
        return 2
    jobs = min(args.jobs, len(modules))
    print(f"Running {len(modules)} test modules with {jobs} jobs", flush=True)
    started = time.perf_counter()
    modules = _partition_modules(modules, jobs)
    with (
        _sigterm_as_interrupt(),
        tempfile.TemporaryDirectory(prefix="rightmemory-tests-") as temp_dir,
    ):
        reports = _run_parallel(modules, jobs, Path(temp_dir), test_dir.parent)
    if reports is None:
        print("\nInterrupted; terminated active test processes.", file=sys.stderr)
        return 130
    successful = _print_reports(
        reports,
        jobs=jobs,
        wall_seconds=time.perf_counter() - started,
    )
    return 0 if successful else 1
