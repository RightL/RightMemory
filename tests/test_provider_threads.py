import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.provider_threads import ProviderThreadStore


class ProviderThreadStoreTests(unittest.TestCase):
    def test_records_owned_thread_under_hashed_provider_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ProviderThreadStore(root)

            created = store.record_created(
                provider="codex",
                provider_session_id="thread/with/unsafe/path",
                role="retrieve",
                rightmemory_session_id="session-1",
                policy="persistent",
                created_at="2026-07-17T00:00:00+00:00",
            )
            store.record_success(
                "codex",
                created.provider_session_id,
                activity_at="2026-07-17T00:01:00+00:00",
            )
            loaded = store.load("codex", created.provider_session_id)
            path = store.path("codex", created.provider_session_id)

        self.assertEqual(path.parent.name, "codex")
        self.assertRegex(path.name, r"^[0-9a-f]{64}\.json$")
        self.assertNotIn("thread", path.name)
        self.assertEqual(loaded.last_successful_activity_at, "2026-07-17T00:01:00+00:00")
        self.assertEqual(loaded.status, "active")

    def test_rejects_reassigning_provider_thread_to_another_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderThreadStore(Path(tempdir))
            store.record_created(
                provider="codex",
                provider_session_id="thread-1",
                role="retrieve",
                rightmemory_session_id="session-1",
                policy="persistent",
                created_at="2026-07-17T00:00:00+00:00",
            )

            with self.assertRaises(ValueError):
                store.record_created(
                    provider="codex",
                    provider_session_id="thread-1",
                    role="retrieve",
                    rightmemory_session_id="session-2",
                    policy="persistent",
                    created_at="2026-07-17T00:00:00+00:00",
                )

    def test_scan_reports_invalid_timestamp_without_touching_record(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            path = root / ".runtime" / "agent_cli_threads" / "codex" / "broken.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "codex",
                        "provider_session_id": "thread-1",
                        "role": "retrieve",
                        "rightmemory_session_id": "session-1",
                        "policy": "persistent",
                        "created_at": "not-a-time",
                        "last_successful_activity_at": None,
                        "status": "active",
                        "last_delete_attempt_at": None,
                        "last_delete_error": None,
                    }
                ),
                encoding="utf-8",
            )

            scan = ProviderThreadStore(root).scan("codex")

            self.assertEqual(scan.records, ())
            self.assertEqual(len(scan.malformed), 1)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
