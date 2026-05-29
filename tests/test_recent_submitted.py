import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.recent_submitted import (
    RecentSubmittedMemoryDeliveryStore,
    RecentSubmittedMemoryEntry,
    append_recent_submitted_memory,
    collect_recent_submitted_memory,
    format_recent_submitted_block,
)


class RecentSubmittedMemoryCollectionTests(unittest.TestCase):
    def test_collects_pending_and_current_batch_from_all_update_sessions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._write_state(
                root,
                "update-a",
                pending=[
                    {
                        "id": 2,
                        "message": "remember second submitted item",
                        "submitted_at": "2026-05-19T00:02:00+00:00",
                    }
                ],
            )
            self._write_state(
                root,
                "update-b",
                current_batch=[
                    {
                        "id": 3,
                        "message": "remember active batch item",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    }
                ],
                pending=[
                    {
                        "id": 4,
                        "message": "remember later submitted item",
                        "submitted_at": "2026-05-19T00:01:00+00:00",
                    }
                ],
            )

            entries = collect_recent_submitted_memory(root)

        self.assertEqual(
            [entry.key for entry in entries],
            [
                "update-b:3:2026-05-19T00:00:00+00:00",
                "update-b:4:2026-05-19T00:01:00+00:00",
                "update-a:2:2026-05-19T00:02:00+00:00",
            ],
        )
        self.assertEqual(entries[0].update_session_id, "update-b")
        self.assertEqual(entries[0].candidate_id, 3)
        self.assertEqual(entries[0].message, "remember active batch item")

    def test_collect_ignores_async_worker_state_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store_root = root / ".runtime" / "async" / "update"
            worker_root = store_root / "_worker"
            worker_root.mkdir(parents=True)
            (worker_root / "state.json").write_text(
                json.dumps(
                    {
                        "status": "running",
                        "pid": 123,
                        "started_at": "2026-05-29T00:00:00+00:00",
                        "batch_id": "update-batch-test",
                        "session_ids": ["agent-1"],
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            self._write_state(
                root,
                "agent-1",
                pending=[
                    {
                        "id": 1,
                        "message": "remember real pending item",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    }
                ],
            )

            entries = collect_recent_submitted_memory(root)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].update_session_id, "agent-1")
        self.assertEqual(entries[0].message, "remember real pending item")

    def test_formats_recent_submitted_block_for_retriever(self):
        entries = [
            RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="remember that retriever sees submitted memory",
            )
        ]

        block = format_recent_submitted_block(entries)

        self.assertIn("Recent submitted memory", block)
        self.assertIn("not been consolidated into MEMORY.md yet", block)
        self.assertIn(
            "[update session: update-a | candidate: 1 | submitted_at: 2026-05-19T00:00:00+00:00]",
            block,
        )
        self.assertIn("remember that retriever sees submitted memory", block)

    def test_format_returns_empty_string_when_there_are_no_entries(self):
        self.assertEqual(format_recent_submitted_block([]), "")

    def test_append_returns_original_message_when_there_are_no_entries(self):
        self.assertEqual(append_recent_submitted_memory("retrieve this", []), "retrieve this")

    def test_append_adds_recent_submitted_block_when_entries_exist(self):
        entry = RecentSubmittedMemoryEntry(
            update_session_id="update-a",
            candidate_id=1,
            submitted_at="2026-05-19T00:00:00+00:00",
            message="first",
        )

        message = append_recent_submitted_memory("retrieve this\n", [entry])

        self.assertTrue(message.startswith("retrieve this\n\nRecent submitted memory"))
        self.assertIn("first", message)

    def test_delivery_store_returns_all_entries_then_session_delta(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = RecentSubmittedMemoryDeliveryStore(root)
            first = RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="first",
            )
            second = RecentSubmittedMemoryEntry(
                update_session_id="update-b",
                candidate_id=2,
                submitted_at="2026-05-19T00:01:00+00:00",
                message="second",
            )
            third = RecentSubmittedMemoryEntry(
                update_session_id="update-c",
                candidate_id=3,
                submitted_at="2026-05-19T00:02:00+00:00",
                message="third",
            )

            self.assertEqual(store.new_entries("retrieve-a", [first, second]), [first, second])
            store.record_delivered("retrieve-a", [first, second])

            self.assertEqual(store.new_entries("retrieve-a", [first, second, third]), [third])
            self.assertEqual(store.new_entries("retrieve-b", [first, second, third]), [first, second, third])

            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(state["session_id"], "retrieve-a")
        self.assertEqual(state["delivered"], [first.key, second.key])

    def test_delivery_store_does_not_touch_memory_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_path = root / "MEMORY.md"
            memory_path.write_text("# Memory\n", encoding="utf-8")
            store = RecentSubmittedMemoryDeliveryStore(root)
            entry = RecentSubmittedMemoryEntry(
                update_session_id="update-a",
                candidate_id=1,
                submitted_at="2026-05-19T00:00:00+00:00",
                message="first",
            )

            store.record_delivered("retrieve-a", [entry])

            self.assertEqual(memory_path.read_text(encoding="utf-8"), "# Memory\n")
            self.assertTrue((root / ".runtime" / ".gitignore").exists())

    def test_delivery_store_rejects_malformed_delivered_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"session_id": "retrieve-a", "delivered": [1]}), encoding="utf-8")
            store = RecentSubmittedMemoryDeliveryStore(root)

            with self.assertRaises(ValueError) as caught:
                store.new_entries("retrieve-a", [])

        self.assertIn("recent submitted delivery state must contain string delivered keys", str(caught.exception))

    def test_delivery_store_rejects_non_object_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("[]", encoding="utf-8")
            store = RecentSubmittedMemoryDeliveryStore(root)

            with self.assertRaises(ValueError) as caught:
                store.new_entries("retrieve-a", [])

        self.assertIn("recent submitted delivery state must be an object", str(caught.exception))

    def test_delivery_store_rejects_session_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "recent_submitted" / "retrieve" / "retrieve-a.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"session_id": "other-retrieve", "delivered": []}),
                encoding="utf-8",
            )
            store = RecentSubmittedMemoryDeliveryStore(root)

            with self.assertRaises(ValueError) as caught:
                store.new_entries("retrieve-a", [])

        self.assertIn("recent submitted delivery state session_id mismatch", str(caught.exception))

    def test_collect_raises_for_malformed_update_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "async" / "update" / "broken.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "session_id": "broken",
                        "role": "update",
                        "pending": [],
                        "current_batch": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                collect_recent_submitted_memory(root)

        self.assertIn("async update state must contain integer field: next_id", str(caught.exception))

    def _write_state(self, root: Path, session_id: str, *, pending=None, current_batch=None):
        state_path = root / ".runtime" / "async" / "update" / f"{session_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "session_id": session_id,
                    "role": "update",
                    "phase": "waiting",
                    "started_at": "2026-05-19T00:00:00+00:00",
                    "finished_at": None,
                    "pid": None,
                    "result": None,
                    "error": None,
                    "next_flush_at": "2026-05-19T01:00:00+00:00",
                    "current_batch": current_batch or [],
                    "pending": pending or [],
                    "next_id": 10,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
