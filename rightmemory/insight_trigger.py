from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import json
import math
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .platform import lock_file, unlock_file
from .session import _ensure_runtime_gitignore, _fsync_directory


@dataclass(frozen=True)
class InsightTriggerState:
    points: float = 0.0
    updated_at: str | None = None
    last_successful_insight_at: str | None = None
    last_successful_insight_result: str | None = None
    last_recovery_at: str | None = None
    applied_operation_ids: tuple[str, ...] = ()
    active_operation_id: str | None = None
    active_operation_points: float | None = None


class InsightTriggerStore:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.runtime_root / "insight"
        self.state_path = self.root / "trigger-state.json"
        self.lock_path = self.root / "trigger-state.lock"

    def load(self) -> InsightTriggerState:
        with self._locked():
            return self._read_locked()

    def read(self) -> InsightTriggerState:
        return self.load()

    def increment(self, points: float) -> InsightTriggerState:
        amount = _positive_finite_number(points, "points")
        with self._locked():
            state = self._read_locked()
            next_points = state.points + amount
            if not math.isfinite(next_points):
                raise ValueError("points balance must remain finite")
            next_state = replace(state, points=next_points, updated_at=_now())
            self._write_locked(next_state)
            return next_state

    def increment_once(self, operation_id: str, points: float) -> InsightTriggerState:
        clean_operation_id = _operation_id(operation_id)
        amount = _positive_finite_number(points, "points")
        with self._locked():
            state = self._read_locked()
            if clean_operation_id in state.applied_operation_ids:
                return state
            next_points = state.points + amount
            if not math.isfinite(next_points):
                raise ValueError("points balance must remain finite")
            # The points and receipt must land in the same atomic state write.
            next_state = replace(
                state,
                points=next_points,
                updated_at=_now(),
                applied_operation_ids=(*state.applied_operation_ids, clean_operation_id),
            )
            self._write_locked(next_state)
            return next_state

    def forget_operation(self, operation_id: str) -> None:
        clean_operation_id = _operation_id(operation_id)
        with self._locked():
            state = self._read_locked()
            if clean_operation_id not in state.applied_operation_ids:
                return
            self._write_locked(
                replace(
                    state,
                    applied_operation_ids=tuple(
                        item for item in state.applied_operation_ids if item != clean_operation_id
                    ),
                )
            )

    def consume_if_available(self, threshold: float, result: str | None = None) -> bool:
        amount = _positive_finite_number(threshold, "threshold")
        result = _validate_optional_result(result, "result")
        with self._locked():
            state = self._read_locked()
            if state.points < amount:
                return False
            now = _now()
            next_state = replace(
                state,
                points=state.points - amount,
                updated_at=now,
                last_successful_insight_at=now,
                last_successful_insight_result=result,
            )
            self._write_locked(next_state)
            return True

    def claim_operation(self, threshold: float) -> str | None:
        amount = _positive_finite_number(threshold, "threshold")
        with self._locked():
            state = self._read_locked()
            if state.active_operation_id is not None:
                if state.active_operation_points is None:
                    self._write_locked(replace(state, active_operation_points=amount, updated_at=_now()))
                return state.active_operation_id
            if state.points < amount:
                return None
            operation_id = f"insight-watch-{uuid.uuid4().hex}"
            self._write_locked(
                replace(
                    state,
                    active_operation_id=operation_id,
                    active_operation_points=amount,
                    updated_at=_now(),
                )
            )
            return operation_id

    def complete_operation(self, operation_id: str, threshold: float, *, result: str | None = None) -> bool:
        clean_operation_id = _operation_id(operation_id)
        fallback_amount = _positive_finite_number(threshold, "threshold")
        clean_result = _validate_optional_result(result, "result")
        with self._locked():
            state = self._read_locked()
            if state.active_operation_id != clean_operation_id:
                return False
            amount = state.active_operation_points or fallback_amount
            if state.points < amount:
                raise RuntimeError("insight trigger points changed below the claimed threshold")
            now = _now()
            self._write_locked(
                replace(
                    state,
                    points=state.points - amount,
                    updated_at=now,
                    last_successful_insight_at=now,
                    last_successful_insight_result=clean_result,
                    active_operation_id=None,
                    active_operation_points=None,
                )
            )
            return True

    @contextmanager
    def _locked(self):
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    def _read_locked(self) -> InsightTriggerState:
        if not self.state_path.exists():
            return InsightTriggerState()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return _state_from_json(data)
        except (json.JSONDecodeError, OSError, OverflowError, TypeError, ValueError) as exc:
            return self._recover_locked(exc)

    def _recover_locked(self, reason: Exception) -> InsightTriggerState:
        recovered_at = _now_dt()
        backup_path = self._corrupt_backup_path(recovered_at)
        os.replace(self.state_path, backup_path)
        _fsync_directory(self.root)
        print(
            f"Warning: recovered corrupt insight trigger state at {self.state_path}; "
            f"moved original to {backup_path}: {reason}",
            file=sys.stderr,
            flush=True,
        )
        state = InsightTriggerState(points=0.0, last_recovery_at=_format_time(recovered_at))
        self._write_locked(state)
        return state

    def _corrupt_backup_path(self, recovered_at: datetime) -> Path:
        candidate_at = recovered_at
        while True:
            timestamp = candidate_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = self.root / f"trigger-state.corrupt-{timestamp}.json"
            if not backup_path.exists():
                return backup_path
            candidate_at += timedelta(seconds=1)

    def _write_locked(self, state: InsightTriggerState) -> None:
        _validate_state(state)
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        content = json.dumps(
            asdict(state),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.state_path)
        _fsync_directory(self.state_path.parent)


def _state_from_json(data: Any) -> InsightTriggerState:
    if not isinstance(data, dict):
        raise ValueError("insight trigger state must be an object")
    points = _nonnegative_finite_number(data.get("points"), "points")
    return InsightTriggerState(
        points=points,
        updated_at=_validate_optional_time(data.get("updated_at"), "updated_at"),
        last_successful_insight_at=_validate_optional_time(
            data.get("last_successful_insight_at"),
            "last_successful_insight_at",
        ),
        last_successful_insight_result=_validate_optional_result(
            data.get("last_successful_insight_result"),
            "last_successful_insight_result",
        ),
        last_recovery_at=_validate_optional_time(data.get("last_recovery_at"), "last_recovery_at"),
        applied_operation_ids=_operation_ids(data.get("applied_operation_ids", [])),
        active_operation_id=_optional_operation_id(data.get("active_operation_id")),
        active_operation_points=(
            None
            if data.get("active_operation_points") is None
            else _positive_finite_number(data.get("active_operation_points"), "active_operation_points")
        ),
    )


def _validate_state(state: InsightTriggerState) -> None:
    _nonnegative_finite_number(state.points, "points")
    _validate_optional_time(state.updated_at, "updated_at")
    _validate_optional_time(state.last_successful_insight_at, "last_successful_insight_at")
    _validate_optional_result(state.last_successful_insight_result, "last_successful_insight_result")
    _validate_optional_time(state.last_recovery_at, "last_recovery_at")
    _operation_ids(list(state.applied_operation_ids))
    _optional_operation_id(state.active_operation_id)
    if state.active_operation_points is not None:
        _positive_finite_number(state.active_operation_points, "active_operation_points")


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        raise ValueError("operation_id must be a nonempty single-line string")
    return value


def _operation_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("applied_operation_ids must be a list")
    operation_ids = tuple(_operation_id(item) for item in value)
    if len(set(operation_ids)) != len(operation_ids):
        raise ValueError("applied_operation_ids must not contain duplicates")
    return operation_ids


def _optional_operation_id(value: object) -> str | None:
    return None if value is None else _operation_id(value)


def _positive_finite_number(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number <= 0:
        raise ValueError(f"{field} must be a positive finite number")
    return number


def _nonnegative_finite_number(value: object, field: str) -> float:
    number = _finite_number(value, field)
    if number < 0:
        raise ValueError(f"{field} must be a nonnegative finite number")
    return number


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _validate_optional_time(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime string or null") from exc
    return value


def _validate_optional_result(value: object, field: str) -> str | None:
    if value is None:
        return None
    if value not in {"artifact", "noop"}:
        raise ValueError(f"{field} must be artifact, noop, or null")
    return value


def _now() -> str:
    return _format_time(_now_dt())


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
