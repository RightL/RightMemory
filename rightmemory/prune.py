from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import PrunerConfig
from .memory_git import active_memory_commit_count, first_generation_active_memory_boundary


GIT_TIMEOUT_SECONDS = 30
PRUNE_SUBJECT_PREFIX = "prune:"
PRUNE_GREP = r"^prune:"
MEMORY_REF_RE = re.compile(
    r"(?P<path>MEMORY(?:_[A-Za-z0-9_.-]+)?\.md)#(?P<memory_id>[A-Za-z0-9_.-]+)"
)
BACKTICK_ID_RE = re.compile(r"`(?P<memory_id>[A-Za-z0-9_.-]+)`")
GRACE_RE = re.compile(r"\bgrace\s+(?P<used>\d+)\s*/\s*(?P<total>\d+)\b", re.IGNORECASE)
SECTION_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z ]+:$")


@dataclass(frozen=True)
class RemovedEntry:
    path: str
    memory_id: str
    summary: str = ""

    @property
    def ref(self) -> str:
        return f"{self.path}#{self.memory_id}" if self.path else f"`{self.memory_id}`"


@dataclass(frozen=True)
class GraceEntry:
    path: str
    memory_id: str
    used: int
    total: int
    summary: str = ""

    @property
    def ref(self) -> str:
        return f"{self.path}#{self.memory_id}" if self.path else f"`{self.memory_id}`"


@dataclass(frozen=True)
class PruneLedger:
    raw_body: str = ""
    removed: tuple[RemovedEntry, ...] = ()
    grace: tuple[GraceEntry, ...] = ()


@dataclass(frozen=True)
class PruneDueStatus:
    due: bool
    message: str
    commits_since_boundary: int
    generation_commits: int
    current_head: str | None = None
    boundary_commit: str | None = None
    latest_prune_commit: str | None = None
    previous_ledger: PruneLedger = field(default_factory=PruneLedger)
    revival_grace_checkpoints: int = 2


def prune_due_status(memory_root: Path, config: PrunerConfig) -> PruneDueStatus:
    """Return deterministic prune due state for the memory repository."""
    root = Path(memory_root).resolve()
    current_head = _git_stdout(root, "rev-parse", "--verify", "HEAD")
    latest = latest_prune_commit(root)

    if latest is not None:
        commits_since = active_memory_commit_count(root, f"{latest}..HEAD")
        body = _git_stdout(root, "log", "--max-count=1", "--format=%B", latest)
        if commits_since < config.generation_commits:
            return PruneDueStatus(
                due=False,
                message=(
                    "prune not due: "
                    f"{commits_since}/{config.generation_commits} commits since latest prune checkpoint {latest[:12]}"
                ),
                commits_since_boundary=commits_since,
                generation_commits=config.generation_commits,
                current_head=current_head,
                latest_prune_commit=latest,
                previous_ledger=parse_prune_ledger(body),
                revival_grace_checkpoints=config.revival_grace_checkpoints,
            )
        return PruneDueStatus(
            due=True,
            message="prune due",
            commits_since_boundary=commits_since,
            generation_commits=config.generation_commits,
            current_head=current_head,
            boundary_commit=latest,
            latest_prune_commit=latest,
            previous_ledger=parse_prune_ledger(body),
            revival_grace_checkpoints=config.revival_grace_checkpoints,
        )

    total_commits = active_memory_commit_count(root, "HEAD")
    if total_commits < config.generation_commits:
        return PruneDueStatus(
            due=False,
            message=f"prune not due: {total_commits}/{config.generation_commits} commits in repository history",
            commits_since_boundary=total_commits,
            generation_commits=config.generation_commits,
            current_head=current_head,
            revival_grace_checkpoints=config.revival_grace_checkpoints,
        )
    return PruneDueStatus(
        due=True,
        message="prune due",
        commits_since_boundary=min(total_commits, config.generation_commits),
        generation_commits=config.generation_commits,
        current_head=current_head,
        boundary_commit=first_generation_active_memory_boundary(root, config.generation_commits),
        revival_grace_checkpoints=config.revival_grace_checkpoints,
    )


def latest_prune_commit(memory_root: Path) -> str | None:
    result = _run_git(
        Path(memory_root).resolve(),
        "log",
        "--format=%H %s",
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        commit_hash, separator, subject = line.partition(" ")
        if separator and subject.startswith(PRUNE_SUBJECT_PREFIX):
            return commit_hash
    return None


def parse_prune_ledger(body: str) -> PruneLedger:
    section = ""
    removed: list[RemovedEntry] = []
    grace: list[GraceEntry] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower in {"removed:", "removed"}:
            section = "removed"
            continue
        if lower in {"revival grace:", "revival grace"}:
            section = "grace"
            continue
        if SECTION_HEADING_RE.fullmatch(line):
            section = ""
            continue
        if not line.startswith("- "):
            continue

        entry = line[2:].strip()
        ref = _ledger_reference(entry)
        if ref is None:
            continue
        path, memory_id, ref_end = ref
        rest = entry[ref_end:].strip(" |:-")
        if section == "removed":
            removed.append(RemovedEntry(path=path, memory_id=memory_id, summary=rest))
        elif section == "grace":
            grace_match = GRACE_RE.search(rest)
            if grace_match is None:
                continue
            grace.append(
                GraceEntry(
                    path=path,
                    memory_id=memory_id,
                    used=int(grace_match.group("used")),
                    total=int(grace_match.group("total")),
                    summary=GRACE_RE.sub("", rest).strip(" |:-"),
                )
            )

    return PruneLedger(raw_body=body, removed=tuple(removed), grace=tuple(grace))


def _ledger_reference(entry: str) -> tuple[str, str, int] | None:
    match = MEMORY_REF_RE.search(entry)
    if match is not None:
        return match.group("path"), match.group("memory_id"), match.end()
    match = BACKTICK_ID_RE.search(entry)
    if match is not None:
        return "", match.group("memory_id"), match.end()
    return None


def build_pruner_message(status: PruneDueStatus) -> str:
    if not status.due:
        return status.message
    if status.boundary_commit is None or status.current_head is None:
        raise ValueError("due prune status requires boundary_commit and current_head")

    previous = _ledger_summary(status.previous_ledger)
    return (
        "Prune generation due.\n\n"
        f"Generation threshold: {status.generation_commits} commits\n"
        f"Commits since boundary: {status.commits_since_boundary}\n"
        f"Boundary commit: {status.boundary_commit}\n"
        f"Current head: {status.current_head}\n"
        f"Latest prune commit: {status.latest_prune_commit or 'none'}\n"
        f"Revival grace checkpoints: {status.revival_grace_checkpoints}\n\n"
        "Pruner task:\n"
        "- Compare current active memory with memory snapshots at the boundary commit.\n"
        "- Remove active memory that is unchanged across the generation and no longer useful on the active surface.\n"
        "- Preserve and record revived memory according to the previous prune ledger and the grace policy.\n"
        "- Commit edits as `prune: expired active memory`, or make an empty `prune: checkpoint` if nothing should be removed.\n\n"
        "Previous prune ledger summary:\n"
        f"{previous}\n\n"
        "Previous prune commit body:\n"
        f"{status.previous_ledger.raw_body.strip() or '(none)'}"
    )


def _ledger_summary(ledger: PruneLedger) -> str:
    lines: list[str] = []
    if ledger.removed:
        lines.append("Removed in previous ledger:")
        lines.extend(f"- {entry.ref} | {entry.summary}".rstrip() for entry in ledger.removed)
    if ledger.grace:
        lines.append("Revival grace in previous ledger:")
        lines.extend(
            f"- {entry.ref} | grace {entry.used}/{entry.total} | {entry.summary}".rstrip()
            for entry in ledger.grace
        )
    return "\n".join(lines) if lines else "(none)"


def _git_stdout(root: Path, *args: str) -> str:
    return _run_git(root, *args).stdout.strip()


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stderr = _timeout_output(exc.stderr)
        stderr = f"{stderr}\ngit command timed out after {GIT_TIMEOUT_SECONDS} seconds".strip()
        result = subprocess.CompletedProcess(["git", *args], 124, _timeout_output(exc.stdout), stderr)

    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
