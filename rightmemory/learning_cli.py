from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import default_memory_root
from .learning import LEARNING_DOMAINS
from .learning_service import LearningService
from .profiles import resolve_memory_root


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def main(argv: list[str], *, profile_name: str | None = None) -> int:
    """JSON control surface shared by all installed execution modes."""
    try:
        parser = _Parser(prog="rightmemory learning")
        parser.add_argument("--root", type=Path)
        commands = parser.add_subparsers(dest="command", required=True)
        submit = commands.add_parser("submit", help="retain shared raw learning evidence")
        submit.add_argument("--session", required=True)
        submit.add_argument("--request-id")
        submit.add_argument("--file", type=Path)
        submit.add_argument("evidence", nargs=argparse.REMAINDER)
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
        args = parser.parse_args(argv)
        root = args.root
        if root is None:
            root = resolve_memory_root(
                profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root(),
            ).memory_root
        service = LearningService(root)
        if args.command == "submit":
            if args.file is not None and args.evidence:
                raise ValueError("supply learning evidence as a file or as text")
            evidence = args.file.read_text(encoding="utf-8") if args.file else " ".join(args.evidence)
            result = service.submit(args.session, evidence, args.request_id)
        elif args.command == "begin":
            result = service.begin(args.domain, mode=args.mode, scope=args.scope, retry_task=args.retry)
        elif args.command == "run":
            result = service.run(args.domain, mode=args.mode, scope=args.scope)
        elif args.command == "complete":
            output = "" if args.output_file is None else args.output_file.read_text(encoding="utf-8")
            result = service.complete(args.task, output)
        else:
            result = service.status(args.task)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1
