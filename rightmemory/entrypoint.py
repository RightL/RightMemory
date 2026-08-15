from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import cli
from .config import default_memory_root
from .guidance import submit_guidance
from .profiles import ProfileError, resolve_memory_root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    profile_name, remaining = _parse_global_args(args)
    if remaining[:1] != ["guidance"]:
        return cli.cli_main(args)
    try:
        active = resolve_memory_root(
            profile_name=profile_name,
            cwd=Path.cwd(),
            default_root=default_memory_root(),
        )
        return _guidance_main(remaining[1:], active.memory_root)
    except (ValueError, ProfileError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _parse_global_args(argv: list[str]) -> tuple[str | None, list[str]]:
    parser = argparse.ArgumentParser(prog="rightmemory", add_help=False)
    parser.add_argument("--profile")
    namespace, remaining = parser.parse_known_args(argv)
    return namespace.profile, remaining


def _guidance_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory guidance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="capture pending agent-guidance evidence")
    submit.add_argument("--session", required=True)
    submit.add_argument("message", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command != "submit":
        raise ValueError(f"unknown guidance command: {args.command}")
    evidence = " ".join(args.message).strip()
    if not evidence:
        raise ValueError("guidance evidence must not be empty")
    entry = submit_guidance(memory_root, args.session, evidence)
    print(f"captured pending guidance: {entry.entry_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
