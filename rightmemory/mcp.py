from __future__ import annotations

import argparse
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .config import load_config
from .codex_sdk import CodexSdkRunner
from .debug import DebugTrace
from .curation_mcp import CurationBackend, register_curation_tools
from .runtime import RightMemoryRuntime


SESSION_ID_DESCRIPTION = (
    "A stable identifier chosen once for the current conversation and reused for every "
    "RightMemory call in that conversation."
)
RETRIEVE_NEED_DESCRIPTION = (
    "A concise description of the cross-session context needed for the current work, "
    "not a verbatim copy of the user's message."
)
RETRIEVE_DESCRIPTION = """\
Retrieve cross-session context when it could materially change how the current work is
understood or approached. Ask for the context needed rather than forwarding the user's
message verbatim.

Use relevant returned context in the work, but treat the current conversation and current
evidence as authoritative. Skip clearly self-contained requests for which stored context
is unlikely to matter.

Candidate submission starts the configured curation worker. Inspect returned curation
notices when recovery is needed; continue the user's work while it runs.

If returned context is stale, wrong, misleading, or overbroad, do not follow it; submit
the correction and current evidence through rightmemory_submit."""

SessionId = Annotated[str, Field(description=SESSION_ID_DESCRIPTION, min_length=1)]
RetrieveNeed = Annotated[str, Field(description=RETRIEVE_NEED_DESCRIPTION, min_length=1)]

_MAX_ERROR_DETAIL_CHARS = 400


class McpBackend(Protocol):
    def retrieve(self, session_id: str, need: str) -> str: ...

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

    def actionable_warning(self) -> str | None:
        return _actionable_curation_warning(self.memory_root)

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
    curation_backend: CurationBackend | None = None,
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

    register_curation_tools(server, memory_root, native=False, backend=curation_backend)

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


def _actionable_curation_warning(memory_root: Path) -> str | None:
    from .curation_worker import CurationWorker
    try:
        state = CurationWorker(memory_root).status()
        if state.get("error"):
            return f"RightMemory curation needs recovery: {state['error']}. Inspect `rightmemory curate status`."
    except (OSError, ValueError, RuntimeError) as exc:
        return f"RightMemory could not inspect curation state: {_error_detail(exc)}"
    return None


def _curation_notice(memory_root: Path) -> str | None:
    from .learning_service import LearningService

    try:
        notices = LearningService(memory_root).notices()
    except (OSError, ValueError, RuntimeError) as exc:
        return f"RightMemory could not inspect curation readiness: {_error_detail(exc)}"
    if any(item["status"] != "waiting" for item in notices):
        return json.dumps({"curation": notices}, ensure_ascii=False)
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
            result = _result(output, warning, _curation_notice(backend.memory_root))
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
