import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.recent_submitted import (
    RecentSubmittedMemoryEntry,
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
