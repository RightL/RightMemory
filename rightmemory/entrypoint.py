from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import default_memory_root
from .profiles import ProfileError, resolve_memory_root


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    profile_name, remaining = _parse_global_args(args)
    if remaining[:1] == ["assistant"]:
        from .assistant_cli import main as assistant_main

        return assistant_main(remaining[1:], profile_name=profile_name)
    if remaining[:1] == ["submit"]:
        from .curation_cli import submit_main
        return submit_main(remaining[1:], profile_name=profile_name)
    if remaining[:1] == ["curate"]:
        from .curation_cli import main as curation_main
        return curation_main(remaining[1:], profile_name=profile_name)
    if remaining[:1] == ["validate"]:
        try:
            parser = argparse.ArgumentParser(prog="rightmemory validate")
            parser.add_argument("--root", type=Path)
            parser.parse_args(remaining[1:])
            from .validation import validate_memory_root

            result = validate_memory_root(_validation_root(profile_name, remaining[1:]))
            print(result.report)
            return 0 if result.ok else 1
        except (ValueError, ProfileError, OSError, RuntimeError, UnicodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if remaining[:1] == ["embedding-service"]:
        try:
            from .embedding_service import main as embedding_service_main

            return embedding_service_main(remaining[1:])
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if remaining[:1] == ["agent-files"]:
        try:
            from .agent_files import main as agent_files_main

            return agent_files_main(remaining[1:])
        except (ValueError, FileNotFoundError, OSError, RuntimeError, UnicodeError) as exc:
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
    from . import cli

    return cli.main(args)


def _parse_global_args(argv: list[str]) -> tuple[str | None, list[str]]:
    parser = argparse.ArgumentParser(prog="rightmemory", add_help=False)
    parser.add_argument("--profile")
    namespace, remaining = parser.parse_known_args(argv)
    return namespace.profile, remaining


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


if __name__ == "__main__":
    raise SystemExit(main())
