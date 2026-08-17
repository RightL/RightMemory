from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .agent_cli_cleanup import AgentCliThreadCleanup, provider_thread_is_expired
from .config import RUNTIME_ROLES, AgentCliConfig
from .platform import prepare_command
from .prompt import build_cli_agent_instructions
from .provider_sessions import ProviderSessionRecord, ProviderSessionStore
from .provider_threads import ProviderThreadStore
from .semantic_upgrades import SemanticUpgradeContext


READ_ROLES = {"historian", "retrieve", "reviewer"}
WRITE_ROLES = {
    "dreamer",
    "insight",
    "pruner",
    "shared-view-builder",
    "sync-reconciler",
    "update",
}
NO_SESSION_RIGHTMEMORY_SESSION_ID = "__rightmemory_cli_chat__"
PERSISTENT_POLICY = "persistent"
ONE_SHOT_POLICY = "one-shot"
PROCESS_LOCAL_POLICY = "process-local"


@dataclass(frozen=True)
class AgentCliResult:
    provider_session_id: str
    text: str


class AgentCliCommandError(RuntimeError):
    def __init__(self, message: str, *, stdout: str, stderr: str):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class CliAgentExecutor:
    def __init__(
        self,
        memory_root: Path,
        role: str,
        config: AgentCliConfig,
        semantic_upgrades: SemanticUpgradeContext | None = None,
        state_root: Path | None = None,
        fresh_provider_session: bool = False,
    ):
        _validate_role(role)
        self.memory_root = memory_root
        self.state_root = state_root if state_root is not None else memory_root
        self.role = role
        self.config = config
        self.semantic_upgrades = semantic_upgrades
        self.fresh_provider_session = fresh_provider_session
        self.store = ProviderSessionStore(self.state_root, role)
        self.thread_store = ProviderThreadStore(self.state_root)
        self._process_provider_session_id: str | None = None
        if self.state_root == self.memory_root:
            self._run_opportunistic_cleanup()

    def run_turn(self, message: str) -> str:
        return self.run_one_shot_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, message)

    def run_process_turn(self, message: str) -> str:
        provider_session_id = self._process_provider_session_id
        result = self._run_provider(
            message,
            provider_session_id=provider_session_id,
            resume=provider_session_id is not None,
            rightmemory_session_id=NO_SESSION_RIGHTMEMORY_SESSION_ID,
            policy=PROCESS_LOCAL_POLICY,
        )
        self._process_provider_session_id = result.provider_session_id
        return result.text

    def run_stateless_turn(self, message: str) -> str:
        return self.run_turn(message)

    def run_one_shot_turn(self, rightmemory_session_id: str, message: str) -> str:
        result = self._run_provider(
            message,
            provider_session_id=None,
            resume=False,
            rightmemory_session_id=rightmemory_session_id,
            policy=ONE_SHOT_POLICY,
        )
        return result.text

    def run_session_turn(self, rightmemory_session_id: str, message: str) -> str:
        if self.role != "retrieve" or self.fresh_provider_session:
            return self.run_one_shot_turn(rightmemory_session_id, message)
        record = self._active_persistent_session(rightmemory_session_id)
        provider_session_id = record.provider_session_id if record is not None else None
        result = self._run_provider(
            message,
            provider_session_id,
            provider_session_id is not None,
            rightmemory_session_id,
            PERSISTENT_POLICY,
        )
        now = _now()
        self.store.save(
            ProviderSessionRecord(
                provider=self.config.provider,
                provider_session_id=result.provider_session_id,
                role=self.role,
                rightmemory_session_id=rightmemory_session_id,
                created_at=record.created_at if record is not None else now,
                updated_at=now,
            )
        )
        return result.text

    def has_saved_session(self, rightmemory_session_id: str) -> bool:
        if self.role != "retrieve" or self.fresh_provider_session:
            return False
        return self._active_persistent_session(rightmemory_session_id) is not None

    def has_process_session(self) -> bool:
        return self._process_provider_session_id is not None

    def cleanup(self) -> None:
        return None

    def _run_provider(
        self,
        message: str,
        provider_session_id: str | None,
        resume: bool,
        rightmemory_session_id: str,
        policy: str,
    ) -> AgentCliResult:
        prompt = _turn_prompt(
            self.memory_root,
            self.role,
            message,
            self.semantic_upgrades,
            include_instructions=not resume,
        )
        created_at = _now()
        if self.config.provider == "codex":
            command = build_codex_command(self.memory_root, self.role, self.config, provider_session_id)
            try:
                stdout = _run_cli(command, self.memory_root, "Codex", stdin=prompt)
            except AgentCliCommandError as exc:
                if provider_session_id is None:
                    partial_id = parse_codex_thread_id(exc.stdout)
                    if partial_id:
                        self._record_created(partial_id, rightmemory_session_id, policy, created_at)
                raise
            try:
                result = parse_codex_output(stdout)
            except Exception:
                if provider_session_id is None:
                    partial_id = parse_codex_thread_id(stdout)
                    if partial_id:
                        self._record_created(partial_id, rightmemory_session_id, policy, created_at)
                raise
            if provider_session_id is not None and result.provider_session_id != provider_session_id:
                raise RuntimeError("Codex resumed a different provider thread")
            if provider_session_id is None:
                self._record_created(result.provider_session_id, rightmemory_session_id, policy, created_at)
            self.thread_store.record_success("codex", result.provider_session_id, activity_at=_now())
            return result
        if self.config.provider == "claude":
            if provider_session_id is not None:
                claude_session_id = provider_session_id
            elif self.fresh_provider_session or policy in {ONE_SHOT_POLICY, PROCESS_LOCAL_POLICY}:
                claude_session_id = str(uuid4())
            else:
                claude_session_id = _stable_claude_session_id(self.role, rightmemory_session_id)
            if provider_session_id is None:
                self._record_created(claude_session_id, rightmemory_session_id, policy, created_at)
            command = build_claude_command(self.role, self.config, prompt, claude_session_id, resume)
            stdout = _run_cli(command, self.memory_root, "Claude")
            result = parse_claude_output(stdout)
            if result.provider_session_id != claude_session_id:
                raise RuntimeError("Claude returned a different provider session id")
            self.thread_store.record_success("claude", result.provider_session_id, activity_at=_now())
            return result
        raise ValueError("agent_cli provider must be one of: claude, codex")

    def _record_created(
        self,
        provider_session_id: str,
        rightmemory_session_id: str,
        policy: str,
        created_at: str,
    ) -> None:
        self.thread_store.record_created(
            provider=self.config.provider,
            provider_session_id=provider_session_id,
            role=self.role,
            rightmemory_session_id=rightmemory_session_id,
            policy=policy,
            created_at=created_at,
        )

    def _active_persistent_session(self, rightmemory_session_id: str) -> ProviderSessionRecord | None:
        mapping = self.store.load(rightmemory_session_id)
        if mapping is None or mapping.provider != self.config.provider:
            return None
        try:
            ownership = self.thread_store.load(mapping.provider, mapping.provider_session_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            ownership is None
            or ownership.status != "active"
            or ownership.policy != PERSISTENT_POLICY
            or provider_thread_is_expired(ownership)
        ):
            return None
        if ownership.role != self.role or ownership.rightmemory_session_id != rightmemory_session_id:
            return None
        return mapping

    def _run_opportunistic_cleanup(self) -> None:
        cleanup = AgentCliThreadCleanup(self.state_root)
        try:
            if not cleanup.has_expired_codex_threads():
                return
            result = cleanup.run()
        except Exception as exc:
            print(
                f"Warning: RightMemory Codex thread cleanup failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return
        if result.errors:
            print(
                f"Warning: RightMemory Codex thread cleanup left {result.pending} pending "
                f"and {result.malformed} malformed record(s)",
                file=sys.stderr,
            )


def build_codex_command(
    memory_root: Path,
    role: str,
    config: AgentCliConfig,
    provider_session_id: str | None,
) -> list[str]:
    _validate_role(role)
    command = [
        "codex",
        "exec",
        "--json",
        "--cd",
        str(memory_root),
        "--skip-git-repo-check",
        "--sandbox",
        _codex_sandbox(role),
    ]
    _append_model(command, config)
    _append_codex_reasoning_effort(command, config)
    if provider_session_id:
        command.extend(["resume", provider_session_id])
    return command


def build_claude_command(
    role: str,
    config: AgentCliConfig,
    prompt: str,
    provider_session_id: str,
    resume: bool,
) -> list[str]:
    _validate_role(role)
    _validate_uuid(provider_session_id)
    if config.reasoning_effort is not None:
        raise ValueError("agent_cli reasoning_effort is only supported for Codex")
    command = ["claude", "-p", "--output-format", "json"]
    _append_model(command, config)
    command.extend(["--permission-mode", _claude_permission_mode(role)])
    session_flag = "--resume" if resume else "--session-id"
    command.extend([session_flag, provider_session_id, prompt])
    return command


def parse_codex_output(stdout: str) -> AgentCliResult:
    thread_id = ""
    final_text = ""
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        obj = _json_object(line, f"Codex output line {line_number}")
        if obj.get("type") == "thread.started":
            thread_id = _non_empty_string(obj.get("thread_id")) or thread_id

        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            final_text = _non_empty_string(item.get("text")) or final_text

    if not thread_id:
        raise RuntimeError("Codex output did not include thread_id")
    if not final_text:
        raise RuntimeError("Codex output did not include final agent message")
    return AgentCliResult(provider_session_id=thread_id, text=final_text)


def parse_codex_thread_id(stdout: str) -> str:
    thread_id = ""
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "thread.started":
            thread_id = _non_empty_string(obj.get("thread_id")) or thread_id
    return thread_id


def parse_claude_output(stdout: str) -> AgentCliResult:
    obj = _json_object(stdout, "Claude output")
    session_id = _non_empty_string(obj.get("session_id"))
    result = _non_empty_string(obj.get("result"))
    if not session_id:
        raise RuntimeError("Claude output did not include session_id")
    if not result:
        raise RuntimeError("Claude output did not include result")
    return AgentCliResult(provider_session_id=session_id, text=result)


def _turn_prompt(
    memory_root: Path,
    role: str,
    message: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
    *,
    include_instructions: bool = True,
) -> str:
    caller = f"Caller message:\n{message}\n"
    if not include_instructions:
        return caller
    instructions = build_cli_agent_instructions(memory_root, role, semantic_upgrades=semantic_upgrades).rstrip()
    return f"{instructions}\n\n{caller}"


def _run_cli(command: list[str], memory_root: Path, label: str, *, stdin: str | None = None) -> str:
    run_kwargs: dict[str, Any] = {}
    if stdin is not None:
        run_kwargs["input"] = stdin.encode("utf-8")
    completed = subprocess.run(
        prepare_command(command),
        cwd=str(memory_root),
        capture_output=True,
        text=False,
        check=False,
        **run_kwargs,
    )
    stdout = _decode_cli_output(completed.stdout)
    stderr = _decode_cli_output(completed.stderr)
    if completed.returncode != 0:
        detail = _command_failure_detail(stdout, stderr)
        raise AgentCliCommandError(
            f"{label} CLI exited with status {completed.returncode}{detail}",
            stdout=stdout,
            stderr=stderr,
        )
    return stdout


def _decode_cli_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _command_failure_detail(stdout: str, stderr: str) -> str:
    parts = []
    stderr = stderr.strip()
    stdout = stdout.strip()
    if stderr:
        parts.append(f"stderr: {_short_output(stderr)}")
    if stdout:
        parts.append(f"stdout: {_short_output(stdout)}")
    if not parts:
        return ""
    return "; " + "; ".join(parts)


def _short_output(text: str) -> str:
    return text if len(text) <= 2000 else text[:2000] + "...[truncated]"


def _stable_claude_session_id(role: str, rightmemory_session_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"rightmemory:{role}:{rightmemory_session_id}"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _append_model(command: list[str], config: AgentCliConfig) -> None:
    if config.model:
        command.extend(["--model", config.model])


def _append_codex_reasoning_effort(command: list[str], config: AgentCliConfig) -> None:
    if config.reasoning_effort:
        command.extend(["--config", f"model_reasoning_effort={config.reasoning_effort}"])


def _codex_sandbox(role: str) -> str:
    if role in READ_ROLES:
        return "read-only"
    if role in WRITE_ROLES:
        return "workspace-write"
    raise ValueError(f"RightMemory role has no Codex sandbox mapping: {role}")


def _claude_permission_mode(role: str) -> str:
    if role in READ_ROLES:
        return "plan"
    if role in WRITE_ROLES:
        return "auto"
    raise ValueError(f"RightMemory role has no Claude permission mapping: {role}")


def _validate_role(role: str) -> None:
    if role not in RUNTIME_ROLES:
        raise ValueError(f"unknown RightMemory role: {role}")


def _validate_uuid(value: str) -> None:
    try:
        UUID(value)
    except ValueError as exc:
        raise ValueError("Claude provider_session_id must be a UUID") from exc


def _json_object(content: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} was not valid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} was not a JSON object")
    return data


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()
