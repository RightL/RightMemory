from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent


class CurationBackend(Protocol):
    def submit(self, session_id: str, evidence: str, request_id: str | None = None, *, executor: str = "native") -> dict[str, Any]: ...
    def begin(self, domain: str = "all", *, mode: str = "update", scope: str = "", retry_task: str | None = None) -> dict[str, Any]: ...
    def complete(self, task_id: str, output: str = "") -> dict[str, Any]: ...
    def begin_child(self, task_id: str, domain: str, *, instructions: str = "", retry: bool = False) -> dict[str, Any]: ...
    def complete_child(self, task_id: str, domain: str, attempt: str, *, output: str = "") -> dict[str, Any]: ...
    def status(self, task_id: str | None = None) -> dict[str, Any]: ...
    def count(self, domain: str, task_id: str | None = None) -> dict[str, Any]: ...
    def withdraw(self, candidate_id: str) -> dict[str, Any]: ...


SUBMIT_DESCRIPTION = """Capture concrete evidence that could improve future understanding,
decisions, collaboration, or repeated work. Preserve the context and uncertainty needed
to interpret it. A useful candidate need not already support a stable conclusion. The
Memory, guidance, and skills curators judge it against their own domain cores; submission
does not write a rule, a skill, or a Memory graph entry.

Make clear what useful difference the evidence could make to future work beyond
what the request and available project context already provide.

For work evidence, consider what the user was trying to accomplish and how the experience
could let a future agent repeat meaningful work with much less help from the user. Preserve the
method, human contributions, outcome, and relevant resources that carry that value beyond
what you could work out, do, or find with reasonable thought and effort, given a short
request and available project context.
One-off or unsettled evidence can still be useful without becoming a general rule. Leave
out routine progress and copies of project artifacts with no independent future value.

Combine related evidence at a natural boundary, keeping attempts, corrections, and
outcomes together when their relationship explains what a future agent could learn.
Reuse request_id and identical evidence
when retrying the same submission; distinct later occurrences receive new request IDs.
Pending evidence remains available to retrieval until the program publishes the reviewed
result and retires the processed candidates."""


def register_curation_tools(
    server: MCPServer, memory_root: Path, *, backend: CurationBackend | None = None,
) -> None:
    ready_guidance = (
        "Relay ready curation notices to the main assistant for native delegation through the assistant skill. "
        "Inspect an active task before resuming or retrying it."
    )

    def selected() -> CurationBackend:
        if backend is not None:
            return backend
        from .learning_service import LearningService
        return LearningService(memory_root)

    @server.tool(name="rightmemory_submit", description=SUBMIT_DESCRIPTION + "\n\n" + ready_guidance, structured_output=False)
    def submit(session_id: str, evidence: str, request_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().submit(session_id, evidence, request_id, executor="native"))

    @server.tool(name="rightmemory_begin_curation", description="Prepare a parent curation task. Update processes pending evidence across all domains. Maintain improves existing state in a selected scope. Retry after the prior task has stopped.", structured_output=False)
    def begin(domain: Literal["all", "memory", "guidance", "skills"] = "all", mode: Literal["update", "maintain"] = "update", scope: str = "", retry_task: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().begin(domain, mode=mode, scope=scope, retry_task=retry_task))

    @server.tool(name="rightmemory_complete_curation", description="Validate and publish a prepared task; the program records domain completion and retires fully processed candidates. Retry the same task after interruption. " + ready_guidance, structured_output=False)
    def complete(task_id: str, output: str = "") -> CallToolResult:
        return _invoke(lambda: selected().complete(task_id, output))

    @server.tool(name="rightmemory_begin_curation_worker", description="Prepare a domain worker in the parent task's worktree. Retry after the previous attempt has stopped.", structured_output=False)
    def begin_child(task_id: str, domain: Literal["memory", "guidance", "skills"], instructions: str = "", retry: bool = False) -> CallToolResult:
        return _invoke(lambda: selected().begin_child(task_id, domain, instructions=instructions, retry=retry))

    @server.tool(name="rightmemory_complete_curation_worker", description="Record a domain worker's result for parent review. Use the task ID, domain, and attempt returned by worker preparation.", structured_output=False)
    def complete_child(task_id: str, domain: Literal["memory", "guidance", "skills"], attempt: str, output: str = "") -> CallToolResult:
        return _invoke(lambda: selected().complete_child(task_id, domain, attempt, output=output))

    @server.tool(name="rightmemory_curation_status", description="Inspect pending candidates, curation readiness, or a task without starting model work. " + ready_guidance, structured_output=False)
    def status(task_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().status(task_id))

    @server.tool(name="rightmemory_count_curation_tokens", description="Count a domain's retained text and report its maintenance token limit. Supply a task ID to count the edited worktree.", structured_output=False)
    def count(domain: Literal["memory", "guidance", "skills"], task_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected().count(domain, task_id))

    @server.tool(name="rightmemory_withdraw_candidate", description="Withdraw a pending candidate by identity. This prevents further processing; it does not undo already published domain conclusions.", structured_output=False)
    def withdraw(candidate_id: str) -> CallToolResult:
        return _invoke(lambda: selected().withdraw(candidate_id))


def _invoke(operation: Callable[[], dict[str, Any]]) -> CallToolResult:
    try:
        result = operation()
    except (ValueError, OSError, RuntimeError) as exc:
        from .learning_coordinator import LearningUnavailable

        details = exc.details if isinstance(exc, LearningUnavailable) else {}
        result = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}, **details}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structured_content=result, is_error=result.get("ok") is False,
    )
