from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any

from .agent_cli import CliAgentExecutor, NO_SESSION_RIGHTMEMORY_SESSION_ID
from .config import PrunerConfig, RuntimeConfig, load_config
from .debug import DebugTrace
from .isolated_write import IsolatedWriteSupervisor, MainMemoryDirtyError
from .prompt import build_instructions
from .prune import build_pruner_message, prune_due_status
from .provider_sessions import ProviderSessionStore
from .recent_submitted import (
    RecentSubmittedMemoryDeliveryStore,
    RecentSubmittedMemoryEntry,
    collect_recent_submitted_memory,
)
from .retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    current_memory_head,
    format_memory_diff_block,
    format_recent_submitted_context_block,
    load_daily_snapshot,
    memory_diff_since,
)
from .semantic_upgrades import SemanticUpgradeContext, mark_absorbed, pending_context
from .session import MemoryWriteLock, MessageSessionStore, _ensure_runtime_gitignore, _fsync_directory
from .shared_view_files import publish_approved_file_views, pull_all_file_views, record_file_view_publish_results
from .sync import SyncManager, SyncResult
from .tools import MemoryTools


AUTOMATIC_WRITE_ROLES = {"dreamer", "insight", "pruner", "reviewer", "update"}
CYCLE_ROLES = {"dreamer", "insight"}
HISTORY_READ_ROLES = {"historian", "pruner"}
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
THINK_START_TAG = "<think>"
THINK_END_TAG = "</think>"


@dataclass(frozen=True)
class PreparedRetrieveTurn:
    message: str
    query: str
    recent_submitted_entries: list[RecentSubmittedMemoryEntry]
    memory_commit: str | None


class RightMemoryRuntime:
    def __init__(self, config: RuntimeConfig):
        if config.runtime_mode not in {"standalone", "cli-agent"}:
            raise RuntimeError(f"unsupported runtime mode: {config.runtime_mode}")
        self.config = config
        self.tools = MemoryTools(config.memory_root, role=config.role)
        self.sessions = MessageSessionStore(config.state_root, config.role)
        self.retrieve_context = RetrieveContextStore(config.state_root)
        self.recent_submitted_delivery = RecentSubmittedMemoryDeliveryStore(config.state_root)
        self._message_history: list[Any] = []
        self._active_trace: DebugTrace | None = None
        self._sync_manager: SyncManager | None = None
        self.semantic_upgrades = self._semantic_upgrade_context()
        self._semantic_upgrade_ids = self.semantic_upgrades.ids if self.semantic_upgrades is not None else []
        self.agent = self._build_cli_agent() if config.runtime_mode == "cli-agent" else self._build_agent()

    def run_turn(self, message: str) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        if self.config.runtime_mode == "cli-agent":
            result, post_sync = self._run_locked_turn(
                lambda: self._run_session_cli_agent(NO_SESSION_RIGHTMEMORY_SESSION_ID, message)
            )
            if post_sync is not None:
                self._run_sync_reconciler(post_sync)
            self._publish_file_views_after_write()
            self._mark_semantic_upgrades_absorbed()
            return str(result)
        self._pull_file_views_for_retrieve()
        prepared = self._prepare_retrieve_turn(
            NO_SESSION_RIGHTMEMORY_SESSION_ID,
            message,
        )
        result, post_sync = self._run_locked_turn(
            lambda: self.agent.run_sync(
                prepared.message,
                message_history=None if self.config.role == "retrieve" else self._message_history or None,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
        )
        output = self._result_output(result)
        if self.config.role == "retrieve":
            self._record_successful_retrieve_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, prepared, output)
        else:
            self._store_message_history_from_result(result)
        if post_sync is not None:
            self._run_sync_reconciler(post_sync)
        self._publish_file_views_after_write()
        self._mark_semantic_upgrades_absorbed()
        return output

    def run_session_turn(self, session_id: str, message: str, *, on_started: Callable[[], None] | None = None) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        if session_id == NO_SESSION_RIGHTMEMORY_SESSION_ID:
            raise ValueError(f"session id is reserved for internal no-session turns: {session_id}")
        with self._debug_trace(session_id) as trace:
            self._trace(
                "run_started",
                message=message,
                model_id=self._trace_model_id(),
                api_base=self.config.api_base,
            )
            try:
                isolate_write = self._should_isolate_write_turn()
                run_session = (
                    self._run_session_cli_agent
                    if self.config.runtime_mode == "cli-agent"
                    else self._run_session_model
                )
                if on_started is None:
                    direct_callback = lambda: run_session(session_id, message)
                    isolated_callback = lambda: self._run_session_turn_isolated(session_id, message)
                else:
                    direct_callback = lambda: run_session(session_id, message, on_started=on_started)
                    isolated_callback = lambda: self._run_session_turn_isolated(
                        session_id,
                        message,
                        on_started=on_started,
                    )
                if isolate_write:
                    run_callback = isolated_callback
                else:
                    run_callback = direct_callback
                result, post_sync = self._run_locked_turn(run_callback, isolate_write=isolate_write)
            except Exception as exc:
                self._trace("run_failed", error_type=type(exc).__name__, error=str(exc))
                raise
            if post_sync is not None:
                self._run_sync_reconciler(post_sync)
            self._publish_file_views_after_write()
            self._mark_semantic_upgrades_absorbed()
            output = self._result_output(result)
            self._trace("run_finished", output=output)
        return output

    def run_cycle(self, session_id: str, operator_hint: str | None = None) -> str:
        if self.config.role not in CYCLE_ROLES:
            raise ValueError("run_cycle requires dreamer or insight role")
        hint = (operator_hint or "none").strip() or "none"
        message = "\n".join(
            (
                "<rightmemory_cycle>",
                f"role: {self.config.role}",
                f"operator_hint: {hint}",
                "</rightmemory_cycle>",
            )
        )
        return self.run_session_turn(session_id, message)

    def run_prune_turn(self, session_id: str, pruner_config: PrunerConfig) -> str:
        if self.config.role != "pruner":
            raise ValueError("run_prune_turn requires pruner role")
        if session_id == NO_SESSION_RIGHTMEMORY_SESSION_ID:
            raise ValueError(f"session id is reserved for internal no-session turns: {session_id}")
        with self._debug_trace(session_id) as trace:
            self._trace(
                "run_started",
                message="prune generation check",
                model_id=self._trace_model_id(),
                api_base=self.config.api_base,
            )
            try:
                isolate_write = self._should_isolate_write_turn()
                if isolate_write:
                    run_callback = lambda: self._run_prune_turn_isolated(session_id, pruner_config)
                else:
                    run_callback = lambda: self._run_prune_turn_direct(session_id, pruner_config)
                result, post_sync = self._run_locked_turn(run_callback, isolate_write=isolate_write)
            except Exception as exc:
                self._trace("run_failed", error_type=type(exc).__name__, error=str(exc))
                raise
            if post_sync is not None:
                self._run_sync_reconciler(post_sync)
            self._publish_file_views_after_write()
            output = self._result_output(result)
            self._trace("run_finished", output=output)
        return output

    def _run_session_model(self, session_id: str, message: str, *, on_started: Callable[[], None] | None = None):
        with self.sessions.locked(session_id) as session:
            self._pull_file_views_for_retrieve()
            prepared = self._prepare_retrieve_turn(session_id, message)
            history_json = session.load_json()
            history = self._load_message_history(history_json) if history_json is not None else None
            self._trace("history_loaded", message_count=len(history or []))
            if on_started is not None:
                on_started()
            self._trace("model_started")
            result = self.agent.run_sync(
                prepared.message,
                message_history=None if self.config.role == "retrieve" else history,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
            output = self._result_output(result)
            self._trace("model_finished", output=output)
            if self.config.role == "retrieve":
                self._record_successful_retrieve_turn(session_id, prepared, output)
            else:
                session.save_json(self._dump_message_history(result))
                self._trace("history_saved", path=str(session.paths.history))
            return result

    def _run_session_cli_agent(self, session_id: str, message: str, *, on_started: Callable[[], None] | None = None) -> str:
        with self.sessions.locked(session_id):
            self._pull_file_views_for_retrieve()
            prepared = self._prepare_retrieve_turn(session_id, message)
            if on_started is not None:
                on_started()
            self._trace("model_started")
            if self.config.role == "retrieve":
                result = self.agent.run_stateless_turn(prepared.message)
                self._trace("model_finished", output=str(result))
                self._record_successful_retrieve_turn(session_id, prepared, str(result))
                return result
            result = self.agent.run_session_turn(session_id, prepared.message)
            self._trace("model_finished", output=str(result))
            self._record_successful_retrieve_turn(session_id, prepared, str(result))
            return result

    def _run_prune_turn_direct(self, session_id: str, pruner_config: PrunerConfig):
        status = prune_due_status(
            self.config.memory_root,
            replace(pruner_config, memory_root=self.config.memory_root),
        )
        if not status.due:
            return status.message
        message = build_pruner_message(status)
        if self.config.runtime_mode == "cli-agent":
            return self._run_session_cli_agent(session_id, message)
        return self._run_session_model(session_id, message)

    def _run_session_turn_isolated(
        self,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
    ):
        supervisor = IsolatedWriteSupervisor(self.config.memory_root, self.config.role)
        state = _IsolatedStateOverlay(
            self.config.state_root,
            self.config.role,
            session_id,
            seed_provider_session=self.config.runtime_mode != "cli-agent",
        )
        with self.sessions.locked(session_id):
            with state as state_root:
                try:
                    if on_started is None:
                        run_in_worktree = lambda worktree: self._run_session_turn_in_worktree(
                            worktree,
                            state_root,
                            session_id,
                            message,
                        )
                    else:
                        run_in_worktree = lambda worktree: self._run_session_turn_in_worktree(
                            worktree,
                            state_root,
                            session_id,
                            message,
                            on_started=on_started,
                        )
                    result = supervisor.run(
                        run_in_worktree
                    )
                    state.promote()
                    return result.output
                except Exception:
                    state.archive_failed_provider_session()
                    raise

    def _run_prune_turn_isolated(self, session_id: str, pruner_config: PrunerConfig):
        supervisor = IsolatedWriteSupervisor(self.config.memory_root, self.config.role)
        state = _IsolatedStateOverlay(
            self.config.state_root,
            self.config.role,
            session_id,
            seed_provider_session=self.config.runtime_mode != "cli-agent",
        )
        with self.sessions.locked(session_id):
            with state as state_root:
                try:
                    result = supervisor.run(
                        lambda worktree: self._run_prune_turn_in_worktree(
                            worktree,
                            state_root,
                            session_id,
                            pruner_config,
                        )
                    )
                    state.promote()
                    return result.output
                except Exception:
                    state.archive_failed_provider_session()
                    raise

    def _run_prune_turn_in_worktree(
        self,
        worktree: Path,
        state_root: Path,
        session_id: str,
        pruner_config: PrunerConfig,
    ):
        status = prune_due_status(worktree, replace(pruner_config, memory_root=worktree))
        if not status.due:
            return status.message
        return self._run_session_turn_in_worktree(worktree, state_root, session_id, build_pruner_message(status))

    def _run_session_turn_in_worktree(
        self,
        worktree: Path,
        state_root: Path,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
    ):
        nested_config = replace(
            self.config,
            memory_root=worktree,
            state_root=state_root,
            fresh_provider_session=self.config.runtime_mode == "cli-agent",
            sync=replace(self.config.sync, memory_root=worktree, enabled=False),
        )
        nested = RightMemoryRuntime(nested_config)
        nested._active_trace = self._active_trace
        try:
            if nested.config.runtime_mode == "cli-agent":
                if on_started is None:
                    run_cli = lambda: nested._run_session_cli_agent(session_id, message)
                else:
                    run_cli = lambda: nested._run_session_cli_agent(session_id, message, on_started=on_started)
                result, _post_sync = nested._run_locked_turn(
                    run_cli
                )
                return result
            if on_started is None:
                run_model = lambda: nested._run_session_model(session_id, message)
            else:
                run_model = lambda: nested._run_session_model(session_id, message, on_started=on_started)
            result, _post_sync = nested._run_locked_turn(run_model)
            return result
        finally:
            nested.cleanup()

    def _run_locked_turn(self, run_model: Callable[[], Any], *, isolate_write: bool = False) -> tuple[Any, Any | None]:
        if isolate_write:
            return self._run_isolated_locked_turn(run_model)
        if self.config.role not in AUTOMATIC_WRITE_ROLES or not self.config.sync.enabled:
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

    def _run_isolated_locked_turn(self, run_model: Callable[[], Any]) -> tuple[Any, Any | None]:
        if self.config.role not in AUTOMATIC_WRITE_ROLES:
            return run_model(), None

        dirty_main_repaired = False
        repaired = False
        while True:
            if self.config.sync.enabled:
                with self._memory_write_lock():
                    preflight_repair = self._sync_preflight_locked()
                if preflight_repair is not None:
                    if repaired:
                        raise RuntimeError(
                            f"sync repair did not clear {preflight_repair.status} state: {preflight_repair.message}"
                        )
                    self._run_sync_reconciler(preflight_repair)
                    repaired = True
                    continue
            try:
                result = run_model()
            except MainMemoryDirtyError as exc:
                if dirty_main_repaired:
                    paths = ", ".join(exc.paths) if exc.paths else "memory files"
                    raise RuntimeError(f"dirty-main repair did not clear memory files: {paths}") from exc
                self._run_dirty_main_reconciler(exc.paths)
                dirty_main_repaired = True
                continue
            if self.config.sync.enabled:
                with self._memory_write_lock():
                    return result, self._sync_after_semantic_turn()
            return result, None

    def _should_isolate_write_turn(self) -> bool:
        return self.config.role in AUTOMATIC_WRITE_ROLES and self.config.memory_root == self.config.state_root

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
        if self.config.role not in AUTOMATIC_WRITE_ROLES or not self.config.sync.enabled:
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
        reconciler_config = load_config("sync-reconciler", memory_root=self.config.memory_root)
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

    def _run_dirty_main_reconciler(self, paths: tuple[str, ...]) -> None:
        result = SyncResult(
            "dirty",
            "local main memory has uncommitted changes before automatic semantic work",
            list(paths),
        )
        self._run_sync_reconciler(result)

    def _memory_write_lock(self):
        if self.config.role in {"historian", "retrieve"}:
            return nullcontext()
        return MemoryWriteLock(self.config.memory_root)

    def _prepare_retrieve_turn(
        self,
        session_id: str,
        message: str,
    ) -> PreparedRetrieveTurn:
        if self.config.role != "retrieve":
            return PreparedRetrieveTurn(message, message, [], None)
        snapshot = load_daily_snapshot(self.config.memory_root)
        state = self.retrieve_context.load(session_id)
        current_commit = current_memory_head(self.config.memory_root)
        base_commit = state.delivered_memory_commit or snapshot.base_commit
        diff = memory_diff_since(self.config.memory_root, base_commit, current_commit)
        diff_block = format_memory_diff_block(diff)

        entries = collect_recent_submitted_memory(self.config.memory_root)
        if entries:
            entries = self.recent_submitted_delivery.new_entries(session_id, entries)
        recent_block = format_recent_submitted_context_block(entries)
        request = build_retrieve_request_text(
            snapshot_text=snapshot.text,
            turns=state.turns,
            diff_block=diff_block,
            recent_block=recent_block,
            query=message,
        )
        return PreparedRetrieveTurn(request, message, entries, current_commit)

    def _record_successful_retrieve_turn(
        self,
        session_id: str,
        prepared: PreparedRetrieveTurn,
        output: str,
    ) -> None:
        if self.config.role != "retrieve":
            return
        self.retrieve_context.record_success(
            session_id,
            query=prepared.query,
            answer=output,
            memory_commit=prepared.memory_commit,
        )
        self._record_recent_submitted_delivery(session_id, prepared.recent_submitted_entries)

    def _record_recent_submitted_delivery(
        self,
        session_id: str,
        entries: list[RecentSubmittedMemoryEntry],
    ) -> None:
        if self.config.role != "retrieve":
            return
        self.recent_submitted_delivery.record_delivered(session_id, entries)

    def _pull_file_views_for_retrieve(self) -> None:
        if self.config.role != "retrieve":
            return
        with MemoryWriteLock(self.config.memory_root):
            pull_all_file_views(self.config.memory_root)

    def _publish_file_views_after_write(self) -> None:
        if self.config.role not in AUTOMATIC_WRITE_ROLES:
            return
        results = publish_approved_file_views(self.config.memory_root)
        record_file_view_publish_results(self.config.memory_root, results, trigger=f"{self.config.role}-write")

    def _semantic_upgrade_context(self) -> SemanticUpgradeContext | None:
        if self.config.role != "dreamer":
            return None
        context = pending_context(self.config.memory_root, state_root=self.config.state_root)
        if not context.notes and not context.warnings:
            return None
        return context

    def _mark_semantic_upgrades_absorbed(self) -> None:
        if self.config.role != "dreamer" or not self._semantic_upgrade_ids:
            return
        mark_absorbed(self.config.state_root, self._semantic_upgrade_ids)
        self._semantic_upgrade_ids = []

    def _build_agent(self):
        try:
            from pydantic_ai import Agent
        except ImportError as exc:
            raise RuntimeError("install standalone dependencies with: pip install -e .") from exc

        return Agent(
            model=build_model(self.config),
            instructions=build_instructions(
                self.config.memory_root,
                self.config.role,
                semantic_upgrades=self.semantic_upgrades,
            ),
            tools=self._agent_tools(),
            retries=self.config.max_tool_retries,
        )

    def _build_cli_agent(self) -> CliAgentExecutor:
        if self.config.agent_cli is None:
            raise RuntimeError("cli-agent runtime requires agent_cli config")
        kwargs: dict[str, Any] = {
            "state_root": self.config.state_root,
            "fresh_provider_session": self.config.fresh_provider_session,
        }
        if self.semantic_upgrades is not None:
            kwargs["semantic_upgrades"] = self.semantic_upgrades
        return CliAgentExecutor(
            self.config.memory_root,
            self.config.role,
            self.config.agent_cli,
            **kwargs,
        )

    def _agent_tools(self) -> list[Callable[..., Any]]:
        if self.config.role == "retrieve":
            return [
                self._agent_tool(self.tools.read_skill),
                self._agent_tool(self.tools.read_mf),
            ]
        read_tools = [
            self._agent_tool(self.tools.glob),
            self._agent_tool(self.tools.grep),
            self._agent_tool(self.tools.read),
            self._agent_tool(self.tools.read_command),
            self._agent_tool(self.tools.outline_file),
        ]
        if self.config.role != "insight":
            read_tools.append(self._agent_tool(self.tools.validate_memory))
        if self.config.role in HISTORY_READ_ROLES:
            read_tools.extend(
                [
                    self._agent_tool(self.tools.git_log),
                    self._agent_tool(self.tools.git_show_file),
                ]
            )
        if self.config.role == "historian":
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
        if self.config.role == "shared-view-builder":
            write_tools.extend(
                [
                    self._agent_tool(self.tools.create_extractive_file_view),
                    self._agent_tool(self.tools.create_generative_file_view),
                    self._agent_tool(self.tools.create_question_view),
                ]
            )
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

    def _trace_model_id(self) -> str | None:
        if self.config.model_id is not None:
            return self.config.model_id
        if self.config.agent_cli is not None:
            return self.config.agent_cli.model
        return None

    @contextmanager
    def _debug_trace(self, session_id: str):
        if not self.config.debug_trace:
            yield None
            return
        previous = self._active_trace
        trace = DebugTrace(self.config.state_root, self.config.role, session_id)
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
        return self._sanitize_message_history_json(bytes(all_messages_json()))

    def _store_message_history_from_result(self, result: Any) -> None:
        all_messages_json = getattr(result, "all_messages_json", None)
        if callable(all_messages_json):
            self._message_history = self._load_message_history(self._dump_message_history(result))
            return
        all_messages = getattr(result, "all_messages", None)
        if callable(all_messages):
            self._message_history = list(all_messages())

    def _result_output(self, result: Any) -> str:
        output = getattr(result, "output", None)
        text = str(output if output is not None else result)
        return _strip_visible_thinking(text)

    def _sanitize_message_history_json(self, data: bytes) -> bytes:
        try:
            value = json.loads(data)
        except json.JSONDecodeError:
            return data
        return json.dumps(_sanitize_message_history_value(value), ensure_ascii=False).encode()


class _IsolatedStateOverlay:
    def __init__(self, state_root: Path, role: str, session_id: str, *, seed_provider_session: bool = True):
        self.state_root = state_root
        self.role = role
        self.session_id = session_id
        self.seed_provider_session = seed_provider_session
        self.overlay_root = state_root / ".runtime" / "isolated-state" / f"{role}-{uuid.uuid4().hex}"

    def __enter__(self) -> Path:
        _ensure_runtime_gitignore(self.state_root / ".runtime")
        self._seed()
        return self.overlay_root

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        shutil.rmtree(self.overlay_root, ignore_errors=True)

    def promote(self) -> None:
        _ensure_runtime_gitignore(self.state_root / ".runtime")
        for relative_path in self._promoted_paths():
            _copy_state_file(self.overlay_root / relative_path, self.state_root / relative_path)

    def archive_failed_provider_session(self) -> None:
        if self.seed_provider_session:
            return
        source = ProviderSessionStore(self.overlay_root, self.role).path(self.session_id)
        if not source.exists():
            return
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"provider session record must be a JSON object: {source}")
        archive_session_id = f"failed-isolated-{self.role}-{uuid.uuid4().hex}"
        data["rightmemory_session_id"] = archive_session_id
        destination = ProviderSessionStore(self.state_root, self.role).path(archive_session_id)
        _write_state_json_file(data, destination)

    def _seed(self) -> None:
        for relative_path in self._seeded_paths():
            _copy_state_file(self.state_root / relative_path, self.overlay_root / relative_path)
        _ensure_runtime_gitignore(self.overlay_root / ".runtime")

    def _seeded_paths(self) -> list[Path]:
        paths = [
            MessageSessionStore(self.state_root, self.role).paths(self.session_id).history.relative_to(self.state_root)
        ]
        if self.seed_provider_session:
            paths.append(
                ProviderSessionStore(self.state_root, self.role).path(self.session_id).relative_to(self.state_root)
            )
        if self.role == "dreamer":
            paths.append(Path(".runtime") / "semantic-upgrades.json")
        return paths

    def _promoted_paths(self) -> list[Path]:
        return [
            MessageSessionStore(self.state_root, self.role).paths(self.session_id).history.relative_to(self.state_root),
            ProviderSessionStore(self.state_root, self.role).path(self.session_id).relative_to(self.state_root),
        ]


def _copy_state_file(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if not source.is_file():
        raise RuntimeError(f"runtime state path is not a file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, tmp_path)
        os.replace(tmp_path, destination)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(destination.parent)


def _write_state_json_file(data: dict[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(destination.parent)


def build_model(config: RuntimeConfig):
    if not config.model_id:
        raise RuntimeError("standalone runtime requires model_id")
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


def _sanitize_message_history_value(value: Any, *, in_model_response: bool = False) -> Any:
    if isinstance(value, list):
        return [_sanitize_message_history_value(item, in_model_response=in_model_response) for item in value]
    if isinstance(value, dict):
        child_in_model_response = in_model_response or value.get("kind") == "response"
        sanitized = {
            key: _sanitize_message_history_value(item, in_model_response=child_in_model_response)
            for key, item in value.items()
        }
        if (
            in_model_response
            and sanitized.get("part_kind") == "text"
            and isinstance(sanitized.get("content"), str)
        ):
            sanitized["content"] = _strip_visible_thinking(sanitized["content"])
        return sanitized
    return value


def _strip_visible_thinking(text: str) -> str:
    if THINK_END_TAG in text:
        text = text.rsplit(THINK_END_TAG, 1)[1]
    while THINK_START_TAG in text:
        start = text.find(THINK_START_TAG)
        end = text.find(THINK_END_TAG, start + len(THINK_START_TAG))
        if end < 0:
            text = text[:start]
            break
        text = text[:start] + text[end + len(THINK_END_TAG) :]
    return text.lstrip()
