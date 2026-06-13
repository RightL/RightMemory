from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import MEMORY_ROOT_ENV
from ..session import _ensure_runtime_gitignore, _fsync_directory
from .auth import WEB_RUNTIME_DIR, ensure_web_auth_files


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


def web_settings_path(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR / "settings.json"


def web_service_status(memory_root: Path) -> WebServiceStatus:
    root = Path(memory_root)
    settings = _read_settings(root)
    log_path = web_log_path(root)
    pid = _read_pid(web_pid_path(root))
    if pid is not None:
        if _is_web_process(pid):
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
        web_pid_path(root).unlink(missing_ok=True)
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
    env = {**os.environ, MEMORY_ROOT_ENV: str(root)}
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    _write_pid(web_pid_path(root), process.pid)
    return WebServiceStatus("running", process.pid, host, port, log_path, generated_operator_token)


def stop_web_service(memory_root: Path, timeout_seconds: int = 30) -> StopWebResult:
    if timeout_seconds < 0:
        raise ValueError("timeout must not be negative")
    root = Path(memory_root)
    status = web_service_status(root)
    pid_path = web_pid_path(root)
    if status.state == "running" and status.pid is not None:
        os.kill(status.pid, signal.SIGTERM)
        if _wait_for_exit(status.pid, timeout_seconds):
            pid_path.unlink(missing_ok=True)
            return StopWebResult("stopped", status.pid, status.log_path)
        return StopWebResult("stopping", status.pid, status.log_path)
    if status.state == "stale":
        pid_path.unlink(missing_ok=True)
        return StopWebResult("stale-removed", status.pid, status.log_path)
    return StopWebResult(status.state, status.pid, status.log_path)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(f"{pid}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def _is_web_process(pid: int) -> bool:
    if not _process_exists(pid):
        return False
    command = _process_command(pid)
    return bool(command and "rightmemory.web.app" in command and "--serve" in command)


def _wait_for_exit(pid: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        if not _is_web_process(pid):
            return True
        time.sleep(0.1)
    return not _is_web_process(pid)


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
