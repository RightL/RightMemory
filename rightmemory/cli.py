from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .async_update import AsyncUpdateStore, format_state
from .config import ROLES, load_config, load_review_config
from .review import ReviewScanner, normalize_transcript
from .runtime import RightMemoryRuntime


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "review":
        return _review_main(argv[1:])

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


def _chat_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} chat")
    parser.add_argument("--session", help="persist Pydantic AI message history under this session id")
    return parser


def _daemon_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} daemon")
    parser.add_argument("--stdio-json", action="store_true", help="read JSON lines from stdin and write JSON lines to stdout")
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
    scan.add_argument("--once", action="store_true", help="run one scan pass and exit")
    scan.add_argument("--since-days", type=int, help="only review transcript files modified within this many days")
    normalize = subparsers.add_parser("normalize", help="print normalized transcript JSON without running reviewer")
    normalize.add_argument("--source", choices=("codex", "claude"), required=True, help="transcript provider format")
    normalize.add_argument("--path", required=True, help="path to one provider transcript file")
    normalize.add_argument("--already-reviewed-turns", type=int, default=0, help="cursor to include in normalized JSON")
    args = parser.parse_args(argv)

    if args.command == "normalize":
        return _review_normalize(args.source, args.path, args.already_reviewed_turns)

    if args.command == "scan":
        if not args.once:
            raise ValueError("review scan currently requires --once")
        reviewer_config = load_config("reviewer")
        review_config = load_review_config()
        if args.since_days is not None:
            if args.since_days < 1:
                raise ValueError("--since-days must be a positive integer")
            review_config = replace(review_config, since_days=args.since_days)
        runtime = RightMemoryRuntime(reviewer_config)
        try:
            scanner = ReviewScanner(review_config, runtime.run_session_turn)
            print(scanner.scan_once().format())
            return 0
        finally:
            runtime.cleanup()
    raise ValueError(f"unknown review command: {args.command}")


def _review_normalize(source: str, path: str, already_reviewed_turns: int) -> int:
    if already_reviewed_turns < 0:
        raise ValueError("--already-reviewed-turns must be >= 0")
    normalized = normalize_transcript(source, Path(path).expanduser(), already_reviewed_turns)
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
    store = AsyncUpdateStore(memory_root, role)
    state = store.run_pending_batches(session_id, lambda message: runtime.run_session_turn(session_id, message))
    if state.status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
