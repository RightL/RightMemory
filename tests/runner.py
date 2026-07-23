from __future__ import annotations

import argparse
import io
import json
import os
import signal
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

DEFAULT_JOBS = 6
TERMINATE_GRACE_SECONDS = 3.0
IS_WINDOWS = os.name == "nt"
WINDOWS_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


@dataclass(frozen=True)
class TestModule:
    filename: str
    module_name: str
    source_bytes: int


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
    return sorted(modules, key=lambda module: (-module.source_bytes, module.filename))


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


def _run_module(module_name: str, result_path: Path) -> int:
    started = time.perf_counter()
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, buffer=True)
    result = runner.run(unittest.defaultTestLoader.loadTestsFromName(module_name))
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
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
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
    active: dict[int, subprocess.Popen[str]] = {}
    active_lock, stop = threading.Lock(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=jobs)
    futures, reports = {}, []
    try:
        for index, module in enumerate(scheduled):
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
        description="Run each test module in a fresh process.",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args._run_module is not None or args._result_file is not None:
        if args._run_module is None or args._result_file is None:
            raise SystemExit(
                "internal module and result arguments must be used together"
            )
        return _run_module(args._run_module, args._result_file)
    test_dir = Path(__file__).resolve().parent
    modules = _discover_test_modules(test_dir)
    if not modules:
        print("No test_*.py modules found.", file=sys.stderr)
        return 2
    jobs = min(args.jobs, len(modules))
    print(f"Running {len(modules)} test modules with {jobs} jobs", flush=True)
    started = time.perf_counter()
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
