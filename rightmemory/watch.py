from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MEMORY_ROOT_ENV
from .isolated_write import IsolatedWriteSupervisor
from .platform import (
    detached_process_kwargs,
    lock_file,
    lock_file_nonblocking,
    process_command,
    process_exists,
    process_identity,
    unlock_file,
)
from .session import _ensure_runtime_gitignore, _fsync_directory


INSTALL_STAMP_FILE = "install.stamp"
MANAGED_WATCH_ENV = "RIGHTMEMORY_MANAGED_WATCH"
WATCH_HANDOFF_PID_ENV = "RIGHTMEMORY_WATCH_HANDOFF_PID"
WATCH_START_TIMEOUT_SECONDS = 10
MANAGED_WATCH_TARGETS = ("review", "update-review", "dreamer", "pruner", "insight", "sync")
WATCH_COMMANDS = {
    "review": ("review", "watch"),
    "update-review": ("update-review", "watch"),
    "dreamer": ("dreamer", "watch"),
    "pruner": ("prune", "watch"),
    "insight": ("insight", "watch"),
    "sync": ("sync", "watch"),
}
WATCH_CLEANUP_ROLES = {
    "review": "reviewer",
    "dreamer": "dreamer",
    "pruner": "pruner",
    "insight": "insight",
}


class WatchLock:
    def __init__(self, memory_root: Path, name: str):
        self.memory_root = memory_root
        self.name = name
        self.path = memory_root / ".runtime" / "watch" / f"{name}.lock"
        self._lock_handle: Any | None = None
        self._managed = False

    def __enter__(self) -> WatchLock:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.path.open("a+", encoding="utf-8")
        handoff_pid = _positive_int(os.environ.pop(WATCH_HANDOFF_PID_ENV, ""))
        try:
            lock_file_nonblocking(self._lock_handle)
        except BlockingIOError as exc:
            if handoff_pid is None:
                self._lock_handle.close()
                self._lock_handle = None
                raise RuntimeError(f"another rightmemory {self.name} watch process is already running") from exc
            lock_file(self._lock_handle)
        self._managed = os.environ.get(MANAGED_WATCH_ENV) == self.name
        if self._managed:
            current_pid = os.getpid()
            _register_watch_process(self.memory_root, self.name, current_pid)
            if handoff_pid is not None:
                _retarget_stop_request(self.memory_root, self.name, handoff_pid, current_pid)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock_handle is None:
            return
        try:
            unlock_file(self._lock_handle)
        finally:
            self._lock_handle.close()
            self._lock_handle = None
            if self._managed:
                current_pid = os.getpid()
                _clear_watch_registration(self.memory_root, self.name, current_pid)
                _unlink_if_pid(watch_stop_path(self.memory_root, self.name), current_pid)


class InstallStamp:
    def __init__(self, memory_root: Path):
        self.path = install_stamp_path(memory_root)
        self.initial_value = self.read()

    def read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def changed(self) -> bool:
        return self.read() != self.initial_value


@dataclass(frozen=True)
class ManagedWatchStatus:
    name: str
    state: str
    pid: int | None
    log_path: Path


@dataclass(frozen=True)
class StopWatchResult:
    name: str
    state: str
    pid: int | None
    log_path: Path


def install_stamp_path(memory_root: Path) -> Path:
    return memory_root / ".runtime" / INSTALL_STAMP_FILE


def watch_pid_path(memory_root: Path, name: str) -> Path:
    _watch_name(name)
    return memory_root / ".runtime" / "watch" / f"{name}.pid"


def watch_log_path(memory_root: Path, name: str) -> Path:
    _watch_name(name)
    return memory_root / ".runtime" / "watch" / f"{name}.log"


def watch_stop_path(memory_root: Path, name: str) -> Path:
    _watch_name(name)
    return memory_root / ".runtime" / "watch" / f"{name}.stop"


def watch_identity_path(memory_root: Path, name: str) -> Path:
    _watch_name(name)
    return memory_root / ".runtime" / "watch" / f"{name}.identity"


def consume_watch_stop_request(memory_root: Path, name: str, pid: int) -> bool:
    path = watch_stop_path(memory_root, name)
    requested_pid = _read_pid(path)
    if requested_pid != pid:
        if requested_pid is not None and not process_exists(requested_pid):
            path.unlink(missing_ok=True)
        return False
    path.unlink(missing_ok=True)
    return True


def managed_watch_status(memory_root: Path, name: str) -> ManagedWatchStatus:
    name = _watch_name(name)
    log_path = watch_log_path(memory_root, name)
    pid = _read_pid(watch_pid_path(memory_root, name))
    if pid is not None:
        if _is_managed_watch_process(pid, name, memory_root=memory_root):
            return ManagedWatchStatus(name=name, state="running", pid=pid, log_path=log_path)
        if _watch_lock_held(memory_root, name):
            return ManagedWatchStatus(name=name, state="external", pid=None, log_path=log_path)
        return ManagedWatchStatus(name=name, state="stale", pid=pid, log_path=log_path)
    if _watch_lock_held(memory_root, name):
        return ManagedWatchStatus(name=name, state="external", pid=None, log_path=log_path)
    return ManagedWatchStatus(name=name, state="stopped", pid=None, log_path=log_path)


def start_managed_watch(memory_root: Path, name: str, python_executable: str | None = None) -> ManagedWatchStatus:
    name = _watch_name(name)
    status = managed_watch_status(memory_root, name)
    if status.state == "running":
        return status
    if status.state == "external":
        raise RuntimeError(f"rightmemory {name} watch is already running outside the manager")
    if status.state == "stale":
        _clear_watch_registration(memory_root, name, status.pid)
    watch_pid_path(memory_root, name).unlink(missing_ok=True)
    watch_identity_path(memory_root, name).unlink(missing_ok=True)
    watch_stop_path(memory_root, name).unlink(missing_ok=True)

    cleanup_role = WATCH_CLEANUP_ROLES.get(name)
    if cleanup_role is not None:
        IsolatedWriteSupervisor(memory_root, cleanup_role).cleanup_stale()

    runtime_root = memory_root / ".runtime"
    _ensure_runtime_gitignore(runtime_root)
    watch_dir = runtime_root / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    log_path = watch_log_path(memory_root, name)
    command = [python_executable or sys.executable, "-m", "rightmemory.cli", *WATCH_COMMANDS[name]]
    env = {**os.environ, MEMORY_ROOT_ENV: str(memory_root), MANAGED_WATCH_ENV: name}
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(memory_root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            **detached_process_kwargs(),
        )
    pid = _wait_for_watch_registration(memory_root, name, process)
    return ManagedWatchStatus(name=name, state="running", pid=pid, log_path=log_path)


def stop_managed_watch(memory_root: Path, name: str, timeout_seconds: int = 30) -> StopWatchResult:
    name = _watch_name(name)
    if timeout_seconds < 0:
        raise ValueError("timeout must not be negative")
    status = managed_watch_status(memory_root, name)
    if status.state == "running" and status.pid is not None:
        identity = _read_identity(watch_identity_path(memory_root, name))
        _write_pid(watch_stop_path(memory_root, name), status.pid)
        if _wait_for_exit(status.pid, name, timeout_seconds, identity=identity):
            _clear_watch_registration(memory_root, name, status.pid)
            _unlink_if_pid(watch_stop_path(memory_root, name), status.pid)
            return StopWatchResult(name=name, state="stopped", pid=status.pid, log_path=status.log_path)
        return StopWatchResult(name=name, state="stopping", pid=status.pid, log_path=status.log_path)
    if status.state == "stale":
        _clear_watch_registration(memory_root, name, status.pid)
        watch_stop_path(memory_root, name).unlink(missing_ok=True)
        return StopWatchResult(name=name, state="stale-removed", pid=status.pid, log_path=status.log_path)
    return StopWatchResult(name=name, state=status.state, pid=status.pid, log_path=status.log_path)


def _watch_name(name: str) -> str:
    if name not in MANAGED_WATCH_TARGETS:
        joined = ", ".join(MANAGED_WATCH_TARGETS)
        raise ValueError(f"watch target must be one of: {joined}")
    return name


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        pid = int(value)
    except ValueError:
        return None
    if pid < 1:
        return None
    return pid


def _write_pid(path: Path, pid: int) -> None:
    _write_value(path, str(pid))


def _write_value(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(f"{value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)


def _read_identity(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _register_watch_process(memory_root: Path, name: str, pid: int) -> None:
    identity_path = watch_identity_path(memory_root, name)
    identity = process_identity(pid)
    if identity is None:
        identity_path.unlink(missing_ok=True)
    else:
        _write_value(identity_path, identity)
    _write_pid(watch_pid_path(memory_root, name), pid)


def _clear_watch_registration(memory_root: Path, name: str, pid: int | None) -> None:
    if pid is None or _read_pid(watch_pid_path(memory_root, name)) != pid:
        return
    watch_pid_path(memory_root, name).unlink(missing_ok=True)
    watch_identity_path(memory_root, name).unlink(missing_ok=True)


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _unlink_if_pid(path: Path, pid: int) -> None:
    if _read_pid(path) == pid:
        path.unlink(missing_ok=True)


def _retarget_stop_request(memory_root: Path, name: str, old_pid: int, new_pid: int) -> None:
    path = watch_stop_path(memory_root, name)
    if _read_pid(path) == old_pid:
        _write_pid(path, new_pid)


def _wait_for_watch_registration(
    memory_root: Path,
    name: str,
    process: subprocess.Popen[bytes],
    timeout_seconds: float = WATCH_START_TIMEOUT_SECONDS,
) -> int:
    if not callable(getattr(process, "poll", None)):
        _register_watch_process(memory_root, name, process.pid)
        return process.pid
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        pid = _read_pid(watch_pid_path(memory_root, name))
        identity = _read_identity(watch_identity_path(memory_root, name))
        if pid is not None:
            if identity is not None and process_identity(pid) == identity:
                return pid
            if identity is None and _is_managed_watch_process(pid, name):
                return pid
        time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
    raise RuntimeError(f"rightmemory {name} watch did not register within {timeout_seconds:g} seconds")


def _watch_lock_held(memory_root: Path, name: str) -> bool:
    path = memory_root / ".runtime" / "watch" / f"{name}.lock"
    if not path.exists():
        return False
    locked = False
    with path.open("a+", encoding="utf-8") as handle:
        try:
            lock_file_nonblocking(handle)
            locked = True
        except BlockingIOError:
            return True
        finally:
            if locked:
                unlock_file(handle)
    return False


def _is_managed_watch_process(pid: int, name: str, *, memory_root: Path | None = None) -> bool:
    if not _process_exists(pid):
        return False
    if memory_root is not None:
        identity = _read_identity(watch_identity_path(memory_root, name))
        if identity is not None:
            return process_identity(pid) == identity
    command = _process_command(pid)
    if command is None:
        return False
    expected = " ".join(WATCH_COMMANDS[name])
    return "rightmemory.cli" in command and expected in command


def _wait_for_exit(pid: int, name: str, timeout_seconds: int, *, identity: str | None = None) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if not _watch_process_matches(pid, name, identity):
            return True
        time.sleep(0.1)
    return not _watch_process_matches(pid, name, identity)


def _watch_process_matches(pid: int, name: str, identity: str | None) -> bool:
    if identity is not None:
        return process_identity(pid) == identity
    return _is_managed_watch_process(pid, name)


def _process_exists(pid: int) -> bool:
    return process_exists(pid)


def _process_command(pid: int) -> str | None:
    return process_command(pid)
