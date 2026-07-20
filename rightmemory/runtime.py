from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any

from .agent_cli import CliAgentExecutor, NO_SESSION_RIGHTMEMORY_SESSION_ID
from .automatic_effects import (
    forget_memory_change_pressure_operation,
    memory_change_pressure_points,
    record_memory_change_pressure_once,
)
from .config import PrunerConfig, RuntimeConfig, load_config
from .debug import DebugTrace
from .isolated_write import IsolatedWriteResult, IsolatedWriteSupervisor, MainMemoryDirtyError
from .prompt import build_instructions
from .prune import build_pruner_message, prune_due_status
from .provider_sessions import ProviderSessionStore
from .provider_threads import ProviderThreadStore
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
from .retrieve_selection import (
    RenderedRetrieveSelection,
    RetrieveSelection,
    RetrieveSelectionError,
    RetrieveSelectionRenderer,
    parse_retrieve_selection_json,
)
from .semantic_upgrades import SemanticUpgradeContext, mark_absorbed, pending_context
from .session import (
    MemoryWriteLock,
    MessageSessionStore,
    _ensure_durable_directory,
    _ensure_runtime_gitignore,
    _fsync_directory,
)
from .semantic_operation import FINAL_PHASES, OperationEffect, SemanticOperationRecord, SemanticOperationStore
from .shared_view_files import (
    prepare_file_view_publish_outbox,
    publish_approved_file_views,
    publish_file_view_outbox,
    pull_all_file_views,
    record_file_view_publish_results,
)
from .sync import SyncManager, SyncResult
from .tools import MemoryTools
from .update_review import UpdateExecutionLock, UpdateReviewStore


AUTOMATIC_WRITE_ROLES = {"dreamer", "insight", "pruner", "update"}
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
UPDATE_MODES = {"normal", "review-correction"}
STATE_EFFECT = "session-state"
REVIEW_EFFECT = "update-review"
PRESSURE_EFFECT = "memory-pressure"
PUBLISH_EFFECT = "file-view-publish"
SEMANTIC_UPGRADE_EFFECT = "semantic-upgrades"


@dataclass(frozen=True)
class PreparedRetrieveTurn:
    message: str
    query: str
    recent_submitted_entries: list[RecentSubmittedMemoryEntry]
    memory_commit: str | None


class RightMemoryRuntime:
    def __init__(self, config: RuntimeConfig, *, update_mode: str = "normal"):
        if config.runtime_mode not in {"standalone", "cli-agent"}:
            raise RuntimeError(f"unsupported runtime mode: {config.runtime_mode}")
        if update_mode not in UPDATE_MODES:
            raise ValueError(f"update mode must be one of: {', '.join(sorted(UPDATE_MODES))}")
        if update_mode != "normal" and config.role != "update":
            raise ValueError("non-normal update modes require the update role")
        self.config = config
        self.update_mode = update_mode
        tool_role = "update-correction" if config.role == "update" and update_mode == "review-correction" else config.role
        self.tools = MemoryTools(config.memory_root, role=tool_role)
        self.sessions = MessageSessionStore(config.state_root, config.role)
        self.retrieve_context = RetrieveContextStore(config.state_root)
        self.recent_submitted_delivery = RecentSubmittedMemoryDeliveryStore(config.state_root)
        self._message_history: list[Any] = []
        self._active_trace: DebugTrace | None = None
        self._sync_manager: SyncManager | None = None
        self._last_write_result: IsolatedWriteResult | None = None
        self._last_reviewed_update_commit: str | None = None
        self.semantic_upgrades = self._semantic_upgrade_context()
        self._semantic_upgrade_ids = self.semantic_upgrades.ids if self.semantic_upgrades is not None else []
        self.agent = self._build_cli_agent() if config.runtime_mode == "cli-agent" else self._build_agent()

    def run_turn(self, message: str, *, operation_id: str | None = None) -> str:
        if self.config.role in AUTOMATIC_WRITE_ROLES:
            with self._update_execution_lock():
                self._last_write_result = None
                self._last_reviewed_update_commit = None
                base_commit = self._review_base_commit()
                turn_kwargs: dict[str, object] = {"allow_internal_session": True}
                if operation_id is not None:
                    turn_kwargs["operation_id"] = operation_id
                output = self._run_session_turn_unlocked(
                    NO_SESSION_RIGHTMEMORY_SESSION_ID,
                    message,
                    **turn_kwargs,
                )
                if not self._should_isolate_write_turn():
                    self._create_update_review(base_commit, output)
                return output
        with self._update_execution_lock():
            self._last_write_result = None
            self._last_reviewed_update_commit = None
            base_commit = self._review_base_commit()
            output = self._run_turn_unlocked(message)
            self._create_update_review(base_commit, output)
            return output

    def run_chat_turn(
        self,
        message: str,
        session_id: str | None = None,
        *,
        operation_id: str | None = None,
    ) -> str:
        if self.config.runtime_mode != "cli-agent":
            if session_id is None:
                return self.run_turn(message, operation_id=operation_id)
            return self.run_session_turn(session_id, message, operation_id=operation_id)
        if self.config.role == "retrieve" and session_id is not None:
            return self.run_session_turn(session_id, message)
        if self._should_isolate_write_turn():
            return self.run_turn(message, operation_id=operation_id)
        with self._update_execution_lock():
            self._last_write_result = None
            self._last_reviewed_update_commit = None
            base_commit = self._review_base_commit()
            output = self._run_cli_process_turn_unlocked(message)
            self._create_update_review(base_commit, output)
            return output

    def _run_turn_unlocked(self, message: str) -> str:
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
        if self.config.role == "retrieve":
            rendered, post_sync = self._run_locked_turn(
                lambda: self._run_retrieve_model(
                    NO_SESSION_RIGHTMEMORY_SESSION_ID,
                    prepared,
                    include_returned=False,
                )
            )
            output = rendered.text
            self._record_successful_retrieve_turn(
                NO_SESSION_RIGHTMEMORY_SESSION_ID,
                prepared,
                rendered,
            )
        else:
            result, post_sync = self._run_locked_turn(
                lambda: self.agent.run_sync(
                    prepared.message,
                    message_history=self._message_history or None,
                    model_settings=self._model_settings(),
                    usage_limits=self._usage_limits(),
                )
            )
            output = self._result_output(result)
            self._store_message_history_from_result(result)
        if post_sync is not None:
            self._run_sync_reconciler(post_sync)
        self._publish_file_views_after_write()
        self._mark_semantic_upgrades_absorbed()
        return output

    def _run_cli_process_turn_unlocked(self, message: str) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        result, post_sync = self._run_locked_turn(
            lambda: self._run_session_cli_agent(
                NO_SESSION_RIGHTMEMORY_SESSION_ID,
                message,
                process_local=True,
            )
        )
        if post_sync is not None:
            self._run_sync_reconciler(post_sync)
        self._publish_file_views_after_write()
        self._mark_semantic_upgrades_absorbed()
        return str(result)

    def run_session_turn(
        self,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
        include_returned: bool = False,
        operation_id: str | None = None,
    ) -> str:
        with self._update_execution_lock():
            self._last_write_result = None
            self._last_reviewed_update_commit = None
            base_commit = self._review_base_commit()
            turn_kwargs: dict[str, object] = {
                "on_started": on_started,
                "include_returned": include_returned,
            }
            if operation_id is not None:
                turn_kwargs["operation_id"] = operation_id
            output = self._run_session_turn_unlocked(session_id, message, **turn_kwargs)
            if not self._should_isolate_write_turn():
                self._create_update_review(base_commit, output)
            return output

    def _run_session_turn_unlocked(
        self,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
        allow_internal_session: bool = False,
        include_returned: bool = False,
        operation_id: str | None = None,
    ) -> str:
        if not message.strip():
            raise ValueError("message must not be empty")
        if session_id == NO_SESSION_RIGHTMEMORY_SESSION_ID and not allow_internal_session:
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
                direct_kwargs: dict[str, object] = {}
                if on_started is not None:
                    direct_kwargs["on_started"] = on_started
                if include_returned:
                    direct_kwargs["include_returned"] = True
                direct_callback = lambda: run_session(session_id, message, **direct_kwargs)
                operation_kwargs = {} if operation_id is None else {"operation_id": operation_id}
                if on_started is None:
                    isolated_callback = lambda: self._run_session_turn_isolated(
                        session_id,
                        message,
                        **operation_kwargs,
                    )
                else:
                    isolated_callback = lambda: self._run_session_turn_isolated(
                        session_id,
                        message,
                        on_started=on_started,
                        **operation_kwargs,
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
            if not isolate_write:
                self._publish_file_views_after_write()
                self._mark_semantic_upgrades_absorbed()
            output = self._result_output(result)
            self._trace("run_finished", output=output)
        return output

    def run_cycle(
        self,
        session_id: str,
        operator_hint: str | None = None,
        *,
        operation_id: str | None = None,
    ) -> str:
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
        return self.run_session_turn(session_id, message, operation_id=operation_id)

    def run_prune_turn(
        self,
        session_id: str,
        pruner_config: PrunerConfig,
        *,
        operation_id: str | None = None,
    ) -> str:
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
                    run_callback = lambda: self._run_prune_turn_isolated(
                        session_id,
                        pruner_config,
                        operation_id=operation_id,
                    )
                else:
                    run_callback = lambda: self._run_prune_turn_direct(session_id, pruner_config)
                result, post_sync = self._run_locked_turn(run_callback, isolate_write=isolate_write)
            except Exception as exc:
                self._trace("run_failed", error_type=type(exc).__name__, error=str(exc))
                raise
            if post_sync is not None:
                self._run_sync_reconciler(post_sync)
            if not isolate_write:
                self._publish_file_views_after_write()
            output = self._result_output(result)
            self._trace("run_finished", output=output)
        return output

    def _run_session_model(
        self,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
        include_returned: bool = False,
    ):
        with self.sessions.locked(session_id) as session:
            self._pull_file_views_for_retrieve()
            prepared = self._prepare_retrieve_turn(
                session_id,
                message,
                include_returned=include_returned,
            )
            history_json = session.load_json()
            history = self._load_message_history(history_json) if history_json is not None else None
            self._trace("history_loaded", message_count=len(history or []))
            if on_started is not None:
                on_started()
            self._trace("model_started")
            if self.config.role == "retrieve":
                rendered = self._run_retrieve_model(
                    session_id,
                    prepared,
                    include_returned=include_returned,
                )
                self._trace("model_finished", output=rendered.text)
                self._record_successful_retrieve_turn(session_id, prepared, rendered)
                return rendered.text
            result = self.agent.run_sync(
                prepared.message,
                message_history=history,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
            output = self._result_output(result)
            self._trace("model_finished", output=output)
            session.save_json(self._dump_message_history(result))
            self._trace("history_saved", path=str(session.paths.history))
            return result

    def _run_session_cli_agent(
        self,
        session_id: str,
        message: str,
        *,
        on_started: Callable[[], None] | None = None,
        process_local: bool = False,
        include_returned: bool = False,
    ) -> str:
        with self.sessions.locked(session_id):
            self._pull_file_views_for_retrieve()
            if process_local:
                continuation = self.agent.has_process_session()
            elif self.config.role == "retrieve":
                continuation = (
                    session_id != NO_SESSION_RIGHTMEMORY_SESSION_ID
                    and self.agent.has_saved_session(session_id)
                )
            else:
                continuation = False
            prepared = self._prepare_retrieve_turn(
                session_id,
                message,
                cli_agent_phase="resume" if continuation else "new",
                include_returned=include_returned,
            )
            if on_started is not None:
                on_started()
            self._trace("model_started")
            if self.config.role == "retrieve":
                if process_local:
                    run_agent = self.agent.run_process_turn
                    retry_has_context = True
                elif session_id == NO_SESSION_RIGHTMEMORY_SESSION_ID:
                    run_agent = lambda request: self.agent.run_one_shot_turn(session_id, request)
                    retry_has_context = False
                else:
                    run_agent = lambda request: self.agent.run_session_turn(session_id, request)
                    retry_has_context = True
                rendered = self._run_retrieve_cli_agent(
                    session_id,
                    prepared,
                    include_returned=include_returned,
                    run_agent=run_agent,
                    retry_has_context=retry_has_context,
                )
                self._trace("model_finished", output=rendered.text)
                self._record_successful_retrieve_turn(session_id, prepared, rendered)
                return rendered.text
            if process_local:
                result = self.agent.run_process_turn(prepared.message)
            else:
                result = self.agent.run_one_shot_turn(session_id, prepared.message)
            self._trace("model_finished", output=str(result))
            return result

    def _run_retrieve_model(
        self,
        session_id: str,
        prepared: PreparedRetrieveTurn,
        *,
        include_returned: bool,
    ) -> RenderedRetrieveSelection:
        request = prepared.message
        for attempt in range(self.config.max_tool_retries + 1):
            result = self.agent.run_sync(
                request,
                message_history=None,
                model_settings=self._model_settings(),
                usage_limits=self._usage_limits(),
            )
            try:
                selection = self._coerce_retrieve_selection(getattr(result, "output", result))
                return self._render_retrieve_selection(
                    session_id,
                    prepared,
                    selection,
                    include_returned=include_returned,
                )
            except RetrieveSelectionError as exc:
                if attempt >= self.config.max_tool_retries:
                    raise
                request = _retrieve_retry_request(prepared.message, str(exc))
        raise AssertionError("unreachable retrieve retry state")

    def _run_retrieve_cli_agent(
        self,
        session_id: str,
        prepared: PreparedRetrieveTurn,
        *,
        include_returned: bool,
        run_agent: Callable[[str], str],
        retry_has_context: bool,
    ) -> RenderedRetrieveSelection:
        request = prepared.message
        for attempt in range(self.config.max_tool_retries + 1):
            raw = run_agent(request)
            try:
                selection = parse_retrieve_selection_json(str(raw))
                return self._render_retrieve_selection(
                    session_id,
                    prepared,
                    selection,
                    include_returned=include_returned,
                )
            except RetrieveSelectionError as exc:
                if attempt >= self.config.max_tool_retries:
                    raise
                retry_original = "" if retry_has_context else prepared.message
                request = _retrieve_retry_request(retry_original, str(exc))
        raise AssertionError("unreachable retrieve retry state")

    def _coerce_retrieve_selection(self, value: object) -> RetrieveSelection:
        if isinstance(value, RetrieveSelection):
            return value
        if isinstance(value, str):
            raise RetrieveSelectionError(
                "standalone retrieve must finish through the native terminal selector, not text"
            )
        try:
            return RetrieveSelection.model_validate(value)
        except Exception as exc:
            raise RetrieveSelectionError(f"terminal output is not a valid retrieve selection: {exc}") from exc

    def _render_retrieve_selection(
        self,
        session_id: str,
        prepared: PreparedRetrieveTurn,
        selection: RetrieveSelection,
        *,
        include_returned: bool,
    ) -> RenderedRetrieveSelection:
        state = self.retrieve_context.load(session_id)
        renderer = RetrieveSelectionRenderer(
            self.config.memory_root,
            max_output_chars=self.config.retrieve_max_output_chars,
        )
        return renderer.render(
            selection,
            delivered=state.delivery_coverage,
            recent_entries=prepared.recent_submitted_entries,
            include_returned=include_returned,
        )

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
        operation_id: str | None = None,
    ):
        def run_in_operation(worktree: Path, state_root: Path):
            if on_started is None:
                return self._run_session_turn_in_worktree(worktree, state_root, session_id, message)
            return self._run_session_turn_in_worktree(
                worktree,
                state_root,
                session_id,
                message,
                on_started=on_started,
            )

        return self._run_isolated_operation(
            session_id,
            operation_id,
            {"kind": "semantic-turn", "message": message},
            run_in_operation,
        )

    def _run_prune_turn_isolated(
        self,
        session_id: str,
        pruner_config: PrunerConfig,
        *,
        operation_id: str | None = None,
    ):
        return self._run_isolated_operation(
            session_id,
            operation_id,
            {"kind": "prune-turn"},
            lambda worktree, state_root: self._run_prune_turn_in_worktree(
                worktree,
                state_root,
                session_id,
                pruner_config,
            ),
        )

    def _run_isolated_operation(
        self,
        session_id: str,
        operation_id: str | None,
        operation_input: dict[str, object],
        run_in_operation: Callable[[Path, Path], Any],
    ):
        clean_operation_id = operation_id or f"{self.config.role}-{uuid.uuid4().hex}"
        operation_input = {
            **operation_input,
            "role": self.config.role,
            "session_id": session_id,
            "update_mode": self.update_mode,
        }
        pressure_points = (
            memory_change_pressure_points(self.config.memory_root)
            if self.config.role == "update"
            else None
        )
        supervisor_kwargs = {"update_mode": self.update_mode} if self.update_mode != "normal" else {}
        supervisor = IsolatedWriteSupervisor(self.config.memory_root, self.config.role, **supervisor_kwargs)
        state = _IsolatedStateOverlay(
            self.config.state_root,
            self.config.role,
            session_id,
            operation_id=clean_operation_id,
            seed_provider_session=self.config.runtime_mode != "cli-agent",
        )
        store = SemanticOperationStore(self.config.memory_root)

        def run_in_worktree(worktree: Path):
            with state as state_root:
                return run_in_operation(worktree, state_root)

        tracked_operation = False
        with self.sessions.locked(session_id):
            try:
                recover_prepared = getattr(supervisor, "recover_prepared", None)
                if callable(recover_prepared):
                    recover_prepared()
                self._recover_pending_session_state(
                    session_id,
                    exclude=clean_operation_id,
                )
                existing = store.read(clean_operation_id)
                if existing is None or existing.phase == "running":
                    store.clear_effect_state(clean_operation_id, PUBLISH_EFFECT)
                result = supervisor.run(
                    run_in_worktree,
                    operation_id=clean_operation_id,
                    operation_input=operation_input,
                    effects_for_outcome=lambda paths, commits: self._operation_effect_plan(
                        session_id,
                        paths,
                        commits,
                        pressure_points=pressure_points,
                    ),
                    prepare_effects=self._prepare_operation_effects,
                )
                self._last_write_result = result
                if store.read(clean_operation_id) is None:
                    # Keep test/custom supervisors compatible with the runtime seam.
                    self._create_update_review(result.start_commit, str(result.output))
                    state.promote()
                    cleanup = getattr(state, "cleanup", None)
                    if callable(cleanup):
                        cleanup()
                else:
                    tracked_operation = True
                    self._run_operation_effects(
                        clean_operation_id,
                        state,
                        effect_names={STATE_EFFECT},
                    )
            except Exception:
                record = store.read(clean_operation_id)
                if record is None or record.phase == "running":
                    state.archive_failed_provider_session()
                    cleanup = getattr(state, "cleanup", None)
                    if callable(cleanup):
                        cleanup()
                raise

        if tracked_operation:
            self._run_operation_effects(
                clean_operation_id,
                state,
                exclude_effects={STATE_EFFECT},
            )
            self._retry_pending_operation_effects(exclude=clean_operation_id)
        return result.output

    def _operation_effect_plan(
        self,
        session_id: str,
        changed_paths: tuple[str, ...],
        commits_landed: int,
        *,
        pressure_points: tuple[float, float] | None,
    ) -> tuple[OperationEffect, ...]:
        effects = [OperationEffect(STATE_EFFECT, metadata={"session_id": session_id})]
        effects.append(OperationEffect(PUBLISH_EFFECT))
        if self.config.role == "update" and self.update_mode == "normal" and commits_landed:
            effects.append(OperationEffect(REVIEW_EFFECT))
        if self.config.role == "update" and commits_landed and _changed_memory_paths(changed_paths):
            if pressure_points is None:
                raise RuntimeError("update pressure points were not prepared")
            dreamer_points, insight_points = pressure_points
            effects.append(
                OperationEffect(
                    PRESSURE_EFFECT,
                    metadata={
                        "dreamer_points": dreamer_points,
                        "insight_points": insight_points,
                    },
                )
            )
        if self.config.role == "dreamer" and self._semantic_upgrade_ids:
            effects.append(
                OperationEffect(
                    SEMANTIC_UPGRADE_EFFECT,
                    metadata={"upgrade_ids": list(self._semantic_upgrade_ids)},
                )
            )
        return tuple(effects)

    def _run_operation_effects(
        self,
        operation_id: str,
        state: _IsolatedStateOverlay,
        *,
        effect_names: set[str] | None = None,
        exclude_effects: set[str] | None = None,
    ) -> None:
        store = SemanticOperationStore(self.config.memory_root)
        try:
            with store.effects_locked(operation_id):
                record = store.read(operation_id)
                if record is None:
                    raise FileNotFoundError(f"semantic operation does not exist: {operation_id}")
                if record.phase not in FINAL_PHASES:
                    return
                effects = store.list_pending_effects(operation_id)
                for effect in effects:
                    if effect_names is not None and effect.name not in effect_names:
                        continue
                    if exclude_effects is not None and effect.name in exclude_effects:
                        continue
                    try:
                        self._apply_operation_effect(record, effect, state)
                        store.mark_effect(operation_id, effect.name, "done")
                        if effect.name == PRESSURE_EFFECT:
                            try:
                                forget_memory_change_pressure_operation(
                                    self.config.memory_root,
                                    operation_id,
                                )
                            except Exception:
                                pass
                    except Exception as exc:
                        try:
                            store.mark_effect(
                                operation_id,
                                effect.name,
                                "failed",
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        except Exception:
                            pass
                        self._warn_operation_effect_failure(operation_id, effect.name, exc)
                latest = store.read(operation_id)
                if latest is not None and any(
                    effect.name == PUBLISH_EFFECT and effect.status == "done"
                    for effect in latest.effects
                ):
                    try:
                        store.clear_effect_state(operation_id, PUBLISH_EFFECT)
                    except Exception:
                        pass
        except Exception as exc:
            self._warn_operation_effect_failure(operation_id, "effect-replay", exc)

    def _recover_pending_session_state(self, session_id: str, *, exclude: str) -> None:
        store = SemanticOperationStore(self.config.memory_root)

        def pending_state_records() -> list[SemanticOperationRecord]:
            records = []
            for record in store.list_outstanding_records():
                if (
                    record.operation_id == exclude
                    or record.phase not in FINAL_PHASES
                    or record.input_data.get("role") != self.config.role
                    or record.input_data.get("session_id") != session_id
                    or not any(
                        effect.name == STATE_EFFECT and effect.status != "done"
                        for effect in record.effects
                    )
                ):
                    continue
                records.append(record)
            return records

        records = pending_state_records()
        records.sort(
            key=lambda record: (
                record.outcome.sequence if record.outcome is not None else 0,
                record.operation_id,
            )
        )
        for record in records:
            state = _IsolatedStateOverlay(
                self.config.state_root,
                self.config.role,
                session_id,
                operation_id=record.operation_id,
                seed_provider_session=self.config.runtime_mode != "cli-agent",
            )
            self._run_operation_effects(
                record.operation_id,
                state,
                effect_names={STATE_EFFECT},
            )

        if pending_state_records():
            raise RuntimeError(
                f"previous semantic session state is still pending for {self.config.role}/{session_id}"
            )

    def _warn_operation_effect_failure(self, operation_id: str, effect_name: str, exc: Exception) -> None:
        self._trace(
            "operation_effect_failed",
            operation_id=operation_id,
            effect=effect_name,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        print(
            f"Warning: semantic operation {operation_id} is saved, but effect "
            f"{effect_name} is pending: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

    def _apply_operation_effect(
        self,
        record: SemanticOperationRecord,
        effect: OperationEffect,
        state: _IsolatedStateOverlay,
    ) -> None:
        outcome = record.outcome
        if outcome is None:
            raise RuntimeError(f"completed operation has no outcome: {record.operation_id}")
        if effect.name == STATE_EFFECT:
            state.promote_if_current(record.operation_id, outcome.sequence)
            return
        if effect.name == REVIEW_EFFECT:
            self._create_update_review_for_result(
                _write_result_from_operation(record),
                outcome.output,
                raise_errors=True,
            )
            return
        if effect.name == PRESSURE_EFFECT:
            dreamer_points = effect.metadata.get("dreamer_points")
            insight_points = effect.metadata.get("insight_points")
            if (
                not isinstance(dreamer_points, (int, float))
                or isinstance(dreamer_points, bool)
                or dreamer_points <= 0
                or not isinstance(insight_points, (int, float))
                or isinstance(insight_points, bool)
                or insight_points <= 0
            ):
                raise ValueError("memory-pressure effect requires positive point values")
            record_memory_change_pressure_once(
                self.config.memory_root,
                record.operation_id,
                dreamer_points=float(dreamer_points),
                insight_points=float(insight_points),
            )
            return
        if effect.name == PUBLISH_EFFECT:
            store = SemanticOperationStore(self.config.memory_root)
            with store.publish_locked():
                if not _advance_effect_watermark(
                    self.config.memory_root,
                    "file-view-publish",
                    record.operation_id,
                    outcome.sequence,
                ):
                    return
                self._publish_file_views_after_write(
                    raise_on_failure=True,
                    operation_id=record.operation_id,
                )
            return
        if effect.name == SEMANTIC_UPGRADE_EFFECT:
            upgrade_ids = effect.metadata.get("upgrade_ids")
            if not isinstance(upgrade_ids, list) or not all(isinstance(item, str) for item in upgrade_ids):
                raise ValueError("semantic-upgrades effect requires string upgrade_ids")
            mark_absorbed(self.config.state_root, upgrade_ids)
            self._semantic_upgrade_ids = [item for item in self._semantic_upgrade_ids if item not in upgrade_ids]
            return
        raise ValueError(f"unknown semantic operation effect: {effect.name}")

    def _retry_pending_operation_effects(self, *, exclude: str) -> None:
        store = SemanticOperationStore(self.config.memory_root)
        try:
            candidates = [
                record
                for record in store.list_outstanding_records()
                if record.operation_id != exclude
                and record.phase in FINAL_PHASES
                and record.input_data.get("role") == self.config.role
                and store.list_pending_effects(record.operation_id)
            ]
        except Exception as exc:
            self._warn_operation_effect_failure(exclude, "pending-effect-scan", exc)
            return
        if not candidates:
            return

        # One durable round-robin step keeps retries bounded without starvation.
        try:
            record = store.choose_effect_retry(f"role:{self.config.role}", candidates)
        except Exception as exc:
            self._warn_operation_effect_failure(exclude, "effect-retry-cursor", exc)
            return
        if record is None:
            return
        session_id = record.input_data.get("session_id")
        if not isinstance(session_id, str):
            return
        state = _IsolatedStateOverlay(
            self.config.state_root,
            self.config.role,
            session_id,
            operation_id=record.operation_id,
            seed_provider_session=self.config.runtime_mode != "cli-agent",
        )
        with self.sessions.locked(session_id):
            self._run_operation_effects(
                record.operation_id,
                state,
                effect_names={STATE_EFFECT},
            )
        self._run_operation_effects(
            record.operation_id,
            state,
            exclude_effects={STATE_EFFECT},
        )

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
        nested_kwargs = {"update_mode": self.update_mode} if self.update_mode != "normal" else {}
        nested = RightMemoryRuntime(nested_config, **nested_kwargs)
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
        if self.config.role in {"historian", "retrieve", "reviewer"}:
            return nullcontext()
        return MemoryWriteLock(self.config.memory_root)

    def _prepare_retrieve_turn(
        self,
        session_id: str,
        message: str,
        *,
        cli_agent_phase: str | None = None,
        include_returned: bool = False,
    ) -> PreparedRetrieveTurn:
        if self.config.role != "retrieve":
            return PreparedRetrieveTurn(message, message, [], None)
        if cli_agent_phase not in {None, "new", "resume"}:
            raise ValueError("cli_agent_phase must be new, resume, or None")
        if cli_agent_phase == "new":
            self.retrieve_context.reset(session_id)
            self.recent_submitted_delivery.reset(session_id)
        snapshot = load_daily_snapshot(self.config.memory_root)
        state = self.retrieve_context.load(session_id)
        current_commit = current_memory_head(self.config.memory_root)
        base_commit = state.delivered_memory_commit or snapshot.base_commit
        diff = memory_diff_since(self.config.memory_root, base_commit, current_commit)
        diff_block = format_memory_diff_block(diff)

        entries = collect_recent_submitted_memory(self.config.memory_root)
        if entries and not include_returned:
            entries = self.recent_submitted_delivery.new_entries(session_id, entries)
        recent_block = format_recent_submitted_context_block(entries)
        request = build_retrieve_request_text(
            snapshot_text="" if cli_agent_phase == "resume" else snapshot.text,
            turns=[] if cli_agent_phase is not None else state.turns,
            diff_block=diff_block,
            recent_block=recent_block,
            query=message,
        )
        return PreparedRetrieveTurn(request, message, entries, current_commit)

    def _record_successful_retrieve_turn(
        self,
        session_id: str,
        prepared: PreparedRetrieveTurn,
        rendered: RenderedRetrieveSelection,
    ) -> None:
        if self.config.role != "retrieve":
            return
        self.retrieve_context.record_success(
            session_id,
            query=prepared.query,
            answer=rendered.text,
            memory_commit=prepared.memory_commit,
            delivery=rendered.delivery,
        )
        self._record_recent_submitted_delivery(session_id, rendered.recent_entries)

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

    def _publish_file_views_after_write(
        self,
        *,
        raise_on_failure: bool = False,
        operation_id: str | None = None,
    ) -> None:
        if self.config.role not in AUTOMATIC_WRITE_ROLES:
            return
        if operation_id is None:
            results = publish_approved_file_views(self.config.memory_root)
        else:
            store = SemanticOperationStore(self.config.memory_root)
            record = store.read(operation_id)
            if record is None or record.outcome is None or record.phase not in FINAL_PHASES:
                raise RuntimeError(f"semantic operation has no durable publish outbox: {operation_id}")
            results = publish_file_view_outbox(
                store.effect_state_root(operation_id, PUBLISH_EFFECT),
                operation_id=operation_id,
                credential_root=self.config.memory_root,
            )
        record_file_view_publish_results(self.config.memory_root, results, trigger=f"{self.config.role}-write")
        failures = [result for result in results if getattr(result, "status", None) == "failed"]
        if raise_on_failure and failures:
            detail = "; ".join(f"{result.view_id}: {result.message}" for result in failures)
            raise RuntimeError(f"file-view publish failed: {detail}")

    def _prepare_operation_effects(self, operation_id: str, source_root: Path) -> None:
        store = SemanticOperationStore(self.config.memory_root)
        prepare_file_view_publish_outbox(
            source_root,
            store.effect_state_root(operation_id, PUBLISH_EFFECT),
        )

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

        kwargs: dict[str, Any] = {
            "model": build_model(self.config),
            "instructions": build_instructions(
                self.config.memory_root,
                self.config.role,
                semantic_upgrades=self.semantic_upgrades,
            ),
            "tools": self._agent_tools(),
            "retries": self.config.max_tool_retries,
        }
        if self.config.role == "retrieve":
            kwargs["output_type"] = RetrieveSelection
        return Agent(**kwargs)

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
                self._agent_tool(self.tools.read_detail),
                self._agent_tool(self.tools.read_markdown),
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
        if self.config.role in {"historian", "reviewer"}:
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

    def _update_execution_lock(self):
        if self.config.role == "update":
            return UpdateExecutionLock(self.config.memory_root)
        return nullcontext()

    def _review_base_commit(self) -> str | None:
        if self.config.role != "update" or self.update_mode != "normal":
            return None
        return current_memory_head(self.config.memory_root)

    def _create_update_review(
        self,
        base_commit: str | None,
        summary: str,
        *,
        raise_errors: bool = False,
    ) -> None:
        if self.config.role != "update" or self.update_mode != "normal":
            return
        landed = self._last_write_result
        if landed is not None:
            self._create_update_review_for_result(landed, summary, raise_errors=raise_errors)
            return
        update_commit = current_memory_head(self.config.memory_root)
        if base_commit is None:
            return
        self._write_update_review(
            base_commit,
            update_commit,
            "Memory + Pursuit",
            summary,
            raise_errors=raise_errors,
        )

    def _create_update_review_for_result(
        self,
        landed: IsolatedWriteResult,
        summary: str,
        *,
        raise_errors: bool = False,
    ) -> None:
        if landed.commits_landed == 0:
            return
        self._write_update_review(
            landed.start_commit,
            landed.landed_commit,
            _rightmemory_write_surface(landed.changed_paths),
            summary,
            raise_errors=raise_errors,
        )

    def _write_update_review(
        self,
        base_commit: str,
        update_commit: str | None,
        write_surface: str,
        summary: str,
        *,
        raise_errors: bool,
    ) -> None:
        if update_commit is None or update_commit == base_commit:
            return
        if update_commit == self._last_reviewed_update_commit:
            return
        try:
            diff = _git_rightmemory_diff(self.config.memory_root, base_commit, update_commit)
            store = UpdateReviewStore(self.config.memory_root)
            store.queue_review(
                base_commit=base_commit,
                update_commit=update_commit,
                write_surface=write_surface,
                summary=summary,
                diff=diff,
            )
            store.create_review(
                base_commit=base_commit,
                update_commit=update_commit,
                write_surface=write_surface,
                summary=summary,
                diff=diff,
            )
            self._last_reviewed_update_commit = update_commit
        except Exception as exc:
            self._trace("update_review_failed", error_type=type(exc).__name__, error=str(exc))
            if raise_errors:
                raise
            print(
                f"Warning: update commit {update_commit} landed, but its local review document could not be created: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    @property
    def last_write_result(self) -> IsolatedWriteResult | None:
        return self._last_write_result

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
    def __init__(
        self,
        state_root: Path,
        role: str,
        session_id: str,
        *,
        operation_id: str | None = None,
        seed_provider_session: bool = True,
    ):
        self.state_root = state_root
        self.role = role
        self.session_id = session_id
        self.seed_provider_session = seed_provider_session
        if operation_id is None:
            self.overlay_root = state_root / ".runtime" / "isolated-state" / f"{role}-{uuid.uuid4().hex}"
        else:
            self.overlay_root = SemanticOperationStore(state_root).state_root(operation_id)

    def __enter__(self) -> Path:
        _ensure_runtime_gitignore(self.state_root / ".runtime")
        shutil.rmtree(self.overlay_root, ignore_errors=True)
        self._seed()
        return self.overlay_root

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def cleanup(self) -> None:
        shutil.rmtree(self.overlay_root, ignore_errors=True)

    def promote_if_current(self, operation_id: str, sequence: int) -> bool:
        history = MessageSessionStore(self.state_root, self.role).paths(self.session_id).history
        watermark = (
            self.state_root
            / ".runtime"
            / "operations"
            / "session-state"
            / self.role
            / f"{history.stem}.json"
        )
        current_key: tuple[int, str] | None = None
        if watermark.exists():
            data = json.loads(watermark.read_text(encoding="utf-8"))
            current_id = data.get("operation_id") if isinstance(data, dict) else None
            current_sequence = data.get("sequence") if isinstance(data, dict) else None
            if not isinstance(current_id, str) or type(current_sequence) is not int or current_sequence < 1:
                raise ValueError(f"invalid semantic session-state watermark: {watermark}")
            current_key = (current_sequence, current_id)
        if current_key is not None and current_key > (sequence, operation_id):
            self.cleanup()
            return False

        # Write the ordering fence first. A crash then safely retries the same promotion.
        _write_state_json_file(
            {"operation_id": operation_id, "sequence": sequence},
            watermark,
        )
        self.promote()
        self.cleanup()
        return True

    def promote(self) -> None:
        _ensure_runtime_gitignore(self.state_root / ".runtime")
        for relative_path in [*self._promoted_paths(), *self._provider_thread_paths()]:
            _copy_state_file(self.overlay_root / relative_path, self.state_root / relative_path)

    def archive_failed_provider_session(self) -> None:
        if self.seed_provider_session:
            return
        for relative_path in self._provider_thread_paths():
            _copy_state_file(self.overlay_root / relative_path, self.state_root / relative_path)
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

    def _provider_thread_paths(self) -> list[Path]:
        root = ProviderThreadStore(self.overlay_root).root
        if not root.exists():
            return []
        return [path.relative_to(self.overlay_root) for path in sorted(root.glob("*/*.json"))]


def _advance_effect_watermark(
    state_root: Path,
    effect_name: str,
    operation_id: str,
    sequence: int,
) -> bool:
    path = state_root / ".runtime" / "operations" / "effects" / f"{effect_name}.json"
    current_key: tuple[int, str] | None = None
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        current_id = data.get("operation_id") if isinstance(data, dict) else None
        current_sequence = data.get("sequence") if isinstance(data, dict) else None
        if not isinstance(current_id, str) or type(current_sequence) is not int or current_sequence < 1:
            raise ValueError(f"invalid semantic effect watermark: {path}")
        current_key = (current_sequence, current_id)
    if current_key is not None and current_key > (sequence, operation_id):
        return False
    _write_state_json_file(
        {"operation_id": operation_id, "sequence": sequence},
        path,
    )
    return True


def _copy_state_file(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if not source.is_file():
        raise RuntimeError(f"runtime state path is not a file: {source}")
    _ensure_durable_directory(destination.parent)
    tmp_path = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, tmp_path)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_directory(destination.parent)


def _write_state_json_file(data: dict[str, Any], destination: Path) -> None:
    _ensure_durable_directory(destination.parent)
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


def _git_rightmemory_diff(memory_root: Path, base_commit: str, update_commit: str) -> str:
    result = subprocess.run(
        [
            "git",
            "diff",
            base_commit,
            update_commit,
            "--",
            "MEMORY.md",
            ":(glob)MEMORY_*.md",
            "PURSUITS.md",
            ":(glob)PURSUIT_*.md",
        ],
        cwd=memory_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout.rstrip()


def _rightmemory_write_surface(paths: tuple[str, ...]) -> str:
    changed_memory = any(path == "MEMORY.md" or path.startswith("MEMORY_") for path in paths)
    changed_pursuit = any(path == "PURSUITS.md" or path.startswith("PURSUIT_") for path in paths)
    if changed_memory and changed_pursuit:
        return "Memory + Pursuit"
    if changed_memory:
        return "Memory"
    if changed_pursuit:
        return "Pursuit"
    return "RightMemory"


def _changed_memory_paths(paths: tuple[str, ...]) -> bool:
    return any(path == "MEMORY.md" or (path.startswith("MEMORY_") and path.endswith(".md")) for path in paths)


def _write_result_from_operation(record: SemanticOperationRecord) -> IsolatedWriteResult:
    outcome = record.outcome
    if outcome is None:
        raise RuntimeError(f"completed operation has no outcome: {record.operation_id}")
    return IsolatedWriteResult(
        output=outcome.output,
        commits_landed=1 if record.phase == "committed" else 0,
        start_commit=outcome.start_commit,
        landed_commit=outcome.landed_commit or outcome.start_commit,
        changed_paths=outcome.changed_paths,
        operation_id=record.operation_id,
    )


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


def _retrieve_retry_request(original: str, error: str) -> str:
    return (
        f"{original.rstrip()}\n\n"
        "# Retrieve selection validation error\n\n"
        f"{error}\n\n"
        "Inspect more source context if needed, then submit one replacement selection. "
        "Do not add prose or reuse the invalid selection.\n"
    )


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
