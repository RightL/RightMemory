from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import default_memory_root
from .profiles import resolve_memory_root


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def main(argv: list[str], *, profile_name: str | None = None) -> int:
    """JSON CLI over the same model-free core exposed by assistant MCP."""
    try:
        parser = _Parser(prog="rightmemory assistant")
        parser.add_argument("--root", type=Path)
        commands = parser.add_subparsers(dest="command", required=True)
        context = commands.add_parser("context", help="prepare native Retrieve file access")
        context.add_argument("--session", required=True)
        submit = commands.add_parser("submit", help="save candidate evidence without starting a worker")
        submit.add_argument("--session", required=True)
        submit.add_argument("--request-id", required=True)
        submit.add_argument("--file", type=Path, required=True)
        update = commands.add_parser("update")
        updates = update.add_subparsers(dest="action", required=True)
        begin = updates.add_parser("begin")
        begin.add_argument("--retry", metavar="TASK")
        complete = updates.add_parser("complete")
        complete.add_argument("--task", required=True)
        complete.add_argument("--output-file", type=Path)
        status = updates.add_parser("status")
        status.add_argument("--task")
        sync = commands.add_parser("sync")
        sync.add_argument("action", choices=("refresh", "pull", "push"), nargs="?", default="refresh")
        commands.add_parser("validate", help="validate the bound root independently")
        mcp = commands.add_parser("mcp", help="run the thin native-assistant MCP server")
        mcp.add_argument("--transport", choices=("stdio",), default="stdio")
        args = parser.parse_args(argv)
        root = args.root
        if root is None:
            root = resolve_memory_root(
                profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root(),
            ).memory_root
        root = root.expanduser().resolve()
        if args.command == "mcp":
            from .assistant_mcp import mcp_main
            return mcp_main(root, ["--transport", args.transport])
        if args.command == "validate":
            from .validation import validate_memory_root
            result = validate_memory_root(root).to_dict()
        else:
            from .assistant import AssistantService
            service = AssistantService(root)
            if args.command == "context":
                result = service.context(args.session)
            elif args.command == "submit":
                result = service.submit(args.session, args.request_id, args.file.read_text(encoding="utf-8"))
            elif args.command == "sync":
                result = service.sync(args.action)
            elif args.action == "begin":
                result = service.begin_update(args.retry)
            elif args.action == "complete":
                output = "" if args.output_file is None else args.output_file.read_text(encoding="utf-8")
                result = service.complete_update(args.task, output)
            else:
                result = service.update_status(args.task)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                         ensure_ascii=False))
        return 1
