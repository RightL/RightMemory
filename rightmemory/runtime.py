from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from functools import wraps
from typing import Any

from .config import RuntimeConfig, load_config
from .debug import DebugTrace
from .prompt import build_instructions
from .session import MemoryWriteLock, MessageSessionStore
from .sync import SyncManager
from .tools import MemoryTools


SEMANTIC_SYNC_ROLES = {"dreamer", "reviewer", "update"}
SYNC_TOOL_ROLES = {"sync-reconciler"}
SYNC_REPAIR_STATUSES = {"conflict", "dirty"}
SYNC_REPAIR_SESSION_ID = "runtime-sync-repair"
SUPPORTED_MODEL_SETTINGS = {
    "max_tokens",
    "temperature",
    "top_p",
    "timeout",
    "parallel_tool_calls",
    "thinking",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "stop_sequences",
    "extra_headers",
    "extra_body",
}
RECOVERABLE_TOOL_ERRORS = (ValueError, FileNotFoundError)
MODEL_REQUEST_LIMIT = 100


class RightMemoryRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.tools = MemoryTools(config.memory_root)
        self.sessions = MessageSessionStore(config.memory_root, config.role)
        self._message_history: list[Any] = []
        self._active_trace: DebugTrace | None = None
        self._sync_manager: SyncManager | None = None
        self.agent = self._build_agent()

    def run_turn(self, message: str) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        result, post_sync = self._run_locked_turn(
            lambda: self.agent.run_sync(
                message,
                message_history=self._message_history or None,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
        )
        all_messages = getattr(result, "all_messages", None)
        if callable(all_messages):
            self._message_history = list(all_messages())
        if post_sync is not None:
            self._run_sync_reconciler(post_sync)
        output = getattr(result, "output", None)
        return str(output if output is not None else result)

    def run_session_turn(self, session_id: str, message: str) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        with self._debug_trace(session_id) as trace:
            self._trace(
                "run_started",
                message=message,
                model_id=self.config.model_id,
                api_base=self.config.api_base,
            )
            try:
                result, post_sync = self._run_locked_turn(lambda: self._run_session_model(session_id, message))
            except Exception as exc:
                self._trace("run_failed", error_type=type(exc).__name__, error=str(exc))
                raise
            if post_sync is not None:
                self._run_sync_reconciler(post_sync)
            output = getattr(result, "output", None)
            self._trace("run_finished", output=str(output if output is not None else result))
        output = getattr(result, "output", None)
        return str(output if output is not None else result)

    def _run_session_model(self, session_id: str, message: str):
        with self.sessions.locked(session_id) as session:
            history_json = session.load_json()
            history = self._load_message_history(history_json) if history_json is not None else None
            self._trace("history_loaded", message_count=len(history or []))
            self._trace("model_started")
            result = self.agent.run_sync(
                message,
                message_history=history,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
            output = getattr(result, "output", None)
            self._trace("model_finished", output=str(output if output is not None else result))
            session.save_json(self._dump_message_history(result))
            self._trace("history_saved", path=str(session.paths.history))
            return result

    def _run_locked_turn(self, run_model: Callable[[], Any]) -> tuple[Any, Any | None]:
        if self.config.role not in SEMANTIC_SYNC_ROLES or not self.config.sync.enabled:
            with self._memory_write_lock():
                return run_model(), None

        repaired = False
        while True:
            with self._memory_write_lock():
                preflight_repair = self._sync_preflight_locked()
                if preflight_repair is None:
                    result = run_model()
                    return result, self._sync_after_semantic_turn()
            if repaired:
                raise RuntimeError(
                    f"sync repair did not clear {preflight_repair.status} state: {preflight_repair.message}"
                )
            self._run_sync_reconciler(preflight_repair)
            repaired = True

    def _sync(self) -> SyncManager:
        if self._sync_manager is None:
            self._sync_manager = SyncManager(self.config.sync)
        return self._sync_manager

    def _sync_preflight_locked(self):
        result = self._sync().preflight()
        if result.status in SYNC_REPAIR_STATUSES:
            return result
        return None

    def _sync_after_semantic_turn(self):
        if self.config.role not in SEMANTIC_SYNC_ROLES or not self.config.sync.enabled:
            return None
        result = self._sync().push()
        if result.status in SYNC_REPAIR_STATUSES:
            return result
        return None

    def sync_push(self) -> str:
        """Push committed memory changes to the configured Git upstream."""
        result = self._sync().push()
        return result.context_block()

    def _run_sync_reconciler(self, result) -> None:
        reconciler_config = load_config("sync-reconciler")
        if reconciler_config.memory_root != self.config.memory_root:
            raise ValueError(
                "sync-reconciler memory root mismatch: "
                f"current runtime uses {self.config.memory_root}, sync-reconciler uses {reconciler_config.memory_root}"
            )
        runtime = RightMemoryRuntime(reconciler_config)
        try:
            runtime.run_session_turn(SYNC_REPAIR_SESSION_ID, self._sync().repair_message(result))
        finally:
            runtime.cleanup()

    def _memory_write_lock(self):
        if self.config.role == "retrieve":
            return nullcontext()
        return MemoryWriteLock(self.config.memory_root)

    def _build_agent(self):
        try:
            from pydantic_ai import Agent
        except ImportError as exc:
            raise RuntimeError("install standalone dependencies with: pip install -e .") from exc

        return Agent(
            model=build_model(self.config),
            instructions=build_instructions(self.config.memory_root, self.config.role),
            tools=self._agent_tools(),
            retries=self.config.max_tool_retries,
        )

    def _agent_tools(self) -> list[Callable[..., Any]]:
        read_tools = [
            self._agent_tool(self.tools.glob),
            self._agent_tool(self.tools.grep),
            self._agent_tool(self.tools.read),
            self._agent_tool(self.tools.read_command),
            self._agent_tool(self.tools.outline_file),
            self._agent_tool(self.tools.validate_memory),
        ]
        if self.config.role == "retrieve":
            return read_tools
        write_tools = [
            *read_tools,
            self._agent_tool(self.tools.edit_file),
            self._agent_tool(self.tools.create_file),
            self._agent_tool(self.tools.delete_file),
            self._agent_tool(self.tools.rename_file),
            self._agent_tool(self.tools.git_status),
            self._agent_tool(self.tools.git_diff),
            self._agent_tool(self.tools.git_add),
            self._agent_tool(self.tools.git_commit),
        ]
        if self.config.role == "sync-reconciler":
            write_tools.append(self._agent_tool(self.tools.git_discard))
        if self.config.sync.enabled and self.config.role in SYNC_TOOL_ROLES:
            write_tools.append(self._agent_tool(self.sync_push))
        return write_tools

    def _agent_tool(self, tool: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = _retryable_tool(tool)
        if self.config.debug_trace:
            wrapped = self._traceable_tool(wrapped)
        return wrapped

    def _traceable_tool(self, tool: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(tool)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._trace("tool_started", tool=tool.__name__)
            try:
                result = tool(*args, **kwargs)
            except Exception as exc:
                self._trace("tool_failed", tool=tool.__name__, error_type=type(exc).__name__, error=str(exc))
                raise
            self._trace("tool_finished", tool=tool.__name__, result=_short_trace_value(result))
            return result

        return wrapper

    def _model_settings(self) -> dict[str, Any] | None:
        if not self.config.model_kwargs:
            return None
        unsupported = sorted(set(self.config.model_kwargs) - _supported_model_settings())
        if unsupported:
            joined = ", ".join(unsupported)
            raise ValueError(f"unsupported Pydantic AI model setting(s) in [model.kwargs]: {joined}")
        return dict(self.config.model_kwargs)

    def _usage_limits(self):
        try:
            from pydantic_ai import UsageLimits
        except ImportError as exc:
            raise RuntimeError("install standalone dependencies with: pip install -e .") from exc

        return UsageLimits(request_limit=MODEL_REQUEST_LIMIT)

    def cleanup(self) -> None:
        cleanup = getattr(self.agent, "cleanup", None)
        if callable(cleanup):
            cleanup()

    @contextmanager
    def _debug_trace(self, session_id: str):
        if not self.config.debug_trace:
            yield None
            return
        previous = self._active_trace
        trace = DebugTrace(self.config.memory_root, self.config.role, session_id)
        self._active_trace = trace
        try:
            yield trace
        finally:
            self._active_trace = previous

    def _trace(self, event: str, **fields: Any) -> None:
        if self._active_trace is not None:
            self._active_trace.append(event, **fields)

    def _load_message_history(self, data: bytes) -> list[Any]:
        try:
            from pydantic_ai.messages import ModelMessagesTypeAdapter
        except ImportError as exc:
            raise RuntimeError("install standalone dependencies with: pip install -e .") from exc
        return list(ModelMessagesTypeAdapter.validate_json(data))

    def _dump_message_history(self, result: Any) -> bytes:
        all_messages_json = getattr(result, "all_messages_json", None)
        if not callable(all_messages_json):
            raise RuntimeError("Pydantic AI result does not expose all_messages_json()")
        return bytes(all_messages_json())


def build_model(config: RuntimeConfig):
    if config.model_id.startswith("anthropic/"):
        return _build_anthropic_model(config)
    return _build_openai_compatible_model(config)


def _build_openai_compatible_model(config: RuntimeConfig):
    try:
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:
        raise RuntimeError("install standalone dependencies with: pip install -e .") from exc

    provider_kwargs: dict[str, str] = {}
    if config.api_base is not None:
        provider_kwargs["base_url"] = config.api_base
    if config.api_key is not None:
        provider_kwargs["api_key"] = config.api_key
    provider = OpenAIProvider(**provider_kwargs)
    model_name = _openai_model_name(config.model_id)
    return OpenAIChatModel(model_name, provider=provider)


def _build_anthropic_model(config: RuntimeConfig):
    try:
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
    except ImportError as exc:
        raise RuntimeError("install standalone dependencies with: pip install -e .") from exc

    provider_kwargs: dict[str, str] = {}
    if config.api_base is not None:
        provider_kwargs["base_url"] = config.api_base
    if config.api_key is not None:
        provider_kwargs["api_key"] = config.api_key
    provider = AnthropicProvider(**provider_kwargs)
    model_name = config.model_id.removeprefix("anthropic/")
    return AnthropicModel(model_name, provider=provider)


def _openai_model_name(model_id: str) -> str:
    if model_id.startswith("hosted_vllm/"):
        return model_id.removeprefix("hosted_vllm/")
    if model_id.startswith("openai/"):
        return model_id.removeprefix("openai/")
    return model_id


def _supported_model_settings() -> set[str]:
    try:
        from pydantic_ai.settings import ModelSettings
    except ImportError:
        return SUPPORTED_MODEL_SETTINGS
    annotations = getattr(ModelSettings, "__annotations__", {})
    return set(annotations) or SUPPORTED_MODEL_SETTINGS


def _retryable_tool(tool: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(tool)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return tool(*args, **kwargs)
        except RECOVERABLE_TOOL_ERRORS as exc:
            from pydantic_ai import ModelRetry

            raise ModelRetry(str(exc)) from exc

    return wrapper


def _short_trace_value(value: Any) -> str:
    text = str(value)
    if len(text) > 1000:
        return text[:1000] + "...[truncated]"
    return text
