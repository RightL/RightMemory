from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .session import MemoryWriteLock, _ensure_runtime_gitignore


GUIDANCE_INBOX_PATH = "AGENT_GUIDANCE_INBOX.md"
GUIDANCE_INBOX_HEADING = "# Pending Agent Guidance"
_GUIDANCE_HEADING_RE = re.compile(r"^## (GI-\d{8}-[0-9a-f]{8})$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class GuidanceConflictError(ValueError):
    pass


@dataclass(frozen=True)
class GuidanceEntry:
    entry_id: str
    session_id: str
    submitted_at: str
    body: str


def parse_guidance_inbox(text: str) -> list[GuidanceEntry]:
    errors, entries = _parse_guidance_inbox(text)
    if errors:
        raise ValueError("invalid agent guidance inbox:\n- " + "\n- ".join(errors))
    return entries


def validate_guidance_inbox(text: str) -> list[str]:
    errors, _entries = _parse_guidance_inbox(text)
    return errors


def render_guidance_inbox(entries: list[GuidanceEntry]) -> str:
    parts = [GUIDANCE_INBOX_HEADING]
    for entry in entries:
        parts.append(
            "\n".join(
                (
                    f"## {entry.entry_id}",
                    "",
                    f"Session: {entry.session_id}",
                    f"Submitted: {entry.submitted_at}",
                    "",
                    entry.body.strip(),
                )
            )
        )
    return "\n\n".join(parts).rstrip() + "\n"


def merge_guidance_inbox(base_text: str, ours_text: str, theirs_text: str) -> str:
    base = parse_guidance_inbox(base_text)
    ours = parse_guidance_inbox(ours_text)
    theirs = parse_guidance_inbox(theirs_text)
    base_map = {entry.entry_id: entry for entry in base}
    ours_map = {entry.entry_id: entry for entry in ours}
    theirs_map = {entry.entry_id: entry for entry in theirs}

    chosen: dict[str, GuidanceEntry] = {}
    for entry_id in sorted(set(base_map) | set(ours_map) | set(theirs_map)):
        base_entry = base_map.get(entry_id)
        ours_entry = ours_map.get(entry_id)
        theirs_entry = theirs_map.get(entry_id)
        if ours_entry == theirs_entry:
            selected = ours_entry
        elif ours_entry == base_entry:
            selected = theirs_entry
        elif theirs_entry == base_entry:
            selected = ours_entry
        else:
            raise GuidanceConflictError(f"conflicting guidance entry: {entry_id}")
        if selected is not None:
            chosen[entry_id] = selected

    order = [entry.entry_id for entry in base if entry.entry_id in chosen]
    existing = set(order)
    additions = [entry for entry_id, entry in chosen.items() if entry_id not in existing]
    additions.sort(key=lambda entry: (entry.submitted_at, entry.entry_id))
    order.extend(entry.entry_id for entry in additions)
    return render_guidance_inbox([chosen[entry_id] for entry_id in order])


def submit_guidance(memory_root: Path, session_id: str, evidence: str) -> GuidanceEntry:
    root = Path(memory_root).resolve()
    clean_session = _validate_session_id(session_id)
    clean_evidence = evidence.strip()
    if not clean_evidence:
        raise ValueError("guidance evidence must not be empty")

    _require_repository_root(root)
    identifier = uuid.uuid4().hex
    branch = f"rightmemory-guidance-{identifier}"
    worktree = root / ".runtime" / "worktrees" / f"guidance-{identifier}"
    _ensure_runtime_gitignore(root / ".runtime")
    relative_worktree = worktree.relative_to(root).as_posix()
    if _git_result(root, "check-ignore", "-q", relative_worktree).returncode != 0:
        raise RuntimeError(f"runtime worktree path is not ignored by git: {relative_worktree}")

    with MemoryWriteLock(root):
        start_head = _git(root, "rev-parse", "HEAD")
        _git(root, "worktree", "add", "-b", branch, str(worktree), start_head)
        try:
            entry = _append_guidance(worktree, clean_session, clean_evidence)
            if _git(root, "rev-parse", "HEAD") != start_head:
                raise RuntimeError("main HEAD changed during guidance capture")
            _git(root, "merge", "--ff-only", _git(worktree, "rev-parse", "HEAD"))
            return entry
        finally:
            _git_result(root, "worktree", "remove", "--force", str(worktree))
            _git_result(root, "branch", "-D", branch)


def _append_guidance(worktree: Path, session_id: str, evidence: str) -> GuidanceEntry:
    path = worktree / GUIDANCE_INBOX_PATH
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"{GUIDANCE_INBOX_PATH} must be a regular file")
    existing_text = path.read_text(encoding="utf-8") if path.is_file() else GUIDANCE_INBOX_HEADING + "\n"
    entries = parse_guidance_inbox(existing_text)
    submitted_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    day = submitted_at[:10].replace("-", "")
    entry_id = _new_entry_id(day, {entry.entry_id for entry in entries})
    entry = GuidanceEntry(entry_id, session_id, submitted_at, evidence)
    rendered = render_guidance_inbox([*entries, entry])
    errors = validate_guidance_inbox(rendered)
    if errors:
        raise ValueError("invalid agent guidance inbox:\n- " + "\n- ".join(errors))
    path.write_text(rendered, encoding="utf-8")
    _git(worktree, "add", "-f", "--", GUIDANCE_INBOX_PATH)
    _git(worktree, "commit", "-m", "guidance: capture pending evidence")
    return entry


def _parse_guidance_inbox(text: str) -> tuple[list[str], list[GuidanceEntry]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    errors: list[str] = []
    entries: list[GuidanceEntry] = []
    if not lines or lines[0] != GUIDANCE_INBOX_HEADING:
        errors.append(f"first line must be `{GUIDANCE_INBOX_HEADING}`")
        return errors, entries

    starts: list[tuple[int, str]] = []
    fence_char: str | None = None
    fence_length = 0
    for index, line in enumerate(lines[1:], start=1):
        fence = _FENCE_RE.match(line)
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
        match = _GUIDANCE_HEADING_RE.fullmatch(line)
        if match is not None:
            starts.append((index, match.group(1)))
        elif line.startswith("## GI-"):
            errors.append(f"line {index + 1}: malformed guidance entry heading")

    if fence_char is not None:
        errors.append("unclosed fenced code block")

    preamble_end = starts[0][0] if starts else len(lines)
    if any(line.strip() for line in lines[1:preamble_end]):
        errors.append("content appears before first guidance entry")

    seen: set[str] = set()
    for position, (start, entry_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        if entry_id in seen:
            errors.append(f"duplicate guidance entry id: {entry_id}")
        seen.add(entry_id)
        block = list(lines[start + 1 : end])
        while block and not block[0].strip():
            block.pop(0)
        session_line = block.pop(0) if block else ""
        submitted_line = block.pop(0) if block else ""
        if not session_line.startswith("Session: ") or not session_line.removeprefix("Session: ").strip():
            errors.append(f"{entry_id} is missing `Session` provenance")
            session_id = ""
        else:
            session_id = session_line.removeprefix("Session: ").strip()
        if not submitted_line.startswith("Submitted: "):
            errors.append(f"{entry_id} is missing `Submitted` provenance")
            submitted_at = ""
        else:
            submitted_at = submitted_line.removeprefix("Submitted: ").strip()
            if not _valid_submitted_at(submitted_at):
                errors.append(f"{entry_id} has invalid `Submitted` timestamp")
        while block and not block[0].strip():
            block.pop(0)
        body = "\n".join(block).strip()
        if not body:
            errors.append(f"{entry_id} has empty guidance evidence")
        entries.append(GuidanceEntry(entry_id, session_id, submitted_at, body))

    return errors, entries


def _valid_submitted_at(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_session_id(value: str) -> str:
    clean = value.strip()
    if not clean or any(character in clean for character in "\x00\r\n"):
        raise ValueError("session id must be a non-empty single line")
    return clean


def _new_entry_id(day: str, existing_ids: set[str]) -> str:
    for _attempt in range(64):
        candidate = f"GI-{day}-{uuid.uuid4().hex[:8]}"
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("could not allocate a unique guidance entry id")


def _require_repository_root(root: Path) -> None:
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise RuntimeError(f"memory root is not the git repository root: {root}")


def _git(cwd: Path, *args: str) -> str:
    result = _git_result(cwd, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _git_result(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
