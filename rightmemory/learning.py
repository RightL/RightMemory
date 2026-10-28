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

from .git_storage import git as _git, git_result as _git_result, require_repository_root as _require_repository_root
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory


LEARNING_DIRECTORY = "learning"
LEARNING_DOMAINS = ("memory", "guidance", "skills")
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
    status: str


@dataclass(frozen=True)
class LearningClaim:
    domain: str
    holder: str
    token: str
    task_id: str
    base_commit: str
    candidate_ids: tuple[str, ...]
    candidate_hashes: tuple[str, ...]
    mode: str = "update"
    scope: str = ""


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
    withdrawn: dict[str, _Receipt]
    claims: dict[str, LearningClaim]
    batches: dict[str, tuple[str, ...]]
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
    """Capture shared evidence in Git without admitting it into curated state.

    A request id identifies one immutable submission within its session. Without
    one, identical evidence in the same session is the same submission. Retries
    remain idempotent after completion or withdrawal because receipts retain
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
    for directory in (root / ".runtime", root / ".runtime" / "worktrees"):
        if directory.is_symlink():
            raise ValueError("learning runtime directory must not be a symbolic link")
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
            return LearningSubmission(candidate_id, True, LEARNING_DOMAINS, "pending")
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
        if candidate.candidate_id not in state.withdrawn
        and not _fully_processed(state, _Receipt.from_candidate(candidate))
        and (domain is None or candidate.candidate_id not in state.receipts[domain])
    )


def record_learning_processed(
    root: Path, domain: str, candidate_ids: Iterable[str]
) -> tuple[Path, ...]:
    """Record completion and retire fully processed sources in one transaction.

    The coordinator owns publication with the validated semantic result. Returned
    paths include added receipts and deleted sources; none are model-editable.
    """
    _require_domain(domain)
    root = Path(root)
    state = _require_state(root)
    identifiers = _candidate_ids(candidate_ids)
    active = state.claims.get(domain)
    if active is not None and set(identifiers).intersection(active.candidate_ids):
        raise ValueError("release the verified domain claim before recording its completion")
    prepared: list[tuple[str, _Receipt]] = []
    for candidate_id in identifiers:
        if candidate_id in state.withdrawn:
            raise ValueError(f"learning candidate was withdrawn: {candidate_id}")
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
    return tuple(root / relative for relative, _receipt in prepared) + retire_processed_learning(root)


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
    identity = _Receipt.from_candidate(original)
    if not _fully_processed(state, identity) and state.withdrawn.get(original.candidate_id) != identity:
        raise ValueError(f"learning candidate is neither processed by all domains nor withdrawn: {original.candidate_id}")


def retire_processed_learning(root: Path) -> tuple[Path, ...]:
    """Normalize active evidence after completion or sync; keep identity receipts."""
    root = Path(root)
    state = _require_state(root)
    paths: list[Path] = []
    for candidate_id, candidate in sorted(state.candidates.items()):
        if _fully_processed(state, _Receipt.from_candidate(candidate)) or candidate_id in state.withdrawn:
            paths.append(root / _candidate_path(candidate_id))
    for domain, identifiers in sorted(state.batches.items()):
        if all(item in state.receipts[domain] or item in state.withdrawn for item in identifiers):
            paths.append(root / _batch_path(domain))
    for path in paths:
        path.unlink()
        _fsync_directory(path.parent)
    return tuple(paths)


def withdraw_learning_candidate(root: Path, candidate_id: str) -> tuple[Path, ...]:
    """Withdraw unclaimed evidence; the caller owns the Git transaction."""
    _require_candidate_id(candidate_id)
    root = Path(root)
    state = _require_state(root)
    if any(candidate_id in claim.candidate_ids for claim in state.claims.values()):
        raise ValueError("learning candidate belongs to an active curator task")
    if candidate_id in state.withdrawn:
        return retire_processed_learning(root)
    candidate = state.candidates.get(candidate_id)
    if candidate is None:
        if any(candidate_id in receipts for receipts in state.receipts.values()):
            return ()
        raise ValueError(f"unknown learning candidate: {candidate_id}")
    relative = _withdrawn_path(candidate_id)
    _write_json(root, relative, vars(_Receipt.from_candidate(candidate)))
    return (root / relative, *retire_processed_learning(root))


def learning_claims(root: Path) -> tuple[LearningClaim, ...]:
    return tuple(_require_state(Path(root)).claims.values())


def learning_batch_candidate_ids(root: Path, domain: str) -> tuple[str, ...]:
    _require_domain(domain)
    return _require_state(Path(root)).batches.get(domain, ())


def write_learning_batch(root: Path, domain: str, candidate_ids: Iterable[str]) -> Path:
    """Record a trigger's full admitted pool in the caller's Git transaction."""
    _require_domain(domain)
    root = Path(root)
    identifiers = _candidate_ids(candidate_ids)
    if not identifiers:
        raise ValueError("learning batch must admit candidate evidence")
    state = _require_state(root)
    existing = state.batches.get(domain)
    if existing is not None and existing != identifiers and any(
        item not in state.receipts[domain] and item not in state.withdrawn for item in existing
    ):
        raise ValueError("learning domain already has an admitted batch")
    if any(item not in state.candidates or item in state.withdrawn or item in state.receipts[domain]
           for item in identifiers) and existing != identifiers:
        raise ValueError("learning batch references unavailable pending evidence")
    path = _batch_path(domain)
    _write_json(root, path, {"domain": domain, "candidate_ids": identifiers})
    return root / path


def write_learning_claim(root: Path, claim: LearningClaim) -> Path:
    root = Path(root)
    claim = parse_learning_claim(vars(claim))
    state = _require_state(root)
    existing = state.claims.get(claim.domain)
    if existing is not None and existing != claim:
        raise ValueError("learning domain already has an active claim")
    for candidate_id, evidence_hash in zip(claim.candidate_ids, claim.candidate_hashes):
        if candidate_id not in state.candidates or candidate_id in state.withdrawn:
            raise ValueError(f"claim references unavailable candidate: {candidate_id}")
        if candidate_id in state.receipts[claim.domain]:
            raise ValueError(f"claim references already processed candidate: {candidate_id}")
        if state.candidates[candidate_id].evidence_sha256 != evidence_hash:
            raise ValueError(f"claim evidence hash does not match candidate: {candidate_id}")
    relative = _claim_path(claim.domain)
    _write_json(root, relative, vars(claim))
    return root / relative


def remove_learning_claim(root: Path, claim: LearningClaim) -> Path:
    root = Path(root)
    state = _require_state(root)
    if state.claims.get(claim.domain) != claim:
        raise ValueError("learning claim is no longer current")
    path = root / _claim_path(claim.domain)
    path.unlink()
    _fsync_directory(path.parent)
    return path


def learning_validation_errors(root: Path) -> list[str]:
    return _read_state(Path(root)).errors


def learning_candidate_files(root: Path) -> tuple[Path, ...]:
    root = Path(root)
    return tuple(root / _candidate_path(candidate.candidate_id) for candidate in list_learning_candidates(root))


def learning_curated_files(root: Path) -> tuple[Path, ...]:
    """Return curated Markdown and skill assets, excluding sources and receipts."""
    return tuple(_require_state(Path(root)).curated_files)


def learning_readable_files(root: Path) -> tuple[Path, ...]:
    state = _require_state(Path(root))
    return tuple(sorted([*learning_candidate_files(root), *state.curated_files]))


def learning_files(root: Path) -> tuple[Path, ...]:
    """Return every validated regular learning file, including internal state."""
    root = Path(root)
    state = _require_state(root)
    receipts = [
        root / _receipt_path(domain, candidate_id)
        for domain in LEARNING_DOMAINS
        for candidate_id in state.receipts[domain]
    ]
    internal = [
        *(root / _withdrawn_path(candidate_id) for candidate_id in state.withdrawn),
        *(root / _claim_path(domain) for domain in state.claims),
        *(root / _batch_path(domain) for domain in state.batches),
    ]
    return tuple(sorted([*state.candidate_files, *state.curated_files, *receipts, *internal]))


def is_learning_path(relative_path: str) -> bool:
    """Recognize canonical learning files, including internal source receipts."""
    parts = _path_parts(relative_path)
    return parts is not None and (
        _is_candidate_path(parts)
        or _is_receipt_path(parts)
        or _is_withdrawn_path(parts)
        or _is_claim_path(parts)
        or _is_batch_path(parts)
        or any(learning_write_path(domain, relative_path) for domain in LEARNING_DOMAINS)
    )


def is_learning_evidence_path(relative_path: str) -> bool:
    parts = _path_parts(relative_path)
    return parts is not None and (
        _is_candidate_path(parts) or _is_receipt_path(parts) or _is_withdrawn_path(parts)
    )


def is_learning_state_path(relative_path: str) -> bool:
    """Program-owned evidence, receipts, claims and admissions; never model output."""
    parts = _path_parts(relative_path)
    return is_learning_evidence_path(relative_path) or (
        parts is not None and (_is_claim_path(parts) or _is_batch_path(parts))
    )


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
        if is_learning_state_path(path.relative_to(root).as_posix())
    ]
    return _sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def learning_write_path(domain: str, relative_path: str) -> bool:
    """Recognize curator-owned content; all candidate lifecycle files are program-owned."""
    _require_domain(domain)
    parts = _path_parts(relative_path)
    if parts is None or len(parts) < 3 or parts[0] != LEARNING_DIRECTORY:
        return False
    if domain == "guidance":
        return parts[1] == "guidance" and parts[-1].endswith(".md")
    if domain == "memory":
        return False
    return (parts[1] == "experiences" and parts[-1].endswith(".md")) or (
        parts[1] == "skills" and len(parts) >= 4 and _SLUG_RE.fullmatch(parts[2]) is not None
    )


def _read_state(root: Path) -> _LearningState:
    state = _LearningState({}, {domain: {} for domain in LEARNING_DOMAINS}, {}, {}, {}, [], [], [])
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
            elif _is_withdrawn_path(parts):
                receipt = _parse_receipt(_parse_json(path.read_text(encoding="utf-8")))
                if receipt.candidate_id != path.stem:
                    raise ValueError("filename does not match withdrawal identity")
                state.withdrawn[receipt.candidate_id] = receipt
            elif _is_claim_path(parts):
                claim = parse_learning_claim(_parse_json(path.read_text(encoding="utf-8")))
                if claim.domain != path.stem:
                    raise ValueError("filename does not match claim domain")
                state.claims[claim.domain] = claim
            elif _is_batch_path(parts):
                data = _parse_json(path.read_text(encoding="utf-8"))
                _require_fields(data, {"domain", "candidate_ids"}, "learning batch")
                _require_domain(data["domain"])
                if data["domain"] != path.stem:
                    raise ValueError("filename does not match batch domain")
                raw_ids = data["candidate_ids"]
                if not isinstance(raw_ids, list):
                    raise ValueError("learning batch candidate ids must be a list")
                identifiers = _candidate_ids(raw_ids)
                if not identifiers or len(identifiers) != len(raw_ids):
                    raise ValueError("learning batch candidate ids must be non-empty and unique")
                state.batches[data["domain"]] = identifiers
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
    all_receipt_ids = set(state.withdrawn).union(*(receipts.keys() for receipts in state.receipts.values()))
    for candidate_id in sorted(all_receipt_ids):
        receipts = [
            state.receipts[domain][candidate_id]
            for domain in LEARNING_DOMAINS
            if candidate_id in state.receipts[domain]
        ]
        withdrawal = state.withdrawn.get(candidate_id)
        identities = receipts + ([] if withdrawal is None else [withdrawal])
        candidate = state.candidates.get(candidate_id)
        expected = _Receipt.from_candidate(candidate) if candidate is not None else identities[0]
        if any(receipt != expected for receipt in identities):
            state.errors.append(f"learning/processed: receipts do not match candidate {candidate_id}")
        if candidate is None and withdrawal is None and len(receipts) != len(LEARNING_DOMAINS):
            state.errors.append(f"learning/candidates/{candidate_id}.json: missing unprocessed evidence")
    known_ids = set(state.candidates) | all_receipt_ids
    for domain, identifiers in state.batches.items():
        for candidate_id in identifiers:
            if candidate_id not in known_ids:
                state.errors.append(f"learning/batches/{domain}.json: unknown candidate {candidate_id}")
    for domain, claim in state.claims.items():
        if claim.mode == "update" and domain in state.batches and not set(claim.candidate_ids).issubset(state.batches[domain]):
            state.errors.append(f"learning/claims/{domain}.json: evidence is outside admitted batch")
        for candidate_id, evidence_hash in zip(claim.candidate_ids, claim.candidate_hashes):
            if candidate_id not in state.candidates or candidate_id in state.withdrawn:
                state.errors.append(f"learning/claims/{domain}.json: unavailable candidate {candidate_id}")
            elif candidate_id in state.receipts[domain]:
                state.errors.append(f"learning/claims/{domain}.json: candidate already processed {candidate_id}")
            elif state.candidates[candidate_id].evidence_sha256 != evidence_hash:
                state.errors.append(f"learning/claims/{domain}.json: candidate evidence changed {candidate_id}")
    return state


def parse_learning_claim(data: object) -> LearningClaim:
    _require_fields(data, set(LearningClaim.__dataclass_fields__), "learning claim")
    assert isinstance(data, dict)
    domain = data["domain"]
    _require_domain(domain)
    for name in ("holder", "token"):
        if not isinstance(data[name], str) or re.fullmatch(r"[0-9a-f]{32}", data[name]) is None:
            raise ValueError(f"invalid learning claim {name}")
    task_id = data["task_id"]
    if not isinstance(task_id, str) or re.fullmatch(rf"{domain}-[0-9a-f]{{32}}", task_id) is None:
        raise ValueError("invalid learning claim task id")
    base = data["base_commit"]
    if not isinstance(base, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base) is None:
        raise ValueError("invalid learning claim base commit")
    mode = data["mode"]
    if not isinstance(mode, str) or mode not in {"update", "maintain"} or not isinstance(data["scope"], str):
        raise ValueError("invalid learning claim task scope")
    ids = data["candidate_ids"]
    if not isinstance(ids, (list, tuple)):
        raise ValueError("learning claim candidate ids must be a list")
    ids = _candidate_ids(ids)
    if len(ids) != len(data["candidate_ids"]):
        raise ValueError("learning claim candidate ids must be unique")
    if (mode == "update") != bool(ids):
        raise ValueError("only candidate updates claim a non-empty evidence batch")
    hashes = data["candidate_hashes"]
    if not isinstance(hashes, (tuple, list)) or len(hashes) != len(ids):
        raise ValueError("learning claim evidence hashes must match its candidates")
    if any(not isinstance(value, str) or _ID_RE.fullmatch(value) is None for value in hashes):
        raise ValueError("invalid learning claim evidence hash")
    return LearningClaim(domain, data["holder"], data["token"], task_id, base, ids, tuple(hashes), mode, data["scope"])


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
    result = tuple(values)
    for value in result:
        _require_candidate_id(value)
    return tuple(dict.fromkeys(result))


def _candidate_path(candidate_id: str) -> str:
    return f"learning/candidates/{candidate_id}.json"


def _receipt_path(domain: str, candidate_id: str) -> str:
    return f"learning/processed/{domain}/{candidate_id}.json"


def _withdrawn_path(candidate_id: str) -> str:
    return f"learning/withdrawn/{candidate_id}.json"


def _claim_path(domain: str) -> str:
    return f"learning/claims/{domain}.json"


def _batch_path(domain: str) -> str:
    return f"learning/batches/{domain}.json"


def _fully_processed(state: _LearningState, receipt: _Receipt) -> bool:
    return all(state.receipts[domain].get(receipt.candidate_id) == receipt for domain in LEARNING_DOMAINS)


def _existing_submission(state: _LearningState, expected: _Receipt) -> LearningSubmission | None:
    candidate_id = expected.candidate_id
    candidate = state.candidates.get(candidate_id)
    actual = (
        _Receipt.from_candidate(candidate)
        if candidate is not None
        else state.withdrawn.get(candidate_id) or next(
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
    pending = () if candidate_id in state.withdrawn else tuple(
        domain for domain in LEARNING_DOMAINS if candidate_id not in state.receipts[domain]
    )
    status = "withdrawn" if candidate_id in state.withdrawn else ("pending" if pending else "completed")
    return LearningSubmission(candidate_id, False, pending, status)


def _require_clean_identity(root: Path, candidate_id: str) -> None:
    paths = [
        _candidate_path(candidate_id),
        _withdrawn_path(candidate_id),
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


def _is_withdrawn_path(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 3 and parts[:2] == ("learning", "withdrawn")
        and parts[2].endswith(".json") and _ID_RE.fullmatch(parts[2][:-5]) is not None
    )


def _is_claim_path(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 3 and parts[:2] == ("learning", "claims")
        and parts[2].endswith(".json") and parts[2][:-5] in LEARNING_DOMAINS
    )


def _is_batch_path(parts: tuple[str, ...]) -> bool:
    return (
        len(parts) == 3 and parts[:2] == ("learning", "batches")
        and parts[2].endswith(".json") and parts[2][:-5] in LEARNING_DOMAINS
    )


def _valid_directory(parts: tuple[str, ...]) -> bool:
    if parts == ("learning",):
        return True
    if len(parts) < 2 or parts[0] != "learning":
        return False
    kind = parts[1]
    if kind in {"candidates", "withdrawn", "claims", "batches"}:
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
