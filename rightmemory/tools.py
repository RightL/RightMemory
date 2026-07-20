from __future__ import annotations

import re
import shlex
import subprocess
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from .graph import (
    PURSUIT_ACTION_RE,
    PURSUIT_DETAIL_FILE_RE,
    build_graph_manifest,
    resolve_backing_reference,
)
from .share_models import ShareFilePart, ShareQuestionPart, ShareRelationship, load_shares, save_shares, validate_share_id
from .share_results import normalize_share_capability
from .shared_view_files import (
    render_file_view,
    validate_file_view_recipe_source,
    write_extractive_file_view_recipe,
    write_generative_file_view,
)
from .shared_view_models import validate_heading_id
from .shared_view_questions import validate_question_view_source, write_question_view
from .update_review import validate_corrections_markdown


FULL_READ_LINE_LIMIT = 200
READ_TOOL_LINE_LIMIT = 2000
COMMAND_OUTPUT_CHAR_LIMIT = 30000
MAX_LIST_FILES = 500
MAX_SEARCH_MATCHES = 200
MAX_OUTLINE_ITEMS = 500
COMMIT_MESSAGE_LINE_LIMIT = 120
MAX_CLOSE_MATCHES = 3
MAX_EDIT_MATCH_LINES = 8
MAX_MATCH_PREVIEW_CHARS = 180

ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|M#|S#|MF#|MQ#|#)([A-Za-z0-9_.-]+)\}(?:\s*(?:\u2192|->)\s*\[(.*?)\])?")
UNSUPPORTED_ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{([A-Za-z]+#)([A-Za-z0-9_.-]+)\}")
TERMINAL_HEADING_KINDS = {"F#", "M#", "S#", "MF#", "MQ#"}
ANY_HEADING_RE = re.compile(r"^(#+)\s+(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
NODE_RE = re.compile(r"^\s*-\s+`([^`]+)`.*?(?:\s*(?:\u2192|->)\s*\[(.*?)\])?\s*$")
MEMORY_DETAIL_FILE_RE = re.compile(r"^MEMORY_[A-Za-z0-9_.-]+\.md$")
MEMORY_SKILL_FILE_RE = re.compile(r"^MEMORY_SKILL_[A-Za-z0-9_.-]+\.md$")
INSIGHT_LOG_FILE_RE = re.compile(r"^insight_logs/[A-Za-z0-9_.-]+\.md$")
SHARED_VIEW_REGISTRY_PATH = "shared_views.toml"
SHARE_REGISTRY_PATH = "shares.toml"
SHARED_VIEW_DEFINITION_FILE_RE = re.compile(
    r"^shared_views/[A-Za-z0-9_.-]+/(?:view\.md|retriever\.md|recipe\.toml|question\.toml|\.gitignore)$"
)
RUNTIME_SHARED_VIEW_PATH_PREFIX = ".runtime/shared_views/"
RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX = ".runtime/shared_views/imports/"
GIT_REVISION_RE = re.compile(r"^[A-Za-z0-9_.^~/-]+$")
PRUNE_SUBJECT_PREFIX = "prune:"
ACTIVE_MEMORY_ROLES = {"dreamer", "pruner", "reviewer", "sync-reconciler", "update"}
FULL_RIGHTMEMORY_WRITE_ROLES = {"update", "update-correction"}
CORRECTION_WRITE_ROLES = {"update-correction"}
CORRECTIONS_READ_ROLES = {"update", "update-correction", "sync-reconciler"}
INSIGHT_ROLES = {"insight"}
RETRIEVE_ROLES = {"retrieve"}
SYNC_RECONCILER_ROLES = {"sync-reconciler"}
SHARED_VIEW_BUILDER_ROLES = {"shared-view-builder"}
INSIGHT_READ_PATHS = ("MEMORY.md", "MEMORY_*.md", "insight_logs/*.md")
RETRIEVE_READ_PATHS = ("MEMORY.md", "MEMORY_*.md", "PURSUITS.md", "PURSUIT_*.md")
PURSUIT_RULES_PATH = "PURSUIT_RULES.md"
CORRECTIONS_PATH = "corrections.md"
FIXED_CORRECTION_COLLECTION_PATHS = {
    "MEMORY_agent-corrections-design.md",
    "MEMORY_agent-corrections-writing.md",
}
CORRECTIONS_CAPACITY_ERROR_RE = re.compile(
    r"^corrections\.md contains \d+ entries; at most 15 are allowed$"
)


class MemoryTools:
    def __init__(self, memory_root: Path, role: str | None = None):
        self.memory_root = memory_root.resolve()
        self.role = role
        self._read_signatures: dict[Path, str] = {}

    def list_files(self, pattern: str = "*.md") -> list[str]:
        """List files under the RightMemory root that match a glob pattern."""
        pattern = pattern.strip() or "*.md"
        pattern = self._normalize_glob_pattern(pattern)
        paths = [
            path.relative_to(self.memory_root).as_posix()
            for path in self.memory_root.glob(pattern)
            if path.is_file() and self._is_under_root(path) and self._is_allowed_read_file(path)
        ]
        paths.sort()
        if len(paths) > MAX_LIST_FILES:
            raise ValueError(f"pattern matched more than {MAX_LIST_FILES} files; use a narrower pattern")
        return paths

    def glob(self, pattern: str = "*.md", path: str = ".") -> list[str]:
        """Find files under the RightMemory root by glob pattern."""
        pattern = pattern.strip() or "**/*"
        base = self._resolve_path(path)
        if not base.is_dir():
            raise ValueError(f"glob path is not a directory: {path}")
        self._check_allowed_read_search_dir(base, path)
        raw_pattern = Path(pattern)
        if raw_pattern.is_absolute() or ".." in raw_pattern.parts:
            raise ValueError("glob pattern must be relative and must not contain '..'")
        files = [
            item
            for item in base.glob(pattern)
            if item.is_file() and self._is_under_root(item) and self._is_allowed_read_file(item)
        ]
        files.sort(key=lambda item: (-item.stat().st_mtime, item.relative_to(self.memory_root).as_posix()))
        if len(files) > MAX_LIST_FILES:
            raise ValueError(f"pattern matched more than {MAX_LIST_FILES} files; use a narrower pattern")
        return [item.relative_to(self.memory_root).as_posix() for item in files]

    def read(self, path: str, offset: int = 1, limit: int = READ_TOOL_LINE_LIMIT) -> str:
        """Read a line-numbered file range under the RightMemory root."""
        resolved = self._allowed_read_path(path)
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

    def read_skill(self, skill_id: str) -> str:
        """Read a complete MEMORY_SKILL body by S# id."""
        clean_id = self._validate_memory_reference_id(skill_id)
        relative = f"MEMORY_SKILL_{clean_id}.md"
        path = self.memory_root / relative
        if not self._is_safe_read_file(path):
            return self._missing_skill_message(clean_id)
        return self._read_text(path)

    def read_mf(self, mf_id: str) -> str:
        """Read a mirrored view's complete line-numbered canonical dist/MEMORY.md."""
        clean_id = self._validate_memory_reference_id(mf_id)
        path = (
            self.memory_root
            / RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX
            / clean_id
            / "dist"
            / "MEMORY.md"
        )
        if not self._is_safe_read_file(path):
            return self._missing_mf_message(clean_id)
        return self._read_numbered_source(path, f"MF#{clean_id}")

    def read_detail(self, detail_id: str) -> str:
        """Read the root-relative graph detail file for an F# heading id."""
        clean_id = self._validate_memory_reference_id(detail_id)
        reference = resolve_backing_reference(self.memory_root, clean_id, "F#")
        if reference is None or not self._is_safe_read_file(reference.path):
            return self._missing_typed_backing_message("F# detail", clean_id, "F#")
        relative = reference.path.relative_to(self.memory_root).as_posix()
        text = self._read_text(reference.path).rstrip()
        return f"===== {relative} =====\n{text}"

    def read_markdown(self, markdown_id: str) -> str:
        """Read complete line-numbered free-form Markdown for an M# heading id."""
        clean_id = self._validate_memory_reference_id(markdown_id)
        reference = resolve_backing_reference(self.memory_root, clean_id, "M#")
        if reference is None or not self._is_safe_read_file(reference.path):
            return self._missing_typed_backing_message("M# Markdown", clean_id, "M#")
        return self._read_numbered_source(reference.path, f"M#{clean_id}")

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_lines: int = FULL_READ_LINE_LIMIT,
    ) -> str:
        """Read a file under the RightMemory root, optionally by inclusive line range."""
        resolved = self._allowed_read_path(path)
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
        resolved = self._allowed_read_path(path)
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
        pattern: str = "*.md",
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
        glob: str = "*.md",
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
        resolved = self._allowed_read_path(path)
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
        relative_path = self._allowed_write_path(path)
        resolved = self.memory_root / relative_path
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
        relative_path = self._allowed_write_path(path)
        resolved = self.memory_root / relative_path
        if resolved.exists():
            raise ValueError(f"file already exists: {relative_path}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._write_text(resolved, content)
        self._mark_read_text(resolved, content)
        return f"created {relative_path}"

    def delete_file(self, path: str) -> str:
        """Delete a file under the RightMemory root."""
        relative_path = self._allowed_write_path(path)
        resolved = self.memory_root / relative_path
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {relative_path}")
        resolved.unlink()
        self._read_signatures.pop(resolved, None)
        return f"deleted {relative_path}"

    def rename_file(self, old_path: str, new_path: str) -> str:
        """Rename a file under the RightMemory root without overwriting an existing file."""
        source_relative = self._allowed_write_path(old_path)
        destination_relative = self._allowed_write_path(new_path)
        source = self.memory_root / source_relative
        destination = self.memory_root / destination_relative
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

    def create_extractive_file_view(
        self,
        view_id: str,
        title: str,
        intent: str,
        include_headings: list[str] | None = None,
        include_nodes: list[str] | None = None,
        include_files: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        publish_hub_url: str | None = None,
        publish_credential_id: str | None = None,
    ) -> str:
        """Create and render a canonical extractive MF# file view, or return actionable validation failures."""
        self._require_shared_view_builder_tool()
        include_headings = include_headings or []
        include_nodes = include_nodes or []
        include_files = include_files or []
        exclude_ids = exclude_ids or []
        errors = self._shared_view_selection_errors(
            include_headings=include_headings,
            include_nodes=include_nodes,
            include_files=include_files,
            exclude_ids=exclude_ids,
        )
        if not (include_headings or include_nodes or include_files):
            errors.append("select at least one include_headings, include_nodes, or include_files entry")
        if errors:
            return "failed: " + "; ".join(errors)
        try:
            write_extractive_file_view_recipe(
                self.memory_root,
                view_id=view_id,
                title=title,
                intent=intent,
                include_headings=include_headings,
                include_nodes=include_nodes,
                include_files=include_files,
                exclude_ids=exclude_ids,
                approved=False,
                publish_hub_url=publish_hub_url,
                publish_credential_id=publish_credential_id,
            )
            recipe = validate_file_view_recipe_source(
                self.memory_root,
                view_id,
                require_selection=True,
                require_publish=bool(publish_hub_url or publish_credential_id),
            )
            render_file_view(self.memory_root, recipe.view_id)
            rendered = self.memory_root / "shared_views" / recipe.view_id / "dist" / "MEMORY.md"
            if not self._file_view_rendered_context(rendered):
                return "failed: selected ids rendered an empty Published Context; choose ids that match memory content"
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return (
            f"success: wrote extractive file view {recipe.view_id} with "
            f"{len(recipe.include_headings)} heading(s), {len(recipe.include_nodes)} node(s), "
            f"{len(recipe.include_files)} file(s), and {len(recipe.exclude_ids)} excluded id(s)"
        )

    def create_generative_file_view(
        self,
        view_id: str,
        title: str,
        intent: str,
        published_context: str,
        publish_hub_url: str | None = None,
        publish_credential_id: str | None = None,
    ) -> str:
        """Create and render a canonical generative MF# file view."""
        self._require_shared_view_builder_tool()
        if not str(published_context).strip():
            return "failed: published_context must not be empty"
        try:
            write_generative_file_view(
                self.memory_root,
                view_id=view_id,
                title=title,
                intent=intent,
                published_context=published_context,
                approved=False,
                publish_hub_url=publish_hub_url,
                publish_credential_id=publish_credential_id,
            )
            recipe = validate_file_view_recipe_source(
                self.memory_root,
                view_id,
                require_selection=False,
                require_publish=bool(publish_hub_url or publish_credential_id),
            )
            rendered = self.memory_root / "shared_views" / recipe.view_id / "dist" / "MEMORY.md"
            if not self._file_view_rendered_context(rendered):
                return "failed: published_context rendered an empty Published Context"
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return f"success: wrote generative file view {recipe.view_id}"

    def create_question_view(
        self,
        view_id: str,
        title: str,
        intent: str,
        retriever_instructions: str,
        start_timeout_seconds: int = 10,
        answer_timeout_seconds: int = 180,
    ) -> str:
        """Create canonical MQ# question-view source files, or return actionable validation failures."""
        self._require_shared_view_builder_tool()
        try:
            write_question_view(
                self.memory_root,
                view_id=view_id,
                title=title,
                intent=intent,
                retriever_instructions=retriever_instructions,
                approved=False,
                start_timeout_seconds=start_timeout_seconds,
                answer_timeout_seconds=answer_timeout_seconds,
            )
            config = validate_question_view_source(self.memory_root, view_id)
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return (
            f"success: wrote canonical question view {config.view_id} with "
            f"start timeout {config.start_timeout_seconds}s and answer timeout {config.answer_timeout_seconds}s"
        )

    def create_or_update_share_relationship(
        self,
        share_id: str,
        title: str,
        provider_id: str,
        hub_url: str | None = None,
        credential_id: str | None = None,
        capability: str = "auto",
        file_view_id: str | None = None,
        file_intent: str | None = None,
        question_view_id: str | None = None,
        question_intent: str | None = None,
        question_base_url: str | None = None,
        git_url: str | None = None,
        git_branch: str | None = None,
    ) -> str:
        """Create canonical provider share registry data, or return actionable validation failures."""
        self._require_shared_view_builder_tool()
        try:
            clean_share_id = validate_share_id(share_id)
            clean_git_url = str(git_url).strip() if git_url else None
            clean_capability = "file_context" if clean_git_url else normalize_share_capability(capability)
            if clean_capability == "auto":
                clean_capability = _capability_from_selected_views(file_view_id, question_view_id)
            if clean_git_url and clean_capability != "file_context":
                return "failed: Git transport supports file_context capability only"
            if clean_git_url and (question_view_id or question_intent or question_base_url):
                return "failed: Git transport supports file_context capability only"
            parts: list[str] = []
            file_part = None
            question_part = None
            if clean_capability in {"file_context", "both"}:
                if not file_view_id or not file_intent:
                    return "failed: file_context capability requires file_view_id and file_intent"
                try:
                    validate_file_view_recipe_source(self.memory_root, file_view_id, require_selection=False)
                except (OSError, ValueError) as exc:
                    return f"failed: file view source is invalid: {exc}"
                parts.append("file")
                file_part = ShareFilePart(
                    view_id=validate_heading_id(file_view_id),
                    intent=str(file_intent).strip(),
                    approved=False,
                )
            if clean_capability in {"live_questions", "both"}:
                if not question_view_id or not question_intent or not question_base_url:
                    return "failed: live_questions capability requires question_view_id, question_intent, and question_base_url"
                try:
                    validate_question_view_source(self.memory_root, question_view_id)
                except (OSError, ValueError) as exc:
                    return f"failed: question view source is invalid: {exc}"
                parts.append("question")
                question_part = ShareQuestionPart(
                    view_id=validate_heading_id(question_view_id),
                    intent=str(question_intent).strip(),
                    question_base_url=str(question_base_url).strip(),
                    approved=False,
                )
            if not parts:
                return "failed: share capability selected no shareable capability"
            shares = load_shares(self.memory_root)
            if clean_git_url:
                clean_hub_url = None
                clean_credential_id = None
            else:
                if not hub_url or not str(hub_url).strip():
                    return "failed: HTTP share relationship requires hub_url"
                if not credential_id or not str(credential_id).strip():
                    return "failed: HTTP share relationship requires credential_id"
                clean_hub_url = str(hub_url).strip().rstrip("/")
                clean_credential_id = validate_heading_id(credential_id)
            shares[clean_share_id] = ShareRelationship(
                share_id=clean_share_id,
                role="provider",
                title=str(title).strip(),
                provider_id=validate_heading_id(provider_id),
                hub_url=clean_hub_url,
                credential_id=clean_credential_id,
                transport="git" if clean_git_url else "http",
                git_url=clean_git_url,
                git_branch=str(git_branch).strip() if git_branch and str(git_branch).strip() else None,
                state="draft",
                parts=tuple(parts),
                file=file_part,
                question=question_part,
            )
            save_shares(self.memory_root, shares)
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return f"success: wrote share relationship {clean_share_id} with capability {clean_capability}"

    def git_status(self) -> str:
        """Return short git status for the RightMemory root."""
        if self._has_role_read_scope():
            return self._run_git(["git", "status", "--short", "--", *self._role_read_patterns()])
        return self._run_git(["git", "status", "--short"])

    def git_diff(self, paths: list[str] | None = None) -> str:
        """Return git diff for selected paths, or the whole RightMemory root."""
        command = ["git", "diff"]
        if paths:
            command.append("--")
            for path in paths:
                resolved = self._allowed_read_path(path)
                command.append(resolved.relative_to(self.memory_root).as_posix())
        elif self._has_role_read_scope():
            command.extend(["--", *self._role_read_patterns()])
        return self._run_git(command)

    def git_log(self, grep: str = r"^prune:", max_count: int = 20) -> str:
        """Return recent git commits matching a message pattern."""
        grep = grep.strip()
        if not grep:
            raise ValueError("grep must not be empty")
        if "\x00" in grep or "\n" in grep:
            raise ValueError("grep must not contain NUL bytes or newlines")
        if isinstance(max_count, bool) or max_count < 1:
            raise ValueError("max_count must be a positive integer")
        max_count = min(max_count, 200)
        if grep == r"^prune:":
            commits = self._prune_subject_commits(max_count)
            if not commits:
                return "no matching commits"
            return "\n".join(
                self._run_git(
                    [
                        "git",
                        "log",
                        "--max-count=1",
                        "--format=commit %H%nsubject %s%n%B%n---",
                        commit_hash,
                    ]
                )
                for commit_hash in commits
            )
        output = self._run_git(
            [
                "git",
                "log",
                f"--max-count={max_count}",
                "--extended-regexp",
                f"--grep={grep}",
                "--format=commit %H%nsubject %s%n%B%n---",
            ]
        )
        return output or "no matching commits"

    def git_show_file(self, revision: str, path: str, max_lines: int = FULL_READ_LINE_LIMIT) -> str:
        """Read a memory file as it existed at a git revision."""
        revision = self._validate_git_revision(revision)
        relative_path = self._allowed_history_path(path)
        if isinstance(max_lines, bool) or max_lines < 1:
            raise ValueError("max_lines must be a positive integer")
        object_type = self._git_path_type_at_revision(revision, relative_path)
        if object_type is None:
            return f"[file not present at revision: {revision}:{relative_path}]"
        if object_type != "blob":
            raise ValueError(f"historical path is not a file at revision: {revision}:{relative_path}")
        text = self._run_git(["git", "show", f"{revision}:{relative_path}"])
        lines = text.splitlines()
        end = min(len(lines), max_lines)
        if not lines:
            return "[empty file]"
        output = self._format_lines(lines, 1, end)
        if end < len(lines):
            output += f"\n[truncated: showing lines 1-{end} of {len(lines)}; raise max_lines for more]"
        return output

    def git_add(self, paths: list[str]) -> str:
        """Stage selected role-owned files under the RightMemory root."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = []
        has_head = self._git_has_head()
        for path in paths:
            relative_path = self._allowed_commit_path(path)
            head_path_type = self._git_path_type_in_head(relative_path) if has_head else None
            index_path_kind = self._git_path_kind_in_index(relative_path)
            self._reject_stage_directory_path(
                relative_path,
                head_path_type,
                index_path_kind,
            )
            relative_paths.append(relative_path)
        self._run_git(["git", "add", "--", *relative_paths])
        return "staged: " + ", ".join(relative_paths)

    def git_commit(self, message: str, body: str | None = None, allow_empty: bool = False) -> str:
        """Commit staged role-owned files under the RightMemory root."""
        message = self._validate_commit_subject(message)
        body = self._validate_commit_body(body)
        if allow_empty and message != "prune: checkpoint":
            raise ValueError("empty commits are limited to prune: checkpoint")
        staged = self._run_git(["git", "diff", "--cached", "--name-only", "--no-renames", "--"])
        staged_files = [line for line in staged.splitlines() if line]
        if not staged_files and not allow_empty:
            raise ValueError("no staged changes to commit")
        for path in staged_files:
            self._allowed_commit_path(path)

        command = ["git", "commit", "-m", message]
        if allow_empty:
            command.insert(2, "--allow-empty")
        if body is not None:
            command.extend(["-m", body])
        self._run_git(command)
        commit_hash = self._run_git(["git", "rev-parse", "--short", "HEAD"])
        status = self.git_status()
        if status:
            return f"committed {commit_hash}: {message}\n{status}"
        return f"committed {commit_hash}: {message}"

    def git_discard(self, paths: list[str]) -> str:
        """Discard selected role-owned file changes."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = [self._allowed_commit_path(path) for path in paths]
        has_head = self._git_has_head()
        head_path_types: dict[str, str | None] = {}
        if has_head:
            head_path_types = {
                path: self._git_path_type_in_head(path) for path in relative_paths
            }
        index_path_kinds = {
            path: self._git_path_kind_in_index(path) for path in relative_paths
        }
        for path in relative_paths:
            self._reject_discard_directory_path(
                path,
                head_path_types.get(path),
                index_path_kinds.get(path),
            )
            if (
                head_path_types.get(path) is not None
                and index_path_kinds.get(path) is None
                and self._worktree_file_exists(path)
            ):
                raise ValueError(
                    "cannot discard staged deletion with replacement; "
                    "preserve, move, or copy the replacement elsewhere, "
                    f"or commit it separately before discarding: {path}"
                )
            if head_path_types.get(path) is None and index_path_kinds.get(path) is None:
                raise ValueError(
                    "cannot discard untracked path with git_discard; "
                    f"use delete_file for plain untracked cleanup: {path}"
                )

        if has_head:
            tracked_paths = [
                path for path in relative_paths if head_path_types.get(path) is not None
            ]
            self._run_git(["git", "reset", "--", *relative_paths])
            if tracked_paths:
                self._run_git(["git", "checkout", "--", *tracked_paths])
        else:
            self._run_git(["git", "rm", "-f", "--cached", "--ignore-unmatch", "--", *relative_paths])
            tracked_paths = []

        for path in relative_paths:
            if path not in tracked_paths and index_path_kinds.get(path) is not None:
                self._unlink_worktree_file(path)
        return "discarded: " + ", ".join(relative_paths)

    def validate_memory(self, *, enforce_correction_capacity: bool = True) -> str:
        """Validate the complete RightMemory graph and updater correction file."""
        manifest = build_graph_manifest(self.memory_root)
        errors = [*manifest.errors, *self._structure_errors(manifest.graph_files)]
        corrections_path = self.memory_root / CORRECTIONS_PATH
        if corrections_path.is_file():
            correction_errors = validate_corrections_markdown(corrections_path.read_text(encoding="utf-8"))
            if not enforce_correction_capacity:
                correction_errors = [
                    error for error in correction_errors if not CORRECTIONS_CAPACITY_ERROR_RE.fullmatch(error)
                ]
            errors.extend(correction_errors)
        if errors:
            return "validation failed:\n" + "\n".join(f"- {error}" for error in errors)
        return f"validation passed: {len(manifest.items)} ids across {len(manifest.files)} RightMemory files"

    def _memory_files(self) -> list[Path]:
        return build_graph_manifest(self.memory_root).files

    def _require_shared_view_builder_tool(self) -> None:
        if self.role not in SHARED_VIEW_BUILDER_ROLES:
            raise ValueError("shared-view compiler tools are only available to the shared-view-builder role")

    def _shared_view_selection_errors(
        self,
        *,
        include_headings: list[str],
        include_nodes: list[str],
        include_files: list[str],
        exclude_ids: list[str],
    ) -> list[str]:
        errors: list[str] = []
        manifest = build_graph_manifest(self.memory_root)
        memory_ids = {item_id: item.item_kind for item_id, item in manifest.items.items()}
        graph_files = {
            path.relative_to(self.memory_root).as_posix()
            for path in manifest.graph_files
        }
        errors.extend(self._id_selection_errors("include_headings", include_headings, memory_ids, expected_kind="heading"))
        errors.extend(self._id_selection_errors("include_nodes", include_nodes, memory_ids, expected_kind="node"))
        errors.extend(self._id_selection_errors("exclude_ids", exclude_ids, memory_ids, expected_kind=None))
        for value in include_files:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or path.as_posix() not in graph_files:
                errors.append(f"include_files entry must be a RightMemory graph file: {value}")
        return errors

    def _id_selection_errors(
        self,
        label: str,
        ids: list[str],
        memory_ids: dict[str, str],
        *,
        expected_kind: str | None,
    ) -> list[str]:
        errors: list[str] = []
        seen: set[str] = set()
        for item_id in ids:
            if item_id in seen:
                errors.append(f"{label} contains duplicate id: {item_id}")
                continue
            seen.add(item_id)
            actual_kind = memory_ids.get(item_id)
            if actual_kind is None:
                errors.append(f"{label} id not found in active memory: {item_id}")
            elif expected_kind is not None and actual_kind != expected_kind:
                errors.append(f"{label} id {item_id} is a {actual_kind}, not a {expected_kind}")
        return errors

    def _memory_id_kinds(self) -> dict[str, str]:
        manifest = build_graph_manifest(self.memory_root)
        return {item_id: item.item_kind for item_id, item in manifest.items.items()}

    def _file_view_rendered_context(self, path: Path) -> str:
        if not path.is_file():
            return ""
        marker = "## Published Context"
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            return ""
        return text.split(marker, 1)[1].strip()

    def _read_lines(self, resolved: Path, original_path: str, mark_read: bool = True) -> list[str]:
        if not resolved.is_file():
            raise FileNotFoundError(f"file not found: {original_path}")
        text = self._read_text(resolved)
        if mark_read:
            self._mark_read_text(resolved, text)
        return text.splitlines()

    def _read_numbered_source(self, path: Path, label: str) -> str:
        lines = self._read_lines(path, label)
        if not lines:
            return f"Source: {label}\n\n[empty source]"
        return f"Source: {label}\n\n{self._format_lines(lines, 1, len(lines))}"

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
            return [self._allowed_read_path(path).relative_to(self.memory_root).as_posix()]
        if resolved.is_dir():
            relative_base = resolved.relative_to(self.memory_root).as_posix()
            self._check_allowed_read_search_dir(resolved, path)
            if relative_base == ".":
                return self.list_files(glob_pattern)
            if self._is_runtime_shared_view_import_path(relative_base):
                return self.glob("**/*", relative_base)
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
        self._reject_runtime_shared_view_rg_globs(args)
        args, explicit_path_count, expanded_path_count = self._expand_rg_path_globs(args)
        if explicit_path_count and expanded_path_count == 0:
            return "no matches"
        path_indices = self._rg_path_token_indices(args)
        if self._has_role_read_scope() and not path_indices:
            readable_paths = self._existing_role_read_paths()
            if not readable_paths:
                return "no matches"
            path_start = len(args)
            args = [*args, *readable_paths]
            path_indices = set(range(path_start, len(args)))
        elif self._has_role_read_scope() and path_indices:
            args, path_indices = self._expand_role_read_command_dirs(args, path_indices)
            if not path_indices:
                return "no matches"
        self._validate_read_command_path_tokens(args, path_indices)
        args = self._exclude_runtime_shared_view_rg_paths(args)
        if "--files" not in args and "--with-filename" not in args and "--no-filename" not in args:
            args = [args[0], "--with-filename", *args[1:]]
        end_options = args.index("--") if "--" in args else len(args)
        args = [*args[:end_options], "--path-separator=/", *args[end_options:]]
        process = subprocess.run(
            args,
            cwd=self.memory_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode not in {0, 1}:
            detail = process.stderr.strip() or process.stdout.strip()
            if process.returncode == 2 and "No files were searched" in detail:
                return "no matches"
            raise RuntimeError(f"rg command failed: {detail}")
        output = process.stdout.strip()
        output = self._filter_runtime_shared_view_rg_output(output)
        return self._cap_command_output(output) if output else "no matches"

    def _expand_rg_path_globs(self, args: list[str]) -> tuple[list[str], int, int]:
        path_indices = self._rg_path_token_indices(args)
        if not path_indices:
            return args, 0, 0

        expanded: list[str] = []
        explicit_path_count = 0
        expanded_path_count = 0
        for index, token in enumerate(args):
            if index not in path_indices:
                expanded.append(token)
                continue
            explicit_path_count += 1
            if not self._has_glob_meta(token):
                expanded.append(token)
                expanded_path_count += 1
                continue
            matches = self._expand_glob_path_token(token)
            expanded.extend(matches)
            expanded_path_count += len(matches)
        return expanded, explicit_path_count, expanded_path_count

    def _rg_path_token_indices(self, args: list[str]) -> set[int]:
        option_takes_value = {"-g", "--glob", "--iglob", "--type", "-t", "-e", "--regexp", "-f", "--file"}
        pattern_options = {"-e", "--regexp", "-f", "--file"}
        path_indices: set[int] = set()
        has_pattern = "--files" in args
        skip_next = False
        end_options = False

        for index, token in enumerate(args[1:], start=1):
            if skip_next:
                skip_next = False
                continue
            if not end_options and token == "--":
                end_options = True
                continue
            if not end_options and token in option_takes_value:
                if token in pattern_options:
                    has_pattern = True
                skip_next = True
                continue
            if not end_options and self._rg_option_has_attached_value(token):
                if token.startswith(("-e", "--regexp=", "-f", "--file=")):
                    has_pattern = True
                continue
            if not end_options and token.startswith("-"):
                continue

            if has_pattern:
                path_indices.add(index)
            else:
                has_pattern = True
        return path_indices

    def _rg_option_has_attached_value(self, token: str) -> bool:
        return (
            token.startswith("--glob=")
            or token.startswith("--iglob=")
            or token.startswith("--type=")
            or token.startswith("--regexp=")
            or token.startswith("--file=")
            or (len(token) > 2 and token[:2] in {"-g", "-t", "-e", "-f"})
        )

    def _has_glob_meta(self, token: str) -> bool:
        return any(char in token for char in "*?[")

    def _expand_glob_path_token(self, token: str) -> list[str]:
        pattern = self._normalize_glob_pattern(token)
        matches = [
            path.relative_to(self.memory_root).as_posix()
            for path in self.memory_root.glob(pattern)
            if (path.is_file() or path.is_dir())
            and self._is_under_root(path)
            and self._is_allowed_read_command_candidate(path)
        ]
        matches.sort()
        if len(matches) > MAX_LIST_FILES:
            raise ValueError(f"pattern matched more than {MAX_LIST_FILES} paths; use a narrower pattern")
        return matches

    def _read_command_git(self, args: list[str]) -> str:
        if args == ["git", "status", "--short"]:
            return self.git_status()
        if len(args) >= 2 and args[1] == "diff":
            self._validate_git_diff_command(args)
            if self._has_role_read_scope() and len(args) == 2:
                return self.git_diff()
            return self._run_git(args)
        raise ValueError("supported git read commands are: git status --short, git diff")

    def _read_raw_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        resolved = self._allowed_read_path(path)
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
        output = output.replace("\r\n", "\n").replace("\r", "\n")
        if len(output) <= COMMAND_OUTPUT_CHAR_LIMIT:
            return output
        return (
            output[:COMMAND_OUTPUT_CHAR_LIMIT]
            + f"\n[truncated: output exceeded {COMMAND_OUTPUT_CHAR_LIMIT} characters]"
        )

    def _validate_memory_reference_id(self, value: str) -> str:
        clean = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", clean):
            raise ValueError("id must contain only letters, numbers, dot, underscore, or dash")
        return clean

    def _missing_skill_message(self, skill_id: str) -> str:
        return f"Skill not found: {skill_id}\n\n{self._available_skills_block()}"

    def _available_skills_block(self) -> str:
        ids = []
        for path in sorted(self.memory_root.glob("MEMORY_SKILL_*.md")):
            if self._is_safe_read_file(path):
                ids.append(path.stem.removeprefix("MEMORY_SKILL_"))
        if not ids:
            return "Available skills:\n- none"
        return "Available skills:\n" + "\n".join(f"- {item}" for item in ids)

    def _missing_typed_backing_message(self, label: str, item_id: str, kind: str) -> str:
        manifest = build_graph_manifest(self.memory_root)
        ids = sorted(reference.id for reference in manifest.backing.values() if reference.kind == kind)
        available = "\n".join(f"- {value}" for value in ids) if ids else "- none"
        return f"{label} not found: {item_id}\n\nAvailable {label} ids:\n{available}"

    def _missing_mf_message(self, mf_id: str) -> str:
        return f"MF import not found: {mf_id}\n\n{self._available_mf_imports_block()}"

    def _available_mf_imports_block(self) -> str:
        imports_root = self.memory_root / RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX
        if not imports_root.is_dir() or not self._is_under_root(imports_root):
            ids = []
        else:
            ids = sorted(path.name for path in imports_root.iterdir() if path.is_dir() and self._is_under_root(path))
        if not ids:
            return "Available MF imports:\n- none"
        return "Available MF imports:\n" + "\n".join(f"- {item}" for item in ids)

    def _is_safe_read_file(self, path: Path) -> bool:
        return path.is_file() and self._is_under_root(path)

    def _validate_read_command_path_tokens(self, args: list[str], path_indices: set[int]) -> None:
        for index in path_indices:
            token = args[index]
            if token.startswith("~"):
                raise ValueError("read command paths must stay under the RightMemory root")
            raw = Path(token)
            if raw.is_absolute() or ".." in raw.parts:
                resolved = self._resolve_path(token)
                if not self._is_under_root(resolved):
                    raise ValueError("read command paths must stay under the RightMemory root")
            resolved = self._resolve_path(token)
            relative_path = resolved.relative_to(self.memory_root).as_posix()
            if self._is_runtime_shared_view_path(relative_path):
                if not self._is_runtime_shared_view_import_path(relative_path):
                    raise ValueError("runtime shared-view imports are only readable by retrieve")
            if self._has_role_read_scope():
                self._check_allowed_read_command_path(token)

    def _reject_runtime_shared_view_rg_globs(self, args: list[str]) -> None:
        glob_options = {"-g", "--glob", "--iglob"}
        skip_next = False
        for index, token in enumerate(args[1:], start=1):
            if skip_next:
                skip_next = False
                continue
            if token in glob_options:
                if index + 1 < len(args):
                    self._reject_runtime_shared_view_glob(args[index + 1])
                    skip_next = True
                continue
            for prefix in ("--glob=", "--iglob="):
                if token.startswith(prefix):
                    self._reject_runtime_shared_view_glob(token.split("=", 1)[1])
                    break
            else:
                if len(token) > 2 and token[:2] == "-g":
                    self._reject_runtime_shared_view_glob(token[2:])

    def _reject_runtime_shared_view_glob(self, pattern: str) -> None:
        normalized = pattern[1:] if pattern.startswith("!") else pattern
        if normalized.startswith(RUNTIME_SHARED_VIEW_PATH_PREFIX) or f"/{RUNTIME_SHARED_VIEW_PATH_PREFIX}" in normalized:
            if not self._is_runtime_shared_view_import_path(normalized):
                raise ValueError("runtime shared-view imports are only readable by retrieve")

    def _exclude_runtime_shared_view_rg_paths(self, args: list[str]) -> list[str]:
        if self.role == "retrieve":
            return args
        insert_at = args.index("--") if "--" in args else len(args)
        return [*args[:insert_at], "--glob", f"!{RUNTIME_SHARED_VIEW_PATH_PREFIX}**", *args[insert_at:]]

    def _filter_runtime_shared_view_rg_output(self, output: str) -> str:
        if not output:
            return output
        kept = [
            line
            for line in output.splitlines()
            if not line.startswith(RUNTIME_SHARED_VIEW_PATH_PREFIX)
            or (self.role == "retrieve" and line.startswith(RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX))
        ]
        return "\n".join(kept)

    def _expand_role_read_command_dirs(self, args: list[str], path_indices: set[int]) -> tuple[list[str], set[int]]:
        expanded: list[str] = []
        expanded_path_indices: set[int] = set()
        for index, token in enumerate(args):
            if index not in path_indices:
                expanded.append(token)
                continue
            resolved = self._resolve_path(token)
            if not resolved.is_dir():
                expanded_path_indices.add(len(expanded))
                expanded.append(token)
                continue
            relative_path = resolved.relative_to(self.memory_root).as_posix()
            if self.role == "retrieve" and self._is_runtime_shared_view_import_path(relative_path):
                expanded_path_indices.add(len(expanded))
                expanded.append(relative_path)
                continue
            self._check_allowed_read_search_dir(resolved, token)
            for relative_path in self._existing_role_read_paths_under(resolved):
                expanded_path_indices.add(len(expanded))
                expanded.append(relative_path)
        return expanded, expanded_path_indices

    def _validate_git_diff_command(self, args: list[str]) -> None:
        if len(args) == 2:
            return
        if len(args) < 4 or args[2] != "--":
            raise ValueError("supported git diff form is: git diff or git diff -- <paths>")
        separator = 2
        for path in args[separator + 1 :]:
            self._allowed_read_path(path)

    def _format_lines(self, lines: list[str], start: int, end: int) -> str:
        selected = lines[start - 1 : end]
        return "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start))

    def _validate_positive(self, name: str, value: int) -> None:
        if value < 1:
            raise ValueError(f"{name} must be >= 1")

    def _structure_errors(self, files: list[Path]) -> list[str]:
        errors: list[str] = []
        for path in files:
            heading_stack: list[tuple[int, int]] = []
            active_terminal: tuple[int, int] | None = None
            fence_char: str | None = None
            fence_length = 0
            relative_path = path.relative_to(self.memory_root)
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
                if fence is not None:
                    marker = fence.group(1)
                    if fence_char is None:
                        fence_char = marker[0]
                        fence_length = len(marker)
                    elif marker[0] == fence_char and len(marker) >= fence_length:
                        fence_char = None
                        fence_length = 0
                    continue
                if fence_char is not None:
                    continue
                heading_match = ANY_HEADING_RE.match(line)
                if heading_match is None:
                    if active_terminal is not None and NODE_RE.match(line):
                        errors.append(
                            f"terminal `####` heading cannot contain node lines at {relative_path}:{line_number}"
                        )
                    continue

                depth = len(heading_match.group(1))
                unsupported = UNSUPPORTED_ANCHOR_KIND_RE.match(line)
                if unsupported is not None and ANCHOR_KIND_RE.match(line) is None:
                    errors.append(
                        f"unsupported heading marker `{unsupported.group(2)}` at {relative_path}:{line_number}"
                    )
                while heading_stack and heading_stack[-1][0] >= depth:
                    heading_stack.pop()
                parent_depth = heading_stack[-1][0] if heading_stack else None
                if active_terminal is not None and depth <= active_terminal[0]:
                    active_terminal = None

                if depth > 4:
                    errors.append(f"headings deeper than `####` are not allowed at {relative_path}:{line_number}")
                elif depth == 4:
                    anchor_match = ANCHOR_KIND_RE.match(line)
                    if anchor_match is None or anchor_match.group(2) not in TERMINAL_HEADING_KINDS:
                        errors.append(
                            f"`####` terminal reference must use `{{F#slug}}`, `{{M#slug}}`, `{{S#slug}}`, `{{MF#slug}}`, or `{{MQ#slug}}` at {relative_path}:{line_number}"
                        )
                    if parent_depth != 3:
                        errors.append(f"`####` terminal reference must be under a `###` heading at {relative_path}:{line_number}")
                    if anchor_match is not None and anchor_match.group(2) in TERMINAL_HEADING_KINDS:
                        active_terminal = (depth, line_number)

                heading_stack.append((depth, line_number))
        return errors

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
        if self._is_allowed_write_path(relative_path):
            return relative_path
        raise ValueError(f"can only stage, commit, or discard {self._write_policy_label()}: {relative_path}")

    def _write_policy_label(self) -> str:
        if self.role in INSIGHT_ROLES:
            return "insight_logs/*.md"
        if self.role in SYNC_RECONCILER_ROLES:
            return "RightMemory state files, shared-view source files, or insight_logs/*.md"
        if self.role in SHARED_VIEW_BUILDER_ROLES:
            return "shared_views/<id> source files"
        if self.role in FULL_RIGHTMEMORY_WRITE_ROLES:
            suffix = ", or corrections.md" if self.role in CORRECTION_WRITE_ROLES else ""
            return f"MEMORY.md, MEMORY_*.md, PURSUITS.md, or PURSUIT_*.md{suffix}"
        return "MEMORY.md or MEMORY_*.md"

    def _is_allowed_write_path(self, relative_path: str) -> bool:
        if (
            relative_path in FIXED_CORRECTION_COLLECTION_PATHS
            and self.role not in FULL_RIGHTMEMORY_WRITE_ROLES | SYNC_RECONCILER_ROLES
        ):
            return False
        if self.role in INSIGHT_ROLES:
            return self._is_insight_log_path(relative_path)
        if self.role in SYNC_RECONCILER_ROLES:
            return (
                self._is_active_rightmemory_path(relative_path)
                or relative_path in {PURSUIT_RULES_PATH, CORRECTIONS_PATH}
                or relative_path == SHARED_VIEW_REGISTRY_PATH
                or relative_path == SHARE_REGISTRY_PATH
                or self._is_shared_view_definition_path(relative_path)
                or self._is_insight_log_path(relative_path)
            )
        if self.role in SHARED_VIEW_BUILDER_ROLES:
            return self._is_shared_view_definition_path(relative_path)
        if self.role in FULL_RIGHTMEMORY_WRITE_ROLES:
            return self._is_active_rightmemory_path(relative_path) or (
                self.role in CORRECTION_WRITE_ROLES and relative_path == CORRECTIONS_PATH
            )
        return self._is_active_memory_path(relative_path)

    def _allowed_write_path(self, path: str) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if self._is_allowed_write_path(relative_path):
            return relative_path
        raise ValueError(f"can only write {self._write_policy_label()}: {relative_path}")

    def _allowed_history_path(self, path: str) -> str:
        raw_path = Path(path)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise ValueError("history paths must be relative memory files")
        relative_path = raw_path.as_posix()
        if self._is_active_memory_path(relative_path):
            return relative_path
        raise ValueError(f"can only read historical MEMORY.md or MEMORY_*.md files: {relative_path}")

    def _allowed_read_path(self, path: str) -> Path:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if self._is_allowed_read_relative_file(relative_path):
            return resolved
        if self._is_runtime_shared_view_path(relative_path):
            raise ValueError("runtime shared-view imports are only readable by retrieve")
        raise ValueError(f"can only read {self._read_policy_label()}: {relative_path}")

    def _has_role_read_scope(self) -> bool:
        return self.role in INSIGHT_ROLES | RETRIEVE_ROLES

    def _read_policy_label(self) -> str:
        if self.role == "retrieve":
            return "MEMORY.md, MEMORY_*.md, PURSUITS.md, or PURSUIT_*.md"
        return "MEMORY.md, MEMORY_*.md, or insight_logs/*.md"

    def _is_allowed_read_file(self, path: Path) -> bool:
        relative_path = path.relative_to(self.memory_root).as_posix()
        if relative_path == CORRECTIONS_PATH and self.role not in CORRECTIONS_READ_ROLES:
            return False
        if self._is_runtime_shared_view_path(relative_path):
            if self._is_runtime_shared_view_import_path(relative_path):
                return self.role == "retrieve"
            return False
        if not self._has_role_read_scope():
            return True
        return self._is_allowed_read_relative_file(relative_path)

    def _is_allowed_read_relative_file(self, relative_path: str) -> bool:
        if relative_path == CORRECTIONS_PATH and self.role not in CORRECTIONS_READ_ROLES:
            return False
        if self._is_runtime_shared_view_path(relative_path):
            if self._is_runtime_shared_view_import_path(relative_path):
                return self.role == "retrieve"
            return False
        if not self._has_role_read_scope():
            return True
        if self.role == "retrieve":
            return self._is_active_rightmemory_path(relative_path)
        return self._is_active_memory_path(relative_path) or self._is_insight_log_path(relative_path)

    def _check_allowed_read_search_dir(self, path: Path, original_path: str) -> None:
        relative_path = path.relative_to(self.memory_root).as_posix()
        if self._is_runtime_shared_view_import_path(relative_path):
            if self.role == "retrieve":
                return
            raise ValueError("runtime shared-view imports are only readable by retrieve")
        if self._is_runtime_shared_view_path(relative_path):
            raise ValueError("runtime shared-view imports are only readable by retrieve")
        if not self._has_role_read_scope():
            return
        if relative_path in {".", "insight_logs"}:
            return
        raise ValueError(f"can only search {self._read_policy_label()}: {relative_path or original_path}")

    def _is_allowed_read_command_candidate(self, path: Path) -> bool:
        if not self._has_role_read_scope():
            return True
        relative_path = path.relative_to(self.memory_root).as_posix()
        if path.is_dir():
            if self._is_runtime_shared_view_import_path(relative_path):
                return self.role == "retrieve"
            return relative_path == "insight_logs"
        return self._is_allowed_read_relative_file(relative_path)

    def _check_allowed_read_command_path(self, path: str) -> None:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if resolved.is_dir():
            self._check_allowed_read_search_dir(resolved, path)
            return
        if self._is_allowed_read_relative_file(relative_path):
            return
        raise ValueError(f"can only read {self._read_policy_label()}: {relative_path}")

    def _existing_role_read_paths(self) -> list[str]:
        paths = [
            path.relative_to(self.memory_root).as_posix()
            for pattern in self._role_read_patterns()
            for path in self.memory_root.glob(pattern)
            if path.is_file() and self._is_under_root(path) and self._is_allowed_read_file(path)
        ]
        return sorted(set(paths))

    def _role_read_patterns(self) -> tuple[str, ...]:
        return RETRIEVE_READ_PATHS if self.role == "retrieve" else INSIGHT_READ_PATHS

    def _existing_role_read_paths_under(self, directory: Path) -> list[str]:
        directory = directory.resolve(strict=False)
        return [
            relative_path
            for relative_path in self._existing_role_read_paths()
            if self._path_is_self_or_under(self.memory_root / relative_path, directory)
        ]

    def _path_is_self_or_under(self, path: Path, directory: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(directory)
        except ValueError:
            return False
        return True

    def _is_active_memory_path(self, relative_path: str) -> bool:
        return relative_path == "MEMORY.md" or bool(MEMORY_DETAIL_FILE_RE.fullmatch(relative_path))

    def _is_active_pursuit_path(self, relative_path: str) -> bool:
        return relative_path == "PURSUITS.md" or bool(PURSUIT_DETAIL_FILE_RE.fullmatch(relative_path))

    def _is_active_rightmemory_path(self, relative_path: str) -> bool:
        return self._is_active_memory_path(relative_path) or self._is_active_pursuit_path(relative_path)

    def _is_insight_log_path(self, relative_path: str) -> bool:
        return bool(INSIGHT_LOG_FILE_RE.fullmatch(relative_path))

    def _is_shared_view_definition_path(self, relative_path: str) -> bool:
        return bool(SHARED_VIEW_DEFINITION_FILE_RE.fullmatch(relative_path))

    def _is_runtime_shared_view_path(self, relative_path: str) -> bool:
        return relative_path.startswith(RUNTIME_SHARED_VIEW_PATH_PREFIX)

    def _is_runtime_shared_view_import_path(self, relative_path: str) -> bool:
        return relative_path.startswith(RUNTIME_SHARED_VIEW_IMPORTS_PATH_PREFIX)

    def _is_memory_skill_file(self, path: Path) -> bool:
        relative_path = path.relative_to(self.memory_root).as_posix()
        return bool(MEMORY_SKILL_FILE_RE.fullmatch(relative_path))

    def _validate_git_revision(self, revision: str) -> str:
        revision = revision.strip()
        if not revision:
            raise ValueError("revision must not be empty")
        if revision.startswith("-") or not GIT_REVISION_RE.fullmatch(revision):
            raise ValueError("revision contains unsupported characters")
        return revision

    def _git_path_type_at_revision(self, revision: str, path: str) -> str | None:
        self._run_git(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"])
        process = subprocess.run(
            ["git", "cat-file", "-t", f"{revision}:{path}"],
            cwd=self.memory_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            return None
        return process.stdout.strip()

    def _prune_subject_commits(self, max_count: int) -> list[str]:
        output = self._run_git(["git", "log", "--format=%H %s"])
        commits: list[str] = []
        for line in output.splitlines():
            commit_hash, separator, subject = line.partition(" ")
            if separator and subject.startswith(PRUNE_SUBJECT_PREFIX):
                commits.append(commit_hash)
                if len(commits) >= max_count:
                    break
        return commits

    def _validate_commit_subject(self, message: str) -> str:
        message = message.strip()
        if not message:
            raise ValueError("commit message must not be empty")
        if "\x00" in message:
            raise ValueError("commit subject must not contain NUL bytes")
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return process.returncode == 0

    def _git_path_type_in_head(self, path: str) -> str | None:
        process = subprocess.run(
            ["git", "cat-file", "-t", f"HEAD:{path}"],
            cwd=self.memory_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            return None
        return process.stdout.strip()

    def _git_path_kind_in_index(self, path: str) -> str | None:
        process = subprocess.run(
            ["git", "ls-files", "-z", "--", path],
            cwd=self.memory_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise RuntimeError(f"git command failed: {detail}")
        entries = [entry for entry in process.stdout.split("\0") if entry]
        if not entries:
            return None
        if any(entry == path for entry in entries):
            return "file"
        prefix = path.rstrip("/") + "/"
        if any(entry.startswith(prefix) for entry in entries):
            return "tree"
        return None

    def _reject_discard_directory_path(
        self,
        path: str,
        head_path_type: str | None,
        index_path_kind: str | None,
    ) -> None:
        resolved = self.memory_root / path
        if resolved.exists() and not (resolved.is_file() or resolved.is_symlink()):
            raise ValueError(f"cannot discard directory path: {path}")
        if head_path_type == "tree" or index_path_kind == "tree":
            raise ValueError(f"cannot discard directory path: {path}")

    def _reject_stage_directory_path(
        self,
        path: str,
        head_path_type: str | None = None,
        index_path_kind: str | None = None,
    ) -> None:
        resolved = self.memory_root / path
        if resolved.exists() and not (resolved.is_file() or resolved.is_symlink()):
            raise ValueError(f"cannot stage directory path: {path}")
        if head_path_type == "tree" or index_path_kind == "tree":
            raise ValueError(f"cannot stage directory path: {path}")

    def _unlink_worktree_file(self, path: str) -> None:
        resolved = self.memory_root / path
        if resolved.is_file() or resolved.is_symlink():
            resolved.unlink()
        elif resolved.exists():
            raise RuntimeError(f"cannot discard directory path: {path}")

    def _worktree_file_exists(self, path: str) -> bool:
        resolved = self.memory_root / path
        return resolved.is_file() or resolved.is_symlink()

def _capability_from_selected_views(file_view_id: str | None, question_view_id: str | None) -> str:
    has_file = bool(str(file_view_id or "").strip())
    has_question = bool(str(question_view_id or "").strip())
    if has_file and has_question:
        return "both"
    if has_file:
        return "file_context"
    if has_question:
        return "live_questions"
    return "auto"
