from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent


class LearningBackend(Protocol):
    def submit(self, session_id: str, evidence: str, request_id: str | None = None) -> dict[str, Any]: ...
    def begin(self, domain: str, *, mode: str = "update", scope: str = "", retry_task: str | None = None) -> dict[str, Any]: ...
    def complete(self, task_id: str, output: str = "") -> dict[str, Any]: ...
    def status(self, task_id: str | None = None) -> dict[str, Any]: ...
    def run(self, domain: str, *, mode: str = "update", scope: str = "") -> dict[str, Any]: ...


def register_learning_tools(
    server: MCPServer, memory_root: Path, *, native: bool, backend: LearningBackend | None = None,
) -> None:
    """Install the common learning control surface without loading a model."""
    ready_guidance = (
        "Relay ready notices to the main assistant, which uses begin and native delegation through the assistant skill. "
        "Resume an existing task after confirming its previous worker stopped."
        if native else
        "Act on returned ready learning notices with rightmemory_run_learning for that domain, preserving the notice's "
        "mode and scope. Follow completion notices for subsequent ready batches."
    )
    def selected() -> LearningBackend:
        if backend is not None:
            return backend
        from .learning_service import LearningService

        return LearningService(memory_root)

    @server.tool(
        name="rightmemory_submit_learning",
        description=(
            "Capture concrete evidence about user redirections, habits, preferences, or work experience outside ordinary "
            "Memory when it could improve future understanding, decisions, collaboration, or repeated work. Preserve the context "
            "and uncertainty needed to interpret it. A useful candidate need not already support a stable conclusion; "
            "the domain curators use their shared cores during Update to judge adoption and generalization. "
            "For work evidence, consider whether the situation or a submethod may recur and whether the evidence "
            "would help a later agent reproduce a meaningful result from a short prompt. Preserve the method, human "
            "contributions, outcome, and relevant resources. Unsettled or one-off evidence can still be useful; capture "
            "it without inventing a general rule or declaring an unproven method reliable. Leave out routine progress "
            "and copies of project artifacts that add no independent future value. "
            "Reuse request_id and identical evidence when retrying; distinct "
            "later occurrences receive new request IDs. Returns independent guidance and skills curator readiness. "
            + ready_guidance
        ),
        structured_output=False,
    )
    def submit(session_id: str, evidence: str, request_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().submit(session_id, evidence, request_id))

    @server.tool(name="rightmemory_begin_learning", description="Prepare one isolated guidance or skills curation task with shared update or maintain instructions.", structured_output=False)
    def begin(domain: Literal["guidance", "skills"], mode: Literal["update", "maintain"] = "update", scope: str = "", retry_task: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().begin(domain, mode=mode, scope=scope, retry_task=retry_task))

    @server.tool(name="rightmemory_complete_learning", description="Validate and publish a prepared curator task and record processing; retry the same task after interruption. " + ready_guidance, structured_output=False)
    def complete(task_id: str, output: str = "") -> CallToolResult:
        return _invoke(lambda: selected().complete(task_id, output))

    @server.tool(name="rightmemory_learning_status", description="Inspect learning readiness or a curator task without starting model work. " + ready_guidance, structured_output=False)
    def status(task_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().status(task_id))

    if not native:
        @server.tool(name="rightmemory_run_learning", description="Run a ready or resumable curator task through the configured executor and finalize its isolated output. " + ready_guidance, structured_output=False)
        def run(domain: Literal["guidance", "skills"], mode: Literal["update", "maintain"] = "update", scope: str = "") -> CallToolResult:
            return _invoke(lambda: selected().run(domain, mode=mode, scope=scope))


def _invoke(operation: Callable[[], dict[str, Any]]) -> CallToolResult:
    try:
        result = operation()
    except (ValueError, OSError, RuntimeError) as exc:
        result = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structured_content=result, is_error=result.get("ok") is False,
    )
