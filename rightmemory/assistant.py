"""Native retrieval and sync, alongside the shared curation service."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .assistant_context import prepare_retrieve_context
from .config import load_sync_config
from .learning_service import LearningService
from .session import _safe_session_id
from .sync import SyncManager


class AssistantService:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser().resolve()
        if not self.memory_root.is_dir():
            raise FileNotFoundError(f"RightMemory memory root does not exist: {self.memory_root}")
        self.sync_config = load_sync_config(self.memory_root)

    def context(self, session_id: str) -> dict[str, Any]:
        _safe_session_id(session_id)
        return self._response({"session_id": session_id, **prepare_retrieve_context(self.memory_root)})

    def sync(self, action: str = "refresh") -> dict[str, Any]:
        manager = SyncManager(self.sync_config)
        if action == "refresh":
            result = manager.refresh_for_retrieve()
        elif action == "pull":
            result = manager.pull()
        elif action == "push":
            result = manager.push()
        else:
            raise ValueError("sync action must be refresh, pull, or push")
        return self._response({"sync": asdict(result)})

    def sync_repair(
        self, action: str, task_id: str | None = None, retry_task: str | None = None,
    ) -> dict[str, Any]:
        from .native_sync import NativeSyncRepair

        repair = NativeSyncRepair(self.memory_root)
        if retry_task is not None and action != "begin":
            raise ValueError("retry_task is only valid for repair begin")
        if task_id is not None and action == "begin":
            raise ValueError("repair begin uses retry_task, not task_id")
        if action == "begin":
            result = repair.begin(retry_task)
        elif action == "status":
            result = repair.status(task_id)
        elif action in {"validate", "complete"} and task_id is not None:
            result = repair.validate(task_id) if action == "validate" else repair.complete(task_id)
        else:
            raise ValueError("repair requires begin, status, or validate/complete with a task id")
        return self._response(result)

    def _response(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .native_sync import NativeSyncRepair

        return {"ok": True, **payload, "curation": LearningService(self.memory_root).notices(),
                "sync_repair": NativeSyncRepair(self.memory_root).notice()}
