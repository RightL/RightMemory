from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp import Client
from mcp.types import CallToolResult

from rightmemory import entrypoint
from rightmemory.config import RuntimeConfig
from rightmemory.learning import list_learning_candidates
from rightmemory.learning_service import LearningService
from rightmemory.mcp import DefaultMcpBackend, create_mcp_server
from rightmemory.sync import SyncResult
from tests.isolated_write_test_base import IsolatedWriteTestBase


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.warning: str | None = None
        self.submit_warning: str | None = None
        self.error: Exception | None = None

    def retrieve(self, session_id: str, need: str) -> str:
        self.calls.append(("retrieve", session_id, need))
        return "retrieved context"

    def submit(
        self, session_id: str, evidence: str, request_id: str | None = None,
    ) -> str | None:
        self.calls.append(("submit", session_id, evidence, request_id))
        if self.error is not None:
            raise self.error
        return self.submit_warning

    def actionable_warning(self) -> str | None:
        return self.warning


async def _call_tool(server, name: str, arguments: dict[str, str]) -> CallToolResult:
    async with Client(server, raise_exceptions=True) as client:
        return await client.call_tool(name, arguments)


def call_tool(server, name: str, arguments: dict[str, str]) -> CallToolResult:
    result = asyncio.run(_call_tool(server, name, arguments))
    if not isinstance(result, CallToolResult):
        raise AssertionError(f"expected CallToolResult, got {type(result).__name__}")
    return result


async def _list_tools(server):
    async with Client(server, raise_exceptions=True) as client:
        return (await client.list_tools()).tools


class McpToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.server = create_mcp_server(Path("/unused"), backend=self.backend)

    def test_server_exposes_exactly_retrieve_and_submit(self):
        tools = asyncio.run(_list_tools(self.server))
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "rightmemory_retrieve",
                "rightmemory_submit",
            },
        )

    def test_retrieve_trims_arguments_and_returns_context_plus_actionable_warning(self):
        self.backend.warning = "warning"
        result = call_tool(
            self.server,
            "rightmemory_retrieve",
            {"session_id": " session ", "need": " need "},
        )

        self.assertEqual(self.backend.calls, [("retrieve", "session", "need")])
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 2)
        self.assertIsNone(result.structured_content)

    def test_submit_routes_raw_evidence_and_request_id_with_empty_success(self):
        evidence = "  Original evidence\n\n    code\n"
        request_id = " request-one "
        result = call_tool(
            self.server,
            "rightmemory_submit",
            {"session_id": " session ", "evidence": evidence, "request_id": request_id},
        )

        self.assertEqual(self.backend.calls, [("submit", "session", evidence, request_id)])
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, [])
        self.assertIsNone(result.structured_content)

    def test_submit_without_request_id_passes_none(self):
        result = call_tool(
            self.server,
            "rightmemory_submit",
            {"session_id": "session", "evidence": "Evidence"},
        )

        self.assertEqual(self.backend.calls, [("submit", "session", "Evidence", None)])
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, [])
        self.assertIsNone(result.structured_content)

    def test_submit_returns_actionable_warnings_as_successful_text(self):
        for submit_warning, recovery_warning, count in (
            ("capture warning", None, 1),
            (None, "recovery warning", 1),
            ("capture warning", "recovery warning", 2),
        ):
            with self.subTest(submit_warning=bool(submit_warning), recovery_warning=bool(recovery_warning)):
                self.backend.submit_warning = submit_warning
                self.backend.warning = recovery_warning
                result = call_tool(
                    self.server,
                    "rightmemory_submit",
                    {"session_id": "session", "evidence": "Evidence"},
                )

                self.assertFalse(result.is_error)
                self.assertEqual(len(result.content), count)
                self.assertIsNone(result.structured_content)

    def test_submit_failure_is_not_an_empty_success(self):
        for error in (ValueError("invalid evidence"), OSError("write failed"), RuntimeError("capture failed")):
            with self.subTest(error_type=type(error).__name__):
                self.backend.error = error
                result = call_tool(
                    self.server,
                    "rightmemory_submit",
                    {"session_id": "session", "evidence": "Evidence"},
                )

                self.assertTrue(result.is_error)
                self.assertGreater(len(result.content), 0)
                self.assertIsNone(result.structured_content)

    def test_submit_rejects_invalid_session_before_backend_capture(self):
        for session_id in (" ", "line\nbreak", "null\x00byte"):
            with self.subTest(session_id=session_id):
                result = call_tool(
                    self.server,
                    "rightmemory_submit",
                    {"session_id": session_id, "evidence": "Evidence"},
                )

                self.assertTrue(result.is_error)
                self.assertGreater(len(result.content), 0)
        self.assertEqual(self.backend.calls, [])


def _submission_response(
    *, sync_status="pushed", worker_status="started", status="pending", created=True,
):
    return {
        "ok": True,
        "saved": True,
        "candidate_id": "a" * 64,
        "created": created,
        "pending_domains": ["memory", "guidance", "skills"] if status == "pending" else [],
        "status": status,
        "sync": {"status": sync_status},
        "worker": {"status": worker_status},
        "curation": [{"domain": "memory", "ready": True, "candidate_count": 20}],
    }


class DefaultMcpBackendTests(unittest.TestCase):
    def test_configured_submit_routes_capture_arguments_without_returning_receipt(self):
        root = Path("/memory")
        backend = DefaultMcpBackend(root, codex_runner=Mock())
        server = create_mcp_server(root, backend=backend)
        evidence = "  Evidence\n\n    preserve spacing\n"
        request_id = " request-one "

        with patch("rightmemory.learning_service.LearningService") as service_class, patch(
            "rightmemory.mcp._actionable_curation_warning", return_value=None,
        ):
            service = service_class.return_value
            service.submit.return_value = _submission_response()
            result = call_tool(
                server,
                "rightmemory_submit",
                {"session_id": " session ", "evidence": evidence, "request_id": request_id},
            )

        service_class.assert_called_once_with(root)
        service.submit.assert_called_once_with("session", evidence, request_id, executor="configured")
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, [])
        self.assertIsNone(result.structured_content)

    def test_healthy_new_and_repeated_submissions_are_silent(self):
        backend = DefaultMcpBackend(Path("/memory"), codex_runner=Mock())
        server = create_mcp_server(backend.memory_root, backend=backend)

        with patch("rightmemory.learning_service.LearningService") as service_class, patch(
            "rightmemory.mcp._actionable_curation_warning", return_value=None,
        ):
            for status, created in (("pending", True), ("pending", False), ("completed", False)):
                for sync_status in ("pushed", "disabled"):
                    for worker_status in ("started", "running"):
                        with self.subTest(status=status, created=created, sync=sync_status, worker=worker_status):
                            service_class.return_value.submit.return_value = _submission_response(
                                status=status, created=created,
                                sync_status=sync_status, worker_status=worker_status,
                            )
                            result = call_tool(
                                server,
                                "rightmemory_submit",
                                {"session_id": "session", "evidence": "Evidence"},
                            )

                            self.assertFalse(result.is_error)
                            self.assertEqual(result.content, [])
                            self.assertIsNone(result.structured_content)

    def test_saved_submissions_report_sync_and_worker_failures_without_error_flag(self):
        backend = DefaultMcpBackend(Path("/memory"), codex_runner=Mock())
        server = create_mcp_server(backend.memory_root, backend=backend)
        cases = [
            _submission_response(sync_status=status)
            for status in ("offline", "conflict", "dirty", "pending")
        ] + [
            _submission_response(worker_status=status)
            for status in ("error", "deferred")
        ]
        for response in cases:
            with self.subTest(sync=response["sync"]["status"], worker=response["worker"]["status"]):
                with patch("rightmemory.learning_service.LearningService") as service_class, patch(
                    "rightmemory.mcp._actionable_curation_warning", return_value=None,
                ):
                    service_class.return_value.submit.return_value = response
                    result = call_tool(
                        server,
                        "rightmemory_submit",
                        {"session_id": "session", "evidence": "Evidence"},
                    )

                self.assertFalse(result.is_error)
                self.assertEqual(len(result.content), 1)
                self.assertIsNone(result.structured_content)

    def test_saved_submission_reports_curation_status_inspection_failure(self):
        backend = DefaultMcpBackend(Path("/memory"), codex_runner=Mock())
        response = _submission_response()
        response["curation"] = {"status": "error", "error": "status unavailable"}
        server = create_mcp_server(backend.memory_root, backend=backend)

        with patch("rightmemory.learning_service.LearningService") as service_class, patch(
            "rightmemory.mcp._actionable_curation_warning", return_value=None,
        ):
            service_class.return_value.submit.return_value = response
            result = call_tool(
                server,
                "rightmemory_submit",
                {"session_id": "session", "evidence": "Evidence"},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 1)
        self.assertIsNone(result.structured_content)

    def test_withdrawn_retry_returns_warning_even_when_sync_and_worker_are_healthy(self):
        backend = DefaultMcpBackend(Path("/memory"), codex_runner=Mock())
        server = create_mcp_server(backend.memory_root, backend=backend)

        with patch("rightmemory.learning_service.LearningService") as service_class, patch(
            "rightmemory.mcp._actionable_curation_warning", return_value=None,
        ):
            service_class.return_value.submit.return_value = _submission_response(
                status="withdrawn", created=False,
            )
            result = call_tool(
                server,
                "rightmemory_submit",
                {"session_id": "session", "evidence": "Evidence", "request_id": "request"},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 1)
        self.assertIsNone(result.structured_content)

    def test_retrieve_does_not_query_learning_notices(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(role="retrieve", memory_root=root, state_root=root)
            runtime = SimpleNamespace(
                run_session_turn=Mock(return_value="context"), cleanup=Mock(),
            )
            backend = DefaultMcpBackend(root, codex_runner=Mock())
            server = create_mcp_server(root, backend=backend)

            with patch("rightmemory.mcp.load_config", return_value=config), patch(
                "rightmemory.mcp.RightMemoryRuntime", return_value=runtime,
            ), patch(
                "rightmemory.curation_worker.CurationWorker.status", return_value={"live": True},
            ) as worker_status, patch(
                "rightmemory.learning_service.LearningService.notices",
            ) as notices:
                result = call_tool(
                    server, "rightmemory_retrieve", {"session_id": "session", "need": "need"},
                )

            self.assertFalse(result.is_error)
            self.assertEqual(len(result.content), 1)
            self.assertIsNone(result.structured_content)
            runtime.run_session_turn.assert_called_once_with("session", "need")
            runtime.cleanup.assert_called_once_with()
            worker_status.assert_called_once_with()
            notices.assert_not_called()

    def test_retrieve_returns_worker_status_failures_as_warnings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(role="retrieve", memory_root=root, state_root=root)
            runtime = SimpleNamespace(
                run_session_turn=Mock(return_value="context"), cleanup=Mock(),
            )
            backend = DefaultMcpBackend(root, codex_runner=Mock())
            server = create_mcp_server(root, backend=backend)
            failures = (
                {"live": False, "error": "worker failed"},
                OSError("status unavailable"),
                ValueError("invalid status"),
                RuntimeError("status unavailable"),
            )

            with patch("rightmemory.mcp.load_config", return_value=config), patch(
                "rightmemory.mcp.RightMemoryRuntime", return_value=runtime,
            ):
                for failure in failures:
                    with self.subTest(failure_type=type(failure).__name__):
                        kwargs = {"return_value": failure} if isinstance(failure, dict) else {"side_effect": failure}
                        with patch("rightmemory.curation_worker.CurationWorker.status", **kwargs):
                            result = call_tool(
                                server, "rightmemory_retrieve", {"session_id": "session", "need": "need"},
                            )

                        self.assertFalse(result.is_error)
                        self.assertEqual(len(result.content), 2)
                        self.assertIsNone(result.structured_content)

    def test_factory_owned_backend_closes_with_server_lifespan(self):
        with patch(
            "rightmemory.codex_sdk.CodexSdkRunner.close",
            autospec=True,
        ) as close_runner:
            server = create_mcp_server(Path("/memory"))

            async def enter_lifespan() -> None:
                assert server.settings.lifespan is not None
                async with server.settings.lifespan(server):
                    close_runner.assert_not_called()

            asyncio.run(enter_lifespan())

        close_runner.assert_called_once()

    def test_retrieves_share_backend_runner_without_per_request_close(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(role="retrieve", memory_root=root, state_root=root)
            codex_runner = unittest.mock.Mock()
            runtimes = [
                SimpleNamespace(
                    run_session_turn=unittest.mock.Mock(return_value=f"context-{index}"),
                    cleanup=unittest.mock.Mock(),
                )
                for index in range(2)
            ]
            backend = DefaultMcpBackend(root, codex_runner=codex_runner)

            with patch("rightmemory.mcp.load_config", return_value=config), patch(
                "rightmemory.mcp.RightMemoryRuntime",
                side_effect=runtimes,
            ) as runtime_class:
                self.assertEqual(backend.retrieve("session-1", "need-1"), "context-0")
                self.assertEqual(backend.retrieve("session-2", "need-2"), "context-1")

            self.assertEqual(runtime_class.call_count, 2)
            for call in runtime_class.call_args_list:
                self.assertIs(call.kwargs["codex_runner"], codex_runner)
            for runtime in runtimes:
                runtime.cleanup.assert_called_once_with()
            codex_runner.close.assert_not_called()

    def test_retrieve_writes_one_ordered_mcp_timing_summary(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="retrieve",
                memory_root=root,
                state_root=root,
                debug_trace=True,
            )
            runtime = SimpleNamespace(
                run_session_turn=unittest.mock.Mock(return_value="context"),
                cleanup=unittest.mock.Mock(),
            )
            server = create_mcp_server(root)

            with patch("rightmemory.mcp.load_config", return_value=config), patch(
                "rightmemory.mcp.RightMemoryRuntime",
                return_value=runtime,
            ), patch(
                "rightmemory.mcp._actionable_curation_warning",
                return_value=None,
            ):
                call_tool(
                    server,
                    "rightmemory_retrieve",
                    {"session_id": "timed-session", "need": "context"},
                )

            trace_path = (
                root
                / ".runtime"
                / "debug"
                / "retrieve"
                / "timed-session.jsonl"
            )
            events = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["event"], "mcp_timing")
            self.assertEqual(event["outcome"], "success")
            self.assertIsNone(event["error_type"])
            self.assertIsNotNone(
                datetime.fromisoformat(event["backend_entry_wall_timestamp"]).tzinfo
            )

            timing_fields = [
                "config_load_ms",
                "runtime_construction_ms",
                "run_session_turn_ms",
                "runtime_cleanup_ms",
                "actionable_warning_ms",
                "result_construction_ms",
                "total_ms",
            ]
            expected_fields = [
                "backend_entry_wall_timestamp",
                *timing_fields,
                "outcome",
                "error_type",
            ]
            keys = list(event)
            first_timing_field = keys.index("backend_entry_wall_timestamp")
            self.assertEqual(
                keys[first_timing_field : first_timing_field + len(expected_fields)],
                expected_fields,
            )
            for field in timing_fields:
                self.assertIsInstance(event[field], (int, float))
                self.assertGreaterEqual(event[field], 0.0)
            runtime.cleanup.assert_called_once_with()

    def test_failed_retrieve_writes_timing_summary_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="retrieve",
                memory_root=root,
                state_root=root,
                debug_trace=True,
            )
            runtime = SimpleNamespace(
                run_session_turn=unittest.mock.Mock(
                    side_effect=RuntimeError("retrieve failed")
                ),
                cleanup=unittest.mock.Mock(),
            )
            server = create_mcp_server(root)

            with patch("rightmemory.mcp.load_config", return_value=config), patch(
                "rightmemory.mcp.RightMemoryRuntime",
                return_value=runtime,
            ):
                result = call_tool(
                    server,
                    "rightmemory_retrieve",
                    {"session_id": "failed-session", "need": "context"},
                )

            self.assertTrue(result.is_error)

            trace_path = (
                root
                / ".runtime"
                / "debug"
                / "retrieve"
                / "failed-session.jsonl"
            )
            event = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(event["event"], "mcp_timing")
            self.assertEqual(event["outcome"], "failure")
            self.assertEqual(event["error_type"], "RuntimeError")
            for field in (
                "config_load_ms",
                "runtime_construction_ms",
                "run_session_turn_ms",
                "runtime_cleanup_ms",
                "actionable_warning_ms",
                "result_construction_ms",
                "total_ms",
            ):
                self.assertGreaterEqual(event[field], 0.0)
            runtime.cleanup.assert_called_once_with()


class McpSubmissionPersistenceTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        self.backend = DefaultMcpBackend(self.root, codex_runner=Mock())
        self.server = create_mcp_server(self.root, backend=self.backend)

    def test_sync_and_wake_failures_keep_saved_evidence_and_retry_does_not_duplicate(self):
        evidence = "  Original evidence\n\n    preserve this code\n"
        arguments = {"session_id": "session", "evidence": evidence, "request_id": "request"}
        with patch(
            "rightmemory.learning_service.SyncManager.push", side_effect=OSError("sync unavailable"),
        ) as push, patch(
            "rightmemory.curation_worker.CurationWorker.wake", side_effect=RuntimeError("worker unavailable"),
        ) as wake, patch(
            "rightmemory.curation_worker.CurationWorker.status", return_value={"live": False},
        ):
            first = call_tool(self.server, "rightmemory_submit", arguments)
            candidates = list_learning_candidates(self.root)
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate.evidence, evidence)
            self.assertEqual(candidate.session_id, "session")
            self.assertEqual(candidate.request_id, "request")
            captured_head = self._git("rev-parse", "HEAD")
            path = self.root / "learning" / "candidates" / f"{candidate.candidate_id}.json"
            captured_bytes = path.read_bytes()

            repeated = call_tool(self.server, "rightmemory_submit", arguments)

        for result in (first, repeated):
            self.assertFalse(result.is_error)
            self.assertEqual(len(result.content), 1)
            self.assertIsNone(result.structured_content)
        self.assertNotEqual(captured_head, self.initial_head)
        self.assertEqual(self._git("rev-parse", "HEAD"), captured_head)
        self.assertEqual(path.read_bytes(), captured_bytes)
        self.assertEqual(list_learning_candidates(self.root), candidates)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual(push.call_count, 2)
        self.assertEqual(wake.call_count, 2)

    def test_capture_failure_has_error_flag_and_does_not_sync_or_wake(self):
        with patch("rightmemory.learning_service.SyncManager.push") as push, patch(
            "rightmemory.curation_worker.CurationWorker.wake",
        ) as wake, patch(
            "rightmemory.curation_worker.CurationWorker.status",
        ) as worker_status:
            result = call_tool(
                self.server,
                "rightmemory_submit",
                {"session_id": "session", "evidence": " \n", "request_id": "request"},
            )

        self.assertTrue(result.is_error)
        self.assertGreater(len(result.content), 0)
        self.assertIsNone(result.structured_content)
        self.assertEqual(list_learning_candidates(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        push.assert_not_called()
        wake.assert_not_called()
        worker_status.assert_not_called()

    def test_withdrawn_retry_keeps_receipt_without_restoring_candidate(self):
        arguments = {"session_id": "session", "evidence": "Evidence", "request_id": "request"}
        with patch(
            "rightmemory.learning_service.SyncManager.push", return_value=SyncResult("disabled", ""),
        ), patch(
            "rightmemory.curation_worker.CurationWorker.wake", return_value={"status": "running"},
        ), patch(
            "rightmemory.curation_worker.CurationWorker.status", return_value={"live": True},
        ):
            first = call_tool(self.server, "rightmemory_submit", arguments)
            candidate_id = list_learning_candidates(self.root)[0].candidate_id
            LearningService(self.root).withdraw(candidate_id)
            withdrawn_head = self._git("rev-parse", "HEAD")
            repeated = call_tool(self.server, "rightmemory_submit", arguments)

        self.assertFalse(first.is_error)
        self.assertEqual(first.content, [])
        self.assertFalse(repeated.is_error)
        self.assertEqual(len(repeated.content), 1)
        self.assertIsNone(repeated.structured_content)
        self.assertEqual(list_learning_candidates(self.root), ())
        self.assertEqual(self._git("rev-parse", "HEAD"), withdrawn_head)
        self.assertEqual(self._git("status", "--short"), "")


class McpEntrypointTests(unittest.TestCase):
    def test_mcp_main_closes_backend_after_normal_server_exit(self):
        from rightmemory import mcp as mcp_module

        backend = SimpleNamespace(close=unittest.mock.Mock())
        server = SimpleNamespace(run=unittest.mock.Mock())

        with patch.object(
            mcp_module,
            "DefaultMcpBackend",
            return_value=backend,
        ), patch.object(mcp_module, "create_mcp_server", return_value=server):
            result = mcp_module.mcp_main(Path("/memory"), [])

        self.assertEqual(result, 0)
        server.run.assert_called_once_with(transport="stdio")
        backend.close.assert_called_once_with()

    def test_mcp_main_closes_backend_after_exceptional_server_exit(self):
        from rightmemory import mcp as mcp_module

        backend = SimpleNamespace(close=unittest.mock.Mock())
        server = SimpleNamespace(
            run=unittest.mock.Mock(side_effect=RuntimeError("server failed"))
        )

        with patch.object(
            mcp_module,
            "DefaultMcpBackend",
            return_value=backend,
        ), patch.object(mcp_module, "create_mcp_server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "server failed"):
                mcp_module.mcp_main(Path("/memory"), [])

        backend.close.assert_called_once_with()

    def test_entrypoint_resolves_root_and_starts_mcp(self):
        root = Path("/resolved-memory")
        from rightmemory import mcp as mcp_module

        with patch.object(
            entrypoint,
            "resolve_memory_root",
            return_value=SimpleNamespace(memory_root=root),
        ), patch.object(mcp_module, "mcp_main", return_value=0) as run:
            result = entrypoint.main(["--profile", "project", "mcp"])

        self.assertEqual(result, 0)
        run.assert_called_once_with(root, [])


if __name__ == "__main__":
    unittest.main()
