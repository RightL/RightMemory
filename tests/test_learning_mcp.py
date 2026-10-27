import asyncio
import unittest
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from rightmemory.learning_mcp import register_learning_tools


class Backend:
    def __init__(self):
        self.calls = []
        self.error = None

    def _record(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return {"ok": True, "value": "retained"}

    def submit(self, session_id, evidence, request_id=None):
        return self._record("submit", session_id, evidence, request_id)

    def begin(self, domain, *, mode="update", scope="", retry_task=None):
        return self._record("begin", domain, mode, scope, retry_task)

    def complete(self, task_id, output=""):
        return self._record("complete", task_id, output)

    def status(self, task_id=None):
        return self._record("status", task_id)

    def run(self, domain, *, mode="update", scope=""):
        return self._record("run", domain, mode, scope)


class LearningMcpTests(unittest.TestCase):
    def _server(self, native):
        server = MCPServer("Learning test", log_level="WARNING")
        backend = Backend()
        register_learning_tools(server, Path("/unused-root"), native=native, backend=backend)
        return server, backend

    def test_native_registration_has_no_internal_executor_tool(self):
        async def names(native):
            server, _backend = self._server(native)
            async with Client(server, raise_exceptions=True) as client:
                return {tool.name for tool in (await client.list_tools()).tools}

        common = {
            "rightmemory_submit_learning", "rightmemory_begin_learning",
            "rightmemory_complete_learning", "rightmemory_learning_status",
        }
        self.assertEqual(asyncio.run(names(True)), common)
        self.assertEqual(asyncio.run(names(False)), common | {"rightmemory_run_learning"})

    def test_routes_control_arguments_without_rewriting_evidence(self):
        server, backend = self._server(False)
        cases = [
            ("rightmemory_submit_learning", {"session_id": "s", "evidence": "  source\n", "request_id": "r"}, ("submit", "s", "  source\n", "r")),
            ("rightmemory_submit_learning", {"session_id": "s", "evidence": "source"}, ("submit", "s", "source", None)),
            ("rightmemory_begin_learning", {"domain": "guidance"}, ("begin", "guidance", "update", "", None)),
            ("rightmemory_begin_learning", {"domain": "skills", "mode": "maintain", "scope": "cases", "retry_task": "old"}, ("begin", "skills", "maintain", "cases", "old")),
            ("rightmemory_complete_learning", {"task_id": "task", "output": "Done"}, ("complete", "task", "Done")),
            ("rightmemory_learning_status", {"task_id": "task"}, ("status", "task")),
            ("rightmemory_run_learning", {"domain": "skills", "mode": "maintain", "scope": "cases"}, ("run", "skills", "maintain", "cases")),
        ]

        async def exercise():
            async with Client(server, raise_exceptions=True) as client:
                for name, arguments, expected in cases:
                    with self.subTest(name=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error)
                        self.assertEqual(backend.calls[-1], expected)
                        self.assertEqual(result.structured_content, {"ok": True, "value": "retained"})

        asyncio.run(exercise())

    def test_failure_preserves_structured_error(self):
        server, backend = self._server(True)
        backend.error = RuntimeError("claim replaced")

        async def exercise():
            async with Client(server, raise_exceptions=True) as client:
                result = await client.call_tool("rightmemory_complete_learning", {"task_id": "task"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"], {"type": "RuntimeError", "message": "claim replaced"})

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
