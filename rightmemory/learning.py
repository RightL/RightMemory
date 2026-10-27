from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .guidance import _git, _git_result, _require_repository_root
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory


LEARNING_DIRECTORY = "learning"
LEARNING_DOMAINS = ("guidance", "skills")
_ID_RE = re.compile(r"[0-9a-f]{64}")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class LearningCandidate:
    candidate_id: str
    session_id: str
    request_id: str | None
    submitted_at: str
    evidence: str

    @property
    def evidence_sha256(self) -> str:
        return _sha256(self.evidence)


@dataclass(frozen=True)
class LearningSubmission:
    candidate_id: str
    created: bool
    pending_domains: tuple[str, ...]


@dataclass(frozen=True)
class _Receipt:
    candidate_id: str
    session_id: str
    request_id: str | None
    evidence_sha256: str

    @classmethod
    def from_candidate(cls, candidate: LearningCandidate) -> _Receipt:
        return cls(
            candidate.candidate_id,
            candidate.session_id,
            candidate.request_id,
            candidate.evidence_sha256,
        )


@dataclass
class _LearningState:
    candidates: dict[str, LearningCandidate]
    receipts: dict[str, dict[str, _Receipt]]
    candidate_files: list[Path]
    curated_files: list[Path]
    errors: list[str]


def submit_learning(
    root: Path,
    session_id: str,
    evidence: str,
    *,
    request_id: str | None = None,
) -> LearningSubmission:
    """Capture evidence in Git without admitting it into either curated domain.

    A request id identifies one immutable submission within its session. Without
    one, identical evidence in the same session is the same submission. Retries
    remain idempotent after deliberate source cleanup because receipts retain
    the submission identity and evidence hash.
    """
    root = Path(root).resolve()
    session_id = _identifier(session_id, "session id")
    request_id = None if request_id is None else _identifier(request_id, "request id")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("learning evidence must be a non-empty string")
    evidence_hash = _sha256(evidence)
    candidate_id = _candidate_id(session_id, request_id, evidence_hash)
    expected = _Receipt(candidate_id, session_id, request_id, evidence_hash)
    _require_repository_root(root)
    _ensure_runtime_gitignore(root / ".runtime")

    with MemoryWriteLock(root):
        state = _require_state(root)
        _require_clean_identity(root, candidate_id)
        existing = _existing_submission(state, expected)
        if existing is not None:
            return existing
        start_head = _git(root, "rev-parse", "HEAD")
        identifier = uuid.uuid4().hex
        branch = f"rightmemory-learning-{identifier}"
        worktree = root / ".runtime" / "worktrees" / f"learning-{identifier}"
        relative_worktree = worktree.relative_to(root).as_posix()
        if _git_result(root, "check-ignore", "-q", relative_worktree).returncode != 0:
            raise RuntimeError(f"runtime worktree path is not ignored by git: {relative_worktree}")
        try:
            _git(root, "worktree", "add", "-b", branch, str(worktree), start_head)
            _require_state(worktree)
            candidate = LearningCandidate(
                candidate_id,
                session_id,
                request_id,
                datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                evidence,
            )
            relative = _candidate_path(candidate_id)
            _write_json(worktree, relative, _candidate_data(candidate))
            _git(worktree, "add", "-f", "--", relative)
            _git(worktree, "commit", "-m", "learning: capture evidence")
            captured = _require_state(worktree)
            changed = _git(worktree, "diff", "--name-only", "-z", start_head, "HEAD")
            if (
                set(changed.split("\0")) - {""} != {relative}
                or captured.candidates.get(candidate_id) != candidate
                or _git(worktree, "status", "--porcelain")
            ):
                raise RuntimeError("learning capture changed state outside its submitted evidence")
            _require_state(root)
            _require_clean_identity(root, candidate_id)
            if _git(root, "rev-parse", "HEAD") != start_head:
                raise RuntimeError("main HEAD changed during learning capture")
            _git(
                root,
                "merge",
                "--ff-only",
                "--no-overwrite-ignore",
                _git(worktree, "rev-parse", "HEAD"),
            )
            return LearningSubmission(candidate_id, True, LEARNING_DOMAINS)
        finally:
            _git_result(root, "worktree", "remove", "--force", str(worktree))
            _git_result(root, "branch", "-D", branch)


def list_learning_candidates(
    root: Path, *, domain: str | None = None
) -> tuple[LearningCandidate, ...]:
    if domain is not None:
        _require_domain(domain)
    state = _require_state(Path(root))
    return tuple(
        candidate
        for candidate in sorted(
            state.candidates.values(), key=lambda item: (item.submitted_at, item.candidate_id)
        )
        if domain is None or candidate.candidate_id not in state.receipts[domain]
    )


def record_learning_processed(
    root: Path, domain: str, candidate_ids: Iterable[str]
) -> tuple[Path, ...]:
    """Record a curator's completed inputs in its publication worktree.

    This performs no Git publication and never retires source evidence. Callers
    own the surrounding isolated transaction and validate the completed output
    before recording its receipts.
    """
    _require_domain(domain)
    root = Path(root)
    state = _require_state(root)
    identifiers = _candidate_ids(candidate_ids)
    prepared: list[tuple[str, _Receipt]] = []
    for candidate_id in identifiers:
        candidate = state.candidates.get(candidate_id)
        existing = state.receipts[domain].get(candidate_id)
        if candidate is None:
            if existing is None:
                raise ValueError(f"unknown learning candidate: {candidate_id}")
            continue
        receipt = _Receipt.from_candidate(candidate)
        if existing is None:
            prepared.append((_receipt_path(domain, candidate_id), receipt))
    for relative, receipt in prepared:
        _write_json(root, relative, vars(receipt))
    return tuple(root / _receipt_path(domain, candidate_id) for candidate_id in identifiers)


def can_retire_learning_candidate(
    root: Path, candidate_id: str, *, evidence_sha256: str | None = None
) -> bool:
    _require_candidate_id(candidate_id)
    state = _require_state(Path(root))
    candidate = state.candidates.get(candidate_id)
    if candidate is None:
        return False
    if evidence_sha256 is not None and candidate.evidence_sha256 != evidence_sha256:
        return False
    return _fully_processed(state, _Receipt.from_candidate(candidate))


def validate_learning_candidate_deletion(
    root: Path, relative_path: str, original_text: str
) -> None:
    """Check a source deletion against its original blob and current receipts."""
    parts = _path_parts(relative_path)
    if parts is None or not _is_candidate_path(parts):
        raise ValueError(f"not a learning candidate path: {relative_path}")
    original = _parse_candidate(_parse_json(original_text))
    if original.candidate_id != PurePosixPath(relative_path).stem:
        raise ValueError("learning candidate filename does not match its identity")
    state = _require_state(Path(root))
    if not _fully_processed(state, _Receipt.from_candidate(original)):
        raise ValueError(f"learning candidate is not processed by both domains: {original.candidate_id}")


def cleanup_processed_learning(
    root: Path, *, candidate_ids: Iterable[str] | None = None
) -> tuple[Path, ...]:
    """Deliberately retire eligible sources; callers own retention judgment and Git.

    A supplied scope is all-or-nothing for eligibility. Omitting it selects all
    fully processed sources. Receipts remain to preserve retry idempotence.
    """
    root = Path(root)
    state = _require_state(root)
    identifiers = (
        _candidate_ids(candidate_ids)
        if candidate_ids is not None
        else tuple(
            sorted(
                candidate.candidate_id
                for candidate in state.candidates.values()
                if _fully_processed(state, _Receipt.from_candidate(candidate))
            )
        )
    )
    paths: list[Path] = []
    for candidate_id in identifiers:
        candidate = state.candidates.get(candidate_id)
        if candidate is None:
            if all(candidate_id in state.receipts[domain] for domain in LEARNING_DOMAINS):
                continue
            raise ValueError(f"unknown learning candidate: {candidate_id}")
        if not _fully_processed(state, _Receipt.from_candidate(candidate)):
            raise ValueError(f"learning candidate is not processed by both domains: {candidate_id}")
        paths.append(root / _candidate_path(candidate_id))
    for path in paths:
        path.unlink()
        _fsync_directory(path.parent)
    return tuple(paths)


def learning_validation_errors(root: Path) -> list[str]:
    return _read_state(Path(root)).errors


def learning_candidate_files(root: Path) -> tuple[Path, ...]:
    return tuple(_require_state(Path(root)).candidate_files)


def learning_curated_files(root: Path) -> tuple[Path, ...]:
    """Return curated Markdown and skill assets, excluding sources and receipts."""
    return tuple(_require_state(Path(root)).curated_files)


def learning_readable_files(root: Path) -> tuple[Path, ...]:
    state = _require_state(Path(root))
    return tuple(sorted([*state.candidate_files, *state.curated_files]))


def learning_files(root: Path) -> tuple[Path, ...]:
    """Return every validated regular learning file, including internal receipts."""
    root = Path(root)
    state = _require_state(root)
    receipts = [
        root / _receipt_path(domain, candidate_id)
        for domain in LEARNING_DOMAINS
        for candidate_id in state.receipts[domain]
    ]
    return tuple(sorted([*state.candidate_files, *state.curated_files, *receipts]))


def is_learning_path(relative_path: str) -> bool:
    """Recognize canonical learning files, including internal source receipts."""
    parts = _path_parts(relative_path)
    return parts is not None and (
        _is_candidate_path(parts)
        or any(learning_write_path(domain, relative_path) for domain in LEARNING_DOMAINS)
    )


def is_learning_evidence_path(relative_path: str) -> bool:
    parts = _path_parts(relative_path)
    return parts is not None and (_is_candidate_path(parts) or _is_receipt_path(parts))


def merge_learning_evidence(relative_path: str, *versions: str | bytes) -> str:
    """Reconcile copies of immutable evidence, never infer or rewrite its content."""
    parts = _path_parts(relative_path)
    if not versions or parts is None or not is_learning_evidence_path(relative_path):
        raise ValueError("learning evidence merge requires a canonical path and source versions")
    if _is_candidate_path(parts):
        candidates = [_parse_candidate(_parse_json(text)) for text in versions]
        identities = [_Receipt.from_candidate(candidate) for candidate in candidates]
        if any(identity != identities[0] for identity in identities):
            raise ValueError("conflicting learning evidence for the same submission identity")
        selected = min(
            candidates,
            key=lambda candidate: (
                datetime.fromisoformat(candidate.submitted_at[:-1] + "+00:00"),
                candidate.submitted_at,
            ),
        )
        data = _candidate_data(selected)
    else:
        receipts = [_parse_receipt(_parse_json(text)) for text in versions]
        if any(receipt != receipts[0] for receipt in receipts):
            raise ValueError("conflicting processed receipts for the same submission identity")
        data = vars(receipts[0])
    if data["candidate_id"] != PurePosixPath(relative_path).stem:
        raise ValueError("learning evidence filename does not match its identity")
    return _render_json(data)


def learning_evidence_sha256(root: Path) -> str:
    """Bind raw sources and program-owned receipts across a curated-content repair."""
    root = Path(root)
    entries = [
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in learning_files(root)
        if is_learning_evidence_path(path.relative_to(root).as_posix())
    ]
    return _sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def learning_write_path(domain: str, relative_path: str) -> bool:
    """Recognize domain-owned content and receipts, never raw candidate edits."""
    _require_domain(domain)
    parts = _path_parts(relative_path)
    if parts is None or len(parts) < 3 or parts[0] != LEARNING_DIRECTORY:
        return False
    if _is_receipt_path(parts):
        return parts[2] == domain
    if domain == "guidance":
        return parts[1] == "guidance" and parts[-1].endswith(".md")
    return (parts[1] == "experiences" and parts[-1].endswith(".md")) or (
        parts[1] == "skills" and len(parts) >= 4 and _SLUG_RE.fullmatch(parts[2]) is not None
    )


def _read_state(root: Path) -> _LearningState:
    state = _LearningState({}, {domain: {} for domain in LEARNING_DOMAINS}, [], [], [])
    learning_root = root / LEARNING_DIRECTORY
    if not learning_root.exists() and not learning_root.is_symlink():
        return state
    skill_directories: set[str] = set()

    def visit(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        parts = _path_parts(relative)
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("must not be a symbolic link")
            if stat.S_ISDIR(mode):
                if parts is None or not _valid_directory(parts):
                    raise ValueError("not a learning directory")
                if len(parts) == 3 and parts[1] == "skills":
                    skill_directories.add(parts[2])
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    visit(child)
                return
            if not stat.S_ISREG(mode):
                raise ValueError("must be a regular file")
            if parts is None:
                raise ValueError("invalid learning path")
            if _is_candidate_path(parts):
                candidate = _parse_candidate(_parse_json(path.read_text(encoding="utf-8")))
                if candidate.candidate_id != path.stem:
                    raise ValueError("filename does not match candidate identity")
                state.candidates[candidate.candidate_id] = candidate
                state.candidate_files.append(path)
            elif _is_receipt_path(parts):
                receipt = _parse_receipt(_parse_json(path.read_text(encoding="utf-8")))
                if receipt.candidate_id != path.stem:
                    raise ValueError("filename does not match receipt identity")
                state.receipts[parts[2]][receipt.candidate_id] = receipt
            elif any(learning_write_path(domain, relative) for domain in LEARNING_DOMAINS):
                if path.suffix.lower() == ".md":
                    path.read_text(encoding="utf-8")
                state.curated_files.append(path)
            else:
                raise ValueError("not a learning content path")
        except (OSError, UnicodeError, ValueError) as exc:
            state.errors.append(f"{relative}: {exc}")

    visit(learning_root)
    for slug in sorted(skill_directories):
        path = root / LEARNING_DIRECTORY / "skills" / slug / "SKILL.md"
        if path not in state.curated_files:
            state.errors.append(f"learning/skills/{slug}: missing regular SKILL.md")
    all_receipt_ids = set().union(*(receipts.keys() for receipts in state.receipts.values()))
    for candidate_id in sorted(all_receipt_ids):
        receipts = [
            state.receipts[domain][candidate_id]
            for domain in LEARNING_DOMAINS
            if candidate_id in state.receipts[domain]
        ]
        candidate = state.candidates.get(candidate_id)
        expected = _Receipt.from_candidate(candidate) if candidate is not None else receipts[0]
        if any(receipt != expected for receipt in receipts):
            state.errors.append(f"learning/processed: receipts do not match candidate {candidate_id}")
        if candidate is None and len(receipts) != len(LEARNING_DOMAINS):
            state.errors.append(f"learning/candidates/{candidate_id}.json: missing unprocessed evidence")
    return state


def _require_state(root: Path) -> _LearningState:
    state = _read_state(root)
    if state.errors:
        raise ValueError("invalid learning store:\n- " + "\n- ".join(state.errors))
    return state


def _parse_candidate(data: object) -> LearningCandidate:
    fields = {"candidate_id", "session_id", "request_id", "submitted_at", "evidence", "evidence_sha256"}
    _require_fields(data, fields, "learning candidate")
    assert isinstance(data, dict)
    identity = _parse_receipt({name: data[name] for name in _Receipt.__dataclass_fields__})
    evidence = data["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence must be a non-empty string")
    if _sha256(evidence) != identity.evidence_sha256:
        raise ValueError("evidence hash does not match candidate content")
    submitted_at = data["submitted_at"]
    if not isinstance(submitted_at, str) or not submitted_at.endswith("Z"):
        raise ValueError("submitted_at must be a UTC timestamp")
    try:
        parsed_time = datetime.fromisoformat(submitted_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("submitted_at must be a UTC timestamp") from exc
    if parsed_time.utcoffset() != UTC.utcoffset(parsed_time):
        raise ValueError("submitted_at must be a UTC timestamp")
    return LearningCandidate(
        identity.candidate_id,
        identity.session_id,
        identity.request_id,
        submitted_at,
        evidence,
    )


def _parse_receipt(data: object) -> _Receipt:
    _require_fields(data, set(_Receipt.__dataclass_fields__), "learning receipt")
    assert isinstance(data, dict)
    candidate_id = data["candidate_id"]
    _require_candidate_id(candidate_id)
    session_id = _identifier(data["session_id"], "session id")
    if session_id != data["session_id"]:
        raise ValueError("session id must not have surrounding whitespace")
    request_id = data["request_id"]
    if request_id is not None and _identifier(request_id, "request id") != request_id:
        raise ValueError("request id must not have surrounding whitespace")
    evidence_hash = data["evidence_sha256"]
    if not isinstance(evidence_hash, str) or _ID_RE.fullmatch(evidence_hash) is None:
        raise ValueError("evidence_sha256 must be a SHA-256 hash")
    if candidate_id != _candidate_id(session_id, request_id, evidence_hash):
        raise ValueError("candidate id does not match its submission identity")
    return _Receipt(candidate_id, session_id, request_id, evidence_hash)


def _candidate_data(candidate: LearningCandidate) -> dict[str, object]:
    return {**vars(candidate), "evidence_sha256": candidate.evidence_sha256}


def _candidate_id(session_id: str, request_id: str | None, evidence_hash: str) -> str:
    identity = (
        [session_id, "request", request_id]
        if request_id is not None
        else [session_id, "evidence", evidence_hash]
    )
    return _sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in "\x00\r\n"):
        raise ValueError(f"{label} must be a non-empty single line")
    return value.strip()


def _require_domain(domain: str) -> None:
    if domain not in LEARNING_DOMAINS:
        raise ValueError(f"unknown learning domain: {domain}")


def _require_candidate_id(candidate_id: str) -> None:
    if not isinstance(candidate_id, str) or _ID_RE.fullmatch(candidate_id) is None:
        raise ValueError("learning candidate id must be a SHA-256 hash")


def _candidate_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("candidate_ids must be a collection of candidate ids")
    result = tuple(dict.fromkeys(values))
    for value in result:
        _require_candidate_id(value)
    return result


def _candidate_path(candidate_id: str) -> str:
    return f"learning/candidates/{candidate_id}.json"


def _receipt_path(domain: str, candidate_id: str) -> str:
    return f"learning/processed/{domain}/{candidate_id}.json"


def _fully_processed(state: _LearningState, receipt: _Receipt) -> bool:
    return all(state.receipts[domain].get(receipt.candidate_id) == receipt for domain in LEARNING_DOMAINS)


def _existing_submission(state: _LearningState, expected: _Receipt) -> LearningSubmission | None:
    candidate_id = expected.candidate_id
    candidate = state.candidates.get(candidate_id)
    actual = (
        _Receipt.from_candidate(candidate)
        if candidate is not None
        else next(
            (
                state.receipts[domain][candidate_id]
                for domain in LEARNING_DOMAINS
                if candidate_id in state.receipts[domain]
            ),
            None,
        )
    )
    if actual is None:
        return None
    if actual != expected:
        raise ValueError("learning request id was already submitted with different evidence")
    return LearningSubmission(
        candidate_id,
        False,
        tuple(domain for domain in LEARNING_DOMAINS if candidate_id not in state.receipts[domain]),
    )


def _require_clean_identity(root: Path, candidate_id: str) -> None:
    paths = [
        _candidate_path(candidate_id),
        *(_receipt_path(domain, candidate_id) for domain in LEARNING_DOMAINS),
    ]
    changed = _git(root, "diff", "--name-only", "-z", "HEAD", "--", *paths)
    untracked = _git(root, "ls-files", "--others", "-z", "--", *paths)
    if changed or untracked:
        raise RuntimeError(f"learning submission has uncommitted changes: {candidate_id}")


def _path_parts(relative: str) -> tuple[str, ...] | None:
    if not isinstance(relative, str) or any(char in relative for char in "\\:\x00\r\n"):
        return None
    parts = tuple(relative.split("/"))
    if not parts or any(part in {"", ".", ".."} or part.casefold() == ".git" for part in parts):
        return None
    if PurePosixPath(relative).is_absolute():
        return None
    return parts


def _is_candidate_path(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 3
        and parts[:2] == ("learning", "candidates")
        and parts[2].endswith(".json")
        and _ID_RE.fullmatch(parts[2][:-5]) is not None
    )


def _is_receipt_path(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 4
        and parts[:2] == ("learning", "processed")
        and parts[2] in LEARNING_DOMAINS
        and parts[3].endswith(".json")
        and _ID_RE.fullmatch(parts[3][:-5]) is not None
    )


def _valid_directory(parts: tuple[str, ...]) -> bool:
    if parts == ("learning",):
        return True
    if len(parts) < 2 or parts[0] != "learning":
        return False
    kind = parts[1]
    if kind == "candidates":
        return len(parts) == 2
    if kind == "processed":
        return len(parts) == 2 or (len(parts) == 3 and parts[2] in LEARNING_DOMAINS)
    if kind in {"guidance", "experiences"}:
        return True
    if kind == "skills":
        return len(parts) == 2 or _SLUG_RE.fullmatch(parts[2]) is not None
    return False


def _require_fields(data: object, fields: set[str], label: str) -> None:
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError(f"{label} has invalid fields")


def _parse_json(text: str | bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _render_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _write_json(root: Path, relative: str, data: dict[str, object]) -> None:
    path = root / relative
    parent = root
    for segment in PurePosixPath(relative).parts[:-1]:
        parent /= segment
        parent.mkdir(exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError(f"learning directory must be a regular directory: {parent}")
        _fsync_directory(parent.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"learning content must be a regular file: {path}")
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_render_json(data).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
