from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


FULL_READ_LINE_LIMIT = 200
MAX_LIST_FILES = 500
MAX_SEARCH_MATCHES = 200
MAX_OUTLINE_ITEMS = 500
KNOWN_EDGE_TYPES = {
    "dep",
    "emb",
    "bak",
    "agg",
    "ver",
    "ext",
    "up",
    "rel",
    "loc",
    "run",
    "cfg",
    "out",
    "in",
    "doc",
    "todo",
}
COMMIT_MESSAGE_LINE_LIMIT = 120

ANCHOR_RE = re.compile(r"^(#{1,4})\s+.*?\{(?:F#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*?(?:\s*→\s*\[(.*?)\])?\s*$")
EDGE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*):\s*([A-Za-z0-9_.-]+)\s*$")
MEMORY_DETAIL_FILE_RE = re.compile(r"^MEMORY_[A-Za-z0-9_.-]+\.md$")
DREAM_LOG_FILE_RE = re.compile(r"^dream_logs/[A-Za-z0-9_.-]+\.md$")
PATCH_BEGIN = "*** Begin Patch"
PATCH_END = "*** End Patch"
ADD_FILE_PREFIX = "*** Add File: "
UPDATE_FILE_PREFIX = "*** Update File: "
DELETE_FILE_PREFIX = "*** Delete File: "
PATCH_OPERATION_PREFIXES = (ADD_FILE_PREFIX, UPDATE_FILE_PREFIX, DELETE_FILE_PREFIX)


@dataclass(frozen=True)
class MemoryId:
    id: str
    file: Path
    line_number: int
    edges: tuple[tuple[str, str], ...]


class MemoryTools:
    def __init__(self, memory_root: Path):
        self.memory_root = memory_root.resolve()

    def list_files(self, pattern: str = "MEMORY*.md") -> list[str]:
        """List files under the RightMemory root that match a glob pattern."""
        pattern = pattern.strip() or "MEMORY*.md"
        pattern = self._normalize_glob_pattern(pattern)
        paths = [
            path.relative_to(self.memory_root).as_posix()
            for path in self.memory_root.glob(pattern)
            if path.is_file() and self._is_under_root(path)
        ]
        paths.sort()
        if len(paths) > MAX_LIST_FILES:
            raise ValueError(f"pattern matched more than {MAX_LIST_FILES} files; use a narrower pattern")
        return paths

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_lines: int = FULL_READ_LINE_LIMIT,
    ) -> str:
        """Read a file under the RightMemory root, optionally by inclusive line range."""
        resolved = self._resolve_path(path)
        lines = self._read_lines(resolved, path)
        self._validate_positive("max_lines", max_lines)

        start = 1 if start_line is None else start_line
        if end_line is None:
            end = min(len(lines), start + max_lines - 1)
        else:
            end = min(end_line, start + max_lines - 1)
        if start < 1:
            raise ValueError("start_line must be >= 1")
        if end < start:
            raise ValueError("end_line must be >= start_line")
        end = min(end, len(lines))

        output = self._format_lines(lines, start, end)
        if end < len(lines) and (end_line is None or end < end_line):
            output += (
                f"\n[truncated: showing lines {start}-{end} of {len(lines)}; "
                "use start_line/end_line or search_files/read_around for more]"
            )
        return output

    def read_around(self, path: str, line_number: int, context_lines: int = 40) -> str:
        """Read a window of lines around a target line in a file under the RightMemory root."""
        resolved = self._resolve_path(path)
        lines = self._read_lines(resolved, path)
        self._validate_positive("line_number", line_number)
        if context_lines < 0:
            raise ValueError("context_lines must be >= 0")
        if line_number > len(lines):
            raise ValueError(f"line_number exceeds file length: {len(lines)}")
        start = max(1, line_number - context_lines)
        end = min(len(lines), line_number + context_lines)
        return self._format_lines(lines, start, end)

    def search_files(
        self,
        query: str,
        pattern: str = "MEMORY*.md",
        context_lines: int = 2,
        max_matches: int = 50,
        case_sensitive: bool = False,
    ) -> str:
        """Search matching files and return matched lines with nearby context."""
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if context_lines < 0:
            raise ValueError("context_lines must be >= 0")
        self._validate_positive("max_matches", max_matches)
        max_matches = min(max_matches, MAX_SEARCH_MATCHES)

        needle = query if case_sensitive else query.lower()
        blocks: list[str] = []
        match_count = 0
        for relative_path in self.list_files(pattern):
            resolved = self._resolve_path(relative_path)
            lines = self._read_lines(resolved, relative_path)
            for index, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.lower()
                if needle not in haystack:
                    continue
                match_count += 1
                start = max(1, index - context_lines)
                end = min(len(lines), index + context_lines)
                blocks.append(f"{relative_path}:{index} match\n" + self._format_lines(lines, start, end))
                if match_count >= max_matches:
                    return "\n\n".join(blocks) + f"\n[truncated: reached max_matches={max_matches}]"

        if not blocks:
            return "no matches"
        return "\n\n".join(blocks)

    def outline_file(self, path: str, max_items: int = MAX_OUTLINE_ITEMS) -> str:
        """Return Markdown headings with line numbers for one file under the RightMemory root."""
        resolved = self._resolve_path(path)
        lines = self._read_lines(resolved, path)
        self._validate_positive("max_items", max_items)
        max_items = min(max_items, MAX_OUTLINE_ITEMS)

        items: list[str] = []
        for line_number, line in enumerate(lines, start=1):
            match = HEADING_RE.match(line)
            if not match:
                continue
            depth = len(match.group(1))
            items.append(f"{line_number}: {'  ' * (depth - 1)}{match.group(1)} {match.group(2)}")
            if len(items) >= max_items:
                return "\n".join(items) + f"\n[truncated: reached max_items={max_items}]"
        if not items:
            return "no headings"
        return "\n".join(items)

    def apply_patch(self, patch: str) -> str:
        """Apply a Codex-style patch to files under the RightMemory root.

        Format:
        *** Begin Patch
        *** Update File: path
        @@
         context line
        -old line
        +new line
        *** End Patch
        """
        if not patch.strip():
            raise ValueError("patch must not be empty")
        touched = self._apply_codex_patch(patch)
        files = ", ".join(sorted(touched)) if touched else "no file paths detected"
        return f"applied patch: {files}"

    def git_status(self) -> str:
        """Return short git status for the RightMemory root."""
        return self._run_git(["git", "status", "--short"])

    def git_diff(self, paths: list[str] | None = None) -> str:
        """Return git diff for selected paths, or the whole RightMemory root."""
        command = ["git", "diff"]
        if paths:
            command.append("--")
            for path in paths:
                resolved = self._resolve_path(path)
                command.append(resolved.relative_to(self.memory_root).as_posix())
        return self._run_git(command)

    def git_add(self, paths: list[str]) -> str:
        """Stage selected memory files or dream logs under the RightMemory root."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = [self._allowed_commit_path(path) for path in paths]
        self._run_git(["git", "add", "--", *relative_paths])
        return "staged: " + ", ".join(relative_paths)

    def git_commit(self, message: str) -> str:
        """Commit staged memory files and dream logs under the RightMemory root."""
        message = self._validate_commit_message(message)
        staged = self._run_git(["git", "diff", "--cached", "--name-only", "--"])
        staged_files = [line for line in staged.splitlines() if line]
        if not staged_files:
            raise ValueError("no staged changes to commit")
        for path in staged_files:
            self._allowed_commit_path(path)

        self._run_git(["git", "commit", "-m", message])
        commit_hash = self._run_git(["git", "rev-parse", "--short", "HEAD"])
        status = self.git_status()
        if status:
            return f"committed {commit_hash}: {message}\n{status}"
        return f"committed {commit_hash}: {message}"

    def validate_memory(self) -> str:
        """Validate RightMemory ids, graph edges, and protected pending-task sections."""
        files = self._memory_files()
        ids: dict[str, MemoryId] = {}
        errors: list[str] = []

        for file_path in files:
            for item in self._parse_file(file_path):
                if item.id in ids:
                    previous = ids[item.id]
                    errors.append(
                        f"duplicate id `{item.id}` at {self._loc(item)}; first seen at {self._loc(previous)}"
                    )
                else:
                    ids[item.id] = item

        for item in ids.values():
            seen_edges: set[tuple[str, str]] = set()
            for edge_type, target in item.edges:
                edge = (edge_type, target)
                if edge in seen_edges:
                    errors.append(f"duplicate edge `{edge_type}:{target}` at {self._loc(item)}")
                    continue
                seen_edges.add(edge)
                if target == item.id:
                    errors.append(f"self-edge `{edge_type}:{target}` at {self._loc(item)}")
                    continue
                if edge_type not in KNOWN_EDGE_TYPES:
                    errors.append(f"unknown edge type `{edge_type}` at {self._loc(item)}")
                    continue
                target_item = ids.get(target)
                if target_item is None:
                    errors.append(f"dangling edge `{edge_type}:{target}` at {self._loc(item)}")

        errors.extend(self._pending_section_errors(files))
        if errors:
            return "validation failed:\n" + "\n".join(f"- {error}" for error in errors)
        return f"validation passed: {len(ids)} ids across {len(files)} memory files"

    def _memory_files(self) -> list[Path]:
        files = [
            path
            for pattern in ("MEMORY.md", "MEMORY_*.md")
            for path in self.memory_root.glob(pattern)
            if path.is_file() and self._is_under_root(path)
        ]
        return sorted(set(files))

    def _read_lines(self, resolved: Path, original_path: str) -> list[str]:
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {original_path}")
        return resolved.read_text(encoding="utf-8").splitlines()

    def _format_lines(self, lines: list[str], start: int, end: int) -> str:
        selected = lines[start - 1 : end]
        return "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start))

    def _validate_positive(self, name: str, value: int) -> None:
        if value < 1:
            raise ValueError(f"{name} must be >= 1")

    def _parse_file(self, file_path: Path) -> list[MemoryId]:
        items: list[MemoryId] = []
        for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
            match = ANCHOR_RE.match(line) or NODE_RE.match(line)
            if not match:
                continue
            item_id = match.group(2) if line.startswith("#") else match.group(1)
            edge_text = match.group(3) if line.startswith("#") else match.group(2)
            items.append(
                MemoryId(
                    id=item_id,
                    file=file_path,
                    line_number=line_number,
                    edges=tuple(self._parse_edges(edge_text or "")),
                )
            )
        return items

    def _parse_edges(self, edge_text: str) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        for raw_edge in edge_text.split(","):
            raw_edge = raw_edge.strip()
            if not raw_edge:
                continue
            match = EDGE_RE.match(raw_edge)
            if not match:
                continue
            edges.append((match.group(1), match.group(2)))
        return edges

    def _pending_section_errors(self, files: list[Path]) -> list[str]:
        errors: list[str] = []
        for path in files:
            current = self._pending_section(path.read_text(encoding="utf-8"))
            if current is None:
                continue
            original = self._head_file(path)
            if original is None:
                continue
            original_section = self._pending_section(original)
            if original_section is not None and current != original_section:
                errors.append(f"protected pending-task section changed in {path.relative_to(self.memory_root)}")
        return errors

    def _pending_section(self, text: str) -> str | None:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("# User Pending Task and Thoughts"):
                return "\n".join(lines[index:])
        return None

    def _head_file(self, path: Path) -> str | None:
        relative = path.relative_to(self.memory_root).as_posix()
        process = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            return None
        return process.stdout

    def _apply_codex_patch(self, patch: str) -> set[str]:
        lines = patch.splitlines()
        if not lines or lines[0] != PATCH_BEGIN:
            raise ValueError(f"patch must start with `{PATCH_BEGIN}`")
        if lines[-1] != PATCH_END:
            raise ValueError(f"patch must end with `{PATCH_END}`")

        touched: set[str] = set()
        backups: dict[Path, str | None] = {}
        index = 1
        try:
            while index < len(lines) - 1:
                line = lines[index]
                if line.startswith(ADD_FILE_PREFIX):
                    relative_path, index = self._apply_add_file(
                        line.removeprefix(ADD_FILE_PREFIX), lines, index + 1, backups
                    )
                elif line.startswith(UPDATE_FILE_PREFIX):
                    relative_path, index = self._apply_update_file(
                        line.removeprefix(UPDATE_FILE_PREFIX), lines, index + 1, backups
                    )
                elif line.startswith(DELETE_FILE_PREFIX):
                    relative_path = self._apply_delete_file(line.removeprefix(DELETE_FILE_PREFIX), backups)
                    index += 1
                else:
                    raise ValueError(f"unknown patch operation: {line}")
                touched.add(relative_path)
        except Exception:
            self._restore_backups(backups)
            raise
        return touched

    def _apply_add_file(
        self,
        path: str,
        lines: list[str],
        index: int,
        backups: dict[Path, str | None],
    ) -> tuple[str, int]:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if resolved.exists():
            raise ValueError(f"file already exists: {relative_path}")

        content: list[str] = []
        while index < len(lines) - 1 and not self._is_patch_operation(lines[index]):
            line = lines[index]
            if not line.startswith("+"):
                raise ValueError(f"add file lines must start with `+`: {line}")
            content.append(line[1:])
            index += 1

        self._snapshot(resolved, backups)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._write_lines(resolved, content)
        return relative_path, index

    def _apply_update_file(
        self,
        path: str,
        lines: list[str],
        index: int,
        backups: dict[Path, str | None],
    ) -> tuple[str, int]:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        current = self._read_lines(resolved, path)
        hunks: list[list[tuple[str, str]]] = []
        current_hunk: list[tuple[str, str]] = []

        while index < len(lines) - 1 and not self._is_patch_operation(lines[index]):
            line = lines[index]
            if line.startswith("@@"):
                if current_hunk:
                    hunks.append(current_hunk)
                    current_hunk = []
            elif line.startswith((" ", "-", "+")):
                current_hunk.append((line[0], line[1:]))
            else:
                raise ValueError(f"update lines must start with space, `-`, `+`, or `@@`: {line}")
            index += 1

        if current_hunk:
            hunks.append(current_hunk)
        if not hunks:
            raise ValueError(f"update patch has no hunks: {relative_path}")

        updated = self._apply_update_hunks(current, hunks, relative_path)
        self._snapshot(resolved, backups)
        self._write_lines(resolved, updated)
        return relative_path, index

    def _apply_delete_file(self, path: str, backups: dict[Path, str | None]) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {relative_path}")
        self._snapshot(resolved, backups)
        resolved.unlink()
        return relative_path

    def _apply_update_hunks(
        self,
        current: list[str],
        hunks: list[list[tuple[str, str]]],
        relative_path: str,
    ) -> list[str]:
        cursor = 0
        for hunk in hunks:
            old_chunk = [text for operation, text in hunk if operation in {" ", "-"}]
            new_chunk = [text for operation, text in hunk if operation in {" ", "+"}]
            if not old_chunk:
                current[cursor:cursor] = new_chunk
                cursor += len(new_chunk)
                continue

            start = self._find_subsequence(current, old_chunk, cursor)
            if start is None:
                raise ValueError(f"patch context not found in {relative_path}: {old_chunk[0]}")
            current[start : start + len(old_chunk)] = new_chunk
            cursor = start + len(new_chunk)
        return current

    def _find_subsequence(self, lines: list[str], target: list[str], start_index: int) -> int | None:
        for index in range(start_index, len(lines) - len(target) + 1):
            if lines[index : index + len(target)] == target:
                return index
        return None

    def _write_lines(self, path: Path, lines: list[str]) -> None:
        text = "\n".join(lines)
        if text:
            text += "\n"
        path.write_text(text, encoding="utf-8")

    def _snapshot(self, path: Path, backups: dict[Path, str | None]) -> None:
        if path in backups:
            return
        backups[path] = path.read_text(encoding="utf-8") if path.exists() else None

    def _restore_backups(self, backups: dict[Path, str | None]) -> None:
        for path, original in backups.items():
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_text(original, encoding="utf-8")

    def _is_patch_operation(self, line: str) -> bool:
        return line.startswith(PATCH_OPERATION_PREFIXES)

    def _allowed_commit_path(self, path: str) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if relative_path == "MEMORY.md":
            return relative_path
        if MEMORY_DETAIL_FILE_RE.fullmatch(relative_path):
            return relative_path
        if DREAM_LOG_FILE_RE.fullmatch(relative_path):
            return relative_path
        raise ValueError(f"can only stage or commit MEMORY.md, MEMORY_*.md, or dream_logs/*.md: {relative_path}")

    def _validate_commit_message(self, message: str) -> str:
        message = message.strip()
        if not message:
            raise ValueError("commit message must not be empty")
        lines = message.splitlines()
        if len(lines) != 1:
            raise ValueError("commit message must be a single line")
        if len(message) > COMMIT_MESSAGE_LINE_LIMIT:
            raise ValueError(f"commit message must be <= {COMMIT_MESSAGE_LINE_LIMIT} characters")
        return message

    def _normalize_glob_pattern(self, pattern: str) -> str:
        raw_path = Path(pattern)
        if raw_path.is_absolute():
            if not self._is_under_root(raw_path):
                raise ValueError(f"glob pattern escapes RightMemory root: {pattern}")
            return raw_path.resolve(strict=False).relative_to(self.memory_root).as_posix()
        if ".." in raw_path.parts:
            raise ValueError("glob pattern must not contain '..'")
        return pattern

    def _resolve_path(self, path: str) -> Path:
        raw_path = Path(path)
        if raw_path.is_absolute():
            resolved = raw_path.resolve(strict=False)
        else:
            resolved = (self.memory_root / raw_path).resolve(strict=False)
        if not self._is_under_root(resolved):
            raise ValueError(f"path escapes RightMemory root: {path}")
        return resolved

    def _is_under_root(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.memory_root)
        except ValueError:
            return False
        return True

    def _run_git(self, command: list[str]) -> str:
        process = subprocess.run(
            command,
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise RuntimeError(f"git command failed: {detail}")
        return process.stdout.strip()

    def _loc(self, item: MemoryId) -> str:
        return f"{item.file.relative_to(self.memory_root)}:{item.line_number}"
