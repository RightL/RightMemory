import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.provider_threads import ProviderThreadStore


class ProviderThreadStoreTests(unittest.TestCase):
    def test_thread_lease_is_exclusive_and_release_keeps_lock_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderThreadStore(Path(tempdir))
            lease = store.acquire_lease("codex", "thread-1")
            path = store.lease_path("codex", "thread-1")
            try:
                self.assertTrue(path.exists())
                self.assertIsNone(store.try_acquire_lease("codex", "thread-1"))
            finally:
                lease.release()

            self.assertTrue(path.exists())
            reacquired = store.try_acquire_lease("codex", "thread-1")
            self.assertIsNotNone(reacquired)
            reacquired.release()
            self.assertTrue(store.delete_lease("codex", "thread-1"))
            self.assertFalse(path.exists())

    def test_atomic_temp_name_does_not_repeat_hashed_destination_name(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderThreadStore(Path(tempdir))
            replacements = []
            real_replace = os.replace

            def capture_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                real_replace(source, destination)

            with patch("rightmemory.provider_threads.os.replace", side_effect=capture_replace):
                store.record_created(
                    provider="codex",
                    provider_session_id="thread-1",
                    role="retrieve",
                    rightmemory_session_id="session-1",
                    policy="persistent",
                    created_at="2026-07-17T00:00:00+00:00",
                )

            source, destination = replacements[-1]
            expected = store.path("codex", "thread-1")

        self.assertEqual(destination, expected)
        self.assertEqual(source.parent, destination.parent)
        self.assertNotIn(destination.name, source.name)
        self.assertRegex(source.name, r"^\.\d+\.[0-9a-f]{32}\.tmp$")

    @unittest.skipUnless(os.name == "nt", "Windows path-length regression")
    def test_records_thread_under_long_isolated_operation_state_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir)
            operation_key = "a" * 64
            provider_session_id = "thread-1"
            unpadded_root = base / "memory" / ".runtime" / "operations" / "state" / operation_key
            unpadded_path = ProviderThreadStore(unpadded_root).path("codex", provider_session_id)
            padding_length = 240 - len(str(unpadded_path)) - 1
            self.assertGreater(padding_length, 0)
            state_root = (
                base
                / ("p" * padding_length)
                / "memory"
                / ".runtime"
                / "operations"
                / "state"
                / operation_key
            )
            store = ProviderThreadStore(state_root)

            store.record_created(
                provider="codex",
                provider_session_id=provider_session_id,
                role="update",
                rightmemory_session_id="session-1",
                policy="persistent",
                created_at="2026-07-17T00:00:00+00:00",
            )
            path = store.path("codex", provider_session_id)

            self.assertTrue(path.exists())
            self.assertEqual(len(str(path)), 240)
            self.assertGreaterEqual(len(str(path)) + 32, 260)

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
        self.assertRegex(path.name, r"^[0-9a-f]{32}\.json$")
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
