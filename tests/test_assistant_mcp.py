from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client, ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from rightmemory.assistant_mcp import create_mcp_server, mcp_main
from tests.isolated_write_test_base import IsolatedWriteTestBase


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.result = {"ok": True, "ready": {"batch_id": "batch-1"}}
        self.error: Exception | None = None

    def _record(self, *call):
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        return self.result

    def context(self, session_id):
        return self._record("context", session_id)


    def sync(self, action="refresh"):
        return self._record("sync", action)


async def _call(server, name, arguments):
    async with Client(server, raise_exceptions=True) as client:
        return await client.call_tool(name, arguments)


def call(server, name, arguments) -> CallToolResult:
    result = asyncio.run(_call(server, name, arguments))
    if not isinstance(result, CallToolResult):
        raise AssertionError(type(result))
    return result


async def _list(server):
    async with Client(server, raise_exceptions=True) as client:
        return (await client.list_tools()).tools


class AssistantMcpTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.root = Path("/unused-assistant-root").resolve()
        self.server = create_mcp_server(self.root, backend=self.backend)

    def test_exposes_the_assistant_operations(self):
        self.assertEqual(
            {tool.name for tool in asyncio.run(_list(self.server))},
            {
                "rightmemory_assistant_context",
                "rightmemory_assistant_sync",
                "rightmemory_assistant_validate",
                "rightmemory_submit",
                "rightmemory_withdraw_candidate",
                "rightmemory_begin_curation",
                "rightmemory_complete_curation",
                "rightmemory_curation_status",
            },
        )

    def test_routes_operations_without_rewriting_ids_evidence_or_receipts(self):
        cases = [
            ("context", {"session_id": "s"}, ("context", "s")),
            ("sync", {}, ("sync", "refresh")),
            ("sync", {"action": "push"}, ("sync", "push")),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name=name, arguments=arguments):
                result = call(self.server, f"rightmemory_assistant_{name}", arguments)
                self.assertFalse(result.is_error)
                self.assertEqual(self.backend.calls[-1], expected)
                self.assertEqual(result.structured_content, self.backend.result)

    def test_reports_core_failures_as_json_errors(self):
        for error in (ValueError("bad input"), RuntimeError("conflict"), FileNotFoundError("task")):
            with self.subTest(error=type(error).__name__):
                self.backend.error = error
                result = call(self.server, "rightmemory_assistant_context", {"session_id": "s"})
                self.assertTrue(result.is_error)
                self.assertEqual(
                    result.structured_content,
                    {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}},
                )

    def test_preserves_an_unsuccessful_core_result(self):
        self.backend.result = {"ok": False, "status": "conflict"}
        result = call(self.server, "rightmemory_assistant_sync", {})
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content, self.backend.result)

    def test_missing_required_input_does_not_call_the_core(self):
        result = call(self.server, "rightmemory_assistant_context", {})
        self.assertTrue(result.is_error)
        self.assertEqual(self.backend.calls, [])

    def test_invalid_sync_action_does_not_call_the_core(self):
        result = call(self.server, "rightmemory_assistant_sync", {"action": "erase"})
        self.assertTrue(result.is_error)
        self.assertEqual(self.backend.calls, [])

    def test_validation_uses_only_the_bound_root(self):
        result_data = {"ok": False, "errors": ["invalid"]}
        with patch("rightmemory.assistant_mcp.validate_memory_root") as validate:
            validate.return_value = SimpleNamespace(to_dict=lambda: result_data)
            result = call(self.server, "rightmemory_assistant_validate", {})
        validate.assert_called_once_with(self.root)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content, result_data)
        self.assertEqual(self.backend.calls, [])

    def test_stdio_entrypoint_selects_the_bound_root(self):
        with patch("rightmemory.assistant_mcp.create_mcp_server") as factory:
            result = mcp_main(self.root, ["--transport", "stdio"])
        self.assertEqual(result, 0)
        factory.assert_called_once_with(self.root)
        factory.return_value.run.assert_called_once_with(transport="stdio")


class AssistantMcpStdioTests(IsolatedWriteTestBase):
    def test_entrypoint_stdio_handshake_and_model_free_core_calls(self):
        (self.root / ".git/info").mkdir(exist_ok=True)
        with (self.root / ".git/info/exclude").open("a", encoding="utf-8") as stream:
            stream.write("\n/rightmemory.toml\n")
        (self.root / "rightmemory.toml").write_text(
            "[curation.batch]\ntrigger_candidates = 1\n", encoding="utf-8",
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "rightmemory.entrypoint", "assistant", "--root", str(self.root),
                "mcp", "--transport", "stdio",
            ],
            cwd=Path(__file__).resolve().parents[1],
        )
        evidence = "The fictional Atlas project keeps its blue test fixture in the west room."

        async def exercise():
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=10) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.server_info.name, "RightMemory Assistant")
                    listed = await client.list_tools()
                    self.assertEqual(len(listed.tools), 8)

                    result = await client.call_tool(
                        "rightmemory_assistant_context", {"session_id": "stdio-session"}
                    )
                    self.assertFalse(result.is_error)
                    context = result.structured_content
                    self.assertEqual(context["memory_root"], str(self.root.resolve()))
                    self.assertEqual(context["memory_commit"], self.initial_head)
                    self.assertEqual(context["pending_count"], 0)
                    self.assertTrue(Path(context["instructions_file"]).is_file())

                    arguments = {
                        "session_id": "stdio-session", "request_id": "evidence-1",
                        "evidence": evidence,
                    }
                    first = await client.call_tool("rightmemory_submit", arguments)
                    self.assertFalse(first.is_error)
                    self.assertTrue(first.structured_content["saved"])
                    self.assertTrue(first.structured_content["created"])
                    self.assertTrue(all(item["ready"] for item in first.structured_content["curation"]))

                    repeated = await client.call_tool("rightmemory_submit", arguments)
                    self.assertFalse(repeated.is_error)
                    self.assertFalse(repeated.structured_content["created"])
                    self.assertEqual(
                        repeated.structured_content["candidate_id"],
                        first.structured_content["candidate_id"],
                    )
                    rejected = await client.call_tool("rightmemory_submit", {**arguments, "evidence": "Different fact."})
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(rejected.structured_content["error"]["type"], "ValueError")

                    pending = await client.call_tool(
                        "rightmemory_assistant_context", {"session_id": "stdio-session"}
                    )
                    self.assertFalse(pending.is_error)
                    self.assertEqual(pending.structured_content["pending_count"], 1)
                    self.assertTrue(Path(pending.structured_content["learning_index_file"]).is_file())
                    validated = await client.call_tool("rightmemory_assistant_validate", {})
                    self.assertFalse(validated.is_error)
                    self.assertTrue(validated.structured_content["ok"])

        asyncio.run(exercise())
        self.assertNotEqual(self._git("rev-parse", "HEAD"), self.initial_head)


if __name__ == "__main__":
    unittest.main()
