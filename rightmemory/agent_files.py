from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Iterator


AGENT_FILES_ROOT_ENV = "RIGHTMEMORY_AGENT_FILES_ROOT"
CATALOG_VERSION = 1
CATALOG_PATH = "catalog.json"
INSTRUCTION_FILENAMES = frozenset({"AGENTS.md", "CLAUDE.md"})
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        ".worktree",
        ".worktrees",
        "__pycache__",
        "node_modules",
        "venv",
    }
)


@dataclass(frozen=True)
class AgentFileContent:
    digest: str
    content: str
    sources: tuple[str, ...]

    @property
    def short_id(self) -> str:
        return self.digest[:12]


@dataclass(frozen=True)
class AgentFileCatalog:
    targets: tuple[str, ...] = ()
    contents: tuple[AgentFileContent, ...] = ()


@dataclass(frozen=True)
class RegistrationResult:
    added: tuple[str, ...]
    existing: tuple[str, ...]


@dataclass(frozen=True)
class CollectionResult:
    file_count: int
    content_count: int
    warnings: tuple[str, ...]


def default_agent_files_root() -> Path:
    value = os.environ.get(AGENT_FILES_ROOT_ENV, "~/.rightmemory-agent-files")
    return Path(value).expanduser().resolve()


def register_paths(state_root: Path, paths: list[Path]) -> RegistrationResult:
    catalog = load_catalog(state_root)
    targets = set(catalog.targets)
    added: list[str] = []
    existing: list[str] = []

    for path in paths:
        target = str(_validated_target(path))
        if target in targets:
            existing.append(target)
        else:
            targets.add(target)
            added.append(target)

    if added:
        _write_catalog(
            state_root,
            AgentFileCatalog(tuple(sorted(targets)), catalog.contents),
        )
    return RegistrationResult(tuple(added), tuple(existing))


def collect_catalog(state_root: Path) -> CollectionResult:
    catalog = load_catalog(state_root)
    if not catalog.targets:
        raise ValueError("no agent-file paths are registered")

    warnings: list[str] = []
    discovered: dict[str, str] = {}
    for raw_target in catalog.targets:
        for path in _discover_instruction_files(Path(raw_target), warnings):
            try:
                resolved = path.resolve(strict=True)
                discovered[str(resolved)] = normalize_instruction_content(resolved.read_bytes())
            except (OSError, UnicodeError) as exc:
                warnings.append(f"could not read {path}: {exc}")

    grouped: dict[str, tuple[str, list[str]]] = {}
    for source, content in sorted(discovered.items()):
        digest = instruction_digest(content)
        if digest not in grouped:
            grouped[digest] = (content, [])
        grouped[digest][1].append(source)

    contents = tuple(
        AgentFileContent(digest, content, tuple(sources))
        for digest, (content, sources) in sorted(grouped.items())
    )
    _write_catalog(state_root, AgentFileCatalog(catalog.targets, contents))
    return CollectionResult(len(discovered), len(contents), tuple(warnings))


def load_catalog(state_root: Path) -> AgentFileCatalog:
    path = state_root / CATALOG_PATH
    if not path.exists():
        return AgentFileCatalog()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != CATALOG_VERSION:
            raise ValueError(f"expected version {CATALOG_VERSION}")
        targets = tuple(sorted(set(data["targets"])))
        contents = tuple(
            AgentFileContent(
                digest=item["digest"],
                content=item["content"],
                sources=tuple(sorted(set(item["sources"]))),
            )
            for item in data["contents"]
        )
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid agent-file catalog: {exc}") from exc

    for item in contents:
        if instruction_digest(item.content) != item.digest:
            raise ValueError(f"agent-file catalog digest mismatch: {item.digest}")
    return AgentFileCatalog(targets, tuple(sorted(contents, key=lambda item: item.digest)))


def find_content(catalog: AgentFileCatalog, content_id: str) -> AgentFileContent:
    query = content_id.strip().lower()
    matches = [content for content in catalog.contents if content.digest.startswith(query)]
    if not query or not matches:
        raise ValueError(f"unknown agent-file content id: {content_id}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous agent-file content id: {content_id}")
    return matches[0]


def normalize_instruction_content(raw: bytes) -> str:
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def instruction_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None, *, state_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rightmemory agent-files",
        description="Collect AGENTS.md and CLAUDE.md files without interpreting them.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register", help="register directories or instruction files")
    register.add_argument("paths", nargs="+", type=Path)
    commands.add_parser("collect", help="refresh the catalog from registered paths")
    commands.add_parser("list", help="list unique contents and their source paths")
    show = commands.add_parser("show", help="show one unique content")
    show.add_argument("content_id", help="full content hash or a unique prefix")

    args = parser.parse_args(argv)
    root = default_agent_files_root() if state_root is None else Path(state_root).expanduser()

    if args.command == "register":
        result = register_paths(root, args.paths)
        for path in result.added:
            print(f"registered: {path}")
        for path in result.existing:
            print(f"already registered: {path}")
        return 0

    if args.command == "collect":
        result = collect_catalog(root)
        print(
            f"collected {result.file_count} instruction files "
            f"in {result.content_count} unique content groups"
        )
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 0

    catalog = load_catalog(root)
    if args.command == "list":
        if not catalog.contents:
            print("no collected agent files")
            return 0
        for index, content in enumerate(catalog.contents):
            if index:
                print()
            label = "source" if len(content.sources) == 1 else "sources"
            print(f"{content.short_id}  {len(content.sources)} {label}")
            for path in content.sources:
                print(f"  {path}")
        return 0

    if args.command == "show":
        content = find_content(catalog, args.content_id)
        print(content.digest)
        print("Sources:")
        for path in content.sources:
            print(f"- {path}")
        print()
        print(content.content, end="" if content.content.endswith("\n") else "\n")
        return 0

    raise ValueError(f"unknown agent-files command: {args.command}")


def _validated_target(path: Path) -> Path:
    target = path.expanduser().resolve(strict=True)
    if target.is_dir() or (target.is_file() and target.name in INSTRUCTION_FILENAMES):
        return target
    expected = " or ".join(sorted(INSTRUCTION_FILENAMES))
    raise ValueError(f"registered path must be a directory or file named {expected}: {target}")


def _discover_instruction_files(target: Path, warnings: list[str]) -> Iterator[Path]:
    if not target.exists():
        warnings.append(f"registered path does not exist: {target}")
        return
    if target.is_file():
        yield target
        return

    def on_error(exc: OSError) -> None:
        warnings.append(f"could not scan {exc.filename or target}: {exc}")

    for current, directories, filenames in os.walk(
        target,
        topdown=True,
        onerror=on_error,
        followlinks=False,
    ):
        directories[:] = sorted(
            name for name in directories if name not in IGNORED_DIRECTORY_NAMES
        )
        for filename in sorted(INSTRUCTION_FILENAMES.intersection(filenames)):
            yield Path(current) / filename


def _write_catalog(state_root: Path, catalog: AgentFileCatalog) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / CATALOG_PATH
    temporary = state_root / f".{CATALOG_PATH}.{os.getpid()}.tmp"
    payload = {
        "version": CATALOG_VERSION,
        "targets": list(catalog.targets),
        "contents": [
            {
                "digest": content.digest,
                "content": content.content,
                "sources": list(content.sources),
            }
            for content in catalog.contents
        ],
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
