from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from .platform import lock_file, unlock_file
from .session import _ensure_runtime_gitignore, _fsync_directory


_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class WatchOperationStore:
    """Persist the one operation currently owned by a managed watch."""

    def __init__(self, memory_root: Path, role: str):
        if not _ROLE_RE.fullmatch(role):
            raise ValueError("watch role contains invalid characters")
        self.role = role
        self.runtime_root = Path(memory_root) / ".runtime"
        self.root = self.runtime_root / role
        self.state_path = self.root / "active-operation.json"
        self.lock_path = self.root / "active-operation.lock"

    def claim(self) -> str:
        with self._locked():
            operation_id = self._read_locked()
            if operation_id is not None:
                return operation_id
            operation_id = f"{self.role}-watch-{uuid.uuid4().hex}"
            self._write_locked(operation_id)
            return operation_id

    def complete(self, operation_id: str) -> bool:
        with self._locked():
            if self._read_locked() != operation_id:
                return False
            self.state_path.unlink(missing_ok=True)
            _fsync_directory(self.state_path.parent)
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

    def _read_locked(self) -> str | None:
        if not self.state_path.exists():
            return None
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        operation_id = data.get("operation_id") if isinstance(data, dict) else None
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError(f"invalid watch operation state: {self.state_path}")
        return operation_id

    def _write_locked(self, operation_id: str) -> None:
        temp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        content = json.dumps({"operation_id": operation_id}, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
            _fsync_directory(self.state_path.parent)
        finally:
            temp.unlink(missing_ok=True)
