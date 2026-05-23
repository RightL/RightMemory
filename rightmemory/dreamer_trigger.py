from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from .session import _ensure_runtime_gitignore, _fsync_directory


@dataclass(frozen=True)
class DreamerTriggerState:
    points: float = 0.0
    updated_at: str | None = None
    last_successful_dream_at: str | None = None
    last_recovery_at: str | None = None


class DreamerTriggerStore:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.runtime_root / "dreamer"
        self.state_path = self.root / "trigger-state.json"
        self.lock_path = self.root / "trigger-state.lock"

    def load(self) -> DreamerTriggerState:
        with self._locked():
            return self._read_locked()

    def read(self) -> DreamerTriggerState:
        return self.load()

    def increment(self, points: float) -> DreamerTriggerState:
        amount = _positive_finite_number(points, "points")
        with self._locked():
            state = self._read_locked()
            next_points = state.points + amount
            if not math.isfinite(next_points):
                raise ValueError("points balance must remain finite")
            next_state = replace(state, points=next_points, updated_at=_now())
            self._write_locked(next_state)
            return next_state

    def consume_if_available(self, threshold: float) -> bool:
        amount = _positive_finite_number(threshold, "threshold")
        with self._locked():
            state = self._read_locked()
            if state.points < amount:
                return False
            now = _now()
            next_state = replace(
                state,
                points=state.points - amount,
                updated_at=now,
                last_successful_dream_at=now,
            )
            self._write_locked(next_state)
            return True

    @contextmanager
    def _locked(self):
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_locked(self) -> DreamerTriggerState:
        if not self.state_path.exists():
            return DreamerTriggerState()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return _state_from_json(data)
        except (json.JSONDecodeError, OSError, OverflowError, TypeError, ValueError) as exc:
            return self._recover_locked(exc)

    def _recover_locked(self, reason: Exception) -> DreamerTriggerState:
        recovered_at = _now_dt()
        backup_path = self._corrupt_backup_path(recovered_at)
        os.replace(self.state_path, backup_path)
        _fsync_directory(self.root)
        print(
            f"Warning: recovered corrupt dreamer trigger state at {self.state_path}; "
            f"moved original to {backup_path}: {reason}",
            file=sys.stderr,
            flush=True,
        )
        state = DreamerTriggerState(points=0.0, last_recovery_at=_format_time(recovered_at))
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

    def _write_locked(self, state: DreamerTriggerState) -> None:
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


def _state_from_json(data: Any) -> DreamerTriggerState:
    if not isinstance(data, dict):
        raise ValueError("dreamer trigger state must be an object")
    points = _nonnegative_finite_number(data.get("points"), "points")
    return DreamerTriggerState(
        points=points,
        updated_at=_validate_optional_time(data.get("updated_at"), "updated_at"),
        last_successful_dream_at=_validate_optional_time(
            data.get("last_successful_dream_at"),
            "last_successful_dream_at",
        ),
        last_recovery_at=_validate_optional_time(data.get("last_recovery_at"), "last_recovery_at"),
    )


def _validate_state(state: DreamerTriggerState) -> None:
    _nonnegative_finite_number(state.points, "points")
    _validate_optional_time(state.updated_at, "updated_at")
    _validate_optional_time(state.last_successful_dream_at, "last_successful_dream_at")
    _validate_optional_time(state.last_recovery_at, "last_recovery_at")


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


def _now() -> str:
    return _format_time(_now_dt())


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
