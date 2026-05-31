from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .config import ROLES, AgentCliConfig
from .prompt import build_cli_agent_instructions
from .provider_sessions import ProviderSessionRecord, ProviderSessionStore
from .semantic_upgrades import SemanticUpgradeContext


READ_ROLES = {"historian", "retrieve"}
WRITE_ROLES = {"dreamer", "insight", "pruner", "reviewer", "sync-reconciler", "update"}
NO_SESSION_RIGHTMEMORY_SESSION_ID = "__rightmemory_cli_chat__"


@dataclass(frozen=True)
class AgentCliResult:
    provider_session_id: str
    text: str


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

    def run_turn(self, message: str) -> str:
        return self.run_session_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, message)

    def run_session_turn(self, rightmemory_session_id: str, message: str) -> str:
        record = self.store.load(rightmemory_session_id)
        if record is not None and record.provider != self.config.provider:
            raise RuntimeError(
                "stored provider session uses a different CLI provider: "
                f"{record.provider} for session {rightmemory_session_id}, configured {self.config.provider}"
            )
        provider_session_id = record.provider_session_id if record is not None else None
        result = self._run_provider(
            message,
            provider_session_id,
            provider_session_id is not None,
            rightmemory_session_id,
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

    def cleanup(self) -> None:
        return None

    def _run_provider(
        self,
        message: str,
        provider_session_id: str | None,
        resume: bool,
        rightmemory_session_id: str,
    ) -> AgentCliResult:
        prompt = _turn_prompt(self.memory_root, self.role, message, self.semantic_upgrades)
        if self.config.provider == "codex":
            command = build_codex_command(self.memory_root, self.role, self.config, prompt, provider_session_id)
            stdout = _run_cli(command, self.memory_root, "Codex")
            return parse_codex_output(stdout)
        if self.config.provider == "claude":
            if provider_session_id is not None:
                claude_session_id = provider_session_id
            elif self.fresh_provider_session:
                claude_session_id = str(uuid4())
            else:
                claude_session_id = _stable_claude_session_id(self.role, rightmemory_session_id)
            command = build_claude_command(self.role, self.config, prompt, claude_session_id, resume)
            stdout = _run_cli(command, self.memory_root, "Claude")
            return parse_claude_output(stdout)
        raise ValueError("agent_cli provider must be one of: claude, codex")


def build_codex_command(
    memory_root: Path,
    role: str,
    config: AgentCliConfig,
    prompt: str,
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
    if provider_session_id:
        command.extend(["resume", provider_session_id, prompt])
        return command
    command.append(prompt)
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
) -> str:
    instructions = build_cli_agent_instructions(memory_root, role, semantic_upgrades=semantic_upgrades).rstrip()
    return f"{instructions}\n\nCaller message:\n{message}\n"


def _run_cli(command: list[str], memory_root: Path, label: str) -> str:
    completed = subprocess.run(
        command,
        cwd=str(memory_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = _command_failure_detail(completed.stdout, completed.stderr)
        raise RuntimeError(f"{label} CLI exited with status {completed.returncode}{detail}")
    return completed.stdout


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
    if role not in ROLES:
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
