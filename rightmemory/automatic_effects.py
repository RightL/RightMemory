from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

from .config import load_insight_watch_config
from .insight_trigger import InsightTriggerStore
from .semantic_operation import FINAL_PHASES, OperationEffect, SemanticOperationRecord, SemanticOperationStore
from .session import _ensure_durable_directory, _fsync_directory
from .shared_view_files import (
    prepare_file_view_publish_outbox, publish_file_view_outbox, record_file_view_publish_results,
)


PRESSURE_EFFECT = "memory-pressure"
PUBLISH_EFFECT = "file-view-publish"


def memory_change_pressure_points(memory_root: Path) -> float:
    root = Path(memory_root)
    insight = load_insight_watch_config(memory_root=root)
    return insight.update_candidate_points


def record_memory_change_pressure_once(
    memory_root: Path,
    operation_id: str,
    *,
    insight_points: float,
) -> None:
    """Increment Insight pressure once for a completed semantic operation."""
    root = Path(memory_root)
    InsightTriggerStore(root).increment_once(operation_id, insight_points)


def forget_memory_change_pressure_operation(memory_root: Path, operation_id: str) -> None:
    root = Path(memory_root)
    InsightTriggerStore(root).forget_operation(operation_id)


def memory_operation_effects(
    memory_root: Path, changed_paths: tuple[str, ...], commits: int,
) -> tuple[OperationEffect, ...]:
    effects = [OperationEffect(PUBLISH_EFFECT)]
    if commits and any(
        path == "MEMORY.md" or (path.startswith("MEMORY_") and path.endswith(".md"))
        for path in changed_paths
    ):
        effects.append(OperationEffect(
            PRESSURE_EFFECT, metadata={"insight_points": memory_change_pressure_points(memory_root)},
        ))
    return tuple(effects)


def prepare_memory_operation_effects(memory_root: Path, operation_id: str, source_root: Path) -> None:
    store = SemanticOperationStore(memory_root)
    prepare_file_view_publish_outbox(source_root, store.effect_state_root(operation_id, PUBLISH_EFFECT))


def apply_memory_operation_effect(
    memory_root: Path, record: SemanticOperationRecord, effect: OperationEffect,
) -> None:
    root = Path(memory_root)
    if effect.name == PRESSURE_EFFECT:
        points = effect.metadata.get("insight_points")
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points <= 0:
            raise ValueError("memory-pressure effect requires positive point values")
        record_memory_change_pressure_once(root, record.operation_id, insight_points=float(points))
        return
    if effect.name != PUBLISH_EFFECT:
        raise ValueError(f"unknown memory operation effect: {effect.name}")
    if record.outcome is None or record.phase not in FINAL_PHASES:
        raise RuntimeError("file-view publication requires a completed semantic operation")
    store = SemanticOperationStore(root)
    with store.publish_locked():
        if not advance_effect_watermark(root, PUBLISH_EFFECT, record.operation_id, record.outcome.sequence):
            return
        results = publish_file_view_outbox(
            store.effect_state_root(record.operation_id, PUBLISH_EFFECT),
            operation_id=record.operation_id, credential_root=root,
        )
        record_file_view_publish_results(root, results, trigger=f"{record.input_data.get('role', 'update')}-write")
        failures = [result for result in results if result.status == "failed"]
        if failures:
            detail = "; ".join(f"{result.view_id}: {result.message}" for result in failures)
            raise RuntimeError(f"file-view publish failed: {detail}")


def replay_memory_operation_effects(memory_root: Path, operation_id: str) -> list[dict]:
    """Replay durable side effects without running another curation task."""
    store = SemanticOperationStore(memory_root)
    with store.effects_locked(operation_id):
        record = store.read(operation_id)
        if record is None:
            raise FileNotFoundError(f"semantic operation does not exist: {operation_id}")
        for effect in store.list_pending_effects(operation_id):
            try:
                apply_memory_operation_effect(memory_root, record, effect)
                store.mark_effect(operation_id, effect.name, "done")
            except Exception as exc:
                store.mark_effect(operation_id, effect.name, "failed", error=f"{type(exc).__name__}: {exc}")
                continue
            if effect.name == PRESSURE_EFFECT:
                try:
                    forget_memory_change_pressure_operation(memory_root, operation_id)
                except (OSError, ValueError, RuntimeError):
                    pass
            if effect.name == PUBLISH_EFFECT:
                try:
                    store.clear_effect_state(operation_id, PUBLISH_EFFECT)
                except (OSError, ValueError, RuntimeError):
                    pass
        latest = store.read(operation_id)
        if latest is None:
            raise FileNotFoundError(f"semantic operation disappeared: {operation_id}")
        return [asdict(effect) for effect in latest.effects]


def advance_effect_watermark(
    state_root: Path, effect_name: str, operation_id: str, sequence: int,
) -> bool:
    path = Path(state_root) / ".runtime" / "operations" / "effects" / f"{effect_name}.json"
    current_key = None
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        current_id = data.get("operation_id") if isinstance(data, dict) else None
        current_sequence = data.get("sequence") if isinstance(data, dict) else None
        if not isinstance(current_id, str) or type(current_sequence) is not int or current_sequence < 1:
            raise ValueError(f"invalid semantic effect watermark: {path}")
        current_key = (current_sequence, current_id)
    if current_key is not None and current_key > (sequence, operation_id):
        return False
    _ensure_durable_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump({"operation_id": operation_id, "sequence": sequence}, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return True
