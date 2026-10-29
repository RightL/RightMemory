import asyncio
import unittest
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from rightmemory.curation_mcp import register_curation_tools


class Backend:
    def __init__(self):
        self.calls = []
        self.error = None
        self.result = {"ok": True, "created": True, "selected_candidate_count": 3}

    def _record(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result

    def submit(self, session_id, evidence, request_id=None, *, executor="native"):
        return self._record("submit", session_id, evidence, request_id, executor)

    def withdraw(self, candidate_id):
        return self._record("withdraw", candidate_id)

    def begin(self, domain, *, mode="update", scope="", retry_task=None):
        return self._record("begin", domain, mode, scope, retry_task)

    def complete(self, task_id, output=""):
        return self._record("complete", task_id, output)

    def status(self, task_id=None):
        return self._record("status", task_id)


class CurationMcpTests(unittest.TestCase):
    def _server(self):
        server = MCPServer("Curation test", log_level="WARNING")
        backend = Backend()
        register_curation_tools(server, Path("/unused-root"), backend=backend)
        return server, backend

    def test_native_registration_has_no_internal_executor_tool(self):
        async def names():
            server, _backend = self._server()
            async with Client(server, raise_exceptions=True) as client:
                return {tool.name for tool in (await client.list_tools()).tools}

        expected = {
            "rightmemory_submit", "rightmemory_begin_curation",
            "rightmemory_complete_curation", "rightmemory_curation_status", "rightmemory_withdraw_candidate",
        }
        self.assertEqual(asyncio.run(names()), expected)

    def test_routes_control_arguments_without_rewriting_evidence(self):
        server, backend = self._server()
        cases = [
            ("rightmemory_begin_curation", {"domain": "memory"}, ("begin", "memory", "update", "", None)),
            ("rightmemory_withdraw_candidate", {"candidate_id": "cid"}, ("withdraw", "cid")),
            ("rightmemory_submit", {"session_id": " s ", "evidence": "  source\n", "request_id": " r "}, ("submit", " s ", "  source\n", " r ", "native")),
            ("rightmemory_submit", {"session_id": "s", "evidence": "source"}, ("submit", "s", "source", None, "native")),
            ("rightmemory_begin_curation", {"domain": "guidance"}, ("begin", "guidance", "update", "", None)),
            ("rightmemory_begin_curation", {"domain": "skills", "mode": "maintain", "scope": "cases", "retry_task": "old"}, ("begin", "skills", "maintain", "cases", "old")),
            ("rightmemory_complete_curation", {"task_id": "task", "output": "Done"}, ("complete", "task", "Done")),
            ("rightmemory_complete_curation", {"task_id": "task"}, ("complete", "task", "")),
            ("rightmemory_curation_status", {"task_id": "task"}, ("status", "task")),
            ("rightmemory_curation_status", {}, ("status", None)),
        ]

        async def exercise():
            async with Client(server, raise_exceptions=True) as client:
                for name, arguments, expected in cases:
                    with self.subTest(name=name, arguments=arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error)
                        self.assertEqual(backend.calls[-1], expected)
                        self.assertEqual(result.structured_content, backend.result)
                        self.assertEqual(len(result.content), 1)

        asyncio.run(exercise())

    def test_failure_preserves_structured_error(self):
        server, backend = self._server()

        async def exercise():
            async with Client(server, raise_exceptions=True) as client:
                for error in (ValueError("invalid task"), OSError("read failed"), RuntimeError("claim replaced")):
                    with self.subTest(error_type=type(error).__name__):
                        backend.error = error
                        result = await client.call_tool("rightmemory_complete_curation", {"task_id": "task"})
                        self.assertTrue(result.is_error)
                        self.assertFalse(result.structured_content["ok"])
                        self.assertEqual(result.structured_content["error"]["type"], type(error).__name__)
                        self.assertIn("message", result.structured_content["error"])
                        self.assertEqual(len(result.content), 1)

        asyncio.run(exercise())

    def test_backend_rejection_preserves_error_flag(self):
        server, backend = self._server()
        backend.result = {"ok": False, "status": "rejected"}

        async def exercise():
            async with Client(server, raise_exceptions=True) as client:
                result = await client.call_tool("rightmemory_complete_curation", {"task_id": "task"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content, backend.result)
                self.assertEqual(len(result.content), 1)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
