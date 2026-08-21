from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .agent_cli_cleanup import AgentCliThreadCleanup, provider_thread_is_expired
from .codex_sdk import CodexSdkRunner, CodexSdkTiming
from .config import RUNTIME_ROLES, AgentCliConfig
from .platform import prepare_command
from .prompt import build_cli_agent_instructions
from .provider_prefixes import ProviderPrefixRecord, ProviderPrefixStore
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
FORK_BASE_POLICY = "fork-base"
_EMPTY_RETRIEVE_SELECTION = {"ids": [], "sources": [], "recent_candidates": []}
_PREFIX_BOOTSTRAP_QUERY = (
    "Initialize this Retrieve context without selecting any content. "
    "Return the empty terminal selection."
)


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
        codex_runner: CodexSdkRunner | None = None,
        trace_event: Callable[..., None] | None = None,
    ):
        _validate_role(role)
        self.memory_root = memory_root
        self.state_root = state_root if state_root is not None else memory_root
        self.role = role
        self.config = config
        self.semantic_upgrades = semantic_upgrades
        self.fresh_provider_session = fresh_provider_session
        self._codex_runner = codex_runner
        self._owns_codex_runner = False
        if self.config.provider == "codex" and self._codex_runner is None:
            self._codex_runner = CodexSdkRunner()
            self._owns_codex_runner = True
        self._trace_event = trace_event
        self.store = ProviderSessionStore(self.state_root, role)
        self.prefix_store = ProviderPrefixStore(self.state_root)
        self.thread_store = ProviderThreadStore(self.state_root)
        self._process_provider_session_id: str | None = None
        if self.state_root == self.memory_root and (
            self._codex_runner is None
            or self._codex_runner.claim_opportunistic_cleanup(self.memory_root)
        ):
            self._run_opportunistic_cleanup()

    def run_turn(self, message: str) -> str:
        return self.run_one_shot_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, message)

    def run_process_turn(self, message: str, *, prefix_context: str | None = None) -> str:
        provider_session_id = self._process_provider_session_id
        if provider_session_id is None:
            result = self._run_starting_turn(
                message,
                prefix_context=prefix_context,
                rightmemory_session_id=NO_SESSION_RIGHTMEMORY_SESSION_ID,
                policy=PROCESS_LOCAL_POLICY,
            )
        else:
            result = self._run_provider(
                message,
                provider_session_id=provider_session_id,
                resume=True,
                rightmemory_session_id=NO_SESSION_RIGHTMEMORY_SESSION_ID,
                policy=PROCESS_LOCAL_POLICY,
            )
        self._process_provider_session_id = result.provider_session_id
        return result.text

    def run_stateless_turn(self, message: str) -> str:
        return self.run_turn(message)

    def run_one_shot_turn(
        self,
        rightmemory_session_id: str,
        message: str,
        *,
        prefix_context: str | None = None,
    ) -> str:
        result = self._run_starting_turn(
            message,
            prefix_context=prefix_context,
            rightmemory_session_id=rightmemory_session_id,
            policy=ONE_SHOT_POLICY,
        )
        return result.text

    def run_session_turn(
        self,
        rightmemory_session_id: str,
        message: str,
        *,
        prefix_context: str | None = None,
    ) -> str:
        if self.role != "retrieve" or self.fresh_provider_session:
            return self.run_one_shot_turn(
                rightmemory_session_id,
                message,
                prefix_context=prefix_context,
            )
        record = self._active_persistent_session(rightmemory_session_id)
        provider_session_id = record.provider_session_id if record is not None else None
        if provider_session_id is None:
            result = self._run_starting_turn(
                message,
                prefix_context=prefix_context,
                rightmemory_session_id=rightmemory_session_id,
                policy=PERSISTENT_POLICY,
            )
        else:
            result = self._run_provider(
                message,
                provider_session_id,
                True,
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

    def _run_starting_turn(
        self,
        message: str,
        *,
        prefix_context: str | None,
        rightmemory_session_id: str,
        policy: str,
    ) -> AgentCliResult:
        if self.role == "retrieve" and prefix_context is not None and prefix_context.strip():
            try:
                prefix = self._ensure_prefix_base(prefix_context)
            except Exception as exc:
                print(
                    "Warning: RightMemory Retrieve prefix initialization failed; "
                    f"starting a complete provider conversation: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            else:
                try:
                    return self._run_forked_provider(
                        message,
                        source_provider_session_id=prefix.provider_session_id,
                        rightmemory_session_id=rightmemory_session_id,
                        policy=policy,
                    )
                except Exception as exc:
                    retirement_errors = self._retire_prefix_base(prefix)
                    retirement_detail = (
                        f"; prefix retirement also reported: {'; '.join(retirement_errors)}"
                        if retirement_errors
                        else ""
                    )
                    print(
                        "Warning: RightMemory Retrieve provider fork failed; "
                        "retiring that prefix base and starting a complete provider "
                        f"conversation: {type(exc).__name__}: {exc}{retirement_detail}",
                        file=sys.stderr,
                    )
            message = _join_prefix_context(prefix_context, message)
        return self._run_provider(
            message,
            provider_session_id=None,
            resume=False,
            rightmemory_session_id=rightmemory_session_id,
            policy=policy,
        )

    def _ensure_prefix_base(self, prefix_context: str) -> ProviderPrefixRecord:
        bootstrap_message = _prefix_bootstrap_message(prefix_context)
        prefix_key = self._prefix_key(bootstrap_message)
        with self.prefix_store.locked(self.config.provider, prefix_key):
            current = self._active_prefix(prefix_key)
            if current is not None:
                now = _now()
                self.thread_store.record_success(
                    current.provider,
                    current.provider_session_id,
                    activity_at=now,
                )
                refreshed = replace(current, updated_at=now)
                self.prefix_store.save(refreshed)
                return refreshed
            result = self._run_provider(
                bootstrap_message,
                provider_session_id=None,
                resume=False,
                rightmemory_session_id=prefix_key,
                policy=FORK_BASE_POLICY,
            )
            _require_empty_retrieve_selection(result.text)
            now = _now()
            record = ProviderPrefixRecord(
                provider=self.config.provider,
                prefix_key=prefix_key,
                provider_session_id=result.provider_session_id,
                created_at=now,
                updated_at=now,
            )
            self.prefix_store.save(record)
            return record

    def _retire_prefix_base(self, prefix: ProviderPrefixRecord) -> tuple[str, ...]:
        errors: list[str] = []
        try:
            with self.prefix_store.locked(prefix.provider, prefix.prefix_key):
                try:
                    self.prefix_store.delete_if_matches(
                        prefix.provider,
                        prefix.prefix_key,
                        prefix.provider_session_id,
                    )
                except Exception as exc:
                    errors.append(f"mapping: {type(exc).__name__}: {exc}")
                try:
                    ownership = self.thread_store.load(
                        prefix.provider,
                        prefix.provider_session_id,
                    )
                    if ownership is not None:
                        self.thread_store.mark_delete_pending(
                            ownership,
                            attempted_at=_now(),
                            error="provider fork failed; prefix base retired",
                        )
                except Exception as exc:
                    errors.append(f"ownership: {type(exc).__name__}: {exc}")
        except Exception as exc:
            errors.append(f"lock: {type(exc).__name__}: {exc}")
        return tuple(errors)

    def _active_prefix(self, prefix_key: str) -> ProviderPrefixRecord | None:
        try:
            mapping = self.prefix_store.load(self.config.provider, prefix_key)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if mapping is None:
            return None
        try:
            ownership = self.thread_store.load(mapping.provider, mapping.provider_session_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (
            ownership is None
            or ownership.status != "active"
            or ownership.policy != FORK_BASE_POLICY
            or provider_thread_is_expired(ownership)
            or ownership.role != "retrieve"
            or ownership.rightmemory_session_id != prefix_key
        ):
            return None
        return mapping

    def _prefix_key(self, bootstrap_message: str) -> str:
        prompt = _turn_prompt(
            self.memory_root,
            self.role,
            bootstrap_message,
            self.semantic_upgrades,
            include_instructions=True,
        )
        payload = json.dumps(
            {
                "model": self.config.model,
                "prompt": prompt,
                "provider": self.config.provider,
                "reasoning_effort": self.config.reasoning_effort,
                "sandbox": _codex_sandbox(self.role),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def has_saved_session(self, rightmemory_session_id: str) -> bool:
        if self.role != "retrieve" or self.fresh_provider_session:
            return False
        return self._active_persistent_session(rightmemory_session_id) is not None

    def has_process_session(self) -> bool:
        return self._process_provider_session_id is not None

    def cleanup(self) -> None:
        if self._owns_codex_runner and self._codex_runner is not None:
            self._codex_runner.close()
            self._owns_codex_runner = False

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
            if self._codex_runner is None:
                raise RuntimeError("Codex SDK runner is unavailable")
            timing: CodexSdkTiming | None = None
            sdk_started_at = time.perf_counter()

            def record_timing(value: CodexSdkTiming) -> None:
                nonlocal timing
                timing = value

            def record_started(thread_id: str) -> None:
                self._record_created(thread_id, rightmemory_session_id, policy, created_at)

            try:
                sdk_result = self._codex_runner.run_turn(
                    prompt=prompt,
                    provider_session_id=provider_session_id,
                    cwd=self.memory_root,
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                    sandbox=_codex_sandbox(self.role),
                    on_thread_started=record_started if provider_session_id is None else None,
                    on_timing=record_timing,
                )
            except Exception as exc:
                self._trace_provider_timing(
                    timing or CodexSdkTiming(total_ms=_elapsed_ms(sdk_started_at)),
                    resumed=provider_session_id is not None,
                    outcome="error",
                    error_type=type(exc).__name__,
                )
                raise
            self._trace_provider_timing(
                timing or sdk_result.timing,
                resumed=provider_session_id is not None,
                outcome="success",
            )
            self._record_provider_success("codex", sdk_result.provider_session_id)
            return AgentCliResult(
                provider_session_id=sdk_result.provider_session_id,
                text=sdk_result.text,
            )
        if self.config.provider == "claude":
            if provider_session_id is not None:
                claude_session_id = provider_session_id
            elif self.fresh_provider_session or policy in {
                FORK_BASE_POLICY,
                ONE_SHOT_POLICY,
                PROCESS_LOCAL_POLICY,
            }:
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
            self._record_provider_success("claude", result.provider_session_id)
            return result
        raise ValueError("agent_cli provider must be one of: claude, codex")

    def _run_forked_provider(
        self,
        message: str,
        *,
        source_provider_session_id: str,
        rightmemory_session_id: str,
        policy: str,
    ) -> AgentCliResult:
        prompt = _turn_prompt(
            self.memory_root,
            self.role,
            message,
            self.semantic_upgrades,
            include_instructions=False,
        )
        created_at = _now()
        if self.config.provider == "codex":
            if self._codex_runner is None:
                raise RuntimeError("Codex SDK runner is unavailable")
            timing: CodexSdkTiming | None = None
            sdk_started_at = time.perf_counter()

            def record_timing(value: CodexSdkTiming) -> None:
                nonlocal timing
                timing = value

            def record_started(thread_id: str) -> None:
                self._record_created(
                    thread_id,
                    rightmemory_session_id,
                    policy,
                    created_at,
                    forked_from_provider_session_id=source_provider_session_id,
                )

            try:
                sdk_result = self._codex_runner.run_forked_turn(
                    prompt=prompt,
                    source_provider_session_id=source_provider_session_id,
                    cwd=self.memory_root,
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                    sandbox=_codex_sandbox(self.role),
                    on_thread_started=record_started,
                    on_timing=record_timing,
                )
            except Exception as exc:
                self._trace_provider_timing(
                    timing or CodexSdkTiming(total_ms=_elapsed_ms(sdk_started_at)),
                    resumed=False,
                    forked=True,
                    outcome="error",
                    error_type=type(exc).__name__,
                )
                raise
            self._trace_provider_timing(
                timing or sdk_result.timing,
                resumed=False,
                forked=True,
                outcome="success",
            )
            self._record_provider_success("codex", sdk_result.provider_session_id)
            return AgentCliResult(
                provider_session_id=sdk_result.provider_session_id,
                text=sdk_result.text,
            )
        if self.config.provider == "claude":
            child_provider_session_id = str(uuid4())
            self._record_created(
                child_provider_session_id,
                rightmemory_session_id,
                policy,
                created_at,
                forked_from_provider_session_id=source_provider_session_id,
            )
            command = build_claude_command(
                self.role,
                self.config,
                prompt,
                source_provider_session_id,
                True,
                fork=True,
                fork_provider_session_id=child_provider_session_id,
            )
            stdout = _run_cli(command, self.memory_root, "Claude")
            result = parse_claude_output(stdout)
            if result.provider_session_id == source_provider_session_id:
                raise RuntimeError("Claude fork returned the source provider session id")
            _validate_uuid(result.provider_session_id)
            if result.provider_session_id != child_provider_session_id:
                self._record_created(
                    result.provider_session_id,
                    rightmemory_session_id,
                    policy,
                    created_at,
                    forked_from_provider_session_id=source_provider_session_id,
                )
                preallocated = self.thread_store.load(
                    "claude",
                    child_provider_session_id,
                )
                if preallocated is not None:
                    self.thread_store.mark_delete_pending(
                        preallocated,
                        attempted_at=_now(),
                        error="Claude fork returned a different child session id",
                    )
            self._record_provider_success("claude", result.provider_session_id)
            return result
        raise ValueError("agent_cli provider must be one of: claude, codex")

    def _trace_provider_timing(
        self,
        timing: CodexSdkTiming,
        *,
        resumed: bool,
        forked: bool = False,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        if self._trace_event is None:
            return
        fields: dict[str, Any] = {
            "transport": "codex-sdk",
            "resumed": resumed,
            "forked": forked,
            "client_start_ms": timing.client_start_ms,
            "thread_open_ms": timing.thread_open_ms,
            "turn_ms": timing.turn_ms,
            "server_duration_ms": timing.server_duration_ms,
            "total_ms": timing.total_ms,
            "outcome": outcome,
        }
        if timing.usage is not None:
            fields["usage"] = timing.usage
        if error_type is not None:
            fields["error_type"] = error_type
        self._trace_event("provider_timing", **fields)

    def _record_created(
        self,
        provider_session_id: str,
        rightmemory_session_id: str,
        policy: str,
        created_at: str,
        *,
        forked_from_provider_session_id: str | None = None,
    ) -> None:
        self.thread_store.record_created(
            provider=self.config.provider,
            provider_session_id=provider_session_id,
            role=self.role,
            rightmemory_session_id=rightmemory_session_id,
            policy=policy,
            created_at=created_at,
            forked_from_provider_session_id=forked_from_provider_session_id,
        )

    def _record_provider_success(self, provider: str, provider_session_id: str) -> None:
        activity_at = _now()
        self.thread_store.record_success(provider, provider_session_id, activity_at=activity_at)
        ownership = self.thread_store.load(provider, provider_session_id)
        if ownership is not None and ownership.forked_from_provider_session_id is not None:
            try:
                parent = self.thread_store.load(
                    provider,
                    ownership.forked_from_provider_session_id,
                )
            except (OSError, ValueError, json.JSONDecodeError):
                return
            if (
                parent is not None
                and parent.status == "active"
                and parent.policy == FORK_BASE_POLICY
                and parent.role == ownership.role
            ):
                self.thread_store.record_success(
                    provider,
                    parent.provider_session_id,
                    activity_at=activity_at,
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


def build_claude_command(
    role: str,
    config: AgentCliConfig,
    prompt: str,
    provider_session_id: str,
    resume: bool,
    *,
    fork: bool = False,
    fork_provider_session_id: str | None = None,
) -> list[str]:
    _validate_role(role)
    _validate_uuid(provider_session_id)
    if config.reasoning_effort is not None:
        raise ValueError("agent_cli reasoning_effort is only supported for Codex")
    if fork and not resume:
        raise ValueError("Claude session forking requires resume")
    if fork and fork_provider_session_id is None:
        raise ValueError("Claude session forking requires a child provider session id")
    if not fork and fork_provider_session_id is not None:
        raise ValueError("Claude child provider session id requires session forking")
    if fork_provider_session_id is not None:
        _validate_uuid(fork_provider_session_id)
    command = ["claude", "-p", "--output-format", "json"]
    _append_model(command, config)
    command.extend(["--permission-mode", _claude_permission_mode(role)])
    session_flag = "--resume" if resume else "--session-id"
    command.extend([session_flag, provider_session_id])
    if fork:
        command.extend(["--fork-session", "--session-id", fork_provider_session_id])
    command.append(prompt)
    return command


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


def _prefix_bootstrap_message(prefix_context: str) -> str:
    return _join_prefix_context(
        prefix_context,
        f"# Query\n\n{_PREFIX_BOOTSTRAP_QUERY}\n",
    )


def _join_prefix_context(prefix_context: str, message: str) -> str:
    parts = [part.rstrip() for part in (prefix_context, message) if part.strip()]
    return "\n\n".join(parts).rstrip() + "\n"


def _require_empty_retrieve_selection(value: str) -> None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Retrieve prefix bootstrap did not return strict JSON") from exc
    if parsed != _EMPTY_RETRIEVE_SELECTION:
        raise RuntimeError("Retrieve prefix bootstrap did not return the empty terminal selection")


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


def _elapsed_ms(started_at: float) -> float:
    return round(max(0.0, time.perf_counter() - started_at) * 1000, 3)
