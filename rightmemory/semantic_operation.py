from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .platform import lock_file, process_exists, process_identity, unlock_file
from .session import _ensure_durable_directory, _ensure_runtime_gitignore, _fsync_directory


SCHEMA_VERSION = 1
OperationPhase = Literal["running", "prepared", "committed", "no_change"]
EffectStatus = Literal["pending", "done", "failed"]
FINAL_PHASES = {"committed", "no_change"}
PHASES = {"running", "prepared", *FINAL_PHASES}
EFFECT_STATUSES = {"pending", "done", "failed"}


class OperationConflictError(ValueError):
    """Raised when durable data conflicts with an existing operation identity."""


class OperationBusyError(RuntimeError):
    """Raised when an unfinished operation is still owned by another live process."""


@dataclass(frozen=True)
class OperationOutcome:
    phase: OperationPhase
    output: str
    start_commit: str
    sequence: int
    changed_paths: tuple[str, ...] = ()
    landed_commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        phase = _phase(self.phase)
        if phase == "running":
            raise ValueError("operation outcome phase cannot be running")
        if not isinstance(self.output, str):
            raise ValueError("operation outcome output must be a string")
        start_commit = _required_string(self.start_commit, "start_commit")
        sequence = _positive_int(self.sequence, "outcome sequence")
        changed_paths = _changed_paths(self.changed_paths)
        landed_commit = _optional_string(self.landed_commit, "landed_commit")
        if phase in FINAL_PHASES and landed_commit is None:
            raise ValueError(f"{phase} operation outcome requires landed_commit")
        if phase == "prepared" and landed_commit is not None:
            raise ValueError("prepared operation outcome cannot contain landed_commit")
        if phase == "no_change" and changed_paths:
            raise ValueError("no_change operation outcome cannot contain changed_paths")
        object.__setattr__(self, "phase", phase)
        object.__setattr__(self, "start_commit", start_commit)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "changed_paths", changed_paths)
        object.__setattr__(self, "landed_commit", landed_commit)
        object.__setattr__(self, "metadata", _json_object(self.metadata, "outcome metadata"))


@dataclass(frozen=True)
class OperationEffect:
    name: str
    status: EffectStatus = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        name = _effect_name(self.name)
        status = _effect_status(self.status)
        error = _optional_string(self.error, "effect error")
        if status == "failed" and error is None:
            raise ValueError("failed operation effect requires an error")
        if status != "failed" and error is not None:
            raise ValueError(f"{status} operation effect cannot contain an error")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", _json_object(self.metadata, "effect metadata"))
        object.__setattr__(self, "error", error)


@dataclass(frozen=True)
class SemanticOperationRecord:
    operation_id: str
    input_sha256: str
    input_data: dict[str, Any]
    owner_pid: int
    owner_identity: str | None
    phase: OperationPhase
    outcome: OperationOutcome | None
    effects: tuple[OperationEffect, ...]
    failure: str | None
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        return self.phase in FINAL_PHASES and all(effect.status == "done" for effect in self.effects)


class SemanticOperationStore:
    """Durable, per-operation receipts for semantic write execution and replay."""

    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.runtime_root / "operations"
        self.records_root = self.root / "records"
        self.locks_root = self.root / "locks"
        self.effect_locks_root = self.root / "effect-locks"
        self.effect_states_root = self.root / "effect-state"
        self.states_root = self.root / "state"
        self.outstanding_root = self.root / "outstanding"
        self.retry_cursors_root = self.root / "retry-cursors"
        self.sequence_path = self.root / "sequence.json"

    @contextmanager
    def execution_locked(self):
        """Serialize model execution and Git landing across semantic writers."""
        with self._named_lock(self.root / "execution.lock"):
            yield

    @contextmanager
    def effects_locked(self, operation_id: str):
        """Serialize effect application for one completed operation."""
        clean_id = _operation_id(operation_id)
        with self._named_lock(self.effect_locks_root / f"{_operation_digest(clean_id)}.lock"):
            yield

    @contextmanager
    def publish_locked(self):
        with self._named_lock(self.root / "publish.lock"):
            yield

    def begin(
        self,
        operation_id: str,
        input_data: Mapping[str, Any],
        *,
        effects: Iterable[OperationEffect] = (),
    ) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        clean_input = _json_object(input_data, "input_data")
        input_sha256 = _json_sha256(clean_input)
        input_metadata = _input_metadata(clean_input)
        requested_effects = _effect_plan(effects)
        owner_pid, owner_identity = _current_owner()

        with self._locked(clean_id):
            existing = self._read_locked(clean_id)
            if existing is None:
                now = _now()
                record = SemanticOperationRecord(
                    operation_id=clean_id,
                    input_sha256=input_sha256,
                    input_data=input_metadata,
                    owner_pid=owner_pid,
                    owner_identity=owner_identity,
                    phase="running",
                    outcome=None,
                    effects=requested_effects,
                    failure=None,
                    created_at=now,
                    updated_at=now,
                )
                self._write_locked(record)
                return record

            self._validate_identity(existing, clean_id, input_sha256)
            merged_effects = _merge_effect_plan(
                existing.effects,
                requested_effects,
                allow_new=existing.phase == "running",
            )
            effects_changed = merged_effects != existing.effects
            # Execution ownership ends with the semantic outcome. Effects have
            # their own lock and never share or steal this owner.
            if existing.phase in FINAL_PHASES:
                self._sync_outstanding_locked(existing)
                return existing
            if _is_current_owner(existing, owner_pid, owner_identity):
                if not effects_changed:
                    self._sync_outstanding_locked(existing)
                    return existing
                updated = replace(existing, effects=merged_effects, updated_at=_now())
                self._write_locked(updated)
                return updated
            # The caller holds either the global execution lock or this operation's
            # effect lock, so ownership can move even if the prior process is alive.
            claimed = replace(
                existing,
                owner_pid=owner_pid,
                owner_identity=owner_identity,
                effects=merged_effects,
                updated_at=_now(),
            )
            self._write_locked(claimed)
            return claimed

    def read(self, operation_id: str) -> SemanticOperationRecord | None:
        clean_id = _operation_id(operation_id)
        with self._locked(clean_id):
            return self._read_locked(clean_id)

    def claim_prepared(self, operation_id: str) -> SemanticOperationRecord:
        """Claim a prepared semantic outcome while holding execution.lock."""
        clean_id = _operation_id(operation_id)
        owner_pid, owner_identity = _current_owner()
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase != "prepared":
                raise OperationConflictError(
                    f"operation {clean_id} is not prepared: {record.phase}"
                )
            if _is_current_owner(record, owner_pid, owner_identity):
                return record
            claimed = replace(
                record,
                owner_pid=owner_pid,
                owner_identity=owner_identity,
                updated_at=_now(),
            )
            self._write_locked(claimed)
            return claimed

    def prepare_outcome(
        self,
        operation_id: str,
        *,
        output: str,
        start_commit: str,
        changed_paths: Iterable[str],
        effects: Iterable[OperationEffect] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        requested_effects = _effect_plan(effects)

        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            sequence = record.outcome.sequence if record.outcome is not None else self._next_sequence()
            prepared = OperationOutcome(
                phase="prepared",
                output=output,
                start_commit=start_commit,
                sequence=sequence,
                changed_paths=tuple(changed_paths),
                metadata={} if metadata is None else dict(metadata),
            )
            # Persist model output and the replay plan before Git mutates main HEAD.
            merged_effects = _merge_effect_plan(record.effects, requested_effects, allow_new=True)
            if record.outcome is not None:
                _assert_same_prepared_outcome(record.outcome, prepared)
                if merged_effects == record.effects:
                    self._sync_outstanding_locked(record)
                    return record
            self._require_current_owner(record)
            if record.phase != "running":
                raise OperationConflictError(
                    f"operation {clean_id} cannot prepare an outcome from phase {record.phase}"
                )
            updated = replace(
                record,
                phase="prepared",
                outcome=prepared,
                effects=merged_effects,
                failure=None,
                updated_at=_now(),
            )
            self._write_locked(updated)
            return updated

    def complete_commit(self, operation_id: str, landed_commit: str) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        clean_commit = _required_string(landed_commit, "landed_commit")
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase == "committed":
                if record.outcome is not None and record.outcome.landed_commit == clean_commit:
                    self._sync_outstanding_locked(record)
                    return record
                raise OperationConflictError(f"operation {clean_id} has a different landed commit")
            self._require_current_owner(record)
            if record.phase != "prepared" or record.outcome is None:
                raise OperationConflictError(
                    f"operation {clean_id} cannot complete a commit from phase {record.phase}"
                )
            outcome = replace(record.outcome, phase="committed", landed_commit=clean_commit)
            updated = replace(
                record,
                phase="committed",
                outcome=outcome,
                failure=None,
                updated_at=_now(),
            )
            self._write_locked(updated)
            return updated

    def complete_no_change(
        self,
        operation_id: str,
        completed_commit: str | None = None,
    ) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase == "no_change":
                if completed_commit is not None and record.outcome is not None:
                    clean_commit = _required_string(completed_commit, "completed_commit")
                    if record.outcome.landed_commit != clean_commit:
                        raise OperationConflictError(
                            f"operation {clean_id} has a different completed commit"
                        )
                self._sync_outstanding_locked(record)
                return record
            self._require_current_owner(record)
            if record.phase != "prepared" or record.outcome is None:
                raise OperationConflictError(
                    f"operation {clean_id} cannot complete no_change from phase {record.phase}"
                )
            if record.outcome.changed_paths:
                raise OperationConflictError("no_change operation outcome cannot contain changed paths")
            clean_commit = _required_string(
                completed_commit or record.outcome.start_commit,
                "completed_commit",
            )
            outcome = replace(record.outcome, phase="no_change", landed_commit=clean_commit)
            updated = replace(
                record,
                phase="no_change",
                outcome=outcome,
                failure=None,
                updated_at=_now(),
            )
            self._write_locked(updated)
            return updated

    def record_failure(
        self,
        operation_id: str,
        error: str,
    ) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        clean_error = _required_string(error, "operation failure")
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase in FINAL_PHASES:
                raise OperationConflictError(f"completed operation {clean_id} cannot record a failure")
            self._require_current_owner(record)
            if record.failure == clean_error:
                return record
            updated = replace(
                record,
                failure=clean_error,
                updated_at=_now(),
            )
            self._write_locked(updated)
            return updated

    def restart_prepared(
        self,
        operation_id: str,
        *,
        expected_metadata: Mapping[str, Any],
        reason: str,
    ) -> SemanticOperationRecord:
        """Return a fenced prepared operation to running so it can be recomputed."""
        clean_id = _operation_id(operation_id)
        clean_metadata = _json_object(expected_metadata, "expected outcome metadata")
        clean_reason = _required_string(reason, "operation restart reason")
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            self._require_current_owner(record)
            if record.phase != "prepared" or record.outcome is None:
                raise OperationConflictError(
                    f"operation {clean_id} cannot restart from phase {record.phase}"
                )
            for key, value in clean_metadata.items():
                if record.outcome.metadata.get(key) != value:
                    raise OperationConflictError(
                        f"operation {clean_id} has different outcome metadata: {key}"
                    )
            restarted = replace(
                record,
                phase="running",
                outcome=None,
                effects=(),
                failure=clean_reason,
                updated_at=_now(),
            )
            self._write_locked(restarted)
            return restarted

    def supersede_prepared(
        self,
        operation_id: str,
        *,
        expected_metadata: Mapping[str, Any],
        landed_commit: str,
    ) -> SemanticOperationRecord:
        """Settle a prepared result that lost an external publication fence."""
        clean_id = _operation_id(operation_id)
        clean_metadata = _json_object(expected_metadata, "expected outcome metadata")
        clean_commit = _required_string(landed_commit, "landed_commit")
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            self._require_current_owner(record)
            if record.phase != "prepared" or record.outcome is None:
                raise OperationConflictError(
                    f"operation {clean_id} cannot be superseded from phase {record.phase}"
                )
            for key, value in clean_metadata.items():
                if record.outcome.metadata.get(key) != value:
                    raise OperationConflictError(
                        f"operation {clean_id} has different outcome metadata: {key}"
                    )
            outcome = OperationOutcome(
                phase="no_change",
                output="superseded by another external publication owner",
                start_commit=record.outcome.start_commit,
                sequence=record.outcome.sequence,
                landed_commit=clean_commit,
                metadata={
                    **clean_metadata,
                    "superseded": True,
                },
            )
            settled = replace(
                record,
                phase="no_change",
                outcome=outcome,
                effects=(),
                failure=None,
                updated_at=_now(),
            )
            self._write_locked(settled)
            return settled

    def supersede_running(
        self,
        operation_id: str,
        *,
        landed_commit: str,
        reason: str,
    ) -> SemanticOperationRecord:
        """Settle an abandoned running operation after external terminal proof."""
        clean_id = _operation_id(operation_id)
        clean_commit = _required_string(landed_commit, "landed_commit")
        clean_reason = _required_string(reason, "operation supersede reason")
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase in FINAL_PHASES:
                self._sync_outstanding_locked(record)
                return record
            if record.phase != "running":
                raise OperationConflictError(
                    f"operation {clean_id} cannot supersede running work from phase {record.phase}"
                )
            outcome = OperationOutcome(
                phase="no_change",
                output=clean_reason,
                start_commit=clean_commit,
                sequence=self._next_sequence(),
                landed_commit=clean_commit,
                metadata={"superseded": True},
            )
            settled = replace(
                record,
                phase="no_change",
                outcome=outcome,
                effects=(),
                failure=None,
                updated_at=_now(),
            )
            self._write_locked(settled)
            return settled

    def mark_effect(
        self,
        operation_id: str,
        effect_name: str,
        status: EffectStatus,
        *,
        metadata: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> SemanticOperationRecord:
        clean_id = _operation_id(operation_id)
        clean_name = _effect_name(effect_name)
        clean_status = _effect_status(status)
        metadata_update = _json_object({} if metadata is None else metadata, "effect metadata")
        clean_error = _optional_string(error, "effect error")
        if clean_status == "failed" and clean_error is None:
            raise ValueError("failed operation effect requires an error")
        if clean_status != "failed" and clean_error is not None:
            raise ValueError(f"{clean_status} operation effect cannot contain an error")

        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase not in FINAL_PHASES:
                raise OperationConflictError(
                    f"operation effects cannot run before a final outcome: {clean_id}"
                )
            index = next((i for i, effect in enumerate(record.effects) if effect.name == clean_name), None)
            if index is None:
                raise KeyError(f"operation effect does not exist: {clean_name}")
            current = record.effects[index]
            if current.status == "done" and clean_status != "done":
                raise OperationConflictError(f"completed operation effect cannot return to {clean_status}: {clean_name}")
            for key, value in metadata_update.items():
                if key in current.metadata and current.metadata[key] != value:
                    raise OperationConflictError(
                        f"operation effect cannot overwrite durable metadata: {clean_name}.{key}"
                    )
            merged_metadata = {**current.metadata, **metadata_update}
            updated_effect = OperationEffect(
                name=clean_name,
                status=clean_status,
                metadata=merged_metadata,
                error=clean_error,
            )
            if updated_effect == current:
                self._sync_outstanding_locked(record)
                return record
            effects = list(record.effects)
            effects[index] = updated_effect
            updated = replace(record, effects=tuple(effects), updated_at=_now())
            self._write_locked(updated)
            return updated

    def list_pending_effects(self, operation_id: str) -> tuple[OperationEffect, ...]:
        """Return final-outcome effects that still need application or retry."""
        clean_id = _operation_id(operation_id)
        with self._locked(clean_id):
            record = self._required_record_locked(clean_id)
            if record.phase not in FINAL_PHASES:
                return ()
            # Failed effects remain outstanding so a new owner can retry them.
            return tuple(effect for effect in record.effects if effect.status != "done")

    def list_records(self) -> tuple[SemanticOperationRecord, ...]:
        """Strictly load every durable receipt in operation-id order."""
        if not self.records_root.exists():
            return ()
        operation_ids: list[str] = []
        for path in sorted(self.records_root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            record = _record_from_json(data)
            if path != self.record_path(record.operation_id):
                raise OperationConflictError(
                    f"semantic operation record filename does not match operation id: {path}"
                )
            operation_ids.append(record.operation_id)

        records = []
        for operation_id in sorted(operation_ids):
            with self._locked(operation_id):
                records.append(self._required_record_locked(operation_id))
        return tuple(records)

    def list_outstanding_records(self) -> tuple[SemanticOperationRecord, ...]:
        """Load only operations that still need semantic or effect work."""
        if not self.outstanding_root.exists():
            return ()
        operation_ids: list[str] = []
        for path in sorted(self.outstanding_root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"operation_id"}:
                raise ValueError(f"invalid semantic operation outstanding marker: {path}")
            operation_id = _operation_id(data["operation_id"])
            if path != self._outstanding_path(operation_id):
                raise OperationConflictError(
                    f"semantic operation outstanding marker does not match operation id: {path}"
                )
            operation_ids.append(operation_id)

        records: list[SemanticOperationRecord] = []
        for operation_id in sorted(operation_ids):
            with self._locked(operation_id):
                record = self._read_locked(operation_id)
                if record is None:
                    # An unsettled marker is written first. If the process dies
                    # before its receipt, the marker is only an abandoned start.
                    self._outstanding_path(operation_id).unlink(missing_ok=True)
                    _fsync_directory(self.outstanding_root)
                    continue
                if record.complete:
                    self._sync_outstanding_locked(record)
                    continue
                records.append(record)
        return tuple(records)

    def state_root(self, operation_id: str) -> Path:
        return self.states_root / _operation_digest(_operation_id(operation_id))

    def effect_state_root(self, operation_id: str, effect_name: str) -> Path:
        operation_digest = _operation_digest(_operation_id(operation_id))
        effect_digest = hashlib.sha256(_effect_name(effect_name).encode("utf-8")).hexdigest()
        return self.effect_states_root / operation_digest / effect_digest

    def clear_effect_state(self, operation_id: str, effect_name: str) -> None:
        path = self.effect_state_root(operation_id, effect_name)
        if not path.exists():
            return
        shutil.rmtree(path)
        _fsync_directory(path.parent)

    def choose_effect_retry(
        self,
        queue_name: str,
        records: Iterable[SemanticOperationRecord],
    ) -> SemanticOperationRecord | None:
        """Advance one durable round-robin cursor over pending operations."""
        clean_queue = _effect_name(queue_name)
        candidates = []
        for record in records:
            if record.outcome is None:
                raise ValueError(f"effect retry candidate has no outcome: {record.operation_id}")
            candidates.append((record.outcome.sequence, record.operation_id, record))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))

        digest = hashlib.sha256(clean_queue.encode("utf-8")).hexdigest()
        path = self.retry_cursors_root / f"{digest}.json"
        with self._named_lock(self.retry_cursors_root / f"{digest}.lock"):
            cursor: tuple[int, str] | None = None
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or set(data) != {"sequence", "operation_id"}:
                    raise ValueError(f"invalid semantic effect retry cursor: {path}")
                cursor = (
                    _positive_int(data["sequence"], "effect retry sequence"),
                    _operation_id(data["operation_id"]),
                )
            selected = next(
                (item for item in candidates if cursor is None or (item[0], item[1]) > cursor),
                candidates[0],
            )
            self._write_retry_cursor(path, selected[0], selected[1])
            return selected[2]

    def record_path(self, operation_id: str) -> Path:
        return self.records_root / f"{_operation_digest(_operation_id(operation_id))}.json"

    def _outstanding_path(self, operation_id: str) -> Path:
        return self.outstanding_root / f"{_operation_digest(_operation_id(operation_id))}.json"

    def _lock_path(self, operation_id: str) -> Path:
        return self.locks_root / f"{_operation_digest(operation_id)}.lock"

    @contextmanager
    def _locked(self, operation_id: str):
        with self._named_lock(self._lock_path(operation_id)):
            yield

    @contextmanager
    def _named_lock(self, path: Path):
        _ensure_runtime_gitignore(self.runtime_root)
        _ensure_durable_directory(self.records_root)
        _ensure_durable_directory(path.parent)
        with path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _required_record_locked(self, operation_id: str) -> SemanticOperationRecord:
        record = self._read_locked(operation_id)
        if record is None:
            raise FileNotFoundError(f"semantic operation does not exist: {operation_id}")
        return record

    def _read_locked(self, operation_id: str) -> SemanticOperationRecord | None:
        path = self.record_path(operation_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        record = _record_from_json(data)
        expected_path = self.record_path(record.operation_id)
        if path != expected_path:
            raise OperationConflictError(
                f"semantic operation digest collision: {operation_id} conflicts with {record.operation_id}"
            )
        if record.operation_id != operation_id:
            raise OperationConflictError(
                f"semantic operation digest collision: {operation_id} conflicts with {record.operation_id}"
            )
        return record

    def _write_locked(self, record: SemanticOperationRecord) -> None:
        _validate_record(record)
        if not record.complete:
            # Make unsettled work discoverable before publishing its receipt.
            self._sync_outstanding_locked(record)
        path = self.record_path(record.operation_id)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        content = json.dumps(
            _record_to_json(record),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)
        if record.complete:
            self._sync_outstanding_locked(record)

    def _next_sequence(self) -> int:
        with self._named_lock(self.root / "sequence.lock"):
            last = 0
            if self.sequence_path.exists():
                data = json.loads(self.sequence_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or set(data) != {"last"}:
                    raise ValueError(f"invalid semantic operation sequence: {self.sequence_path}")
                last = _positive_int(data["last"], "last operation sequence")
            next_sequence = last + 1
            temp = self.sequence_path.with_name(
                f".{self.sequence_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            content = json.dumps({"last": next_sequence}, sort_keys=True) + "\n"
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.sequence_path)
            except OSError:
                temp.unlink(missing_ok=True)
                raise
            _fsync_directory(self.sequence_path.parent)
            return next_sequence

    def _write_retry_cursor(self, path: Path, sequence: int, operation_id: str) -> None:
        _ensure_durable_directory(path.parent)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        content = json.dumps(
            {"sequence": sequence, "operation_id": operation_id},
            sort_keys=True,
        ) + "\n"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    def _sync_outstanding_locked(self, record: SemanticOperationRecord) -> None:
        path = self._outstanding_path(record.operation_id)
        if record.complete:
            if path.exists():
                path.unlink()
                _fsync_directory(path.parent)
            return
        _ensure_durable_directory(self.outstanding_root)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        content = json.dumps({"operation_id": record.operation_id}, sort_keys=True) + "\n"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            raise
        _fsync_directory(path.parent)

    def _validate_identity(
        self,
        record: SemanticOperationRecord,
        operation_id: str,
        input_sha256: str,
    ) -> None:
        if record.operation_id != operation_id:
            raise OperationConflictError(
                f"semantic operation digest collision: {operation_id} conflicts with {record.operation_id}"
            )
        if record.input_sha256 != input_sha256:
            raise OperationConflictError(f"operation {operation_id} already exists with different input")

    def _require_current_owner(self, record: SemanticOperationRecord) -> None:
        owner_pid, owner_identity = _current_owner()
        if not _is_current_owner(record, owner_pid, owner_identity):
            if _owner_is_live(record):
                raise OperationBusyError(
                    f"operation {record.operation_id} is owned by live process {record.owner_pid}"
                )
            raise OperationBusyError(
                f"operation {record.operation_id} has a stale owner; call begin to claim it"
            )


def _record_to_json(record: SemanticOperationRecord) -> dict[str, Any]:
    return {
        "schema_version": record.schema_version,
        "operation_id": record.operation_id,
        "input_sha256": record.input_sha256,
        "input_data": record.input_data,
        "owner_pid": record.owner_pid,
        "owner_identity": record.owner_identity,
        "phase": record.phase,
        "outcome": None if record.outcome is None else _outcome_to_json(record.outcome),
        "effects": [_effect_to_json(effect) for effect in record.effects],
        "failure": record.failure,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _record_from_json(data: object) -> SemanticOperationRecord:
    if not isinstance(data, dict):
        raise ValueError("semantic operation record must be a JSON object")
    _exact_keys(
        data,
        {
            "schema_version",
            "operation_id",
            "input_sha256",
            "input_data",
            "owner_pid",
            "owner_identity",
            "phase",
            "outcome",
            "effects",
            "failure",
            "created_at",
            "updated_at",
        },
        "semantic operation record",
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported semantic operation schema version: {data['schema_version']}")
    raw_effects = data["effects"]
    if not isinstance(raw_effects, list):
        raise ValueError("semantic operation effects must be a list")
    raw_outcome = data["outcome"]
    outcome = None if raw_outcome is None else _outcome_from_json(raw_outcome)
    record = SemanticOperationRecord(
        schema_version=SCHEMA_VERSION,
        operation_id=_operation_id(data["operation_id"]),
        input_sha256=_sha256(data["input_sha256"], "input_sha256"),
        input_data=_json_object(data["input_data"], "input_data"),
        owner_pid=_positive_int(data["owner_pid"], "owner_pid"),
        owner_identity=_optional_string(data["owner_identity"], "owner_identity"),
        phase=_phase(data["phase"]),
        outcome=outcome,
        effects=_effects(_effect_from_json(item) for item in raw_effects),
        failure=_optional_string(data["failure"], "failure"),
        created_at=_timestamp(data["created_at"], "created_at"),
        updated_at=_timestamp(data["updated_at"], "updated_at"),
    )
    _validate_record(record)
    return record


def _outcome_to_json(outcome: OperationOutcome) -> dict[str, Any]:
    return {
        "phase": outcome.phase,
        "output": outcome.output,
        "start_commit": outcome.start_commit,
        "sequence": outcome.sequence,
        "changed_paths": list(outcome.changed_paths),
        "landed_commit": outcome.landed_commit,
        "metadata": outcome.metadata,
    }


def _outcome_from_json(data: object) -> OperationOutcome:
    if not isinstance(data, dict):
        raise ValueError("semantic operation outcome must be a JSON object")
    _exact_keys(
        data,
        {
            "phase",
            "output",
            "start_commit",
            "sequence",
            "changed_paths",
            "landed_commit",
            "metadata",
        },
        "semantic operation outcome",
    )
    raw_paths = data["changed_paths"]
    if not isinstance(raw_paths, list):
        raise ValueError("operation outcome changed_paths must be a list")
    return OperationOutcome(
        phase=_phase(data["phase"]),
        output=data["output"],
        start_commit=data["start_commit"],
        sequence=data["sequence"],
        changed_paths=tuple(raw_paths),
        landed_commit=data["landed_commit"],
        metadata=data["metadata"],
    )


def _effect_to_json(effect: OperationEffect) -> dict[str, Any]:
    return {
        "name": effect.name,
        "status": effect.status,
        "metadata": effect.metadata,
        "error": effect.error,
    }


def _effect_from_json(data: object) -> OperationEffect:
    if not isinstance(data, dict):
        raise ValueError("semantic operation effect must be a JSON object")
    _exact_keys(data, {"name", "status", "metadata", "error"}, "semantic operation effect")
    return OperationEffect(
        name=data["name"],
        status=data["status"],
        metadata=data["metadata"],
        error=data["error"],
    )


def _validate_record(record: SemanticOperationRecord) -> None:
    if record.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported semantic operation schema version: {record.schema_version}")
    _operation_id(record.operation_id)
    _sha256(record.input_sha256, "input_sha256")
    _json_object(record.input_data, "input_data")
    _positive_int(record.owner_pid, "owner_pid")
    _optional_string(record.owner_identity, "owner_identity")
    _phase(record.phase)
    _effects(record.effects)
    _optional_string(record.failure, "failure")
    _timestamp(record.created_at, "created_at")
    _timestamp(record.updated_at, "updated_at")
    if record.phase == "running" and record.outcome is not None:
        raise ValueError("running semantic operation cannot contain an outcome")
    if record.phase != "running" and record.outcome is None:
        raise ValueError(f"{record.phase} semantic operation requires an outcome")
    if record.outcome is not None and record.outcome.phase != record.phase:
        raise ValueError("semantic operation phase does not match outcome phase")
    if record.phase in FINAL_PHASES and record.failure is not None:
        raise ValueError("completed semantic operation cannot contain a failure")


def _assert_same_prepared_outcome(existing: OperationOutcome, requested: OperationOutcome) -> None:
    existing_prepared = replace(existing, phase="prepared", landed_commit=None)
    requested_prepared = replace(requested, phase="prepared", landed_commit=None)
    if existing_prepared != requested_prepared:
        raise OperationConflictError("semantic operation has a different prepared outcome")


def _merge_effect_plan(
    existing: tuple[OperationEffect, ...],
    requested: tuple[OperationEffect, ...],
    *,
    allow_new: bool,
) -> tuple[OperationEffect, ...]:
    by_name = {effect.name: effect for effect in existing}
    for planned in requested:
        current = by_name.get(planned.name)
        if current is None:
            if not allow_new:
                raise OperationConflictError(
                    f"operation effect plan is already frozen: {planned.name}"
                )
            by_name[planned.name] = planned
            continue
        for key, value in planned.metadata.items():
            if key not in current.metadata or current.metadata[key] != value:
                raise OperationConflictError(
                    f"operation effect has different planned metadata: {planned.name}.{key}"
                )
    return tuple(sorted(by_name.values(), key=lambda effect: effect.name))


def _effect_plan(effects: Iterable[OperationEffect]) -> tuple[OperationEffect, ...]:
    planned = _effects(effects)
    if any(effect.status != "pending" for effect in planned):
        raise ValueError("new operation effects must have pending status")
    return planned


def _effects(effects: Iterable[OperationEffect]) -> tuple[OperationEffect, ...]:
    items = tuple(effects)
    if any(not isinstance(effect, OperationEffect) for effect in items):
        raise ValueError("semantic operation effects must contain OperationEffect values")
    names = [effect.name for effect in items]
    if len(names) != len(set(names)):
        raise ValueError("semantic operation effect names must be unique")
    return tuple(sorted(items, key=lambda effect: effect.name))


def _current_owner() -> tuple[int, str | None]:
    pid = os.getpid()
    return pid, process_identity(pid)


def _is_current_owner(record: SemanticOperationRecord, pid: int, identity: str | None) -> bool:
    if record.owner_pid != pid:
        return False
    if record.owner_identity is None or identity is None:
        return True
    return record.owner_identity == identity


def _owner_is_live(record: SemanticOperationRecord) -> bool:
    if not process_exists(record.owner_pid):
        return False
    if record.owner_identity is None:
        return True
    current_identity = process_identity(record.owner_pid)
    if current_identity is None:
        return True
    return current_identity == record.owner_identity


def _operation_digest(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _input_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only durable replay data; ordinary semantic turns omit user content."""
    if value.get("kind") == "sync-repair":
        keys = (
            "role",
            "kind",
            "active_start_commit",
            "upstream_commit",
            "candidate_branch",
            "candidate_worktree",
            "pre_repair_tip",
            "expected_merge_parent",
            "merge_conflicted",
            "repair_input_sha256",
            "policy_sha256",
        )
        return {key: value[key] for key in keys if key in value}
    keys = ("role", "session_id", "kind")
    return {key: value[key] for key in keys if key in value}


def _canonical_json(value: object) -> str:
    _validate_json_value(value, "JSON value")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    raw = dict(value)
    try:
        normalized = json.loads(_canonical_json(raw))
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON values: {exc}") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return normalized


def _validate_json_value(value: object, field_name: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} must use string object keys")
            _validate_json_value(item, field_name)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_value(item, field_name)
        return
    raise ValueError(f"{field_name} contains a non-JSON value: {type(value).__name__}")


def _operation_id(value: object) -> str:
    operation_id = _required_string(value, "operation_id")
    if len(operation_id) > 512 or any(character in operation_id for character in "\0\r\n"):
        raise ValueError("operation_id must be a single line of at most 512 characters")
    return operation_id


def _effect_name(value: object) -> str:
    name = _required_string(value, "effect name")
    if len(name) > 200 or any(character in name for character in "\0\r\n"):
        raise ValueError("effect name must be a single line of at most 200 characters")
    return name


def _phase(value: object) -> OperationPhase:
    if not isinstance(value, str) or value not in PHASES:
        raise ValueError(f"operation phase must be one of: {', '.join(sorted(PHASES))}")
    return value  # type: ignore[return-value]


def _effect_status(value: object) -> EffectStatus:
    if not isinstance(value, str) or value not in EFFECT_STATUSES:
        raise ValueError(f"effect status must be one of: {', '.join(sorted(EFFECT_STATUSES))}")
    return value  # type: ignore[return-value]


def _changed_paths(values: Iterable[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("changed_paths must be a sequence of paths")
    paths = tuple(_required_string(value, "changed path") for value in values)
    if len(paths) != len(set(paths)):
        raise ValueError("changed_paths must not contain duplicates")
    return paths


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a SHA-256 hex digest") from exc
    return value


def _timestamp(value: object, field_name: str) -> str:
    text = _required_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return text


def _exact_keys(data: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise ValueError(f"{label} has invalid fields: {'; '.join(detail)}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
