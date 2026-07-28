from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .session import _ensure_durable_directory, _fsync_directory
from .update_coordination import UPDATE_RECORD_PATH_RE
from .update_queue import (
    UpdateCandidate,
    parse_update_candidate_data,
    update_candidate_batch_id,
    update_candidate_data,
)


SCHEMA_VERSION = 1
RECORDS_DIRECTORY = "update_records"


class UpdateRecordFormatError(ValueError):
    """Raised when retained candidate evidence violates its immutable format."""


@dataclass(frozen=True)
class UpdateRecord:
    operation_id: str
    candidates: tuple[UpdateCandidate, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise UpdateRecordFormatError(
                f"unsupported update record schema version: {self.schema_version}"
            )
        candidates = tuple(
            sorted(
                (UpdateCandidate(**candidate.__dict__) for candidate in self.candidates),
                key=lambda item: (
                    item.session_id,
                    item.display_id,
                    item.submitted_at,
                    item.uid,
                ),
            )
        )
        if not candidates:
            raise UpdateRecordFormatError("update record must contain at least one candidate")
        if len({candidate.uid for candidate in candidates}) != len(candidates):
            raise UpdateRecordFormatError("update record candidate uids must be unique")
        expected = update_candidate_batch_id(candidates)
        if self.operation_id != expected:
            raise UpdateRecordFormatError(
                "update record operation id does not match its candidate evidence"
            )
        object.__setattr__(self, "candidates", candidates)

    @classmethod
    def from_candidates(cls, candidates: Iterable[UpdateCandidate]) -> UpdateRecord:
        items = tuple(candidates)
        if not items:
            raise UpdateRecordFormatError("update record must contain at least one candidate")
        return cls(
            operation_id=update_candidate_batch_id(items),
            candidates=items,
        )

    def to_data(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "candidates": [update_candidate_data(item) for item in self.candidates],
        }


class UpdateRecordStore:
    """Access immutable, tracked candidate batches retained as input provenance."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.root = self.memory_root / RECORDS_DIRECTORY

    def record_path(self, operation_id: str) -> Path:
        path = f"{RECORDS_DIRECTORY}/{operation_id}.json"
        if UPDATE_RECORD_PATH_RE.fullmatch(path) is None:
            raise UpdateRecordFormatError("invalid update record operation id")
        return self.memory_root / path

    def write(self, record: UpdateRecord) -> Path:
        valid = UpdateRecord(**record.__dict__)
        path = self.record_path(valid.operation_id)
        if path.exists() or path.is_symlink():
            existing = _read_record(path)
            if existing != valid:
                raise UpdateRecordFormatError(
                    f"immutable update record already contains different evidence: {path}"
                )
            return path
        _write_json(path, valid.to_data())
        return path

    def read(self, operation_id: str) -> UpdateRecord | None:
        path = self.record_path(operation_id)
        if not path.exists() and not path.is_symlink():
            return None
        return _read_record(path)


def validate_update_records(root: Path) -> list[str]:
    memory_root = Path(root)
    records_root = memory_root / RECORDS_DIRECTORY
    if not records_root.exists() and not records_root.is_symlink():
        return []
    if records_root.is_symlink() or not records_root.is_dir():
        return [f"{RECORDS_DIRECTORY}: must be a directory"]
    diagnostics: list[str] = []
    try:
        paths = sorted(records_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [f"{RECORDS_DIRECTORY}: {exc}"]
    for path in paths:
        relative = path.relative_to(memory_root).as_posix()
        if UPDATE_RECORD_PATH_RE.fullmatch(relative) is None:
            diagnostics.append(f"{relative}: path is not a canonical update record")
            continue
        try:
            record = _read_record(path)
        except (OSError, UnicodeError, json.JSONDecodeError, UpdateRecordFormatError) as exc:
            diagnostics.append(f"{relative}: {exc}")
            continue
        if path != UpdateRecordStore(memory_root).record_path(record.operation_id):
            diagnostics.append(f"{relative}: filename does not match embedded operation id")
    return diagnostics


def _read_record(path: Path) -> UpdateRecord:
    if path.is_symlink() or not path.is_file():
        raise UpdateRecordFormatError("must be a regular file")
    data = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_fields,
        parse_constant=_reject_constant,
    )
    if not isinstance(data, dict):
        raise UpdateRecordFormatError("update record must be a JSON object")
    expected = {"schema_version", "operation_id", "candidates"}
    if set(data) != expected:
        missing = sorted(expected - set(data))
        extra = sorted(set(data) - expected)
        detail = []
        if missing:
            detail.append(f"missing field(s): {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected field(s): {', '.join(extra)}")
        raise UpdateRecordFormatError("update record " + "; ".join(detail))
    schema_version = data["schema_version"]
    operation_id = data["operation_id"]
    raw_candidates = data["candidates"]
    if type(schema_version) is not int:
        raise UpdateRecordFormatError("update record schema_version must be an integer")
    if not isinstance(operation_id, str):
        raise UpdateRecordFormatError("update record operation_id must be a string")
    if not isinstance(raw_candidates, list):
        raise UpdateRecordFormatError("update record candidates must be a list")
    try:
        candidates = tuple(parse_update_candidate_data(item) for item in raw_candidates)
    except Exception as exc:
        raise UpdateRecordFormatError(f"invalid update record candidate: {exc}") from exc
    return UpdateRecord(
        schema_version=schema_version,
        operation_id=operation_id,
        candidates=candidates,
    )


def _write_json(path: Path, data: dict[str, object]) -> None:
    _ensure_durable_directory(path.parent)
    temporary = path.with_name(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(
                (
                    json.dumps(
                        data,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateRecordFormatError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise UpdateRecordFormatError(f"invalid JSON number: {value}")
