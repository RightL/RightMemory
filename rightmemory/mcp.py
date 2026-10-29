from __future__ import annotations

import argparse
import shlex
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
from .curation_mcp import SUBMIT_DESCRIPTION
from .debug import DebugTrace
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

If returned context is stale, wrong, misleading, or overbroad, do not follow it; submit
the correction and current evidence through rightmemory_submit."""

SessionId = Annotated[str, Field(description=SESSION_ID_DESCRIPTION, min_length=1)]
RetrieveNeed = Annotated[str, Field(description=RETRIEVE_NEED_DESCRIPTION, min_length=1)]

_MAX_ERROR_DETAIL_CHARS = 400


class McpBackend(Protocol):
    def retrieve(self, session_id: str, need: str) -> str: ...

    def submit(self, session_id: str, evidence: str, request_id: str | None = None) -> str | None: ...

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

    def submit(self, session_id: str, evidence: str, request_id: str | None = None) -> str | None:
        from .learning_service import LearningService

        result = LearningService(self.memory_root).submit(
            session_id, evidence, request_id, executor="configured",
        )
        warnings = []
        sync = result["sync"]
        if sync["status"] not in {"pushed", "disabled"}:
            detail = _brief_detail(sync.get("error") or sync.get("message") or sync["status"])
            warnings.append(f"Synchronization did not finish: {detail}. Inspect `rightmemory status`.")
        worker = result["worker"]
        if worker["status"] not in {"started", "running"}:
            detail = _brief_detail(worker.get("error") or worker.get("reason") or worker["status"])
            warnings.append(
                f"The curation worker did not start: {detail}. "
                f"Inspect {_worker_status_command(self.memory_root)}."
            )
        curation = result.get("curation")
        if isinstance(curation, dict) and curation.get("error"):
            warnings.append(f"Curation status could not be inspected: {_brief_detail(curation['error'])}.")
        if result["status"] == "withdrawn":
            receipt = "This submission was previously withdrawn; this retry did not queue it."
        elif warnings:
            receipt = "RightMemory saved the evidence."
        else:
            return None
        return " ".join([receipt, *warnings, "Do not resubmit the evidence."])

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
        name="rightmemory_submit",
        description=SUBMIT_DESCRIPTION + (
            "\n\nProcessing runs in the background. An empty successful result confirms capture; "
            "continue without waiting or polling. Only actionable failures or recovery warnings are returned."
        ),
        structured_output=False,
    )
    def rightmemory_submit(
        session_id: SessionId, evidence: str, request_id: str | None = None,
    ) -> CallToolResult:
        warning = selected_backend.submit(_clean_session_id(session_id), evidence, request_id)
        recovery = selected_backend.actionable_warning()
        if recovery:
            recovery = f"The submission was recorded; do not resubmit it. {recovery}"
        return _result(warning, recovery)

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
            return (
                f"RightMemory curation reported a failure: {_brief_detail(state['error'])}. "
                f"Inspect {_worker_status_command(memory_root)}."
            )
    except (OSError, ValueError, RuntimeError) as exc:
        return f"RightMemory could not inspect curation state: {_error_detail(exc)}"
    return None


def _worker_status_command(memory_root: Path) -> str:
    command = shlex.join(["rightmemory", "curate", "--root", str(memory_root), "worker", "status"])
    return f"`{command}`"


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
            result = _result(output, warning)
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
    return f"{type(exc).__name__}: {_brief_detail(raw or type(exc).__name__)}"


def _brief_detail(value: str) -> str:
    detail = value.strip().splitlines()[0] if value.strip() else "unknown error"
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS] + "...[truncated]"
    return detail


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)
