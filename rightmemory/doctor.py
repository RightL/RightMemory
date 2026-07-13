from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .agent_cli import WRITE_ROLES
from .config import ROLES, RuntimeConfig, SyncConfig, load_config
from .platform import prepare_command
from .runtime import RightMemoryRuntime


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def format_doctor_report(checks: list[DoctorCheck]) -> str:
    return "\n".join(f"[{'ok' if check.ok else 'fail'}] {check.name} - {check.detail}" for check in checks)


def run_agent_cli_doctor(memory_root: Path | None = None) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    run_nonce = uuid4().hex
    configs = _load_agent_cli_configs(checks, memory_root=memory_root)
    if not configs:
        return checks

    providers = sorted({config.agent_cli.provider for config in configs.values() if config.agent_cli is not None})
    unavailable = []
    resolved = []
    for provider in providers:
        try:
            command = prepare_command([provider])
        except (FileNotFoundError, RuntimeError) as exc:
            unavailable.append(f"{provider}: {exc}")
        else:
            resolved.append(f"{provider}:{command[0]}")
    if unavailable:
        checks.append(DoctorCheck("provider CLI binaries", False, "; ".join(unavailable)))
        return checks
    checks.append(DoctorCheck("provider CLI binaries", True, f"found: {', '.join(resolved)}"))

    with tempfile.TemporaryDirectory(prefix="rightmemory-doctor-") as tempdir:
        temp_root = Path(tempdir)
        memory_root = temp_root / "memory"
        retrieve_token = f"RM_RETRIEVE_{uuid4().hex}"
        try:
            _seed_memory_root(memory_root, retrieve_token)
        except Exception as exc:
            checks.append(DoctorCheck("temporary Git memory repo", False, _exception_detail(exc)))
            return checks
        checks.append(DoctorCheck("temporary Git memory repo", True, str(memory_root)))

        doctor_configs = {role: _doctor_config(config, memory_root) for role, config in configs.items()}
        _check_first_provider_calls(checks, doctor_configs, run_nonce)
        _check_resume_history(checks, doctor_configs, providers, run_nonce)
        _check_retrieve_reads_memory(checks, doctor_configs["retrieve"], retrieve_token, run_nonce)
        write_config = _write_config(doctor_configs)
        _check_write_edits_memory(checks, write_config, memory_root, run_nonce)
        _check_write_commits_memory(checks, write_config, memory_root, run_nonce)
        _check_write_boundary(checks, write_config, temp_root, run_nonce)
    return checks


def _load_agent_cli_configs(checks: list[DoctorCheck], *, memory_root: Path | None = None) -> dict[str, RuntimeConfig]:
    configs: dict[str, RuntimeConfig] = {}
    failures = []
    for role in sorted(ROLES):
        try:
            config = load_config(role, memory_root=memory_root)
        except Exception as exc:
            failures.append(f"{role}: {_exception_detail(exc)}")
            continue
        if config.runtime_mode != "cli-agent" or config.agent_cli is None:
            failures.append(f"{role}: runtime_mode={config.runtime_mode}, agent_cli={config.agent_cli is not None}")
            continue
        configs[role] = config

    if failures:
        checks.append(DoctorCheck("role configs", False, "; ".join(failures)))
        return {}

    roles = ", ".join(f"{role}:{configs[role].agent_cli.provider}" for role in sorted(configs))
    checks.append(DoctorCheck("role configs", True, roles))
    return configs


def _doctor_config(config: RuntimeConfig, memory_root: Path) -> RuntimeConfig:
    sync = SyncConfig(memory_root=memory_root, enabled=False, stale_pull_after_hours=config.sync.stale_pull_after_hours)
    return replace(config, memory_root=memory_root, state_root=memory_root, sync=sync)


def _check_first_provider_calls(
    checks: list[DoctorCheck],
    configs: dict[str, RuntimeConfig],
    run_nonce: str,
) -> None:
    failures = []
    successes = []
    for role in sorted(configs):
        config = configs[role]
        provider = config.agent_cli.provider if config.agent_cli is not None else "unknown"
        label = f"{role}:{provider}"
        token = f"RM_FIRST_{role.upper().replace('-', '_')}_{uuid4().hex}"
        try:
            output = _runtime_turn(config, f"doctor-{run_nonce}-first-{role}", f"Reply exactly `{token}`.")
            if token not in output:
                failures.append(f"{label}: expected token not found")
            else:
                successes.append(label)
        except Exception as exc:
            failures.append(f"{label}: {_exception_detail(exc)}")
    _append_check(checks, "first provider call", failures, f"succeeded for {', '.join(successes)}")


def _check_resume_history(
    checks: list[DoctorCheck],
    configs: dict[str, RuntimeConfig],
    providers: list[str],
    run_nonce: str,
) -> None:
    failures = []
    for provider in providers:
        config = _config_for_provider(configs, provider)
        session_id = f"doctor-{run_nonce}-resume-{provider}"
        token = f"RM_HISTORY_{provider.upper()}_{uuid4().hex}"
        try:
            _runtime_turn(
                config,
                session_id,
                f"Remember this doctor token for the next check: `{token}`. Reply exactly `READY {token}`.",
            )
            output = _runtime_turn(config, session_id, "What doctor token did I ask you to remember? Reply with it.")
            if token not in output:
                failures.append(f"{provider}: prior token not found")
        except Exception as exc:
            failures.append(f"{provider}: {_exception_detail(exc)}")
    _append_check(checks, "resume history", failures, f"succeeded for {', '.join(providers)}")


def _check_retrieve_reads_memory(checks: list[DoctorCheck], config: RuntimeConfig, token: str, run_nonce: str) -> None:
    try:
        output = _runtime_turn(
            config,
            f"doctor-{run_nonce}-retrieve",
            "Read MEMORY.md and reply with the value after DOCTOR_RETRIEVE_TOKEN.",
        )
        if token not in output:
            raise RuntimeError("retrieve output did not include temporary token")
    except Exception as exc:
        checks.append(DoctorCheck("retrieve reads memory", False, _exception_detail(exc)))
        return
    checks.append(DoctorCheck("retrieve reads memory", True, "temporary token returned"))


def _check_write_edits_memory(
    checks: list[DoctorCheck],
    config: RuntimeConfig,
    memory_root: Path,
    run_nonce: str,
) -> None:
    token = f"RM_WRITE_{uuid4().hex}"
    line = f"DOCTOR_WRITE_TOKEN: {token}"
    try:
        _runtime_turn(
            config,
            f"doctor-{run_nonce}-write-edit",
            f"Append this exact line to MEMORY.md: `{line}`. Reply exactly `WROTE {token}` after saving.",
        )
        if line not in (memory_root / "MEMORY.md").read_text(encoding="utf-8"):
            raise RuntimeError("MEMORY.md did not contain the write token")
    except Exception as exc:
        checks.append(DoctorCheck("write role edits memory", False, _exception_detail(exc)))
        return
    checks.append(DoctorCheck("write role edits memory", True, "MEMORY.md updated"))


def _check_write_commits_memory(
    checks: list[DoctorCheck],
    config: RuntimeConfig,
    memory_root: Path,
    run_nonce: str,
) -> None:
    token = f"RM_COMMIT_{uuid4().hex}"
    message = f"memory: doctor agent-cli {token}"
    try:
        _runtime_turn(
            config,
            f"doctor-{run_nonce}-write-commit",
            f"Run git status, stage MEMORY.md, and commit with message `{message}`. Reply exactly `COMMITTED {token}`.",
        )
        subject = _run_git(memory_root, "log", "-1", "--pretty=%s").strip()
        if subject != message:
            raise RuntimeError(f"latest commit was {subject!r}")
    except Exception as exc:
        checks.append(DoctorCheck("write role commits memory", False, _exception_detail(exc)))
        return
    checks.append(DoctorCheck("write role commits memory", True, message))


def _check_write_boundary(
    checks: list[DoctorCheck],
    config: RuntimeConfig,
    temp_root: Path,
    run_nonce: str,
) -> None:
    token = f"RM_BOUNDARY_{uuid4().hex}"
    outside_path = temp_root / "outside-memory-root.txt"
    prompt = (
        f"Try to create this file outside the memory root: {outside_path}\n"
        f"Use this content: {token}\n"
        f"If the default boundaries block it, reply exactly `BOUNDARY_BLOCKED {token}`."
    )
    try:
        _runtime_turn(config, f"doctor-{run_nonce}-write-boundary", prompt)
    except Exception as exc:
        if outside_path.exists():
            checks.append(DoctorCheck("write boundary", False, _exception_detail(exc)))
            return
        if _is_boundary_denial(exc):
            checks.append(DoctorCheck("write boundary", True, f"outside write blocked: {type(exc).__name__}"))
            return
        checks.append(DoctorCheck("write boundary", False, _exception_detail(exc)))
        return
    if outside_path.exists():
        checks.append(DoctorCheck("write boundary", False, f"outside file was created: {outside_path}"))
        return
    checks.append(DoctorCheck("write boundary", True, "sibling path was not created"))


def _is_boundary_denial(exc: Exception) -> bool:
    message = str(exc).lower()
    denial_phrases = (
        "boundary",
        "sandbox",
        "outside workspace",
        "outside the workspace",
        "outside memory root",
        "outside the memory root",
        "not permitted",
        "operation not permitted",
        "denied write",
        "write denied",
        "permission denied",
        "read-only",
        "read only",
    )
    return any(phrase in message for phrase in denial_phrases)


def _runtime_turn(config: RuntimeConfig, session_id: str, message: str) -> str:
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(session_id, message)
    finally:
        runtime.cleanup()


def _config_for_provider(configs: dict[str, RuntimeConfig], provider: str) -> RuntimeConfig:
    retrieve = configs["retrieve"]
    if retrieve.agent_cli is not None and retrieve.agent_cli.provider == provider:
        return retrieve
    for role in sorted(configs):
        config = configs[role]
        if config.agent_cli is not None and config.agent_cli.provider == provider:
            return config
    raise RuntimeError(f"no config for provider: {provider}")


def _write_config(configs: dict[str, RuntimeConfig]) -> RuntimeConfig:
    if "update" in configs:
        return configs["update"]
    for role in sorted(WRITE_ROLES):
        if role in configs:
            return configs[role]
    raise RuntimeError("no write-capable role config found")


def _append_check(checks: list[DoctorCheck], name: str, failures: list[str], ok_detail: str) -> None:
    if failures:
        checks.append(DoctorCheck(name, False, "; ".join(failures)))
        return
    checks.append(DoctorCheck(name, True, ok_detail))


def _seed_memory_root(memory_root: Path, retrieve_token: str) -> None:
    memory_root.mkdir(parents=True)
    (memory_root / "insight_logs").mkdir()
    (memory_root / "MEMORY.md").write_text(
        "# RightMemory Doctor\n\n"
        f"DOCTOR_RETRIEVE_TOKEN: {retrieve_token}\n",
        encoding="utf-8",
    )
    _run_git(memory_root, "init")
    _run_git(memory_root, "config", "user.email", "doctor@rightmemory.local")
    _run_git(memory_root, "config", "user.name", "RightMemory Doctor")
    _run_git(memory_root, "add", "MEMORY.md")
    _run_git(memory_root, "commit", "-m", "memory: seed doctor")


def _run_git(memory_root: Path, *args: str) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=str(memory_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{' '.join(command)} failed with status {completed.returncode}: {detail}")
    return completed.stdout


def _exception_detail(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
