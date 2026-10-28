from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.assistant_context import prepare_retrieve_context
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.shared_view_files import FileViewPullResult
from rightmemory.sync import SyncResult


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
        self.assertTrue(Path(result["learning_index_file"]).is_file())
        self.assertTrue(Path(result["instructions_file"]).is_file())
        self.assertEqual(len(list(Path(result["learning_index_file"]).parent.iterdir())), 2)
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
        self.assertTrue(Path(result["learning_index_file"]).is_file())

    def test_context_records_the_observed_git_version(self) -> None:
        commit = "a" * 40
        with patch("rightmemory.assistant_context.current_memory_head", return_value=commit):
            result = self.prepare()
        self.assertEqual(result["memory_commit"], commit)
        self.assertTrue(result["live_files"])


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


if __name__ == "__main__":
    unittest.main()
