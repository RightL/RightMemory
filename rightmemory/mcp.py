from __future__ import annotations

import argparse
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .async_update import AsyncUpdateStore
from .config import load_config
from .codex_sdk import CodexSdkRunner
from .debug import DebugTrace
from .learning_mcp import register_learning_tools
from .runtime import RightMemoryRuntime
from .update_alerts import collect_update_recovery_summary
from .update_queue import UpdateQueueStore


SESSION_ID_DESCRIPTION = (
    "A stable identifier chosen once for the current conversation and reused for every "
    "RightMemory call in that conversation."
)
RETRIEVE_NEED_DESCRIPTION = (
    "A concise description of the cross-session context needed for the current work, "
    "not a verbatim copy of the user's message."
)
UPDATE_EVIDENCE_DESCRIPTION = (
    "A self-contained account of what happened, what is currently true, and why "
    "preserving it may matter in future work."
)

RETRIEVE_DESCRIPTION = """\
Retrieve cross-session context when it could materially change how the current work is
understood or approached. Ask for the context needed rather than forwarding the user's
message verbatim.

Use relevant returned context in the work, but treat the current conversation and current
evidence as authoritative. Skip clearly self-contained requests for which stored context
is unlikely to matter.

Act on returned learning-ready notices with rightmemory_run_learning, preserving the
notice's domain, mode, and scope. Continue the user's work while curation runs when
background execution is available; inspect an active task before starting another.

If returned context is stale, wrong, misleading, or overbroad, do not follow it; submit
the correction and current evidence through the relevant path: rightmemory_submit_update
for ordinary Memory, or rightmemory_submit_learning for behavioral guidance and reusable work."""

SUBMIT_UPDATE_DESCRIPTION = """\
Submit durable ordinary Memory evidence when omitting it would likely cause
poorer future decisions, substantial rediscovery, or loss of useful context.
Include concrete findings of missing or ambiguous context that meet this bar, even
when the answer is not yet known. Behavioral guidance, corrections about how the agent
works, and reusable work experience go through rightmemory_submit_learning.

Pursuit is read-only to Update. Explicit map edits belong in the human editor or the
maintain-pursuit-map workflow, not in an Update submission.

Submit at a natural boundary once the evidence is clear; completion is not required. Do
not submit transient progress, routine results, unresolved discussion by itself, or
implementation detail already adequately preserved in project-local artifacts. Combine
related evidence due at the same boundary.

State what happened, what is true now, and why it may matter. Do not prescribe stored
wording, identifiers, classification, placement, or edits.

Processing is asynchronous. After an empty successful result, continue the task without
waiting, polling, or resubmitting. Only actionable failures or recovery warnings are
returned."""

SessionId = Annotated[str, Field(description=SESSION_ID_DESCRIPTION, min_length=1)]
RetrieveNeed = Annotated[str, Field(description=RETRIEVE_NEED_DESCRIPTION, min_length=1)]
UpdateEvidence = Annotated[str, Field(description=UPDATE_EVIDENCE_DESCRIPTION, min_length=1)]

_MAX_ERROR_DETAIL_CHARS = 400


class McpBackend(Protocol):
    def retrieve(self, session_id: str, need: str) -> str: ...

    def submit_update(self, session_id: str, evidence: str) -> str | None: ...

    def actionable_warning(self) -> str | None: ...


@dataclass(frozen=True)
class DefaultMcpBackend:
    memory_root: Path
    codex_runner: CodexSdkRunner = field(default_factory=CodexSdkRunner)

    def retrieve(self, session_id: str, need: str) -> str:
        return self._retrieve(session_id, need)

    def _retrieve(
        self,
        session_id: str,
        need: str,
        *,
        timing: _McpRetrieveTiming | None = None,
    ) -> str:
        config_started = time.perf_counter()
        try:
            config = load_config("retrieve", memory_root=self.memory_root)
        finally:
            if timing is not None:
                timing.config_load_ms = _elapsed_ms(config_started)
        if timing is not None and config.debug_trace:
            timing.trace = DebugTrace(config.state_root, config.role, session_id)

        runtime_started = time.perf_counter()
        try:
            runtime = RightMemoryRuntime(config, codex_runner=self.codex_runner)
        finally:
            if timing is not None:
                timing.runtime_construction_ms = _elapsed_ms(runtime_started)
        try:
            turn_started = time.perf_counter()
            try:
                return runtime.run_session_turn(session_id, need)
            finally:
                if timing is not None:
                    timing.run_session_turn_ms = _elapsed_ms(turn_started)
        finally:
            cleanup_started = time.perf_counter()
            try:
                runtime.cleanup()
            finally:
                if timing is not None:
                    timing.runtime_cleanup_ms = _elapsed_ms(cleanup_started)

    def submit_update(self, session_id: str, evidence: str) -> str | None:
        store = AsyncUpdateStore(self.memory_root, "update")
        candidate_uid = uuid.uuid4().hex
        try:
            store.submit(
                session_id,
                evidence,
                candidate_uid=candidate_uid,
            )
        except Exception as exc:
            if not self._candidate_was_saved(store, session_id, candidate_uid):
                raise
            return (
                "RightMemory saved the update evidence, but could not start or wake its "
                f"update worker: {_error_detail(exc)}. Tell the user to run "
                "`rightmemory status`; do not resubmit the evidence."
            )
        return None

    def _candidate_was_saved(
        self,
        store: AsyncUpdateStore,
        session_id: str,
        candidate_uid: str,
    ) -> bool:
        try:
            state = store.read(session_id)
        except Exception:
            state = None
        if state is not None and candidate_uid in state.accepted_candidate_uids:
            return True
        try:
            return UpdateQueueStore(self.memory_root).read_outbox(candidate_uid) is not None
        except Exception:
            return False

    def actionable_warning(self) -> str | None:
        return _actionable_update_warning(self.memory_root)

    def close(self) -> None:
        self.codex_runner.close()


@dataclass
class _McpRetrieveTiming:
    backend_entry_wall_timestamp: str
    total_started: float
    trace: DebugTrace | None = None
    config_load_ms: float = 0.0
    runtime_construction_ms: float = 0.0
    run_session_turn_ms: float = 0.0
    runtime_cleanup_ms: float = 0.0
    actionable_warning_ms: float = 0.0
    result_construction_ms: float = 0.0

    def emit(self, *, outcome: str, error_type: str | None) -> None:
        if self.trace is None:
            return
        self.trace.append(
            "mcp_timing",
            backend_entry_wall_timestamp=self.backend_entry_wall_timestamp,
            config_load_ms=self.config_load_ms,
            runtime_construction_ms=self.runtime_construction_ms,
            run_session_turn_ms=self.run_session_turn_ms,
            runtime_cleanup_ms=self.runtime_cleanup_ms,
            actionable_warning_ms=self.actionable_warning_ms,
            result_construction_ms=self.result_construction_ms,
            total_ms=_elapsed_ms(self.total_started),
            outcome=outcome,
            error_type=error_type,
        )


def create_mcp_server(
    memory_root: Path,
    *,
    backend: McpBackend | None = None,
) -> MCPServer:
    if backend is None:
        selected_backend = DefaultMcpBackend(
            Path(memory_root).expanduser().resolve()
        )

        @asynccontextmanager
        async def lifespan(_server: MCPServer):
            try:
                yield {}
            finally:
                selected_backend.close()

        server = MCPServer(
            "RightMemory",
            log_level="WARNING",
            lifespan=lifespan,
        )
    else:
        selected_backend = backend
        server = MCPServer("RightMemory", log_level="WARNING")

    @server.tool(
        name="rightmemory_retrieve",
        description=RETRIEVE_DESCRIPTION,
        structured_output=False,
    )
    def rightmemory_retrieve(
        session_id: SessionId,
        need: RetrieveNeed,
    ) -> CallToolResult:
        clean_session = _clean_session_id(session_id)
        clean_need = _clean_text(need, "retrieval need")
        if isinstance(selected_backend, DefaultMcpBackend):
            return _timed_default_retrieve_result(
                selected_backend,
                clean_session,
                clean_need,
            )
        output = selected_backend.retrieve(clean_session, clean_need)
        return _result(output, selected_backend.actionable_warning())

    @server.tool(
        name="rightmemory_submit_update",
        description=SUBMIT_UPDATE_DESCRIPTION,
        structured_output=False,
    )
    def rightmemory_submit_update(
        session_id: SessionId,
        evidence: UpdateEvidence,
    ) -> CallToolResult:
        clean_session = _clean_session_id(session_id)
        clean_evidence = _clean_text(evidence, "update evidence")
        warning = selected_backend.submit_update(clean_session, clean_evidence)
        if warning is None:
            warning = selected_backend.actionable_warning()
        return _result(warning)

    register_learning_tools(server, memory_root, native=False)

    return server


def mcp_main(memory_root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rightmemory mcp",
        description="Serve RightMemory ordinary-agent tools over local MCP stdio.",
    )
    parser.parse_args([] if argv is None else argv)
    backend = DefaultMcpBackend(Path(memory_root).expanduser().resolve())
    try:
        create_mcp_server(memory_root, backend=backend).run(transport="stdio")
    finally:
        backend.close()
    return 0


def _actionable_update_warning(memory_root: Path) -> str | None:
    try:
        return collect_update_recovery_summary(Path(memory_root)).warning()
    except Exception as exc:
        return (
            "RightMemory could not inspect update recovery state: "
            f"{_error_detail(exc)}. Tell the user to run `rightmemory status`."
        )


def _learning_notice(memory_root: Path) -> str | None:
    from .learning_service import LearningService

    try:
        notices = LearningService(memory_root).notices()
    except (OSError, ValueError, RuntimeError) as exc:
        return f"RightMemory could not inspect learning readiness: {_error_detail(exc)}"
    if any(item["status"] != "waiting" for item in notices):
        return json.dumps({"learning": notices}, ensure_ascii=False)
    return None


def _timed_default_retrieve_result(
    backend: DefaultMcpBackend,
    session_id: str,
    need: str,
) -> CallToolResult:
    timing = _McpRetrieveTiming(
        backend_entry_wall_timestamp=datetime.now(UTC).isoformat(),
        total_started=time.perf_counter(),
    )
    outcome = "failure"
    error_type: str | None = None
    try:
        output = backend._retrieve(session_id, need, timing=timing)

        warning_started = time.perf_counter()
        try:
            warning = backend.actionable_warning()
        finally:
            timing.actionable_warning_ms = _elapsed_ms(warning_started)

        result_started = time.perf_counter()
        try:
            result = _result(output, warning, _learning_notice(backend.memory_root))
        finally:
            timing.result_construction_ms = _elapsed_ms(result_started)
        outcome = "success"
        return result
    except BaseException as exc:
        error_type = type(exc).__name__
        raise
    finally:
        timing.emit(outcome=outcome, error_type=error_type)


def _result(*texts: str | None) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=text)
            for text in texts
            if isinstance(text, str) and text.strip()
        ]
    )


def _clean_session_id(value: str) -> str:
    clean = value.strip()
    if not clean or any(character in clean for character in "\x00\r\n"):
        raise ValueError("session id must be a non-empty single line")
    return clean


def _clean_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} must not be empty")
    return clean


def _error_detail(exc: Exception) -> str:
    raw = str(exc).strip()
    detail = raw.splitlines()[0] if raw else type(exc).__name__
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS] + "...[truncated]"
    return f"{type(exc).__name__}: {detail}"


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)
