from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .async_update import AsyncUpdateStore, format_state
from .config import load_config
from .runtime import RightMemoryRuntime


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="rightmemory")
    parser.add_argument("role", choices=("curator", "dreamer"), help="RightMemory runtime role")
    if not argv or argv[0] in {"-h", "--help"}:
        parser.parse_args(argv)
        return 0

    args = parser.parse_args(argv[:1])
    remaining = argv[1:]
    if remaining == ["-h"] or remaining == ["--help"]:
        _turn_parser(args.role).parse_args(remaining)
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
    store = AsyncUpdateStore(memory_root, role)
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    try:
        result = runtime.run_session_turn(session_id, message)
    except Exception as exc:
        store.fail(session_id, str(exc))
        return 1
    store.finish(session_id, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
