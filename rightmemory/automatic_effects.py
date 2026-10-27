from __future__ import annotations

from pathlib import Path

from .config import load_insight_watch_config
from .insight_trigger import InsightTriggerStore


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
