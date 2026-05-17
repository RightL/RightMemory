from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path


FULL_READ_LINE_LIMIT = 200
READ_TOOL_LINE_LIMIT = 2000
COMMAND_OUTPUT_CHAR_LIMIT = 30000
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
MAX_CLOSE_MATCHES = 3
MAX_EDIT_MATCH_LINES = 8
MAX_MATCH_PREVIEW_CHARS = 180

ANCHOR_RE = re.compile(r"^(#{1,4})\s+.*?\{(?:F#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
ANY_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*?(?:\s*→\s*\[(.*?)\])?\s*$")
EDGE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*):\s*([A-Za-z0-9_.-]+)\s*$")
MEMORY_DETAIL_FILE_RE = re.compile(r"^MEMORY_[A-Za-z0-9_.-]+\.md$")
DREAM_LOG_FILE_RE = re.compile(r"^dream_logs/[A-Za-z0-9_.-]+\.md$")
SKILL_ARTIFACT_RE = re.compile(r"^skill_artifacts/[A-Za-z0-9_.-]+/.+")


@dataclass(frozen=True)
class MemoryId:
    id: str
    file: Path
    line_number: int
    edges: tuple[tuple[str, str], ...]
    malformed_edges: tuple[str, ...] = ()


class MemoryTools:
    def __init__(self, memory_root: Path):
        self.memory_root = memory_root.resolve()
        self._read_signatures: dict[Path, str] = {}

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

    def glob(self, pattern: str = "MEMORY*.md", path: str = ".") -> list[str]:
        """Find files under the RightMemory root by glob pattern."""
        pattern = pattern.strip() or "**/*"
        base = self._resolve_path(path)
        if not base.is_dir():
            raise ValueError(f"glob path is not a directory: {path}")
        raw_pattern = Path(pattern)
        if raw_pattern.is_absolute() or ".." in raw_pattern.parts:
            raise ValueError("glob pattern must be relative and must not contain '..'")
        files = [
            item
            for item in base.glob(pattern)
            if item.is_file() and self._is_under_root(item)
        ]
        files.sort(key=lambda item: (-item.stat().st_mtime, item.relative_to(self.memory_root).as_posix()))
        if len(files) > MAX_LIST_FILES:
            raise ValueError(f"pattern matched more than {MAX_LIST_FILES} files; use a narrower pattern")
        return [item.relative_to(self.memory_root).as_posix() for item in files]

    def read(self, path: str, offset: int = 1, limit: int = READ_TOOL_LINE_LIMIT) -> str:
        """Read a line-numbered file range under the RightMemory root."""
        resolved = self._resolve_path(path)
        lines = self._read_lines(resolved, path)
        self._validate_positive("offset", offset)
        self._validate_positive("limit", limit)
        end = min(len(lines), offset + limit - 1)
        if offset > len(lines):
            return f"[empty: offset {offset} exceeds file length {len(lines)}]"
        output = self._format_lines(lines, offset, end)
        if end < len(lines):
            output += f"\n[truncated: showing lines {offset}-{end} of {len(lines)}; use offset/limit for more]"
        return output

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

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str = "MEMORY*.md",
        context_lines: int = 0,
        max_matches: int = 50,
        case_sensitive: bool = True,
    ) -> str:
        """Search file contents with a Python regex and return file:line matches."""
        if not pattern:
            raise ValueError("pattern must not be empty")
        if context_lines < 0:
            raise ValueError("context_lines must be >= 0")
        self._validate_positive("max_matches", max_matches)
        max_matches = min(max_matches, MAX_SEARCH_MATCHES)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc

        blocks: list[str] = []
        for relative_path in self._grep_paths(path, glob):
            resolved = self._resolve_path(relative_path)
            lines = self._read_lines(resolved, relative_path, mark_read=False)
            for index, line in enumerate(lines, start=1):
                if not regex.search(line):
                    continue
                if context_lines:
                    start = max(1, index - context_lines)
                    end = min(len(lines), index + context_lines)
                    blocks.append(f"{relative_path}:{index} match\n" + self._format_lines(lines, start, end))
                else:
                    blocks.append(f"{relative_path}:{index}: {line}")
                if len(blocks) >= max_matches:
                    return "\n\n".join(blocks) + f"\n[truncated: reached max_matches={max_matches}]"
        if not blocks:
            return "no matches"
        return "\n\n".join(blocks)

    def read_command(self, command: str) -> str:
        """Run a restricted read-only shell-like command under the RightMemory root."""
        command = command.strip()
        if not command:
            raise ValueError("command must not be empty")
        self._reject_shell_syntax(command)
        try:
            args = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"invalid command syntax: {exc}") from exc
        if not args:
            raise ValueError("command must not be empty")

        executable = args[0]
        if executable == "cat":
            return self._read_command_cat(args)
        if executable == "sed":
            return self._read_command_sed(args)
        if executable == "rg":
            return self._read_command_rg(args)
        if executable == "git":
            return self._read_command_git(args)
        raise ValueError("unsupported read command; use cat, sed -n, rg, rg --files, git status --short, or git diff")

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

    def edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        """Replace exact text in a file under the RightMemory root."""
        if not old_string:
            raise ValueError("old_string must not be empty")
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {relative_path}")

        text = self._read_text(resolved)
        self._ensure_recent_read(resolved, relative_path, text)
        needle, replacement, count, normalized_newlines = self._replacement_inputs(text, old_string, new_string)
        if count == 0:
            raise ValueError(self._missing_old_string_message(relative_path, text, old_string))
        if old_string == new_string:
            return f"no changes: old_string and new_string are identical in {relative_path}"
        if count > 1 and not replace_all:
            lines = ", ".join(str(line) for line in self._occurrence_line_numbers(text, needle))
            raise ValueError(
                f"old_string matched {count} times in {relative_path} at line(s) {lines}; "
                "provide a larger unique old_string or set replace_all=true"
            )

        updated = text.replace(needle, replacement, -1 if replace_all else 1)
        self._write_text(resolved, updated)
        self._mark_read_text(resolved, updated)
        occurrence_word = "occurrences" if (replace_all and count != 1) else "occurrence"
        suffix = " after normalizing line endings" if normalized_newlines else ""
        return f"edited {relative_path}: replaced {count if replace_all else 1} {occurrence_word}{suffix}"

    def create_file(self, path: str, content: str = "") -> str:
        """Create a new file under the RightMemory root."""
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if resolved.exists():
            raise ValueError(f"file already exists: {relative_path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._write_text(resolved, content)
        self._mark_read_text(resolved, content)
        return f"created {relative_path}"

    def delete_file(self, path: str) -> str:
        """Delete a file under the RightMemory root."""
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {relative_path}")
        resolved.unlink()
        self._read_signatures.pop(resolved, None)
        return f"deleted {relative_path}"

    def rename_file(self, old_path: str, new_path: str) -> str:
        """Rename a file under the RightMemory root without overwriting an existing file."""
        source = self._resolve_path(old_path)
        destination = self._resolve_path(new_path)
        source_relative = source.relative_to(self.memory_root).as_posix()
        destination_relative = destination.relative_to(self.memory_root).as_posix()
        if not source.is_file():
            raise FileNotFoundError(f"file not found: {source_relative}")
        if destination.exists():
            raise ValueError(f"destination already exists: {destination_relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        signature = self._read_signatures.pop(source, None)
        if signature is not None:
            self._read_signatures[destination] = signature
        return f"renamed {source_relative} to {destination_relative}"

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
        """Stage selected memory, dream log, or skill artifact files under the RightMemory root."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = [self._allowed_commit_path(path) for path in paths]
        self._run_git(["git", "add", "--", *relative_paths])
        return "staged: " + ", ".join(relative_paths)

    def git_commit(self, message: str, body: str | None = None) -> str:
        """Commit staged memory, dream log, and skill artifact files under the RightMemory root."""
        message = self._validate_commit_subject(message)
        body = self._validate_commit_body(body)
        staged = self._run_git(["git", "diff", "--cached", "--name-only", "--"])
        staged_files = [line for line in staged.splitlines() if line]
        if not staged_files:
            raise ValueError("no staged changes to commit")
        for path in staged_files:
            self._allowed_commit_path(path)

        command = ["git", "commit", "-m", message]
        if body is not None:
            command.extend(["-m", body])
        self._run_git(command)
        commit_hash = self._run_git(["git", "rev-parse", "--short", "HEAD"])
        status = self.git_status()
        if status:
            return f"committed {commit_hash}: {message}\n{status}"
        return f"committed {commit_hash}: {message}"

    def git_discard(self, paths: list[str]) -> str:
        """Discard selected memory, dream log, or skill artifact file changes."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = [self._allowed_commit_path(path) for path in paths]

        if self._git_has_head():
            tracked_paths = [
                path for path in relative_paths if self._git_path_exists_in_head(path)
            ]
            self._run_git(["git", "reset", "--", *relative_paths])
            if tracked_paths:
                self._run_git(["git", "checkout", "--", *tracked_paths])
        else:
            self._run_git(["git", "rm", "--cached", "--ignore-unmatch", "--", *relative_paths])
            tracked_paths = []

        for path in relative_paths:
            if path not in tracked_paths:
                self._unlink_worktree_file(path)
        return "discarded: " + ", ".join(relative_paths)

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
            for malformed_edge in item.malformed_edges:
                errors.append(f"malformed edge `{malformed_edge}` at {self._loc(item)}")
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

        errors.extend(self._structure_errors(files))
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

    def _read_lines(self, resolved: Path, original_path: str, mark_read: bool = True) -> list[str]:
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {original_path}")
        text = self._read_text(resolved)
        if mark_read:
            self._mark_read_text(resolved, text)
        return text.splitlines()

    def _read_text(self, path: Path) -> str:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def _write_text(self, path: Path, text: str) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _grep_paths(self, path: str | None, glob_pattern: str) -> list[str]:
        if path is None:
            return self.list_files(glob_pattern)
        resolved = self._resolve_path(path)
        if resolved.is_file():
            return [resolved.relative_to(self.memory_root).as_posix()]
        if resolved.is_dir():
            relative_base = resolved.relative_to(self.memory_root).as_posix()
            if relative_base == ".":
                return self.list_files(glob_pattern)
            return self.glob(glob_pattern, relative_base)
        raise FileNotFoundError(f"path not found: {path}")

    def _reject_shell_syntax(self, command: str) -> None:
        forbidden = ["|", ">", "<", ";", "&&", "||", "$(", "`", "\n"]
        for token in forbidden:
            if token in command:
                raise ValueError("read_command does not support shell operators, pipes, redirects, or substitutions")

    def _read_command_cat(self, args: list[str]) -> str:
        if len(args) != 2:
            raise ValueError("cat command must read exactly one file")
        return self._read_raw_file(args[1])

    def _read_command_sed(self, args: list[str]) -> str:
        if len(args) != 4 or args[1] != "-n":
            raise ValueError("supported sed form is: sed -n 'START,ENDp' path")
        match = re.fullmatch(r"(\d+)(?:,(\d+))?p", args[2])
        if match is None:
            raise ValueError("supported sed range is 'START,ENDp' or 'LINEp'")
        start = int(match.group(1))
        end = int(match.group(2) or match.group(1))
        if end < start:
            raise ValueError("sed end line must be >= start line")
        return self._read_raw_file(args[3], start, end)

    def _read_command_rg(self, args: list[str]) -> str:
        self._validate_read_command_paths(args[1:])
        if "--files" not in args and "--with-filename" not in args and "--no-filename" not in args:
            args = [args[0], "--with-filename", *args[1:]]
        process = subprocess.run(
            args,
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode not in {0, 1}:
            detail = process.stderr.strip() or process.stdout.strip()
            raise RuntimeError(f"rg command failed: {detail}")
        output = process.stdout.strip()
        return self._cap_command_output(output) if output else "no matches"

    def _read_command_git(self, args: list[str]) -> str:
        if args == ["git", "status", "--short"]:
            return self.git_status()
        if len(args) >= 2 and args[1] == "diff":
            self._validate_git_diff_command(args)
            return self._run_git(args)
        raise ValueError("supported git read commands are: git status --short, git diff")

    def _read_raw_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {relative_path}")
        text = self._read_text(resolved)
        self._mark_read_text(resolved, text)
        if start_line is None:
            return self._cap_command_output(text)
        self._validate_positive("start_line", start_line)
        self._validate_positive("end_line", end_line or start_line)
        lines = text.splitlines(keepends=True)
        selected = "".join(lines[start_line - 1 : end_line])
        return self._cap_command_output(selected)

    def _cap_command_output(self, output: str) -> str:
        if len(output) <= COMMAND_OUTPUT_CHAR_LIMIT:
            return output
        return (
            output[:COMMAND_OUTPUT_CHAR_LIMIT]
            + f"\n[truncated: output exceeded {COMMAND_OUTPUT_CHAR_LIMIT} characters]"
        )

    def _validate_read_command_paths(self, args: list[str]) -> None:
        option_takes_value = {"-g", "--glob", "--type", "-t"}
        skip_next = False
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token in option_takes_value:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            if token.startswith("~"):
                raise ValueError("read command paths must stay under the RightMemory root")
            raw = Path(token)
            if raw.is_absolute() or ".." in raw.parts:
                resolved = self._resolve_path(token)
                if not self._is_under_root(resolved):
                    raise ValueError("read command paths must stay under the RightMemory root")

    def _validate_git_diff_command(self, args: list[str]) -> None:
        if len(args) == 2:
            return
        if len(args) < 4 or args[2] != "--":
            raise ValueError("supported git diff form is: git diff or git diff -- <paths>")
        separator = 2
        for path in args[separator + 1 :]:
            self._resolve_path(path)

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
            edges, malformed_edges = self._parse_edges(edge_text or "")
            items.append(
                MemoryId(
                    id=item_id,
                    file=file_path,
                    line_number=line_number,
                    edges=tuple(edges),
                    malformed_edges=tuple(malformed_edges),
                )
            )
        return items

    def _parse_edges(self, edge_text: str) -> tuple[list[tuple[str, str]], list[str]]:
        edges: list[tuple[str, str]] = []
        malformed_edges: list[str] = []
        for raw_edge in edge_text.split(","):
            raw_edge = raw_edge.strip()
            if not raw_edge:
                continue
            match = EDGE_RE.match(raw_edge)
            if match is None:
                malformed_edges.append(raw_edge)
                continue
            edges.append((match.group(1), match.group(2)))
        return edges, malformed_edges

    def _structure_errors(self, files: list[Path]) -> list[str]:
        errors: list[str] = []
        for path in files:
            heading_stack: list[tuple[int, int]] = []
            active_pointer: tuple[int, int] | None = None
            relative_path = path.relative_to(self.memory_root)
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                heading_match = ANY_HEADING_RE.match(line)
                if heading_match is None:
                    if active_pointer is not None and NODE_RE.match(line):
                        pointer_line = active_pointer[1]
                        errors.append(
                            f"#### pointer cannot contain child node at {relative_path}:{line_number} "
                            f"(pointer starts at line {pointer_line})"
                        )
                    continue

                depth = len(heading_match.group(1))
                if active_pointer is not None and depth > active_pointer[0]:
                    pointer_line = active_pointer[1]
                    errors.append(
                        f"#### pointer cannot contain child heading at {relative_path}:{line_number} "
                        f"(pointer starts at line {pointer_line})"
                    )
                while heading_stack and heading_stack[-1][0] >= depth:
                    heading_stack.pop()
                parent_depth = heading_stack[-1][0] if heading_stack else None
                if active_pointer is not None and depth <= active_pointer[0]:
                    active_pointer = None

                if depth > 4:
                    errors.append(f"heading deeper than #### at {relative_path}:{line_number}")
                elif depth == 4:
                    anchor_match = ANCHOR_KIND_RE.match(line)
                    if anchor_match is None or anchor_match.group(2) != "F#":
                        errors.append(f"#### pointer must use `{{F#slug}}` at {relative_path}:{line_number}")
                    if parent_depth != 3:
                        errors.append(f"#### pointer must be under a ### heading at {relative_path}:{line_number}")
                    if anchor_match is not None and anchor_match.group(2) == "F#":
                        active_pointer = (depth, line_number)

                heading_stack.append((depth, line_number))
        return errors

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

    def _replacement_inputs(self, text: str, old_string: str, new_string: str) -> tuple[str, str, int, bool]:
        count = text.count(old_string)
        if count:
            return old_string, new_string, count, False

        newline_old, newline_new = self._line_ending_variant(text, old_string, new_string)
        if newline_old == old_string:
            return old_string, new_string, 0, False
        return newline_old, newline_new, text.count(newline_old), True

    def _line_ending_variant(self, text: str, old_string: str, new_string: str) -> tuple[str, str]:
        if "\r\n" in text and "\r\n" not in old_string and "\n" in old_string:
            return self._to_crlf(old_string), self._to_crlf(new_string)
        if "\r\n" not in text and "\r\n" in old_string:
            return self._to_lf(old_string), self._to_lf(new_string)
        return old_string, new_string

    def _to_crlf(self, value: str) -> str:
        return self._to_lf(value).replace("\n", "\r\n")

    def _to_lf(self, value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    def _ensure_recent_read(self, resolved: Path, relative_path: str, text: str) -> None:
        signature = self._read_signatures.get(resolved)
        if signature is None:
            raise ValueError(f"read {relative_path} with read, cat, or sed -n before editing it")
        if signature != self._text_signature(text):
            raise ValueError(f"{relative_path} changed since the last read; read it again before editing")

    def _mark_read_text(self, resolved: Path, text: str) -> None:
        self._read_signatures[resolved] = self._text_signature(text)

    def _text_signature(self, text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()

    def _missing_old_string_message(self, relative_path: str, text: str, old_string: str) -> str:
        matches = self._closest_matches(text, old_string)
        if not matches:
            return f"old_string not found in {relative_path}; file is empty"
        details = "\n".join(
            f"- line {line_number}, similarity {score:.2f}: {self._preview(candidate)}"
            for line_number, score, candidate in matches
        )
        return f"old_string not found in {relative_path}; closest inspected text:\n{details}"

    def _closest_matches(self, text: str, old_string: str) -> list[tuple[int, float, str]]:
        lines = text.splitlines()
        if not lines:
            return []
        target_line_count = max(1, min(MAX_EDIT_MATCH_LINES, len(old_string.splitlines()) or 1))
        window_sizes = sorted(
            {
                max(1, target_line_count - 1),
                target_line_count,
                min(len(lines), target_line_count + 1),
            }
        )
        ranked: list[tuple[int, float, str]] = []
        seen: set[str] = set()
        for window_size in window_sizes:
            for start in range(0, len(lines) - window_size + 1):
                candidate = "\n".join(lines[start : start + window_size])
                if candidate in seen:
                    continue
                seen.add(candidate)
                score = SequenceMatcher(None, old_string, candidate).ratio()
                ranked.append((start + 1, score, candidate))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:MAX_CLOSE_MATCHES]

    def _preview(self, value: str) -> str:
        preview = value.replace("\n", "\\n")
        if len(preview) > MAX_MATCH_PREVIEW_CHARS:
            return preview[: MAX_MATCH_PREVIEW_CHARS - 3] + "..."
        return preview

    def _occurrence_line_numbers(self, text: str, needle: str, limit: int = 5) -> list[int]:
        line_numbers: list[int] = []
        offset = 0
        step = max(1, len(needle))
        while len(line_numbers) < limit:
            index = text.find(needle, offset)
            if index == -1:
                break
            line_numbers.append(text.count("\n", 0, index) + 1)
            offset = index + step
        return line_numbers

    def _allowed_commit_path(self, path: str) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if relative_path == "MEMORY.md":
            return relative_path
        if MEMORY_DETAIL_FILE_RE.fullmatch(relative_path):
            return relative_path
        if DREAM_LOG_FILE_RE.fullmatch(relative_path):
            return relative_path
        if SKILL_ARTIFACT_RE.fullmatch(relative_path):
            return relative_path
        raise ValueError(
            "can only stage, commit, or discard MEMORY.md, MEMORY_*.md, dream_logs/*.md, "
            f"or skill_artifacts/<slug>/...: {relative_path}"
        )

    def _validate_commit_subject(self, message: str) -> str:
        message = message.strip()
        if not message:
            raise ValueError("commit message must not be empty")
        lines = message.splitlines()
        if len(lines) != 1:
            raise ValueError("commit subject must be a single line")
        if len(message) > COMMIT_MESSAGE_LINE_LIMIT:
            raise ValueError(f"commit subject must be <= {COMMIT_MESSAGE_LINE_LIMIT} characters")
        return message

    def _validate_commit_body(self, body: str | None) -> str | None:
        if body is None:
            return None
        body = body.strip()
        if not body:
            return None
        if "\x00" in body:
            raise ValueError("commit body must not contain NUL bytes")
        return body

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

    def _git_has_head(self) -> bool:
        process = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process.returncode == 0

    def _git_path_exists_in_head(self, path: str) -> bool:
        process = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{path}"],
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process.returncode == 0

    def _unlink_worktree_file(self, path: str) -> None:
        resolved = self.memory_root / path
        if resolved.is_file() or resolved.is_symlink():
            resolved.unlink()
        elif resolved.exists():
            raise RuntimeError(f"cannot discard directory path: {path}")

    def _loc(self, item: MemoryId) -> str:
        return f"{item.file.relative_to(self.memory_root)}:{item.line_number}"
