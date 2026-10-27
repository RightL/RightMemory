from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from .validation import validate_memory_root
from .learning_mcp import register_learning_tools


class AssistantBackend(Protocol):
    def context(self, session_id: str) -> dict[str, Any]: ...

    def submit(
        self, session_id: str, request_id: str, evidence: str
    ) -> dict[str, Any]: ...

    def begin_update(self, retry_task: str | None = None) -> dict[str, Any]: ...

    def complete_update(self, task_id: str, output: str = "") -> dict[str, Any]: ...

    def update_status(self, task_id: str | None = None) -> dict[str, Any]: ...

    def sync(self, action: str = "refresh") -> dict[str, Any]: ...


def create_mcp_server(
    memory_root: Path, *, backend: AssistantBackend | None = None
) -> MCPServer:
    """Expose the assistant core without importing an internal role runtime."""
    root = Path(memory_root).expanduser().resolve()
    if backend is None:
        from .assistant import AssistantService

        backend = AssistantService(root)
    selected = backend
    server = MCPServer("RightMemory Assistant", log_level="WARNING")

    @server.tool(
        name="rightmemory_assistant_context",
        description="Prepare source paths and shared instructions for a native Retrieve subagent.",
        structured_output=False,
    )
    def context(session_id: str) -> CallToolResult:
        return _invoke(lambda: selected.context(session_id))

    @server.tool(
        name="rightmemory_assistant_submit",
        description="Save candidate evidence with a stable retry id; return pending and batch notices.",
        structured_output=False,
    )
    def submit(session_id: str, request_id: str, evidence: str) -> CallToolResult:
        return _invoke(lambda: selected.submit(session_id, request_id, evidence))

    @server.tool(
        name="rightmemory_assistant_begin_update",
        description="Prepare a ready batch and isolated worktree for a native Update subagent.",
        structured_output=False,
    )
    def begin_update(retry_task: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected.begin_update(retry_task))

    @server.tool(
        name="rightmemory_assistant_complete_update",
        description="Validate and finalize the specified candidate task; retries use the same task id.",
        structured_output=False,
    )
    def complete_update(task_id: str, output: str = "") -> CallToolResult:
        return _invoke(lambda: selected.complete_update(task_id, output))

    @server.tool(
        name="rightmemory_assistant_update_status",
        description="Inspect native Update execution and recovery state.",
        structured_output=False,
    )
    def update_status(task_id: str | None = None) -> CallToolResult:
        return _invoke(lambda: selected.update_status(task_id))

    @server.tool(
        name="rightmemory_assistant_sync",
        description="Run the configured deterministic sync operation and report its outcome.",
        structured_output=False,
    )
    def sync(action: Literal["refresh", "pull", "push"] = "refresh") -> CallToolResult:
        return _invoke(lambda: selected.sync(action))

    @server.tool(
        name="rightmemory_assistant_validate",
        description="Independently validate the active memory root bound to this server.",
        structured_output=False,
    )
    def validate() -> CallToolResult:
        return _invoke(lambda: validate_memory_root(root).to_dict())

    register_learning_tools(server, root, native=True)
    return server


def _invoke(operation: Callable[[], dict[str, Any]]) -> CallToolResult:
    try:
        payload = operation()
    except (ValueError, RuntimeError, OSError) as exc:
        payload = {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structured_content=payload,
        is_error=payload.get("ok") is False,
    )


def mcp_main(memory_root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rightmemory assistant mcp",
        description="Serve the deterministic assistant core over MCP stdio.",
    )
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    args = parser.parse_args([] if argv is None else argv)
    create_mcp_server(memory_root).run(transport=args.transport)
    return 0
