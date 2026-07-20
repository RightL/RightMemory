from __future__ import annotations

from pathlib import Path

from .config import load_dreamer_watch_config, load_insight_watch_config
from .dreamer_trigger import DreamerTriggerStore
from .insight_trigger import InsightTriggerStore


def memory_change_pressure_points(memory_root: Path) -> tuple[float, float]:
    root = Path(memory_root)
    dreamer = load_dreamer_watch_config(memory_root=root)
    insight = load_insight_watch_config(memory_root=root)
    return dreamer.update_candidate_points, insight.update_candidate_points


def record_memory_change_pressure_once(
    memory_root: Path,
    operation_id: str,
    *,
    dreamer_points: float,
    insight_points: float,
) -> None:
    """Apply both pressure increments once for a completed semantic operation."""
    root = Path(memory_root)
    DreamerTriggerStore(root).increment_once(operation_id, dreamer_points)
    InsightTriggerStore(root).increment_once(operation_id, insight_points)


def forget_memory_change_pressure_operation(memory_root: Path, operation_id: str) -> None:
    root = Path(memory_root)
    DreamerTriggerStore(root).forget_operation(operation_id)
    InsightTriggerStore(root).forget_operation(operation_id)
