from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import cli
from .config import default_memory_root
from .guidance import GUIDANCE_INBOX_PATH, submit_guidance, validate_guidance_inbox
from .profiles import ProfileError, resolve_memory_root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    profile_name, remaining = _parse_global_args(args)
    if remaining[:1] == ["agent-files"]:
        try:
            from .agent_files import main as agent_files_main

            return agent_files_main(remaining[1:])
        except (ValueError, FileNotFoundError, OSError, RuntimeError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if remaining[:1] == ["pursuit"]:
        try:
            active = resolve_memory_root(
                profile_name=profile_name,
                cwd=Path.cwd(),
                default_root=default_memory_root(),
            )
            from .pursuit_cli import pursuit_main

            return pursuit_main(active.memory_root, remaining[1:])
        except (ValueError, ProfileError, FileNotFoundError, OSError, RuntimeError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if remaining[:1] == ["mcp"]:
        try:
            active = resolve_memory_root(
                profile_name=profile_name,
                cwd=Path.cwd(),
                default_root=default_memory_root(),
            )
            from .mcp import mcp_main

            return mcp_main(active.memory_root, remaining[1:])
        except (ValueError, ProfileError, FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if remaining[:1] == ["guidance"]:
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
    result = cli.main(args)
    if result != 0 or remaining[:1] != ["validate"]:
        return result
    try:
        root = _validation_root(profile_name, remaining[1:])
        errors = _guidance_validation_errors(root)
    except (ValueError, ProfileError, FileNotFoundError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("guidance validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    task_errors = _pursuit_task_validation_errors(root)
    if task_errors:
        print("pursuit task validation failed:", file=sys.stderr)
        for error in task_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


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


def _validation_root(profile_name: str | None, argv: list[str]) -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path)
    namespace, _remaining = parser.parse_known_args(argv)
    if namespace.root is not None:
        return namespace.root.expanduser().resolve()
    active = resolve_memory_root(
        profile_name=profile_name,
        cwd=Path.cwd(),
        default_root=default_memory_root(),
    )
    return active.memory_root


def _guidance_validation_errors(memory_root: Path) -> list[str]:
    path = memory_root / GUIDANCE_INBOX_PATH
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink() or not path.is_file():
        return [f"{GUIDANCE_INBOX_PATH} must be a regular file"]
    return validate_guidance_inbox(path.read_text(encoding="utf-8"))


def _pursuit_task_validation_errors(memory_root: Path) -> list[str]:
    path = memory_root / "pursuit_tasks.toml"
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink() or not path.is_file():
        return ["pursuit_tasks.toml must be a regular file"]
    try:
        from .pursuit_tasks import load_registry

        load_registry(memory_root)
    except (ValueError, OSError, UnicodeError) as exc:
        return [str(exc)]
    return []


if __name__ == "__main__":
    raise SystemExit(main())
