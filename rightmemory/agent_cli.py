from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ROLES, AgentCliConfig


READ_ROLES = {"retrieve"}
WRITE_ROLES = {"dreamer", "reviewer", "sync-reconciler", "update"}


@dataclass(frozen=True)
class AgentCliResult:
    provider_session_id: str
    text: str


def build_codex_command(
    memory_root: Path,
    role: str,
    config: AgentCliConfig,
    prompt: str,
    provider_session_id: str | None,
) -> list[str]:
    _validate_role(role)
    if provider_session_id:
        command = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        _append_model(command, config)
        command.extend([provider_session_id, prompt])
        return command

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
    command = ["claude", "-p", "--output-format", "json"]
    _append_model(command, config)
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


def _append_model(command: list[str], config: AgentCliConfig) -> None:
    if config.model:
        command.extend(["--model", config.model])


def _codex_sandbox(role: str) -> str:
    if role in READ_ROLES:
        return "read-only"
    if role in WRITE_ROLES:
        return "workspace-write"
    raise ValueError(f"RightMemory role has no Codex sandbox mapping: {role}")


def _validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown RightMemory role: {role}")


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
