from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


CODEX_BINARY_ENV = "RIGHTMEMORY_CODEX_BINARY"
SSH_BINARY_ENV = "RIGHTMEMORY_SSH_BINARY"
DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS = 10
REMOTE_CODEX_APP_SERVER_COMMAND = shlex.join(
    ("codex", "app-server", "--listen", "stdio://")
)

_SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REMOTE_ATTACHMENT_NAME = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|txt)\Z")

_REMOTE_ATTACHMENT_SCRIPT = """\
import hashlib, os, pathlib, sys, tempfile
name, expected_size, expected_hash = sys.argv[1:]
expected_size = int(expected_size)
root = pathlib.Path.home() / '.cache' / 'rightmemory' / 'attachments'
root.mkdir(parents=True, exist_ok=True)
data = sys.stdin.buffer.read(expected_size + 1)
if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
    raise SystemExit(23)
descriptor, temporary = tempfile.mkstemp(prefix='.upload-', dir=root)
try:
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    destination = root / name
    os.replace(temporary, destination)
    print(destination.resolve())
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
"""

_REMOTE_ATTACHMENT_DELETE_SCRIPT = """\
import pathlib, re, sys
name = sys.argv[1]
if re.fullmatch(r'[0-9a-f]{32}\\.(?:png|jpg|txt)', name) is None:
    raise SystemExit(24)
path = pathlib.Path.home() / '.cache' / 'rightmemory' / 'attachments' / name
try:
    path.unlink()
except FileNotFoundError:
    pass
"""


class TransportConfigurationError(ValueError):
    """A host cannot be launched safely with its current configuration."""


class AttachmentStagingError(RuntimeError):
    """A managed attachment could not be copied to a configured SSH host."""


class AttachmentCleanupError(RuntimeError):
    """A remotely staged attachment could not be removed from an SSH host."""


@dataclass(frozen=True, slots=True)
class SubprocessTransport:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None

    def spawn(self) -> subprocess.Popen[str]:
        return subprocess.Popen(
            list(self.argv),
            cwd=str(self.cwd) if self.cwd is not None else None,
            env=dict(self.env) if self.env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )


def build_local_transport(
    binary: str | os.PathLike[str] | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> SubprocessTransport:
    env = _environment(environment)
    executable = resolve_codex_binary(binary, environment=env)
    local_cwd = Path(cwd).resolve() if cwd is not None else None
    return SubprocessTransport(
        (executable, "app-server", "--listen", "stdio://"),
        cwd=local_cwd,
        env=env,
    )


def build_ssh_transport(
    ssh_alias: str,
    *,
    ssh_binary: str | os.PathLike[str] | None = None,
    connect_timeout_seconds: int = DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> SubprocessTransport:
    alias = validate_ssh_alias(ssh_alias)
    if isinstance(connect_timeout_seconds, bool) or not isinstance(connect_timeout_seconds, int):
        raise TransportConfigurationError("SSH connect timeout must be an integer")
    if connect_timeout_seconds < 1 or connect_timeout_seconds > 60:
        raise TransportConfigurationError("SSH connect timeout must be between 1 and 60 seconds")
    env = _environment(environment)
    executable = resolve_ssh_binary(ssh_binary, environment=env)
    argv = (
        executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        alias,
        REMOTE_CODEX_APP_SERVER_COMMAND,
    )
    return SubprocessTransport(argv, env=env)


def transport_for_host(
    host: Any,
    *,
    local_cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> SubprocessTransport:
    kind = _host_value(host, "kind")
    if kind == "local":
        return build_local_transport(
            _host_value(host, "codex_command_override"),
            cwd=local_cwd,
            environment=environment,
        )
    if kind == "ssh":
        override = _host_value(host, "codex_command_override")
        if override:
            raise TransportConfigurationError(
                "remote Codex command overrides are not supported; configure codex on the SSH host PATH"
            )
        alias = _host_value(host, "ssh_alias")
        if not isinstance(alias, str):
            raise TransportConfigurationError("SSH host is missing its configured alias")
        return build_ssh_transport(alias, environment=environment)
    raise TransportConfigurationError(f"unsupported conversation host kind: {kind!r}")


def stage_ssh_attachment(
    ssh_alias: str,
    source: str | os.PathLike[str],
    remote_name: str,
    *,
    expected_size: int,
    expected_sha256: str,
    ssh_binary: str | os.PathLike[str] | None = None,
    connect_timeout_seconds: int = DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
    transfer_timeout_seconds: int = 45,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Atomically stage one bounded file without interpolating user data into a shell."""

    alias = validate_ssh_alias(ssh_alias)
    if _REMOTE_ATTACHMENT_NAME.fullmatch(remote_name) is None:
        raise TransportConfigurationError("remote attachment name is invalid")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
        or expected_size > 20 * 1024 * 1024
    ):
        raise TransportConfigurationError("remote attachment size is invalid")
    if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise TransportConfigurationError("remote attachment digest is invalid")
    if (
        isinstance(transfer_timeout_seconds, bool)
        or not isinstance(transfer_timeout_seconds, int)
        or transfer_timeout_seconds < 1
        or transfer_timeout_seconds > 120
    ):
        raise TransportConfigurationError("attachment transfer timeout must be between 1 and 120 seconds")
    if (
        isinstance(connect_timeout_seconds, bool)
        or not isinstance(connect_timeout_seconds, int)
        or connect_timeout_seconds < 1
        or connect_timeout_seconds > 60
    ):
        raise TransportConfigurationError("SSH connect timeout must be between 1 and 60 seconds")

    path = Path(source).resolve()
    if not path.is_file() or path.stat().st_size != expected_size:
        raise AttachmentStagingError("managed attachment is unavailable or changed size")
    payload = path.read_bytes()
    if len(payload) != expected_size or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise AttachmentStagingError("managed attachment changed before transfer")

    env = _environment(environment)
    executable = resolve_ssh_binary(ssh_binary, environment=env)
    remote_command = shlex.join(
        (
            "python3",
            "-c",
            _REMOTE_ATTACHMENT_SCRIPT,
            remote_name,
            str(expected_size),
            expected_sha256,
        )
    )
    argv = (
        executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        alias,
        remote_command,
    )
    try:
        completed = subprocess.run(
            list(argv),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=transfer_timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AttachmentStagingError("the SSH attachment transfer did not complete") from exc
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", "replace").strip()[-500:]
        message = "the SSH host rejected the attachment transfer"
        if diagnostic:
            message = f"{message}: {diagnostic}"
        raise AttachmentStagingError(message)
    remote_path = completed.stdout.decode("utf-8", "strict").strip()
    if (
        not remote_path
        or any(character in remote_path for character in "\x00\r\n")
        or not PurePosixPath(remote_path).is_absolute()
        or PurePosixPath(remote_path).name != remote_name
    ):
        raise AttachmentStagingError("the SSH host returned an invalid attachment path")
    return remote_path


def delete_ssh_attachment(
    ssh_alias: str,
    remote_path: str,
    *,
    ssh_binary: str | os.PathLike[str] | None = None,
    connect_timeout_seconds: int = DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
    cleanup_timeout_seconds: int = 15,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Best-effort caller primitive for deleting one validated managed remote file."""

    alias = validate_ssh_alias(ssh_alias)
    remote_name = _validated_remote_attachment_path(remote_path).name
    if (
        isinstance(cleanup_timeout_seconds, bool)
        or not isinstance(cleanup_timeout_seconds, int)
        or cleanup_timeout_seconds < 1
        or cleanup_timeout_seconds > 60
    ):
        raise TransportConfigurationError(
            "attachment cleanup timeout must be between 1 and 60 seconds"
        )
    if (
        isinstance(connect_timeout_seconds, bool)
        or not isinstance(connect_timeout_seconds, int)
        or connect_timeout_seconds < 1
        or connect_timeout_seconds > 60
    ):
        raise TransportConfigurationError(
            "SSH connect timeout must be between 1 and 60 seconds"
        )

    env = _environment(environment)
    executable = resolve_ssh_binary(ssh_binary, environment=env)
    remote_command = shlex.join(
        ("python3", "-c", _REMOTE_ATTACHMENT_DELETE_SCRIPT, remote_name)
    )
    argv = (
        executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout_seconds}",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        alias,
        remote_command,
    )
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=cleanup_timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AttachmentCleanupError(
            "the SSH attachment cleanup did not complete"
        ) from exc
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode("utf-8", "replace").strip()[-500:]
        message = "the SSH host rejected the attachment cleanup"
        if diagnostic:
            message = f"{message}: {diagnostic}"
        raise AttachmentCleanupError(message)


def _validated_remote_attachment_path(value: object) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(character in value for character in "\x00\r\n")
    ):
        raise TransportConfigurationError("remote attachment path is invalid")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or _REMOTE_ATTACHMENT_NAME.fullmatch(path.name) is None
        or tuple(path.parts[-4:-1]) != (".cache", "rightmemory", "attachments")
    ):
        raise TransportConfigurationError("remote attachment path is invalid")
    return path


def resolve_codex_binary(
    binary: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = _environment(environment)
    configured = os.fspath(binary) if binary is not None else env.get(CODEX_BINARY_ENV)
    if configured:
        return _resolve_executable(configured, "Codex", env)

    batch_candidate: str | None = None
    names = ("codex.exe", "codex") if os.name == "nt" else ("codex",)
    for name in names:
        resolved = shutil.which(name, path=_path_value(env))
        if resolved:
            if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
                batch_candidate = resolved
                continue
            return resolved

    bundled = _bundled_codex_binary()
    if bundled is not None:
        return bundled
    # npm commonly exposes only codex.CMD on Windows. It is kept as a final
    # system fallback and is still launched as a separate argv with shell=False;
    # no project path or user message is ever included in this command.
    if batch_candidate is not None:
        return batch_candidate
    raise TransportConfigurationError(
        f"Codex executable was not found; install codex or set {CODEX_BINARY_ENV}"
    )


def resolve_ssh_binary(
    binary: str | os.PathLike[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    env = _environment(environment)
    configured = os.fspath(binary) if binary is not None else env.get(SSH_BINARY_ENV)
    if configured:
        return _resolve_executable(configured, "OpenSSH", env)
    names = ("ssh.exe", "ssh") if os.name == "nt" else ("ssh",)
    for name in names:
        resolved = shutil.which(name, path=_path_value(env))
        if resolved:
            return resolved
    raise TransportConfigurationError(
        f"OpenSSH executable was not found; install ssh or set {SSH_BINARY_ENV}"
    )


def validate_ssh_alias(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TransportConfigurationError("SSH alias must be a non-empty string")
    if len(value) > 128:
        raise TransportConfigurationError("SSH alias is too long")
    if value.startswith("-"):
        raise TransportConfigurationError("SSH alias must not start with a dash")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise TransportConfigurationError("SSH alias must not contain whitespace or control characters")
    if _SSH_ALIAS.fullmatch(value) is None:
        raise TransportConfigurationError(
            "SSH alias may contain only ASCII letters, digits, dots, underscores, and dashes"
        )
    return value


def _resolve_executable(value: str, label: str, env: Mapping[str, str]) -> str:
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise TransportConfigurationError(f"{label} executable is invalid")
    resolved = shutil.which(value, path=_path_value(env))
    if resolved:
        return resolved
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise TransportConfigurationError(f"{label} executable was not found: {value}")


def _bundled_codex_binary() -> str | None:
    try:
        from codex_cli_bin import bundled_codex_path

        bundled = Path(bundled_codex_path())
    except (ImportError, OSError, RuntimeError):
        return None
    return str(bundled) if bundled.is_file() else None


def _environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return os.environ.copy()
    return {str(key): str(value) for key, value in environment.items()}


def _path_value(environment: Mapping[str, str]) -> str | None:
    if os.name != "nt":
        return environment.get("PATH")
    for key, value in environment.items():
        if key.upper() == "PATH":
            return value
    return None


def _host_value(host: Any, name: str) -> Any:
    if isinstance(host, Mapping):
        return host.get(name)
    return getattr(host, name, None)
