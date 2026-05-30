from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .isolated_write import IsolatedWriteSupervisor
from .session import _ensure_runtime_gitignore, _fsync_directory


INSTALL_STAMP_FILE = "install.stamp"
MANAGED_WATCH_TARGETS = ("review", "dreamer", "pruner", "insight", "sync")
WATCH_COMMANDS = {
    "review": ("review", "watch"),
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

    def __enter__(self) -> WatchLock:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise RuntimeError(f"another rightmemory {self.name} watch process is already running") from exc
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None


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


def managed_watch_status(memory_root: Path, name: str) -> ManagedWatchStatus:
    name = _watch_name(name)
    log_path = watch_log_path(memory_root, name)
    pid = _read_pid(watch_pid_path(memory_root, name))
    if pid is not None:
        if _is_managed_watch_process(pid, name):
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
        watch_pid_path(memory_root, name).unlink(missing_ok=True)

    cleanup_role = WATCH_CLEANUP_ROLES.get(name)
    if cleanup_role is not None:
        IsolatedWriteSupervisor(memory_root, cleanup_role).cleanup_stale()

    runtime_root = memory_root / ".runtime"
    _ensure_runtime_gitignore(runtime_root)
    watch_dir = runtime_root / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    log_path = watch_log_path(memory_root, name)
    command = [python_executable or sys.executable, "-m", "rightmemory.cli", *WATCH_COMMANDS[name]]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    _write_pid(watch_pid_path(memory_root, name), process.pid)
    return ManagedWatchStatus(name=name, state="running", pid=process.pid, log_path=log_path)


def stop_managed_watch(memory_root: Path, name: str, timeout_seconds: int = 30) -> StopWatchResult:
    name = _watch_name(name)
    if timeout_seconds < 0:
        raise ValueError("timeout must not be negative")
    status = managed_watch_status(memory_root, name)
    pid_path = watch_pid_path(memory_root, name)
    if status.state == "running" and status.pid is not None:
        os.kill(status.pid, signal.SIGTERM)
        if _wait_for_exit(status.pid, name, timeout_seconds):
            pid_path.unlink(missing_ok=True)
            return StopWatchResult(name=name, state="stopped", pid=status.pid, log_path=status.log_path)
        return StopWatchResult(name=name, state="stopping", pid=status.pid, log_path=status.log_path)
    if status.state == "stale":
        pid_path.unlink(missing_ok=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(f"{pid}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)


def _watch_lock_held(memory_root: Path, name: str) -> bool:
    path = memory_root / ".runtime" / "watch" / f"{name}.lock"
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    return False


def _is_managed_watch_process(pid: int, name: str) -> bool:
    if not _process_exists(pid):
        return False
    command = _process_command(pid)
    if command is None:
        return False
    expected = " ".join(WATCH_COMMANDS[name])
    return "rightmemory.cli" in command and expected in command


def _wait_for_exit(pid: int, name: str, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if not _is_managed_watch_process(pid, name):
            return True
        time.sleep(0.1)
    return not _is_managed_watch_process(pid, name)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raw = b""
    if raw:
        parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if parts:
            return " ".join(parts)
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None
