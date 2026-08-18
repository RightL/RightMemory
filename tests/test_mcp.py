from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import Client
from mcp.types import CallToolResult

from rightmemory import entrypoint
from rightmemory.async_update import (
    STATUS_MANUAL_RECOVERY,
    AsyncUpdateJob,
    AsyncUpdateState,
)
from rightmemory.mcp import DefaultMcpBackend, create_mcp_server
from rightmemory.update_alerts import collect_update_recovery_summary
from rightmemory.update_queue import (
    UpdateCandidate,
    UpdateQueueRecovery,
    UpdateQueueStore,
    update_candidate_batch_id,
)


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.warning: str | None = None
        self.submit_warning: str | None = None

    def retrieve(self, session_id: str, need: str) -> str:
        self.calls.append(("retrieve", session_id, need))
        return "retrieved context"

    def submit_update(self, session_id: str, evidence: str) -> str | None:
        self.calls.append(("submit", session_id, evidence))
        return self.submit_warning

    def capture_guidance(self, session_id: str, evidence: str) -> None:
        self.calls.append(("guidance", session_id, evidence))

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

    def test_server_exposes_only_the_three_ordinary_agent_tools(self):
        tools = asyncio.run(_list_tools(self.server))
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "rightmemory_retrieve",
                "rightmemory_submit_update",
                "rightmemory_capture_guidance",
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
        self.assertEqual(len(result.content), 2)

    def test_successful_update_submission_is_silent(self):
        result = call_tool(
            self.server,
            "rightmemory_submit_update",
            {"session_id": "session", "evidence": "evidence"},
        )

        self.assertEqual(self.backend.calls, [("submit", "session", "evidence")])
        self.assertEqual(result.content, [])

    def test_update_submission_returns_only_an_actionable_warning(self):
        self.backend.submit_warning = "warning"
        result = call_tool(
            self.server,
            "rightmemory_submit_update",
            {"session_id": "session", "evidence": "evidence"},
        )

        self.assertEqual(len(result.content), 1)

    def test_successful_guidance_capture_is_silent(self):
        result = call_tool(
            self.server,
            "rightmemory_capture_guidance",
            {"session_id": " session ", "evidence": " evidence "},
        )

        self.assertEqual(self.backend.calls, [("guidance", "session", "evidence")])
        self.assertEqual(result.content, [])


class DefaultMcpBackendTests(unittest.TestCase):
    def test_post_save_worker_failure_does_not_ask_for_resubmission(self):
        candidate_uid = "a" * 32
        store = SimpleNamespace()
        store.submit = unittest.mock.Mock(side_effect=RuntimeError("worker failed"))
        store.read = unittest.mock.Mock(
            return_value=SimpleNamespace(accepted_candidate_uids=[candidate_uid])
        )

        with patch("rightmemory.mcp.AsyncUpdateStore", return_value=store), patch(
            "rightmemory.mcp.uuid.uuid4",
            return_value=SimpleNamespace(hex=candidate_uid),
        ):
            warning = DefaultMcpBackend(Path("/memory")).submit_update(
                "session",
                "evidence",
            )

        self.assertIsInstance(warning, str)
        store.submit.assert_called_once_with(
            "session",
            "evidence",
            candidate_uid=candidate_uid,
        )

    def test_failure_before_candidate_is_saved_remains_an_error(self):
        candidate_uid = "b" * 32
        store = SimpleNamespace()
        store.submit = unittest.mock.Mock(side_effect=RuntimeError("not saved"))
        store.read = unittest.mock.Mock(
            return_value=SimpleNamespace(accepted_candidate_uids=[])
        )
        queue = SimpleNamespace()
        queue.read_outbox = unittest.mock.Mock(return_value=None)

        with patch("rightmemory.mcp.AsyncUpdateStore", return_value=store), patch(
            "rightmemory.mcp.UpdateQueueStore",
            return_value=queue,
        ), patch(
            "rightmemory.mcp.uuid.uuid4",
            return_value=SimpleNamespace(hex=candidate_uid),
        ):
            with self.assertRaises(RuntimeError):
                DefaultMcpBackend(Path("/memory")).submit_update(
                    "session",
                    "evidence",
                )


class UpdateRecoveryAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_clean_root_has_no_recovery_alert(self):
        summary = collect_update_recovery_summary(self.root)

        self.assertFalse(summary.required)
        self.assertEqual(summary.local_candidates, 0)
        self.assertEqual(summary.synchronized_candidates, 0)
        self.assertIsNone(summary.warning())

    def test_counts_local_manual_recovery_without_mutating_state(self):
        state_root = self.root / ".runtime" / "async" / "update"
        state_root.mkdir(parents=True)
        state = AsyncUpdateState(
            status=STATUS_MANUAL_RECOVERY,
            session_id="session-one",
            role="update",
            current_batch=[
                AsyncUpdateJob(
                    id=1,
                    candidate_uid="1" * 32,
                    message="one",
                    submitted_at="2026-08-18T00:00:00+00:00",
                )
            ],
            pending=[
                AsyncUpdateJob(
                    id=2,
                    candidate_uid="2" * 32,
                    message="two",
                    submitted_at="2026-08-18T00:01:00+00:00",
                )
            ],
            accepted_candidate_uids=["1" * 32, "2" * 32],
            next_id=3,
        )
        path = state_root / "session-one.json"
        content = json.dumps(asdict(state), ensure_ascii=False, indent=2) + "\n"
        path.write_text(content, encoding="utf-8")

        summary = collect_update_recovery_summary(self.root)

        self.assertEqual(summary.local_candidates, 2)
        self.assertEqual(summary.local_sessions, 1)
        self.assertTrue(summary.required)
        self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_counts_synchronized_manual_recovery(self):
        candidates = (
            UpdateCandidate(
                uid="4" * 32,
                session_id="session-four",
                display_id=1,
                message="four",
                submitted_at="2026-08-18T00:00:00+00:00",
            ),
            UpdateCandidate(
                uid="5" * 32,
                session_id="session-five",
                display_id=1,
                message="five",
                submitted_at="2026-08-18T00:01:00+00:00",
            ),
        )
        store = UpdateQueueStore(self.root)
        for candidate in candidates:
            store.write_candidate(candidate)
        store.write_recovery(
            UpdateQueueRecovery(
                batch_id=update_candidate_batch_id(candidates),
                candidate_uids=tuple(candidate.uid for candidate in candidates),
                attempts=2,
                reason_code="processing_failed",
                retry_at=None,
                manual_recovery=True,
            )
        )

        summary = collect_update_recovery_summary(self.root)

        self.assertEqual(summary.synchronized_candidates, 2)
        self.assertTrue(summary.required)

    def test_malformed_local_state_is_reported(self):
        state_root = self.root / ".runtime" / "async" / "update"
        state_root.mkdir(parents=True)
        (state_root / "broken.json").write_text("{", encoding="utf-8")

        with self.assertRaises(ValueError):
            collect_update_recovery_summary(self.root)


class McpEntrypointTests(unittest.TestCase):
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
