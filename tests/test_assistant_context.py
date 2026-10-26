from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant_context import prepare_retrieve_context
from rightmemory.async_update import AsyncUpdateJob, AsyncUpdateState, AsyncUpdateStore
from rightmemory.recent_submitted import collect_recent_submitted_memory_readonly
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.shared_view_files import FileViewPullResult
from rightmemory.sync import SyncResult
from rightmemory.update_queue import UpdateCandidate, UpdateQueueStore


class AssistantRetrieveContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

    def prepare(self, **kwargs):
        # Injection keeps these boundary tests independent of role text.
        return prepare_retrieve_context(self.root, instructions="test instructions", **kwargs)

    def test_prepares_native_sources_without_copying_the_root(self) -> None:
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n## Details {F#details}\n\n"
            "## Notes {M#notes}\n\n## Skill {S#review}\n",
            encoding="utf-8",
        )
        for name, content in {
            "MEMORY_details.md": "- `fact` Evidence. → []\n",
            "MEMORY_notes.md": "# Notes\n",
            "MEMORY_SKILL_review.md": "# Review\n",
            "MEMORY_agent-corrections-writing.md": "# Writing\n",
        }.items():
            (self.root / name).write_text(content, encoding="utf-8")

        result = self.prepare()

        self.assertEqual(Path(result["memory_root"]), self.root.resolve())
        self.assertTrue(result["live_files"])
        self.assertIsNone(result["memory_commit"])
        self.assertEqual(
            {Path(path).name for path in result["source_files"]},
            {
                "MEMORY.md", "PURSUITS.md", "MEMORY_details.md", "MEMORY_notes.md",
                "MEMORY_SKILL_review.md", "MEMORY_agent-corrections-writing.md",
            },
        )
        self.assertTrue(all(Path(path).parent == self.root for path in result["source_files"]))
        self.assertTrue(Path(result["pending_file"]).is_file())
        self.assertTrue(Path(result["instructions_file"]).is_file())
        self.assertEqual(len(list(Path(result["pending_file"]).parent.iterdir())), 2)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["sync"]["status"], "disabled")
        self.assertIsInstance(json.loads(json.dumps(result)), dict)

    def test_refreshes_sync_then_file_views_without_worker_calls(self) -> None:
        calls = []

        def refresh():
            calls.append("sync")
            return SyncResult("fresh", "test")

        def pull(root):
            calls.append("file_views")
            self.assertEqual(root, self.root)
            return [FileViewPullResult("remote", "unavailable", "test")]

        with (
            patch("rightmemory.assistant_context.SyncManager.refresh_for_retrieve", side_effect=refresh),
            patch("rightmemory.assistant_context.pull_all_file_views", side_effect=pull),
            patch.object(AsyncUpdateStore, "wake_worker", side_effect=AssertionError("worker called")),
            patch.object(AsyncUpdateStore, "_worker_snapshot", side_effect=AssertionError("worker inspected")),
        ):
            result = self.prepare()

        self.assertEqual(calls, ["sync", "file_views"])
        self.assertEqual(result["sync"]["status"], "fresh")
        self.assertEqual(result["file_views"][0]["status"], "unavailable")

    def test_sync_failure_preserves_available_local_sources(self) -> None:
        with patch(
            "rightmemory.assistant_context.SyncManager.refresh_for_retrieve",
            side_effect=OSError("offline"),
        ):
            result = self.prepare()

        self.assertEqual(result["sync"]["status"], "error")
        self.assertEqual(len(result["source_files"]), 2)
        self.assertTrue(Path(result["pending_file"]).is_file())

    def test_context_records_the_observed_git_version(self) -> None:
        commit = "a" * 40
        with patch("rightmemory.assistant_context.current_memory_head", return_value=commit):
            result = self.prepare()
        self.assertEqual(result["memory_commit"], commit)
        self.assertTrue(result["live_files"])

    def test_pending_snapshot_filters_receipts_and_has_an_independent_path(self) -> None:
        queue = UpdateQueueStore(self.root)
        queue.write_outbox(_candidate(1))
        queue.write_candidate(_candidate(2))

        first = self.prepare(excluded_candidate_uids=[_candidate(1).uid])
        second = self.prepare()

        self.assertEqual(first["pending_count"], 1)
        self.assertEqual(second["pending_count"], 2)
        self.assertNotEqual(first["pending_file"], second["pending_file"])
        self.assertTrue(Path(first["pending_file"]).is_file())

    def test_mf_imports_point_to_the_existing_validated_dist(self) -> None:
        (self.root / "MEMORY.md").write_text("# Remote {MF#remote}\n", encoding="utf-8")
        package = self.root / ".runtime" / "shared_views" / "imports" / "remote"
        (package / "dist").mkdir(parents=True)
        files = {
            "view.md": "# Remote\n",
            "recipe.toml": 'version = 1\nview_id = "remote"\nkind = "file"\n',
            "rightmemory-shared-view.toml": 'version = 2\nview_id = "remote"\nkind = "file"\n',
            "dist/manifest.toml": (
                'version = 2\nview_id = "remote"\ndocument_kind = "rightmemory-memory"\n'
            ),
            "dist/MEMORY.md": "# Published {#published} → []\n",
        }
        for relative, content in files.items():
            (package / relative).write_text(content, encoding="utf-8")

        result = self.prepare()

        self.assertEqual(result["mf_imports"], {"remote": str(package / "dist")})
        (package / "dist" / "manifest.toml").unlink()
        self.assertEqual(self.prepare()["mf_imports"], {})

    def test_missing_root_is_not_created(self) -> None:
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError):
            prepare_retrieve_context(missing, instructions="test instructions")
        self.assertFalse(missing.exists())


class ReadonlyRecentSubmissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.store = AsyncUpdateStore(self.root, "update")

    def write_state(self, *, operation_id=None) -> Path:
        self.store._write(
            "session",
            AsyncUpdateState(
                status="running",
                phase="running",
                session_id="session",
                role="update",
                pid=999999,
                current_operation_id=operation_id,
                current_batch=[_job(1)],
                pending=[_job(2)],
                next_id=3,
            ),
        )
        return self.store._state_path("session")

    def test_reads_current_pending_and_both_queue_lanes_without_worker_recovery(self) -> None:
        state_path = self.write_state()
        before = state_path.read_bytes()
        queue = UpdateQueueStore(self.root)
        queue.write_candidate(_candidate(1))
        queue.write_outbox(_candidate(1))
        queue.write_candidate(_candidate(3))
        queue.write_outbox(_candidate(4))

        with (
            patch.object(AsyncUpdateStore, "_read_checked_locked", side_effect=AssertionError("mutating read")),
            patch.object(AsyncUpdateStore, "_worker_snapshot", side_effect=AssertionError("worker inspected")),
        ):
            entries = collect_recent_submitted_memory_readonly(self.root)

        self.assertEqual([entry.candidate_uid for entry in entries], [_candidate(i).uid for i in range(1, 5)])
        self.assertEqual(state_path.read_bytes(), before)

    def test_terminal_operation_filters_unacknowledged_batch_from_every_lane(self) -> None:
        operations = SemanticOperationStore(self.root)
        operation_id = "completed-update"
        operations.begin(operation_id, {"role": "update"})
        operations.prepare_outcome(operation_id, output="", start_commit="a" * 40, changed_paths=[])
        operations.complete_no_change(operation_id)
        state_path = self.write_state(operation_id=operation_id)
        before = state_path.read_bytes()
        queue = UpdateQueueStore(self.root)
        queue.write_outbox(_candidate(1))
        queue.write_candidate(_candidate(1))

        entries = collect_recent_submitted_memory_readonly(self.root)

        self.assertEqual([entry.candidate_uid for entry in entries], [_candidate(2).uid])
        self.assertEqual(state_path.read_bytes(), before)
        self.assertIsNotNone(queue.read_outbox(_candidate(1).uid))
        self.assertIsNotNone(queue.read_candidate(_candidate(1).uid))

    def test_explicit_receipt_exclusion_covers_all_lanes(self) -> None:
        self.write_state()
        queue = UpdateQueueStore(self.root)
        queue.write_outbox(_candidate(1))
        queue.write_candidate(_candidate(2))
        queue.write_outbox(_candidate(3))

        entries = collect_recent_submitted_memory_readonly(
            self.root, excluded_candidate_uids=[_candidate(1).uid, _candidate(3).uid]
        )

        self.assertEqual([entry.candidate_uid for entry in entries], [_candidate(2).uid])

    def test_readonly_outbox_does_not_reap_temporary_artifacts(self) -> None:
        queue = UpdateQueueStore(self.root)
        queue.outbox_root.mkdir(parents=True)
        temporary = queue.outbox_root / f".999999.{('a' * 32)}.tmp"
        temporary.write_bytes(b"{}")

        with patch("rightmemory.update_queue.process_exists", side_effect=AssertionError("process inspected")):
            entries = collect_recent_submitted_memory_readonly(self.root)

        self.assertEqual(entries, [])
        self.assertTrue(temporary.is_file())

    def test_malformed_state_is_not_silently_dropped(self) -> None:
        self.store.root.mkdir(parents=True)
        (self.store.root / "session.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            collect_recent_submitted_memory_readonly(self.root)


def _candidate(number: int) -> UpdateCandidate:
    return UpdateCandidate(
        uid=f"{number:032x}",
        session_id="session",
        display_id=number,
        message="test evidence",
        submitted_at=f"2026-05-19T00:00:{number:02d}+00:00",
    )


def _job(number: int) -> AsyncUpdateJob:
    candidate = _candidate(number)
    return AsyncUpdateJob(candidate.display_id, candidate.uid, candidate.message, candidate.submitted_at)


if __name__ == "__main__":
    unittest.main()
