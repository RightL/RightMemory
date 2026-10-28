"""One submission and curation control surface for every execution mode."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import default_memory_root
from .curation_worker import CurationWorker, default_executor
from .learning import LEARNING_DOMAINS
from .learning_service import LearningService
from .profiles import resolve_memory_root


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _root(args, profile_name: str | None) -> Path:
    if args.root is not None:
        return args.root.expanduser().resolve()
    return resolve_memory_root(
        profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root(),
    ).memory_root


def _output(operation) -> int:
    try:
        result = operation()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


def submit_main(argv: list[str], *, profile_name: str | None = None) -> int:
    def execute():
        parser = _Parser(prog="rightmemory submit", description="Capture candidate evidence for the three domain curators.")
        parser.add_argument("--root", type=Path)
        parser.add_argument("--session", required=True)
        parser.add_argument("--request-id")
        parser.add_argument("--file", type=Path)
        parser.add_argument("--executor", choices=("native", "configured"))
        parser.add_argument("evidence", nargs=argparse.REMAINDER)
        args = parser.parse_args(argv)
        if args.file is not None and args.evidence:
            raise ValueError("supply evidence as a file or as text")
        evidence = args.file.read_text(encoding="utf-8") if args.file else " ".join(args.evidence)
        root = _root(args, profile_name)
        return LearningService(root).submit(
            args.session, evidence, args.request_id, executor=args.executor or default_executor(root),
        )
    return _output(execute)


def main(argv: list[str], *, profile_name: str | None = None) -> int:
    def execute():
        parser = _Parser(prog="rightmemory curate")
        parser.add_argument("--root", type=Path)
        commands = parser.add_subparsers(dest="command", required=True)
        for name in ("begin", "run"):
            task = commands.add_parser(name)
            task.add_argument("--domain", choices=LEARNING_DOMAINS, required=True)
            task.add_argument("--mode", choices=("update", "maintain"), default="update")
            task.add_argument("--scope", default="")
            if name == "begin":
                task.add_argument("--retry", metavar="TASK")
        complete = commands.add_parser("complete")
        complete.add_argument("--task", required=True)
        complete.add_argument("--output-file", type=Path)
        status = commands.add_parser("status")
        status.add_argument("--task")
        withdraw = commands.add_parser("withdraw")
        withdraw.add_argument("--candidate", required=True)
        worker = commands.add_parser("worker")
        worker.add_argument("action", choices=("wake", "stop", "status"))
        args = parser.parse_args(argv)
        root = _root(args, profile_name)
        if args.command == "worker":
            worker = CurationWorker(root)
            if args.action == "wake":
                return worker.wake(retry_failed=True)
            if args.action == "stop":
                return {"stopped": worker.stop(), **worker.status()}
            return worker.status()
        service = LearningService(root)
        if args.command == "begin":
            return service.begin(args.domain, mode=args.mode, scope=args.scope, retry_task=args.retry)
        if args.command == "run":
            return service.run(args.domain, mode=args.mode, scope=args.scope)
        if args.command == "complete":
            output = "" if args.output_file is None else args.output_file.read_text(encoding="utf-8")
            return service.complete(args.task, output)
        if args.command == "withdraw":
            return service.withdraw(args.candidate)
        return service.status(args.task)
    return _output(execute)
