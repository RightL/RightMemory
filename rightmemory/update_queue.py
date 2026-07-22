from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypeVar

from .platform import process_exists
from .session import _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory


SCHEMA_VERSION = 1
QUEUE_DIRECTORY = "update_queue"
CANDIDATES_DIRECTORY = "candidates"
RECOVERY_DIRECTORY = "recovery"
LEASE_FILENAME = "lease.json"
LOCAL_OUTBOX_DIRECTORY = Path(".runtime") / "async" / "update" / "outbox"
LOCAL_PUBLICATION_DIRECTORY = Path(".runtime") / "async" / "update" / "publication"

_UID_RE = re.compile(r"^[0-9a-f]{32}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BATCH_ID_RE = re.compile(r"^update-batch-[0-9a-f]{64}$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OUTBOX_RECORD_RE = re.compile(r"^(?P<uid>[0-9a-f]{32})\.json$")
_OUTBOX_TEMP_RE = re.compile(
    r"^\.(?:[0-9a-f]{32}\.json\.)?(?P<pid>[1-9][0-9]*)\.[0-9a-f]{32}\.tmp$"
)
_Record = TypeVar("_Record")


class UpdateQueueFormatError(ValueError):
    """Raised when runtime-owned update queue state violates its wire format."""


@dataclass(frozen=True)
class UpdateCandidate:
    uid: str
    session_id: str
    display_id: int
    message: str
    submitted_at: str

    def __post_init__(self) -> None:
        _require_uid(self.uid, "candidate uid")
        _require_session_id(self.session_id)
        _require_positive_int(self.display_id, "candidate display_id")
        _require_nonempty_string(self.message, "candidate message")
        _require_utc_time(self.submitted_at, "candidate submitted_at")


@dataclass(frozen=True)
class UpdateQueueLease:
    holder: str
    token: str
    base_commit: str
    batch_id: str
    candidate_uids: tuple[str, ...]
    expires_at: str

    def __post_init__(self) -> None:
        _require_uid(self.holder, "lease holder")
        _require_uid(self.token, "lease token")
        _require_oid(self.base_commit, "lease base_commit")
        _require_batch_id(self.batch_id)
        object.__setattr__(
            self,
            "candidate_uids",
            _require_uid_tuple(self.candidate_uids, "lease candidate_uids"),
        )
        _require_utc_time(self.expires_at, "lease expires_at")


@dataclass(frozen=True)
class UpdateQueueRecovery:
    batch_id: str
    candidate_uids: tuple[str, ...]
    attempts: int
    reason_code: str
    retry_at: str | None
    manual_recovery: bool = False

    def __post_init__(self) -> None:
        _require_batch_id(self.batch_id)
        object.__setattr__(
            self,
            "candidate_uids",
            _require_uid_tuple(self.candidate_uids, "recovery candidate_uids"),
        )
        _require_nonnegative_int(self.attempts, "recovery attempts")
        if not isinstance(self.reason_code, str) or not _REASON_CODE_RE.fullmatch(self.reason_code):
            raise UpdateQueueFormatError(
                "recovery reason_code must be a lowercase machine-readable code"
            )
        if type(self.manual_recovery) is not bool:
            raise UpdateQueueFormatError("recovery manual_recovery must be a boolean")
        if self.manual_recovery:
            if self.retry_at is not None:
                raise UpdateQueueFormatError("manual recovery must not contain retry_at")
        else:
            if self.retry_at is None:
                raise UpdateQueueFormatError("automatic recovery requires retry_at")
            _require_utc_time(self.retry_at, "recovery retry_at")


@dataclass(frozen=True)
class PublicationMarker:
    candidate_uid: str
    attempt_id: str
    attempted_at: str
    candidate_sha256: str
    proposed_commit: str | None = None

    def __post_init__(self) -> None:
        _require_uid(self.candidate_uid, "publication candidate_uid")
        _require_uid(self.attempt_id, "publication attempt_id")
        _require_utc_time(self.attempted_at, "publication attempted_at")
        _require_sha256(self.candidate_sha256, "publication candidate_sha256")
        if self.proposed_commit is not None:
            _require_oid(self.proposed_commit, "publication proposed_commit")


@dataclass(frozen=True)
class UpdateQueueSnapshot:
    candidates: tuple[UpdateCandidate, ...] = ()
    lease: UpdateQueueLease | None = None
    recoveries: tuple[UpdateQueueRecovery, ...] = ()


PublicationState = Literal["missing", "never_attempted", "attempted"]


class UpdateQueueStore:
    """Filesystem access for tracked queue state and the ignored local outbox."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.queue_root = self.memory_root / QUEUE_DIRECTORY
        self.candidates_root = self.queue_root / CANDIDATES_DIRECTORY
        self.recovery_root = self.queue_root / RECOVERY_DIRECTORY
        self.lease_path = self.queue_root / LEASE_FILENAME
        self.outbox_root = self.memory_root / LOCAL_OUTBOX_DIRECTORY
        self.publication_root = self.memory_root / LOCAL_PUBLICATION_DIRECTORY

    def snapshot(self) -> UpdateQueueSnapshot:
        snapshot, errors = _inspect_update_queue(self.memory_root)
        if errors:
            raise UpdateQueueFormatError("\n".join(errors))
        return snapshot

    def candidate_path(self, uid: str) -> Path:
        clean_uid = _require_uid(uid, "candidate uid")
        return self.candidates_root / f"{clean_uid}.json"

    def recovery_path(self, batch_id: str) -> Path:
        clean_batch_id = _require_batch_id(batch_id)
        return self.recovery_root / f"{clean_batch_id}.json"

    def write_candidate(self, candidate: UpdateCandidate) -> Path:
        path = self.candidate_path(candidate.uid)
        _write_immutable(path, _candidate_json(candidate), _parse_candidate)
        return path

    def read_candidate(self, uid: str) -> UpdateCandidate | None:
        clean_uid = _require_uid(uid, "candidate uid")
        path = self.candidate_path(clean_uid)
        if not path.exists() and not path.is_symlink():
            return None
        candidate = _read_record(path, _parse_candidate)
        if candidate.uid != clean_uid:
            raise UpdateQueueFormatError(
                f"tracked candidate filename does not match candidate uid: {path}"
            )
        return candidate

    def remove_candidate(self, uid: str) -> None:
        _unlink(self.candidate_path(uid))

    def write_lease(self, lease: UpdateQueueLease) -> Path:
        _write_json(self.lease_path, _lease_json(lease))
        return self.lease_path

    def remove_lease(self) -> None:
        _unlink(self.lease_path)

    def write_recovery(self, recovery: UpdateQueueRecovery) -> Path:
        path = self.recovery_path(recovery.batch_id)
        if path.exists() or path.is_symlink():
            existing = _read_record(path, _parse_recovery)
            if existing.candidate_uids != recovery.candidate_uids:
                raise UpdateQueueFormatError(
                    "recovery candidate_uids cannot change for an existing batch"
                )
        _write_json(path, _recovery_json(recovery))
        return path

    def remove_recovery(self, batch_id: str) -> None:
        _unlink(self.recovery_path(batch_id))

    def write_outbox(self, candidate: UpdateCandidate) -> Path:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        path = self.outbox_root / f"{candidate.uid}.json"
        _write_immutable(path, _candidate_json(candidate), _parse_candidate)
        return path

    def read_outbox(self, uid: str) -> UpdateCandidate | None:
        clean_uid = _require_uid(uid, "candidate uid")
        path = self.outbox_root / f"{clean_uid}.json"
        if not path.exists() and not path.is_symlink():
            return None
        candidate = _read_record(path, _parse_candidate)
        if candidate.uid != clean_uid:
            raise UpdateQueueFormatError(
                f"local outbox filename does not match candidate uid: {path}"
            )
        return candidate

    def outbox_candidates(self) -> tuple[UpdateCandidate, ...]:
        if not self.outbox_root.exists() and not self.outbox_root.is_symlink():
            return ()
        if self.outbox_root.is_symlink() or not self.outbox_root.is_dir():
            raise UpdateQueueFormatError(
                f"local outbox must be a directory: {self.outbox_root}"
            )
        candidates: list[UpdateCandidate] = []
        for path in sorted(self.outbox_root.iterdir(), key=lambda item: item.name):
            temporary = _OUTBOX_TEMP_RE.fullmatch(path.name)
            if temporary is not None:
                if path.is_symlink() or not path.is_file():
                    raise UpdateQueueFormatError(
                        f"local outbox temporary artifact must be a regular file: {path}"
                    )
                if not process_exists(int(temporary.group("pid"))):
                    _unlink(path)
                continue
            if _OUTBOX_RECORD_RE.fullmatch(path.name) is None:
                raise UpdateQueueFormatError(f"unexpected local outbox path: {path}")
            candidate = _read_record(path, _parse_candidate)
            if path.name != f"{candidate.uid}.json":
                raise UpdateQueueFormatError(
                    f"local outbox filename does not match candidate uid: {path}"
                )
            candidates.append(candidate)
        return tuple(sorted(candidates, key=lambda item: item.uid))

    def begin_publication(
        self,
        uid: str,
        *,
        attempted_at: str,
        attempt_id: str | None = None,
    ) -> PublicationMarker:
        candidate = self.read_outbox(uid)
        if candidate is None:
            raise FileNotFoundError(f"local candidate does not exist: {uid}")
        path = self.publication_root / f"{candidate.uid}.json"
        if path.exists() or path.is_symlink():
            marker = self.read_publication_marker(candidate.uid)
            if marker is None:
                raise FileNotFoundError(f"publication marker does not exist: {uid}")
            if marker.candidate_sha256 != candidate_sha256(candidate):
                raise UpdateQueueFormatError(
                    "publication marker does not match its local candidate"
                )
            return marker
        marker = PublicationMarker(
            candidate_uid=candidate.uid,
            attempt_id=attempt_id or uuid.uuid4().hex,
            attempted_at=attempted_at,
            candidate_sha256=candidate_sha256(candidate),
        )
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        _write_json(path, _publication_json(marker))
        return marker

    def record_publication_commit(self, uid: str, commit: str) -> PublicationMarker:
        clean_uid = _require_uid(uid, "candidate uid")
        _require_oid(commit, "publication proposed_commit")
        path = self.publication_root / f"{clean_uid}.json"
        marker = self.read_publication_marker(clean_uid)
        if marker is None:
            raise FileNotFoundError(f"publication marker does not exist: {uid}")
        updated = replace(marker, proposed_commit=commit)
        _write_json(path, _publication_json(updated))
        return updated

    def read_publication_marker(self, uid: str) -> PublicationMarker | None:
        clean_uid = _require_uid(uid, "candidate uid")
        path = self.publication_root / f"{clean_uid}.json"
        if not path.exists() and not path.is_symlink():
            return None
        marker = _read_record(path, _parse_publication_marker)
        if marker.candidate_uid != clean_uid:
            raise UpdateQueueFormatError(
                f"publication marker filename does not match candidate uid: {path}"
            )
        return marker

    def publication_state(self, uid: str) -> PublicationState:
        clean_uid = _require_uid(uid, "candidate uid")
        if self.read_publication_marker(clean_uid) is not None:
            return "attempted"
        if self.read_outbox(clean_uid) is not None:
            return "never_attempted"
        return "missing"

    def clear_publication_marker(self, uid: str) -> None:
        clean_uid = _require_uid(uid, "candidate uid")
        _unlink(self.publication_root / f"{clean_uid}.json")

    def remove_outbox(self, uid: str) -> None:
        clean_uid = _require_uid(uid, "candidate uid")
        _unlink(self.outbox_root / f"{clean_uid}.json")


def validate_update_queue(root: Path) -> list[str]:
    """Return path-scoped diagnostics for the tracked queue below a memory root."""
    _snapshot, errors = _inspect_update_queue(Path(root))
    return errors


def candidate_sha256(candidate: UpdateCandidate) -> str:
    return hashlib.sha256(_json_bytes(_candidate_json(candidate))).hexdigest()


def parse_update_candidate_json(text: str) -> UpdateCandidate:
    """Parse strict synchronized-candidate JSON, including duplicate-key checks."""
    return _parse_candidate(_loads_json(text))


def parse_update_queue_lease_json(text: str) -> UpdateQueueLease:
    """Parse strict synchronized-lease JSON, including duplicate-key checks."""
    return _parse_lease(_loads_json(text))


def update_candidate_batch_id(candidates: Iterable[UpdateCandidate]) -> str:
    """Derive the stable operation identity for synchronized candidate evidence."""
    sessions: dict[str, list[UpdateCandidate]] = {}
    for candidate in candidates:
        valid = UpdateCandidate(**candidate.__dict__)
        sessions.setdefault(valid.session_id, []).append(valid)
    if not sessions:
        raise UpdateQueueFormatError("candidate batch must not be empty")
    participants = [
        {
            "session_id": session_id,
            "jobs": [
                {
                    "id": item.display_id,
                    "candidate_uid": item.uid,
                    "message": item.message,
                    "submitted_at": item.submitted_at,
                }
                for item in sorted(
                    session_candidates,
                    key=lambda entry: (entry.display_id, entry.submitted_at, entry.uid),
                )
            ],
        }
        for session_id, session_candidates in sorted(sessions.items())
    ]
    canonical = json.dumps(
        participants,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"update-batch-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _inspect_update_queue(root: Path) -> tuple[UpdateQueueSnapshot, list[str]]:
    queue_root = root / QUEUE_DIRECTORY
    errors: list[str] = []
    if not queue_root.exists() and not queue_root.is_symlink():
        return UpdateQueueSnapshot(), errors
    if queue_root.is_symlink() or not queue_root.is_dir():
        return UpdateQueueSnapshot(), [_diagnostic(root, queue_root, "must be a directory")]

    allowed = {CANDIDATES_DIRECTORY, RECOVERY_DIRECTORY, LEASE_FILENAME}
    try:
        children = sorted(queue_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return UpdateQueueSnapshot(), [_diagnostic(root, queue_root, _os_reason(exc))]
    for path in children:
        if path.name not in allowed:
            errors.append(_diagnostic(root, path, "unexpected runtime-owned queue path"))

    candidates = _inspect_record_directory(
        root,
        queue_root / CANDIDATES_DIRECTORY,
        _parse_candidate,
        lambda item: f"{item.uid}.json",
        "candidate",
        errors,
    )
    recoveries = _inspect_record_directory(
        root,
        queue_root / RECOVERY_DIRECTORY,
        _parse_recovery,
        lambda item: f"{item.batch_id}.json",
        "recovery",
        errors,
    )

    lease: UpdateQueueLease | None = None
    lease_path = queue_root / LEASE_FILENAME
    if lease_path.exists() or lease_path.is_symlink():
        if lease_path.is_symlink() or not lease_path.is_file():
            errors.append(_diagnostic(root, lease_path, "must be a regular file"))
        else:
            try:
                lease = _read_record(lease_path, _parse_lease)
            except (OSError, UnicodeError, json.JSONDecodeError, UpdateQueueFormatError) as exc:
                errors.append(_diagnostic(root, lease_path, _record_reason(exc)))

    candidate_uids = {candidate.uid for candidate in candidates}
    by_uid = {candidate.uid: candidate for candidate in candidates}
    if lease is not None:
        missing = sorted(set(lease.candidate_uids) - candidate_uids)
        if missing:
            errors.append(
                _diagnostic(
                    root,
                    lease_path,
                    "references missing candidate uid(s): " + ", ".join(missing),
                )
            )
        elif update_candidate_batch_id(by_uid[uid] for uid in lease.candidate_uids) != lease.batch_id:
            errors.append(
                _diagnostic(root, lease_path, "batch_id does not match candidate evidence")
            )

    recovery_owners: dict[str, str] = {}
    for recovery in recoveries:
        recovery_path = queue_root / RECOVERY_DIRECTORY / f"{recovery.batch_id}.json"
        missing = sorted(set(recovery.candidate_uids) - candidate_uids)
        if missing:
            errors.append(
                _diagnostic(
                    root,
                    recovery_path,
                    "references missing candidate uid(s): " + ", ".join(missing),
                )
            )
        elif (
            update_candidate_batch_id(by_uid[uid] for uid in recovery.candidate_uids)
            != recovery.batch_id
        ):
            errors.append(
                _diagnostic(
                    root,
                    recovery_path,
                    "batch_id does not match candidate evidence",
                )
            )
        for uid in recovery.candidate_uids:
            previous = recovery_owners.setdefault(uid, recovery.batch_id)
            if previous != recovery.batch_id:
                errors.append(
                    _diagnostic(
                        root,
                        recovery_path,
                        f"candidate uid {uid} also belongs to recovery batch {previous}",
                    )
                )

    if lease is not None:
        overlap_batches = {
            recovery_owners[uid]
            for uid in lease.candidate_uids
            if uid in recovery_owners and recovery_owners[uid] != lease.batch_id
        }
        if overlap_batches:
            errors.append(
                _diagnostic(
                    root,
                    lease_path,
                    "candidate membership conflicts with recovery batch(es): "
                    + ", ".join(sorted(overlap_batches)),
                )
            )
        same_recovery = next(
            (item for item in recoveries if item.batch_id == lease.batch_id),
            None,
        )
        if same_recovery is not None and same_recovery.candidate_uids != lease.candidate_uids:
            errors.append(
                _diagnostic(
                    root,
                    lease_path,
                    "candidate_uids differ from the matching recovery batch",
                )
            )

    return (
        UpdateQueueSnapshot(
            candidates=tuple(sorted(candidates, key=lambda item: item.uid)),
            lease=lease,
            recoveries=tuple(sorted(recoveries, key=lambda item: item.batch_id)),
        ),
        sorted(set(errors)),
    )


def _inspect_record_directory(
    root: Path,
    directory: Path,
    parser: Callable[[object], _Record],
    expected_name: Callable[[_Record], str],
    label: str,
    errors: list[str],
) -> list[_Record]:
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink() or not directory.is_dir():
        errors.append(_diagnostic(root, directory, "must be a directory"))
        return []
    try:
        paths = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        errors.append(_diagnostic(root, directory, _os_reason(exc)))
        return []

    records = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            errors.append(_diagnostic(root, path, "must be a regular file"))
            continue
        try:
            record = _read_record(path, parser)
        except (OSError, UnicodeError, json.JSONDecodeError, UpdateQueueFormatError) as exc:
            errors.append(_diagnostic(root, path, _record_reason(exc)))
            continue
        if path.name != expected_name(record):
            errors.append(
                _diagnostic(root, path, f"filename does not match embedded {label} identity")
            )
            continue
        records.append(record)
    return records


def _candidate_json(candidate: UpdateCandidate) -> dict[str, object]:
    # Reconstructing validates programmatic callers before bytes reach disk.
    valid = UpdateCandidate(**candidate.__dict__)
    return {
        "schema_version": SCHEMA_VERSION,
        "uid": valid.uid,
        "session_id": valid.session_id,
        "display_id": valid.display_id,
        "message": valid.message,
        "submitted_at": valid.submitted_at,
    }


def _lease_json(lease: UpdateQueueLease) -> dict[str, object]:
    valid = UpdateQueueLease(**lease.__dict__)
    return {
        "schema_version": SCHEMA_VERSION,
        "holder": valid.holder,
        "token": valid.token,
        "base_commit": valid.base_commit,
        "batch_id": valid.batch_id,
        "candidate_uids": list(valid.candidate_uids),
        "expires_at": valid.expires_at,
    }


def _recovery_json(recovery: UpdateQueueRecovery) -> dict[str, object]:
    valid = UpdateQueueRecovery(**recovery.__dict__)
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": valid.batch_id,
        "candidate_uids": list(valid.candidate_uids),
        "attempts": valid.attempts,
        "reason_code": valid.reason_code,
        "retry_at": valid.retry_at,
        "manual_recovery": valid.manual_recovery,
    }


def _publication_json(marker: PublicationMarker) -> dict[str, object]:
    valid = PublicationMarker(**marker.__dict__)
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_uid": valid.candidate_uid,
        "attempt_id": valid.attempt_id,
        "attempted_at": valid.attempted_at,
        "candidate_sha256": valid.candidate_sha256,
        "proposed_commit": valid.proposed_commit,
    }


def _parse_candidate(data: object) -> UpdateCandidate:
    value = _exact_object(
        data,
        {"schema_version", "uid", "session_id", "display_id", "message", "submitted_at"},
        "candidate",
    )
    _require_schema(value, "candidate")
    return UpdateCandidate(
        uid=_required_string_field(value, "uid", "candidate"),
        session_id=_required_string_field(value, "session_id", "candidate"),
        display_id=_required_int_field(value, "display_id", "candidate"),
        message=_required_string_field(value, "message", "candidate"),
        submitted_at=_required_string_field(value, "submitted_at", "candidate"),
    )


def _parse_lease(data: object) -> UpdateQueueLease:
    value = _exact_object(
        data,
        {
            "schema_version",
            "holder",
            "token",
            "base_commit",
            "batch_id",
            "candidate_uids",
            "expires_at",
        },
        "lease",
    )
    _require_schema(value, "lease")
    return UpdateQueueLease(
        holder=_required_string_field(value, "holder", "lease"),
        token=_required_string_field(value, "token", "lease"),
        base_commit=_required_string_field(value, "base_commit", "lease"),
        batch_id=_required_string_field(value, "batch_id", "lease"),
        candidate_uids=_required_string_tuple_field(value, "candidate_uids", "lease"),
        expires_at=_required_string_field(value, "expires_at", "lease"),
    )


def _parse_recovery(data: object) -> UpdateQueueRecovery:
    value = _exact_object(
        data,
        {
            "schema_version",
            "batch_id",
            "candidate_uids",
            "attempts",
            "reason_code",
            "retry_at",
            "manual_recovery",
        },
        "recovery",
    )
    _require_schema(value, "recovery")
    retry_at = value["retry_at"]
    if retry_at is not None and not isinstance(retry_at, str):
        raise UpdateQueueFormatError("recovery retry_at must be a string or null")
    manual = value["manual_recovery"]
    if type(manual) is not bool:
        raise UpdateQueueFormatError("recovery manual_recovery must be a boolean")
    return UpdateQueueRecovery(
        batch_id=_required_string_field(value, "batch_id", "recovery"),
        candidate_uids=_required_string_tuple_field(value, "candidate_uids", "recovery"),
        attempts=_required_int_field(value, "attempts", "recovery"),
        reason_code=_required_string_field(value, "reason_code", "recovery"),
        retry_at=retry_at,
        manual_recovery=manual,
    )


def _parse_publication_marker(data: object) -> PublicationMarker:
    value = _exact_object(
        data,
        {
            "schema_version",
            "candidate_uid",
            "attempt_id",
            "attempted_at",
            "candidate_sha256",
            "proposed_commit",
        },
        "publication marker",
    )
    _require_schema(value, "publication marker")
    proposed = value["proposed_commit"]
    if proposed is not None and not isinstance(proposed, str):
        raise UpdateQueueFormatError(
            "publication marker proposed_commit must be a string or null"
        )
    return PublicationMarker(
        candidate_uid=_required_string_field(value, "candidate_uid", "publication marker"),
        attempt_id=_required_string_field(value, "attempt_id", "publication marker"),
        attempted_at=_required_string_field(value, "attempted_at", "publication marker"),
        candidate_sha256=_required_string_field(
            value, "candidate_sha256", "publication marker"
        ),
        proposed_commit=proposed,
    )


def _read_record(path: Path, parser: Callable[[object], _Record]) -> _Record:
    if path.is_symlink() or not path.is_file():
        raise UpdateQueueFormatError(f"must be a regular file: {path}")
    return parser(_load_json(path))


def _load_json(path: Path) -> object:
    return _loads_json(path.read_text(encoding="utf-8"))


def _loads_json(text: str) -> object:
    def reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise UpdateQueueFormatError(f"duplicate JSON field: {key}")
            value[key] = item
        return value

    def reject_constant(value: str):
        raise UpdateQueueFormatError(f"invalid JSON number: {value}")

    return json.loads(
        text,
        object_pairs_hook=reject_duplicate_fields,
        parse_constant=reject_constant,
    )


def _write_immutable(
    path: Path,
    data: dict[str, object],
    parser: Callable[[object], _Record],
) -> None:
    if path.exists() or path.is_symlink():
        existing = _read_record(path, parser)
        requested = parser(data)
        if existing != requested:
            raise FileExistsError(f"immutable update queue record already exists: {path}")
        return
    _write_json(path, data)


def _write_json(path: Path, data: dict[str, object]) -> None:
    _ensure_durable_directory(path.parent)
    temporary = path.with_name(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _json_bytes(data: dict[str, object]) -> bytes:
    return (
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _exact_object(data: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise UpdateQueueFormatError(f"{label} must be a JSON object")
    actual = set(data)
    missing = sorted(keys - actual)
    unknown = sorted(actual - keys)
    if missing:
        raise UpdateQueueFormatError(f"{label} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise UpdateQueueFormatError(
            f"{label} contains unsupported field(s): {', '.join(unknown)}"
        )
    return data


def _require_schema(data: dict[str, object], label: str) -> None:
    version = data["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise UpdateQueueFormatError(
            f"{label} schema_version must be {SCHEMA_VERSION}"
        )


def _required_string_field(data: dict[str, object], key: str, label: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise UpdateQueueFormatError(f"{label} {key} must be a string")
    return value


def _required_int_field(data: dict[str, object], key: str, label: str) -> int:
    value = data[key]
    if type(value) is not int:
        raise UpdateQueueFormatError(f"{label} {key} must be an integer")
    return value


def _required_string_tuple_field(
    data: dict[str, object], key: str, label: str
) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise UpdateQueueFormatError(f"{label} {key} must be a list of strings")
    return tuple(value)


def _require_uid(value: str, label: str) -> str:
    if not isinstance(value, str) or not _UID_RE.fullmatch(value):
        raise UpdateQueueFormatError(f"{label} must be lowercase UUID hex")
    try:
        if uuid.UUID(hex=value).hex != value:
            raise ValueError(value)
    except ValueError as exc:
        raise UpdateQueueFormatError(f"{label} must be lowercase UUID hex") from exc
    return value


def _require_uid_tuple(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise UpdateQueueFormatError(f"{label} must contain at least one uid")
    result = tuple(_require_uid(value, label) for value in values)
    if result != tuple(sorted(set(result))):
        raise UpdateQueueFormatError(f"{label} must be unique and sorted")
    return result


def _require_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise UpdateQueueFormatError(f"{label} must be a lowercase Git object id")
    return value


def _require_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise UpdateQueueFormatError(f"{label} must be lowercase SHA-256 hex")
    return value


def _require_batch_id(value: str) -> str:
    if not isinstance(value, str) or not _BATCH_ID_RE.fullmatch(value):
        raise UpdateQueueFormatError(
            "batch_id must use update-batch- followed by lowercase SHA-256 hex"
        )
    return value


def _require_session_id(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UpdateQueueFormatError("candidate session_id must be a non-empty canonical string")
    if value in {".", ".."} or any(character in value for character in "/\\"):
        raise UpdateQueueFormatError("candidate session_id must be one safe path segment")
    return value


def _require_nonempty_string(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpdateQueueFormatError(f"{label} must be a non-empty string")
    return value


def _require_positive_int(value: int, label: str) -> int:
    if type(value) is not int or value < 1:
        raise UpdateQueueFormatError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: int, label: str) -> int:
    if type(value) is not int or value < 0:
        raise UpdateQueueFormatError(f"{label} must be a nonnegative integer")
    return value


def _require_utc_time(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise UpdateQueueFormatError(f"{label} must be a canonical UTC datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UpdateQueueFormatError(f"{label} must be a canonical UTC datetime") from exc
    if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat() != value:
        raise UpdateQueueFormatError(f"{label} must be a canonical UTC datetime")
    return value


def _diagnostic(root: Path, path: Path, reason: str) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    return f"{relative}: {reason}"


def _record_reason(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {exc.msg}"
    return str(exc) or type(exc).__name__


def _os_reason(exc: OSError) -> str:
    return f"could not inspect path: {exc.strerror or exc}"
