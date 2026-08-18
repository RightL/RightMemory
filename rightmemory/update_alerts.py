from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .async_update import (
    STATUS_MANUAL_RECOVERY,
    _is_legacy_failed_pending_state,
    _state_from_json,
)
from .update_queue import UpdateQueueStore


@dataclass(frozen=True)
class UpdateRecoverySummary:
    local_candidates: int = 0
    local_sessions: int = 0
    synchronized_candidates: int = 0

    @property
    def required(self) -> bool:
        return bool(self.local_candidates or self.synchronized_candidates)

    def warning(self) -> str | None:
        if not self.required:
            return None

        scopes: list[str] = []
        if self.local_candidates:
            scopes.append(
                f"{self.local_candidates} local "
                f"{_plural('candidate', self.local_candidates)} across "
                f"{self.local_sessions} {_plural('session', self.local_sessions)}"
            )
        if self.synchronized_candidates:
            scopes.append(
                f"{self.synchronized_candidates} synchronized "
                f"{_plural('candidate', self.synchronized_candidates)}"
            )
        return (
            f"RightMemory has {' and '.join(scopes)} requiring manual recovery. "
            "Tell the user to run `rightmemory update retry`; do not resubmit queued evidence."
        )


def collect_update_recovery_summary(memory_root: Path) -> UpdateRecoverySummary:
    root = Path(memory_root)
    local_candidates, local_sessions = _local_recovery_counts(root)
    synchronized_candidates = sum(
        len(recovery.candidate_uids)
        for recovery in UpdateQueueStore(root).snapshot().recoveries
        if recovery.manual_recovery
    )
    return UpdateRecoverySummary(
        local_candidates=local_candidates,
        local_sessions=local_sessions,
        synchronized_candidates=synchronized_candidates,
    )


def _local_recovery_counts(memory_root: Path) -> tuple[int, int]:
    state_root = memory_root / ".runtime" / "async" / "update"
    if not state_root.exists() and not state_root.is_symlink():
        return 0, 0
    if state_root.is_symlink() or not state_root.is_dir():
        raise ValueError("async update state root must be a directory")

    candidates = 0
    sessions = 0
    for path in sorted(state_root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"async update state must be a regular file: {path.name}")
        try:
            state = _state_from_json(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid async update state JSON: {path.name}") from exc
        if state.role != "update":
            raise ValueError(
                f"async update state role mismatch in {path.name}: "
                f"expected update, got {state.role}"
            )
        manual_recovery = (
            state.status == STATUS_MANUAL_RECOVERY
            or _is_legacy_failed_pending_state(state)
        )
        count = len(state.current_batch) + len(state.pending)
        if manual_recovery and count:
            candidates += count
            sessions += 1
    return candidates, sessions


def _plural(noun: str, count: int) -> str:
    return noun if count == 1 else noun + "s"
