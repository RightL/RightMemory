from __future__ import annotations

import errno
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import IO, Any


IS_WINDOWS = os.name == "nt"
WINDOWS_LOCK_RETRY_SECONDS = 0.05


def lock_file_nonblocking(handle: IO[Any]) -> None:
    if IS_WINDOWS:
        try:
            _lock_windows_file_nonblocking(handle)
        except OSError as exc:
            if not _is_windows_lock_contention(exc):
                raise
            raise BlockingIOError(errno.EWOULDBLOCK, "file lock is already held") from exc
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def lock_file(handle: IO[Any]) -> None:
    if IS_WINDOWS:
        while True:
            try:
                _lock_windows_file_nonblocking(handle)
                return
            except OSError as exc:
                if not _is_windows_lock_contention(exc):
                    raise
                time.sleep(WINDOWS_LOCK_RETRY_SECONDS)

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock_file(handle: IO[Any]) -> None:
    if IS_WINDOWS:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def prepare_command(command: list[str]) -> list[str]:
    if not command:
        raise ValueError("command must not be empty")
    if not IS_WINDOWS:
        return list(command)

    executable = _resolve_windows_executable(command[0])
    suffix = executable.suffix.lower()
    if suffix in {".cmd", ".bat"}:
        powershell_shim = executable.with_suffix(".ps1")
        if not powershell_shim.is_file():
            raise RuntimeError(
                f"Windows command {executable} is a batch shim without a matching PowerShell shim; "
                "install a native executable or a matching .ps1 shim"
            )
        executable = powershell_shim
        suffix = ".ps1"

    if suffix == ".ps1":
        powershell = _powershell_executable()
        return [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(executable),
            *command[1:],
        ]
    return [str(executable), *command[1:]]


def detached_process_kwargs() -> dict[str, object]:
    if not IS_WINDOWS:
        return {"start_new_session": True, "close_fds": True}

    flags = 0
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kwargs: dict[str, object] = {"close_fds": True}
    if flags:
        kwargs["creationflags"] = flags
    return kwargs


def python_module_child_env() -> dict[str, str]:
    env = os.environ.copy()
    source_root = _source_checkout_root()
    if source_root is None:
        return env
    source = str(source_root)
    existing = env.get("PYTHONPATH")
    entries = existing.split(os.pathsep) if existing else []
    if source not in entries:
        env["PYTHONPATH"] = os.pathsep.join([source, *entries])
    return env


def restart_current_process(command: list[str]) -> None:
    if not command:
        raise ValueError("command must not be empty")
    if not IS_WINDOWS:
        os.execv(command[0], command)
        raise AssertionError("os.execv returned unexpectedly")

    subprocess.Popen(command, env=os.environ.copy(), **detached_process_kwargs())
    raise SystemExit(0)


def process_exists(pid: int) -> bool:
    if pid < 1:
        return False
    if IS_WINDOWS:
        return _windows_process_exists(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command(pid: int) -> str | None:
    if pid < 1:
        return None
    if IS_WINDOWS:
        return _windows_process_command(pid)
    return _posix_process_command(pid)


def process_identity(pid: int) -> str | None:
    if pid < 1:
        return None
    if IS_WINDOWS:
        return _windows_process_identity(pid)
    return _posix_process_identity(pid)


def _posix_process_command(pid: int) -> str | None:
    raw = b""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        pass
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
            encoding="utf-8",
            errors="replace",
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


def _posix_process_identity(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    _, separator, fields = stat.rpartition(")")
    if not separator:
        return None
    parts = fields.split()
    if len(parts) <= 19:
        return None
    return f"proc:{parts[19]}"


def _windows_process_exists(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return _windows_process_command(pid) is not None

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_identity(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"win:{value}"
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_command(pid: int) -> str | None:
    script = (
        "$p = Get-CimInstance Win32_Process "
        f"-Filter 'ProcessId = {int(pid)}' -ErrorAction SilentlyContinue; "
        "if ($null -ne $p) { $p.CommandLine }"
    )
    for executable in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        try:
            result = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        command = result.stdout.strip()
        if command:
            return command
    return None


def _lock_windows_file_nonblocking(handle: IO[Any]) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _is_windows_lock_contention(exc: OSError) -> bool:
    contention_errnos = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    return exc.errno in contention_errnos or getattr(exc, "winerror", None) == 33


def _source_checkout_root() -> Path | None:
    project_root = Path(__file__).resolve().parents[1]
    if (project_root / "pyproject.toml").is_file() and (project_root / "rightmemory" / "__init__.py").is_file():
        return project_root
    return None


def _resolve_windows_executable(name: str) -> Path:
    raw = Path(name)
    if (raw.is_absolute() or raw.parent != Path(".")) and raw.is_file():
        return raw.resolve()
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"Windows command is not available on PATH: {name}")
    return Path(resolved).resolve()


def _powershell_executable() -> str:
    for name in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise FileNotFoundError("PowerShell is required to run a Windows .ps1 command shim")
