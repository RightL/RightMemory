from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .agent_cli import WRITE_ROLES
from .agent_cli_cleanup import (
    AgentCliThreadCleanup,
    CODEX_FORK_BASE_RETENTION,
    CODEX_THREAD_RETENTION,
)
from .config import ROLES, RuntimeConfig, SyncConfig, load_config
from .platform import prepare_command
from .provider_sessions import ProviderSessionStore
from .provider_threads import ProviderThreadStore
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
    run_nonce = uuid4().hex[:8]
    configs = _load_agent_cli_configs(checks, memory_root=memory_root)
    if not configs:
        return checks
    boundary_parent = _write_config(configs).memory_root.parent

    providers = sorted({config.agent_cli.provider for config in configs.values() if config.agent_cli is not None})
    unavailable = []
    resolved = []
    for provider in providers:
        try:
            resolved.append(_provider_runtime(provider))
        except (FileNotFoundError, ImportError, RuntimeError) as exc:
            unavailable.append(f"{provider}: {exc}")
    if unavailable:
        checks.append(DoctorCheck("provider runtimes", False, "; ".join(unavailable)))
        return checks
    checks.append(DoctorCheck("provider runtimes", True, f"found: {', '.join(resolved)}"))

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
        _check_resume_provider_thread(checks, doctor_configs["retrieve"], run_nonce)
        _check_retrieve_reads_memory(checks, doctor_configs["retrieve"], retrieve_token, run_nonce)
        write_config = _write_config(doctor_configs)
        _check_write_edits_memory(checks, write_config, memory_root, run_nonce)
        _check_write_commits_memory(checks, write_config, memory_root, run_nonce)
        _check_write_boundary(checks, write_config, boundary_parent, run_nonce)
        _check_codex_thread_cleanup(checks, doctor_configs)
    return checks


def _provider_runtime(provider: str) -> str:
    if provider == "codex":
        from codex_cli_bin import bundled_codex_path
        from openai_codex import __version__

        binary = bundled_codex_path()
        return f"codex-sdk-{__version__}:{binary}"
    command = prepare_command([provider])
    return f"{provider}:{command[0]}"


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


def _check_codex_thread_cleanup(
    checks: list[DoctorCheck],
    configs: dict[str, RuntimeConfig],
) -> None:
    config = next(
        (
            configs[role]
            for role in sorted(configs)
            if configs[role].agent_cli is not None and configs[role].agent_cli.provider == "codex"
        ),
        None,
    )
    if config is None:
        return

    store = ProviderThreadStore(config.memory_root)
    owned = store.scan("codex").records
    if not owned:
        checks.append(DoctorCheck("codex thread cleanup", False, "no owned Codex thread was created"))
        return

    thread_ids = {record.provider_session_id for record in owned}
    cleanup_at = (
        datetime.now(UTC)
        + max(CODEX_THREAD_RETENTION, CODEX_FORK_BASE_RETENTION)
        + timedelta(seconds=1)
    )
    try:
        result = AgentCliThreadCleanup(config.memory_root, now=lambda: cleanup_at).run()
        remaining = [thread_id for thread_id in thread_ids if store.load("codex", thread_id) is not None]
        if result.deleted != len(thread_ids) or remaining:
            raise RuntimeError(
                "owned Codex threads were not fully deleted "
                f"(expected={len(thread_ids)}, deleted={result.deleted}, "
                f"remaining={len(remaining)}, pending={result.pending}, errors={len(result.errors)})"
            )
    except Exception as exc:
        checks.append(DoctorCheck("codex thread cleanup", False, _exception_detail(exc)))
        return
    checks.append(DoctorCheck("codex thread cleanup", True, f"deleted {len(thread_ids)} owned thread(s)"))


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
        try:
            output = _runtime_turn(
                config,
                f"doctor-{run_nonce}-first-{role}",
                "Complete this connectivity check without editing files or running Git. "
                "Return a concise normal no-op result for your role.",
            )
            if not output.strip():
                failures.append(f"{label}: provider returned no final output")
            else:
                successes.append(label)
        except Exception as exc:
            failures.append(f"{label}: {_exception_detail(exc)}")
    _append_check(checks, "first provider call", failures, f"succeeded for {', '.join(successes)}")


def _check_resume_provider_thread(
    checks: list[DoctorCheck],
    config: RuntimeConfig,
    run_nonce: str,
) -> None:
    failures = []
    provider = config.agent_cli.provider if config.agent_cli is not None else "unknown"
    session_id = f"doctor-{run_nonce}-resume-{provider}"
    store = ProviderSessionStore(config.memory_root, config.role)
    try:
        _runtime_turn(
            config,
            session_id,
            "Find the RightMemory doctor retrieve token.",
        )
        first = store.load(session_id)
        if first is None:
            raise RuntimeError("first retrieve call did not save a provider thread mapping")
        _runtime_turn(config, session_id, "Re-evaluate the same durable context for this doctor check.")
        second = store.load(session_id)
        if second is None:
            raise RuntimeError("resumed retrieve call did not preserve its provider thread mapping")
        if second.provider_session_id != first.provider_session_id:
            raise RuntimeError("resumed retrieve call used a different provider thread")
    except Exception as exc:
        failures.append(f"{provider}: {_exception_detail(exc)}")
    _append_check(checks, "resume provider thread", failures, f"retrieve reused its {provider} thread")


def _check_retrieve_reads_memory(checks: list[DoctorCheck], config: RuntimeConfig, token: str, run_nonce: str) -> None:
    try:
        output = _runtime_turn(
            config,
            f"doctor-{run_nonce}-retrieve",
            "Find the RightMemory doctor retrieve token.",
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
    section = (
        "## Write Verification {#doctor-write-verification}\n\n"
        f"The disposable RightMemory doctor fixture's durable write verification token is {token}."
    )
    try:
        _runtime_turn(
            config,
            f"doctor-{run_nonce}-write-edit",
            f"Add this durable fixture section to MEMORY.md, commit the change, and then reply exactly "
            f"`WROTE {token}`:\n\n{section}",
        )
        if token not in (memory_root / "MEMORY.md").read_text(encoding="utf-8"):
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
    section = (
        "## Commit Verification {#doctor-commit-verification}\n\n"
        f"The disposable RightMemory doctor fixture's durable commit verification token is {token}."
    )
    message = f"memory: doctor agent-cli {token}"
    try:
        _runtime_turn(
            config,
            f"doctor-{run_nonce}-write-commit",
            f"Add this durable fixture section to MEMORY.md, commit with message `{message}`, and then reply "
            f"exactly `COMMITTED {token}`:\n\n{section}",
        )
        if token not in (memory_root / "MEMORY.md").read_text(encoding="utf-8"):
            raise RuntimeError("MEMORY.md did not contain the commit token")
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
    outside_parent: Path,
    run_nonce: str,
) -> None:
    token = f"RM_BOUNDARY_{uuid4().hex}"
    outside_path = outside_parent / f".rightmemory-doctor-boundary-{token}.tmp"
    prompt = (
        f"Try to create this file outside the memory root: {outside_path}\n"
        f"Use this content: {token}\n"
        f"If the default boundaries block it, reply exactly `BOUNDARY_BLOCKED {token}`."
    )
    runtime_error: Exception | None = None
    try:
        _runtime_turn(config, f"doctor-{run_nonce}-write-boundary", prompt)
    except Exception as exc:
        runtime_error = exc
    if outside_path.exists():
        cleanup_detail = "removed after detection"
        try:
            outside_path.unlink()
        except OSError as exc:
            cleanup_detail = f"cleanup failed: {_exception_detail(exc)}"
        checks.append(
            DoctorCheck("write boundary", False, f"outside file was created: {outside_path}; {cleanup_detail}")
        )
        return
    if runtime_error is not None:
        if _is_boundary_denial(runtime_error):
            checks.append(
                DoctorCheck("write boundary", True, f"outside write blocked: {type(runtime_error).__name__}")
            )
            return
        checks.append(DoctorCheck("write boundary", False, _exception_detail(runtime_error)))
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
        "# RightMemory Doctor {#rightmemory-doctor}\n\n"
        "## Retrieve Verification {#doctor-retrieve-token}\n\n"
        f"The RightMemory doctor retrieve token is {retrieve_token}.\n",
        encoding="utf-8",
    )
    (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
    _run_git(memory_root, "init")
    _run_git(memory_root, "config", "user.email", "doctor@rightmemory.local")
    _run_git(memory_root, "config", "user.name", "RightMemory Doctor")
    _run_git(memory_root, "add", "MEMORY.md", "PURSUITS.md")
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
