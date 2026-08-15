from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .isolated_write import IsolatedWriteSupervisor


GUIDANCE_INBOX_PATH = "AGENT_GUIDANCE_INBOX.md"
GUIDANCE_INBOX_HEADING = "# Pending Agent Guidance"
_GUIDANCE_ID_RE = re.compile(r"^GI-(\d{8})-([0-9a-f]{8})$")
_GUIDANCE_HEADING_RE = re.compile(r"^## (GI-\d{8}-[0-9a-f]{8})$")


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
        body = entry.body.strip()
        parts.append(
            "\n".join(
                (
                    f"## {entry.entry_id}",
                    "",
                    f"Session: {entry.session_id}",
                    f"Submitted: {entry.submitted_at}",
                    "",
                    body,
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

    order: list[str] = []
    for entry in base:
        if entry.entry_id in chosen:
            order.append(entry.entry_id)
    additions = [
        entry
        for entry_id, entry in chosen.items()
        if entry_id not in set(order)
    ]
    additions.sort(key=lambda entry: (entry.submitted_at, entry.entry_id))
    order.extend(entry.entry_id for entry in additions)
    return render_guidance_inbox([chosen[entry_id] for entry_id in order])


def submit_guidance(memory_root: Path, session_id: str, evidence: str) -> GuidanceEntry:
    root = Path(memory_root).resolve()
    clean_session = _validate_session_id(session_id)
    clean_evidence = evidence.strip()
    if not clean_evidence:
        raise ValueError("guidance evidence must not be empty")

    def write_in_worktree(worktree: Path) -> GuidanceEntry:
        path = worktree / GUIDANCE_INBOX_PATH
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ValueError(f"{GUIDANCE_INBOX_PATH} must be a regular file")
        existing_text = (
            path.read_text(encoding="utf-8")
            if path.is_file()
            else GUIDANCE_INBOX_HEADING + "\n"
        )
        entries = parse_guidance_inbox(existing_text)
        existing_ids = {entry.entry_id for entry in entries}
        submitted_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        day = submitted_at[:10].replace("-", "")
        entry_id = _new_entry_id(day, existing_ids)
        entry = GuidanceEntry(entry_id, clean_session, submitted_at, clean_evidence)
        rendered = render_guidance_inbox([*entries, entry])
        errors = validate_guidance_inbox(rendered)
        if errors:
            raise ValueError("invalid agent guidance inbox:\n- " + "\n- ".join(errors))
        path.write_text(rendered, encoding="utf-8")
        _git(worktree, "add", "-f", "--", GUIDANCE_INBOX_PATH)
        _git(worktree, "commit", "-m", "guidance: capture pending evidence")
        return entry

    result = IsolatedWriteSupervisor(root, "guidance").run(write_in_worktree)
    return result.output


def _parse_guidance_inbox(text: str) -> tuple[list[str], list[GuidanceEntry]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    errors: list[str] = []
    entries: list[GuidanceEntry] = []
    if not lines or lines[0] != GUIDANCE_INBOX_HEADING:
        errors.append(f"first line must be `{GUIDANCE_INBOX_HEADING}`")
        return errors, entries

    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines[1:], start=1):
        match = _GUIDANCE_HEADING_RE.fullmatch(line)
        if match is not None:
            starts.append((index, match.group(1)))

    seen: set[str] = set()
    for position, (start, entry_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        if entry_id in seen:
            errors.append(f"duplicate guidance entry id: {entry_id}")
        seen.add(entry_id)
        block = lines[start + 1 : end]
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

    for index, line in enumerate(lines[1:], start=2):
        if line.startswith("## GI-") and _GUIDANCE_HEADING_RE.fullmatch(line) is None:
            errors.append(f"line {index}: malformed guidance entry heading")

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


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    result = subprocess.run(
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
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()
