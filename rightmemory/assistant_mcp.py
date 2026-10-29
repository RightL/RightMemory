from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from .validation import validate_memory_root
from .curation_mcp import CurationBackend, register_curation_tools


class AssistantBackend(Protocol):
    def context(self, session_id: str) -> dict[str, Any]: ...

    def sync(self, action: str = "refresh") -> dict[str, Any]: ...


def create_mcp_server(
    memory_root: Path, *, backend: AssistantBackend | None = None,
    curation_backend: CurationBackend | None = None
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

    register_curation_tools(server, root, backend=curation_backend)
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
