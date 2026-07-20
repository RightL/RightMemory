import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from rightmemory.agent_cli_cleanup import AgentCliThreadCleanup
from rightmemory.codex_app_server import CodexThreadDeleteResult
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.provider_threads import ProviderThreadStore
from rightmemory.recent_submitted import RecentSubmittedMemoryDeliveryStore, RecentSubmittedMemoryEntry
from rightmemory.retrieve_context import RetrieveContextStore


CREATED = "2026-07-16T00:00:00+00:00"
NOW = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)


class FakeDeleteClient:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.calls: list[list[str]] = []

    def delete_threads(self, thread_ids: list[str]) -> list[CodexThreadDeleteResult]:
        self.calls.append(list(thread_ids))
        return [
            CodexThreadDeleteResult(
                thread_id,
                thread_id not in self.failures,
                "busy" if thread_id in self.failures else None,
            )
            for thread_id in thread_ids
        ]


class AgentCliThreadCleanupTests(unittest.TestCase):
    def _create_thread(
        self,
        root: Path,
        thread_id: str,
        *,
        policy: str = "one-shot",
        session_id: str = "session-1",
        created_at: str = CREATED,
        successful_at: str | None = None,
        provider: str = "codex",
    ) -> None:
        store = ProviderThreadStore(root)
        store.record_created(
            provider=provider,
            provider_session_id=thread_id,
            role="retrieve" if policy == "persistent" else "update",
            rightmemory_session_id=session_id,
            policy=policy,
            created_at=created_at,
        )
        if successful_at is not None:
            store.record_success(provider, thread_id, activity_at=successful_at)

    def test_uses_last_successful_activity_and_creation_fallback_at_boundary(self):
        client = FakeDeleteClient()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "never-succeeded", created_at="2026-07-17T00:00:00+00:00")
            self._create_thread(
                root,
                "recent-success",
                created_at=CREATED,
                successful_at="2026-07-17T00:00:01+00:00",
            )

            result = AgentCliThreadCleanup(root, now=lambda: NOW, client=client).run()

            self.assertEqual(client.calls, [["never-succeeded"]])
            self.assertEqual(result.deleted, 1)
            self.assertEqual(result.skipped, 1)
            self.assertIsNone(ProviderThreadStore(root).load("codex", "never-succeeded"))
            self.assertIsNotNone(ProviderThreadStore(root).load("codex", "recent-success"))

    def test_expired_retrieve_detaches_exact_mapping_and_resets_local_delivery_state(self):
        client = FakeDeleteClient()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "thread-1", policy="persistent")
            ProviderSessionStore(root, "retrieve").save(
                ProviderSessionRecord(
                    provider="codex",
                    provider_session_id="thread-1",
                    role="retrieve",
                    rightmemory_session_id="session-1",
                    created_at=CREATED,
                    updated_at=CREATED,
                )
            )
            RetrieveContextStore(root).record_success(
                "session-1",
                query="old query",
                answer="old answer",
                memory_commit="abc123",
            )
            entry = RecentSubmittedMemoryEntry("update-1", 1, CREATED, "candidate")
            RecentSubmittedMemoryDeliveryStore(root).record_delivered("session-1", [entry])

            result = AgentCliThreadCleanup(root, now=lambda: NOW, client=client).run()

            self.assertEqual(result.deleted, 1)
            self.assertIsNone(ProviderSessionStore(root, "retrieve").load("session-1"))
            self.assertEqual(RetrieveContextStore(root).load("session-1").turns, [])
            self.assertEqual(
                RecentSubmittedMemoryDeliveryStore(root).new_entries("session-1", [entry]),
                [entry],
            )

    def test_delete_failure_is_nonfatal_pending_and_retryable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "thread-1")
            failed = AgentCliThreadCleanup(
                root,
                now=lambda: NOW,
                client=FakeDeleteClient({"thread-1"}),
            ).run()
            pending = ProviderThreadStore(root).load("codex", "thread-1")

            retried = AgentCliThreadCleanup(root, now=lambda: NOW, client=FakeDeleteClient()).run()

            self.assertEqual(failed.pending, 1)
            self.assertIn("busy", failed.errors[0])
            self.assertEqual(pending.status, "delete-pending")
            self.assertEqual(retried.deleted, 1)
            self.assertIsNone(ProviderThreadStore(root).load("codex", "thread-1"))

    def test_new_activity_wins_after_stale_scan(self):
        client = FakeDeleteClient()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "thread-1", policy="persistent")
            cleanup = AgentCliThreadCleanup(root, now=lambda: NOW, client=client)
            original = cleanup._prepare_for_deletion

            def refresh_then_prepare(record, **kwargs):
                ProviderThreadStore(root).record_success(
                    "codex",
                    "thread-1",
                    activity_at="2026-07-17T12:00:00+00:00",
                )
                return original(record, **kwargs)

            with patch.object(cleanup, "_prepare_for_deletion", side_effect=refresh_then_prepare):
                result = cleanup.run()

            self.assertEqual(result.deleted, 0)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(client.calls, [])
            self.assertEqual(
                ProviderThreadStore(root).load("codex", "thread-1").last_successful_activity_at,
                "2026-07-17T12:00:00+00:00",
            )

    def test_moved_mapping_and_unregistered_history_are_not_detached(self):
        client = FakeDeleteClient()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "expired-thread", policy="persistent")
            mapping = ProviderSessionRecord(
                provider="codex",
                provider_session_id="unregistered-current-thread",
                role="retrieve",
                rightmemory_session_id="session-1",
                created_at=CREATED,
                updated_at=CREATED,
            )
            ProviderSessionStore(root, "retrieve").save(mapping)
            RetrieveContextStore(root).record_success(
                "session-1",
                query="keep query",
                answer="keep answer",
                memory_commit="abc123",
            )

            AgentCliThreadCleanup(root, now=lambda: NOW, client=client).run()

            self.assertEqual(ProviderSessionStore(root, "retrieve").load("session-1"), mapping)
            self.assertEqual(len(RetrieveContextStore(root).load("session-1").turns), 1)
            self.assertEqual(client.calls, [["expired-thread"]])

    def test_malformed_and_claude_records_are_left_untouched(self):
        client = FakeDeleteClient()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._create_thread(root, "claude-thread", provider="claude")
            malformed = root / ".runtime" / "agent_cli_threads" / "codex" / "bad.json"
            malformed.parent.mkdir(parents=True, exist_ok=True)
            malformed.write_text("{bad json", encoding="utf-8")

            result = AgentCliThreadCleanup(root, now=lambda: NOW, client=client).run()

            self.assertEqual(result.malformed, 1)
            self.assertEqual(client.calls, [])
            self.assertTrue(malformed.exists())
            self.assertIsNotNone(ProviderThreadStore(root).load("claude", "claude-thread"))


if __name__ == "__main__":
    unittest.main()
