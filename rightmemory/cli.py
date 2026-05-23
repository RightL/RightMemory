from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .async_update import AsyncUpdateStore, format_state
from .config import MEMORY_ROOT, ROLES, load_config, load_dreamer_watch_config, load_review_config, load_sync_config
from .dreamer_trigger import DreamerTriggerStore
from .doctor import format_doctor_report, run_agent_cli_doctor
from .review import ReviewScanner, normalize_transcript
from .runtime import RightMemoryRuntime
from .session import MemoryWriteLock
from .sync import SyncManager
from .watch import (
    MANAGED_WATCH_TARGETS,
    InstallStamp,
    ManagedWatchStatus,
    StopWatchResult,
    WatchLock,
    managed_watch_status,
    start_managed_watch,
    stop_managed_watch,
)

DEFAULT_REVIEW_WATCH_INTERVAL_SECONDS = 2 * 60 * 60
DEFAULT_DREAMER_WATCH_RETRY_SECONDS = 60
DEFAULT_SYNC_WATCH_INTERVAL_SECONDS = 60 * 60
WATCH_REFRESH_POLL_SECONDS = 5
DREAMER_WATCH_SESSION_ID = "dreamer-watch"
DREAMER_WATCH_MESSAGE = "Run a scheduled dream cycle."
_DREAMER_WATCH_SKIPPED = "skipped"
_DREAMER_WATCH_SUCCEEDED = "succeeded"
_DREAMER_WATCH_FAILED = "failed"
SYNC_WATCH_SESSION_ID = "sync-watch"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "watch":
        return _watch_manager_main(argv[1:])
    if argv and argv[0] == "review":
        return _review_main(argv[1:])
    if argv and argv[0] == "sync":
        return _sync_main(argv[1:])
    if argv and argv[0] == "doctor":
        return _doctor_main(argv[1:])

    parser = argparse.ArgumentParser(prog="rightmemory")
    parser.add_argument("role", choices=tuple(sorted(ROLES)), help="RightMemory runtime role")
    if not argv or argv[0] in {"-h", "--help"}:
        parser.parse_args(argv)
        return 0

    args = parser.parse_args(argv[:1])
    remaining = argv[1:]
    if remaining == ["-h"] or remaining == ["--help"]:
        _turn_parser(args.role).parse_args(remaining)
        return 0

    if remaining and remaining[0] == "submit":
        if args.role != "update":
            raise ValueError("submit is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _submit_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "pull":
        if args.role != "update":
            raise ValueError("pull is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _pull_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "_submitted-worker" and args.role != "update":
        raise ValueError("_submitted-worker is only supported for the update role")
    if remaining and remaining[0] == "chat" and _is_help_request(remaining[1:]):
        _chat_parser(args.role).parse_args(remaining[1:])
        return 0
    if remaining and remaining[0] == "daemon" and _is_help_request(remaining[1:]):
        _daemon_parser(args.role).parse_args(remaining[1:])
        return 0
    if remaining and remaining[0] == "watch":
        if args.role != "dreamer":
            raise ValueError("watch is only supported for the dreamer role")
        if _is_help_request(remaining[1:]):
            _dreamer_watch_parser().parse_args(remaining[1:])
            return 0
        watch_args = _dreamer_watch_parser().parse_args(remaining[1:])
        return _dreamer_watch(watch_args.interval, watch_args.session)

    config = load_config(args.role)
    if remaining and remaining[0] == "submit":
        submit_args = _submit_parser(args.role).parse_args(remaining[1:])
        return _submit(config.memory_root, args.role, submit_args.session, submit_args.message)
    if remaining and remaining[0] == "pull":
        pull_args = _pull_parser(args.role).parse_args(remaining[1:])
        return _pull(config.memory_root, args.role, pull_args.session)

    runtime = RightMemoryRuntime(config)
    try:
        if remaining and remaining[0] == "_submitted-worker":
            worker_args = _submit_parser(args.role).parse_args(remaining[1:])
            return _submitted_worker(
                runtime,
                config.memory_root,
                args.role,
                worker_args.session,
                worker_args.message,
            )
        if not remaining or remaining[0] == "chat":
            chat_args = _chat_parser(args.role).parse_args(remaining[1:] if remaining else [])
            return _chat(runtime, chat_args.session)
        if remaining[0] == "daemon":
            daemon_args = _daemon_parser(args.role).parse_args(remaining[1:])
            if not daemon_args.stdio_json:
                raise ValueError("daemon currently requires --stdio-json")
            return _daemon_stdio_json(runtime)
        turn_args = _turn_parser(args.role).parse_args(remaining)
        return _session_turn(runtime, turn_args.session, turn_args.message)
    finally:
        runtime.cleanup()


def _is_help_request(args: list[str]) -> bool:
    return args == ["-h"] or args == ["--help"]


class _WatchStopToken:
    requested = False


@contextmanager
def _watch_stop_signal(label: str):
    token = _WatchStopToken()
    previous = signal.getsignal(signal.SIGTERM)

    def handle_stop(signum, frame):
        token.requested = True
        print(f"rightmemory {label} watch stopping after current work", file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, handle_stop)
    try:
        yield token
    finally:
        signal.signal(signal.SIGTERM, previous)


def _watch_manager_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory watch")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "restart"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("target", nargs="?", choices=("all", *MANAGED_WATCH_TARGETS), default="all")
    stop = subparsers.add_parser("stop")
    stop.add_argument("target", nargs="?", choices=("all", *MANAGED_WATCH_TARGETS), default="all")
    stop.add_argument("--timeout", type=int, default=30, help="seconds to wait for graceful stop")
    args = parser.parse_args(argv)

    if args.command == "start":
        return _watch_start(args.target)
    if args.command == "status":
        return _watch_status(args.target)
    if args.command == "stop":
        return _watch_stop(args.target, args.timeout)
    if args.command == "restart":
        stop_result = _watch_stop(args.target, 30)
        if stop_result:
            return stop_result
        return _watch_start(args.target)
    raise ValueError(f"unknown watch command: {args.command}")


def _watch_start(target: str) -> int:
    failed = False
    for name in _watch_targets(target):
        try:
            if name == "sync":
                sync_config = load_sync_config()
                if not sync_config.enabled:
                    print("sync: disabled")
                    continue
                memory_root = sync_config.memory_root
            else:
                config = load_config(_watch_role(name))
                memory_root = config.memory_root
            status = start_managed_watch(memory_root, name, sys.executable)
            print(_format_watch_status(status))
        except Exception as exc:
            failed = True
            print(f"{name}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _watch_stop(target: str, timeout: int) -> int:
    failed = False
    for name in _watch_targets(target):
        try:
            result = stop_managed_watch(MEMORY_ROOT, name, timeout)
            print(_format_stop_result(result))
            if result.state in {"external", "stopping"}:
                failed = True
        except Exception as exc:
            failed = True
            print(f"{name}: failed: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _watch_status(target: str) -> int:
    for name in _watch_targets(target):
        print(_format_watch_status(managed_watch_status(MEMORY_ROOT, name)))
    return 0


def _watch_targets(target: str) -> list[str]:
    if target == "all":
        return list(MANAGED_WATCH_TARGETS)
    if target not in MANAGED_WATCH_TARGETS:
        joined = ", ".join(("all", *MANAGED_WATCH_TARGETS))
        raise ValueError(f"watch target must be one of: {joined}")
    return [target]


def _watch_role(name: str) -> str:
    if name == "review":
        return "reviewer"
    if name == "dreamer":
        return "dreamer"
    raise ValueError(f"unknown watch target: {name}")


def _format_watch_status(status: ManagedWatchStatus) -> str:
    if status.state == "running" and status.pid is not None:
        return f"{status.name}: running pid {status.pid}, log {status.log_path}"
    if status.state == "external":
        return f"{status.name}: running outside manager"
    if status.state == "stale" and status.pid is not None:
        return f"{status.name}: stale pid {status.pid}"
    return f"{status.name}: stopped"


def _format_stop_result(result: StopWatchResult) -> str:
    if result.state == "stopped" and result.pid is not None:
        return f"{result.name}: stopped pid {result.pid}"
    if result.state == "stopping" and result.pid is not None:
        return f"{result.name}: stopping pid {result.pid}"
    if result.state == "stale-removed" and result.pid is not None:
        return f"{result.name}: removed stale pid {result.pid}"
    if result.state == "external":
        return f"{result.name}: running outside manager; stop the foreground process directly"
    return f"{result.name}: stopped"


def _chat_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} chat")
    parser.add_argument("--session", help="persist Pydantic AI message history under this session id")
    return parser


def _daemon_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} daemon")
    parser.add_argument("--stdio-json", action="store_true", help="read JSON lines from stdin and write JSON lines to stdout")
    return parser


def _dreamer_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory dreamer watch")
    parser.add_argument(
        "--interval",
        type=int,
        help="override seconds between trigger checks",
    )
    parser.add_argument(
        "--session",
        default=DREAMER_WATCH_SESSION_ID,
        help="persist dreamer message history under this session id",
    )
    return parser


def _turn_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role}")
    parser.add_argument("--session", required=True, help="persist Pydantic AI message history under this session id")
    parser.add_argument("message", nargs=argparse.REMAINDER)
    return parser


def _submit_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} submit")
    parser.add_argument("--session", required=True, help="persist Pydantic AI message history under this session id")
    parser.add_argument("message", nargs=argparse.REMAINDER)
    return parser


def _pull_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} pull")
    parser.add_argument("--session", required=True, help="read the latest async update state for this session id")
    return parser


def _review_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan configured transcript sources")
    scan.add_argument("--once", action="store_true", help="review one eligible batch and exit")
    scan.add_argument("--since-days", type=int, help="only review transcript files modified within this many days")
    watch = subparsers.add_parser("watch", help="keep scanning configured transcript sources")
    watch.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_REVIEW_WATCH_INTERVAL_SECONDS,
        help="seconds to sleep when no eligible work is found",
    )
    watch.add_argument("--since-days", type=int, help="only review transcript files modified within this many days")
    normalize = subparsers.add_parser("normalize", help="print normalized transcript JSON without running reviewer")
    normalize.add_argument("--source", choices=("codex", "claude"), required=True, help="transcript provider format")
    normalize.add_argument("--path", required=True, help="path to one provider transcript file")
    args = parser.parse_args(argv)

    if args.command == "normalize":
        return _review_normalize(args.source, args.path)

    if args.command == "scan":
        if not args.once:
            raise ValueError("review scan currently requires --once")
        print(_run_review_scan(args.since_days).format())
        return 0
    if args.command == "watch":
        return _review_watch(args.interval, args.since_days)
    raise ValueError(f"unknown review command: {args.command}")


def _sync_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser("watch", help="keep local memory sync state alive")
    watch.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_SYNC_WATCH_INTERVAL_SECONDS,
        help="seconds between sync watch checks",
    )
    args = parser.parse_args(argv)

    if args.command == "watch":
        return _sync_watch(args.interval)
    raise ValueError(f"unknown sync command: {args.command}")


def _doctor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory doctor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("agent-cli", help="check Codex/Claude CLI-agent mode setup")
    args = parser.parse_args(argv)

    if args.command == "agent-cli":
        checks = run_agent_cli_doctor()
        print(format_doctor_report(checks))
        return 0 if all(check.ok for check in checks) else 1
    raise ValueError(f"unknown doctor command: {args.command}")


def _run_review_scan(since_days: int | None = None):
    reviewer_config = load_config("reviewer")
    return _run_review_scan_with_config(reviewer_config, since_days)


def _run_review_scan_with_config(reviewer_config: Any, since_days: int | None = None):
    review_config = load_review_config()
    if since_days is not None:
        if since_days < 1:
            raise ValueError("--since-days must be a positive integer")
        review_config = replace(review_config, since_days=since_days)
    dreamer_watch_config = load_dreamer_watch_config()
    runtime = RightMemoryRuntime(reviewer_config)
    try:
        scanner = ReviewScanner(
            review_config,
            runtime.run_session_turn,
            on_review_success=_dreamer_trigger_incrementer(
                reviewer_config.memory_root,
                dreamer_watch_config.review_session_points,
            ),
        )
        return scanner.scan_once()
    finally:
        runtime.cleanup()


def _review_watch(interval: int, since_days: int | None = None) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    reviewer_config = load_config("reviewer")
    refresh = InstallStamp(reviewer_config.memory_root)
    try:
        with _watch_stop_signal("review") as stop, WatchLock(reviewer_config.memory_root, "review"):
            next_config: Any | None = reviewer_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory review scan", flush=True)
                if next_config is None:
                    result = _run_review_scan(since_days)
                else:
                    result = _run_review_scan_with_config(next_config, since_days)
                    next_config = None
                print(result.format(), flush=True)
                _reexec_if_install_changed(refresh, stop)
                if not stop.requested and (result.reviewed > 0 or result.failed > 0):
                    continue
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory review watch stopped", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("rightmemory review watch stopped", file=sys.stderr)
        return 130


def _run_dream_cycle(session_id: str, dreamer_config: Any | None = None) -> str:
    config = dreamer_config if dreamer_config is not None else load_config("dreamer")
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(session_id, DREAMER_WATCH_MESSAGE)
    finally:
        runtime.cleanup()


def _dreamer_watch_once(watch_config: Any, session_id: str, run_cycle: Callable[[str], str]) -> str:
    store = DreamerTriggerStore(watch_config.memory_root)
    state = store.read()
    if state.points < watch_config.trigger_points:
        return _DREAMER_WATCH_SKIPPED

    timestamp = datetime.now(UTC).isoformat()
    print(f"[{timestamp}] rightmemory dreamer cycle", flush=True)
    try:
        output = run_cycle(session_id)
    except Exception as exc:
        print(f"rightmemory dreamer cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return _DREAMER_WATCH_FAILED

    print(output, flush=True)
    store.consume_if_available(watch_config.trigger_points)
    return _DREAMER_WATCH_SUCCEEDED


def _dreamer_watch(interval: int | None, session_id: str) -> int:
    if interval is not None and interval < 1:
        raise ValueError("--interval must be a positive integer")
    watch_config = load_dreamer_watch_config()
    if interval is not None:
        watch_config = replace(watch_config, check_interval_seconds=interval)
    dreamer_config = load_config("dreamer")
    refresh = InstallStamp(watch_config.memory_root)
    try:
        with _watch_stop_signal("dreamer") as stop, WatchLock(watch_config.memory_root, "dreamer"):
            next_config: Any | None = dreamer_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)

                def run_cycle(current_session_id: str) -> str:
                    nonlocal next_config
                    if next_config is None:
                        return _run_dream_cycle(current_session_id)
                    output = _run_dream_cycle(current_session_id, next_config)
                    next_config = None
                    return output

                status = _dreamer_watch_once(watch_config, session_id, run_cycle)
                _reexec_if_install_changed(refresh, stop)
                if status == _DREAMER_WATCH_SKIPPED:
                    if not _sleep_with_refresh_check(watch_config.check_interval_seconds, refresh, stop):
                        break
                elif status == _DREAMER_WATCH_FAILED:
                    retry_seconds = min(watch_config.check_interval_seconds, DEFAULT_DREAMER_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
        print("rightmemory dreamer watch stopped", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("rightmemory dreamer watch stopped", file=sys.stderr)
        return 130


def _sync_watch(interval: int) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    sync_config = load_sync_config()
    if not sync_config.enabled:
        print("rightmemory sync watch disabled", file=sys.stderr)
        return 0
    refresh = InstallStamp(sync_config.memory_root)
    try:
        with _watch_stop_signal("sync") as stop, WatchLock(sync_config.memory_root, "sync"):
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory sync check", flush=True)
                manager = SyncManager(sync_config)
                try:
                    with MemoryWriteLock(sync_config.memory_root):
                        result = manager.background_pull()
                except Exception as exc:
                    print(
                        f"rightmemory sync check failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(result.message, flush=True)
                    if result.status in {"conflict", "dirty"}:
                        try:
                            _run_sync_reconciler(manager, result)
                        except Exception as exc:
                            print(
                                f"rightmemory sync reconciler failed: {type(exc).__name__}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                _reexec_if_install_changed(refresh, stop)
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory sync watch stopped", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("rightmemory sync watch stopped", file=sys.stderr)
        return 130


def _run_sync_reconciler(manager: SyncManager, result: Any) -> None:
    reconciler_config = load_config("sync-reconciler")
    reconciler_root = Path(reconciler_config.memory_root)
    if reconciler_root != manager.memory_root:
        raise ValueError(
            "sync-reconciler memory root mismatch: "
            f"sync watch uses {manager.memory_root}, sync-reconciler uses {reconciler_root}"
        )
    runtime = RightMemoryRuntime(reconciler_config)
    try:
        print(runtime.run_session_turn(SYNC_WATCH_SESSION_ID, manager.repair_message(result)), flush=True)
    finally:
        runtime.cleanup()


def _sleep_with_refresh_check(seconds: int, refresh: InstallStamp, stop: _WatchStopToken | None = None) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if stop is not None and stop.requested:
            return False
        _reexec_if_install_changed(refresh, stop)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(math.ceil(remaining), WATCH_REFRESH_POLL_SECONDS))


def _reexec_if_install_changed(refresh: InstallStamp, stop: _WatchStopToken | None = None) -> None:
    if stop is not None and stop.requested:
        return
    if not refresh.changed():
        return
    print("rightmemory install changed; restarting watch process", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "rightmemory.cli", *sys.argv[1:]])


def _review_normalize(source: str, path: str) -> int:
    normalized = normalize_transcript(source, Path(path).expanduser())
    if normalized is None:
        raise ValueError("no completed turns found in transcript")
    print(json.dumps(normalized.to_payload(), ensure_ascii=False, indent=2))
    return 0


def _chat(runtime: RightMemoryRuntime, session_id: str | None = None) -> int:
    print("RightMemory standalone chat. Type /exit to quit.", file=sys.stderr)
    while True:
        try:
            message = input("> ")
        except EOFError:
            print(file=sys.stderr)
            return 0
        if message.strip() in {"/exit", "/quit"}:
            return 0
        if not message.strip():
            continue
        if session_id is None:
            print(runtime.run_turn(message))
        else:
            print(runtime.run_session_turn(session_id, message))


def _daemon_stdio_json(runtime: RightMemoryRuntime) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = _handle_json_request(runtime, request)
        except Exception as exc:
            response = {"type": "error", "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def _handle_json_request(runtime: RightMemoryRuntime, request: dict[str, Any]) -> dict[str, str]:
    message = request.get("message")
    if not isinstance(message, str):
        raise ValueError("JSON request must contain string field: message")
    return {"type": "assistant", "message": runtime.run_turn(message)}


def _session_turn(runtime: RightMemoryRuntime, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    print(runtime.run_session_turn(session_id, message))
    return 0


def _submit(memory_root, role: str, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    state = AsyncUpdateStore(memory_root, role).submit(session_id, message)
    print(format_state(state))
    return 0


def _pull(memory_root, role: str, session_id: str) -> int:
    state = AsyncUpdateStore(memory_root, role).read(session_id)
    print(format_state(state))
    return 0


def _submitted_worker(
    runtime: RightMemoryRuntime,
    memory_root,
    role: str,
    session_id: str,
    message_parts: list[str],
) -> int:
    if message_parts:
        raise ValueError("_submitted-worker does not accept message arguments")
    dreamer_watch_config = load_dreamer_watch_config()
    store = AsyncUpdateStore(memory_root, role)
    state = store.run_pending_batches(
        session_id,
        lambda message: runtime.run_session_turn(session_id, message),
        on_batch_success=_dreamer_trigger_incrementer(
            memory_root,
            dreamer_watch_config.update_candidate_points,
        ),
    )
    if state.status == "failed":
        return 1
    return 0


def _dreamer_trigger_incrementer(memory_root: Path, points_per_item: float) -> Callable[[int], None]:
    store = DreamerTriggerStore(memory_root)

    def increment(count: int) -> None:
        try:
            store.increment(count * points_per_item)
        except OSError as exc:
            print(
                f"Warning: could not update dreamer trigger state: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return increment


if __name__ == "__main__":
    raise SystemExit(main())
