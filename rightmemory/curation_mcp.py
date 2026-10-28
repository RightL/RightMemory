from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent


class CurationBackend(Protocol):
    def submit(self, session_id: str, evidence: str, request_id: str | None = None, *, executor: str = "native") -> dict[str, Any]: ...
    def begin(self, domain: str, *, mode: str = "update", scope: str = "", retry_task: str | None = None) -> dict[str, Any]: ...
    def complete(self, task_id: str, output: str = "") -> dict[str, Any]: ...
    def status(self, task_id: str | None = None) -> dict[str, Any]: ...
    def run(self, domain: str, *, mode: str = "update", scope: str = "") -> dict[str, Any]: ...
    def withdraw(self, candidate_id: str) -> dict[str, Any]: ...


SUBMIT_DESCRIPTION = """Capture concrete evidence that could improve future understanding,
decisions, collaboration, or repeated work. Preserve the context and uncertainty needed
to interpret it. A useful candidate need not already support a stable conclusion. The
Memory, guidance, and skills curators judge it against their own domain cores; submission
does not write a rule, a skill, or a Memory graph entry.

For work evidence, consider whether the situation or a submethod may recur and whether
the evidence would help a later agent reproduce a meaningful result from a short prompt.
Preserve the method, human contributions, outcome, and relevant resources. One-off or
unsettled evidence can still be useful without becoming a general rule. Leave out routine
progress and copies of project artifacts with no independent future value.

Combine related evidence at a natural boundary. Reuse request_id and identical evidence
when retrying the same submission; distinct later occurrences receive new request IDs.
Pending evidence remains available to retrieval. After the three curators finish, the
program removes its raw source; curators explicitly preserve useful cases or experiences.
Pursuit edits use the separate maintain-pursuit-map workflow."""


def register_curation_tools(
    server: MCPServer, memory_root: Path, *, native: bool, backend: CurationBackend | None = None,
) -> None:
    ready_guidance = (
        "Relay ready curation notices to the main assistant for native delegation through the assistant skill. "
        "Inspect an active task before resuming or retrying it."
        if native else
        "Submission wakes the configured worker. Continue the user's work; inspect curation status if recovery is needed."
    )

    def selected() -> CurationBackend:
        if backend is not None:
            return backend
        from .learning_service import LearningService
        return LearningService(memory_root)

    @server.tool(name="rightmemory_submit", description=SUBMIT_DESCRIPTION + "\n\n" + ready_guidance, structured_output=False)
    def submit(session_id: str, evidence: str, request_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().submit(session_id, evidence, request_id, executor="native" if native else "configured"))

    @server.tool(name="rightmemory_begin_curation", description="Prepare an isolated domain task. Update processes pending evidence; Maintain improves existing state in a selected scope. Retry only after the previous worker has stopped.", structured_output=False)
    def begin(domain: Literal["memory", "guidance", "skills"], mode: Literal["update", "maintain"] = "update", scope: str = "", retry_task: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().begin(domain, mode=mode, scope=scope, retry_task=retry_task))

    @server.tool(name="rightmemory_complete_curation", description="Validate and publish a prepared task; the program records domain completion and retires fully processed candidates. Retry the same task after interruption. " + ready_guidance, structured_output=False)
    def complete(task_id: str, output: str = "") -> CallToolResult:
        return _invoke(lambda: selected().complete(task_id, output))

    @server.tool(name="rightmemory_curation_status", description="Inspect pending candidates, domain readiness, or a task without starting model work. " + ready_guidance, structured_output=False)
    def status(task_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().status(task_id))

    @server.tool(name="rightmemory_withdraw_candidate", description="Withdraw a pending candidate by identity. This prevents further processing; it does not undo already published domain conclusions.", structured_output=False)
    def withdraw(candidate_id: str) -> CallToolResult:
        return _invoke(lambda: selected().withdraw(candidate_id))

    if not native:
        @server.tool(name="rightmemory_run_curation", description="Run a ready or resumable domain task with the configured executor. Use mode=maintain with a scope to maintain existing state.", structured_output=False)
        def run(domain: Literal["memory", "guidance", "skills"], mode: Literal["update", "maintain"] = "update", scope: str = "") -> CallToolResult:
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
