from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import MEMORY_ROOT_ENV
from ..platform import (
    detached_process_kwargs,
    process_command,
    process_exists,
    process_identity,
    python_module_child_env,
)
from ..session import _ensure_runtime_gitignore, _fsync_directory
from .auth import WEB_RUNTIME_DIR, ensure_web_auth_files


MANAGED_WEB_ENV = "RIGHTMEMORY_MANAGED_WEB"
WEB_START_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class WebServiceStatus:
    state: str
    pid: int | None
    host: str | None
    port: int | None
    log_path: Path
    generated_operator_token: str | None = None


@dataclass(frozen=True)
class StopWebResult:
    state: str
    pid: int | None
    log_path: Path


def web_pid_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "web.pid"


def web_log_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "web.log"


def web_stop_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "web.stop"


def web_identity_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "web.identity"


def web_ready_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "web.ready"


def web_settings_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "settings.json"


def web_service_status(memory_root: Path) -> WebServiceStatus:
    root = Path(memory_root)
    settings = _read_settings(root)
    log_path = web_log_path(root)
    pid = _read_pid(web_pid_path(root))
    if pid is not None:
        if _is_web_process(pid, memory_root=root):
            return WebServiceStatus("running", pid, settings.get("host"), _settings_port(settings), log_path)
        return WebServiceStatus("stale", pid, settings.get("host"), _settings_port(settings), log_path)
    return WebServiceStatus("stopped", None, settings.get("host"), _settings_port(settings), log_path)


def start_web_service(
    memory_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    python_executable: str | None = None,
) -> WebServiceStatus:
    root = Path(memory_root)
    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    status = web_service_status(root)
    if status.state == "running":
        return status
    if status.state == "stale":
        _clear_web_registration(root, status.pid)
    web_pid_path(root).unlink(missing_ok=True)
    web_identity_path(root).unlink(missing_ok=True)
    web_ready_path(root).unlink(missing_ok=True)
    web_stop_path(root).unlink(missing_ok=True)
    generated_operator_token = ensure_web_auth_files(root)
    runtime_dir = root / WEB_RUNTIME_DIR
    _ensure_runtime_gitignore(root / ".runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _write_settings(root, {"host": host, "port": port})
    log_path = web_log_path(root)
    command = [
        python_executable or sys.executable,
        "-m",
        "rightmemory.web.app",
        "--serve",
        "--memory-root",
        str(root),
        "--host",
        host,
        "--port",
        str(port),
    ]
    env = {**_web_child_env(root), MANAGED_WEB_ENV: "1"}
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            **detached_process_kwargs(),
        )
    pid = _wait_for_web_registration(root, process)
    return WebServiceStatus("running", pid, host, port, log_path, generated_operator_token)


def stop_web_service(memory_root: Path, timeout_seconds: int = 30) -> StopWebResult:
    if timeout_seconds < 0:
        raise ValueError("timeout must not be negative")
    root = Path(memory_root)
    status = web_service_status(root)
    if status.state == "running" and status.pid is not None:
        identity = _read_identity(web_identity_path(root))
        _write_pid(web_stop_path(root), status.pid)
        if _wait_for_exit(status.pid, timeout_seconds, identity=identity):
            _clear_web_registration(root, status.pid)
            _unlink_if_pid(web_stop_path(root), status.pid)
            return StopWebResult("stopped", status.pid, status.log_path)
        return StopWebResult("stopping", status.pid, status.log_path)
    if status.state == "stale":
        _clear_web_registration(root, status.pid)
        web_stop_path(root).unlink(missing_ok=True)
        return StopWebResult("stale-removed", status.pid, status.log_path)
    return StopWebResult(status.state, status.pid, status.log_path)


def consume_web_stop_request(memory_root: Path, pid: int) -> bool:
    path = web_stop_path(memory_root)
    requested_pid = _read_pid(path)
    if requested_pid != pid:
        if requested_pid is not None and not process_exists(requested_pid):
            path.unlink(missing_ok=True)
        return False
    path.unlink(missing_ok=True)
    return True


def clear_web_process_files(memory_root: Path, pid: int) -> None:
    _clear_web_registration(memory_root, pid)
    _unlink_if_pid(web_stop_path(memory_root), pid)
    _unlink_if_pid(web_ready_path(memory_root), pid)


def format_web_status(status: WebServiceStatus) -> str:
    if status.state == "running" and status.pid is not None:
        message = f"web: running pid {status.pid}, url http://{status.host}:{status.port}/, log {status.log_path}"
        if status.generated_operator_token:
            message += f", operator token {status.generated_operator_token}"
        return message
    if status.state == "stale" and status.pid is not None:
        return f"web: stale pid {status.pid}"
    return "web: stopped"


def format_stop_result(result: StopWebResult) -> str:
    if result.state == "stopped" and result.pid is not None:
        return f"web: stopped pid {result.pid}"
    if result.state == "stopping" and result.pid is not None:
        return f"web: stopping pid {result.pid}"
    if result.state == "stale-removed" and result.pid is not None:
        return f"web: removed stale pid {result.pid}"
    return "web: stopped"


def _web_child_env(memory_root: Path) -> dict[str, str]:
    env = python_module_child_env()
    env[MEMORY_ROOT_ENV] = str(memory_root)
    return env


def _read_settings(memory_root: Path) -> dict[str, object]:
    try:
        data = json.loads(web_settings_path(memory_root).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"host": "127.0.0.1", "port": 8766}
    if not isinstance(data, dict):
        return {"host": "127.0.0.1", "port": 8766}
    return data


def _write_settings(memory_root: Path, settings: dict[str, object]) -> None:
    path = web_settings_path(memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def _settings_port(settings: dict[str, object]) -> int | None:
    port = settings.get("port")
    return port if isinstance(port, int) else None


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        pid = int(value)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _write_pid(path: Path, pid: int) -> None:
    _write_value(path, str(pid))


def _write_value(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(f"{value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def _read_identity(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def register_web_process(memory_root: Path, pid: int, *, ready: bool = False) -> None:
    identity_path = web_identity_path(memory_root)
    identity = process_identity(pid)
    if identity is None:
        identity_path.unlink(missing_ok=True)
    else:
        _write_value(identity_path, identity)
    _write_pid(web_pid_path(memory_root), pid)
    if ready:
        _write_pid(web_ready_path(memory_root), pid)


def _clear_web_registration(memory_root: Path, pid: int | None) -> None:
    if pid is None or _read_pid(web_pid_path(memory_root)) != pid:
        return
    web_pid_path(memory_root).unlink(missing_ok=True)
    web_identity_path(memory_root).unlink(missing_ok=True)


def _wait_for_web_registration(
    memory_root: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float = WEB_START_TIMEOUT_SECONDS,
) -> int:
    if not callable(getattr(process, "poll", None)):
        register_web_process(memory_root, process.pid, ready=True)
        return process.pid
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        pid = _read_pid(web_ready_path(memory_root))
        identity = _read_identity(web_identity_path(memory_root))
        if pid is not None:
            if identity is not None and process_identity(pid) == identity:
                return pid
            if identity is None and _is_web_process(pid):
                return pid
        time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
    raise RuntimeError(f"rightmemory web service did not register within {timeout_seconds:g} seconds")


def _unlink_if_pid(path: Path, pid: int) -> None:
    if _read_pid(path) == pid:
        path.unlink(missing_ok=True)


def _is_web_process(pid: int, *, memory_root: Path | None = None) -> bool:
    if not _process_exists(pid):
        return False
    if memory_root is not None:
        identity = _read_identity(web_identity_path(memory_root))
        if identity is not None:
            return process_identity(pid) == identity
    command = _process_command(pid)
    return bool(command and "rightmemory.web.app" in command and "--serve" in command)


def _wait_for_exit(pid: int, timeout_seconds: int, *, identity: str | None = None) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if not _web_process_matches(pid, identity):
            return True
        time.sleep(0.1)
    return not _web_process_matches(pid, identity)


def _web_process_matches(pid: int, identity: str | None) -> bool:
    if identity is not None:
        return process_identity(pid) == identity
    return _is_web_process(pid)


def _process_exists(pid: int) -> bool:
    return process_exists(pid)


def _process_command(pid: int) -> str | None:
    return process_command(pid)
