import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.retrieve_context import (
    RetrieveContextStore,
    _changed_root_memory_paths,
    load_daily_snapshot,
    root_memory_paths,
)
from rightmemory.retrieve_selection import RetrieveDeliveryCoverage


class RetrieveContextPathTests(unittest.TestCase):
    def test_root_memory_paths_returns_graph_roots_and_fixed_correction_collections(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (root / "MEMORY_agent-corrections-writing.md").write_text(
                "# Corrections\n",
                encoding="utf-8",
            )
            (root / "MEMORY_agent-corrections-design.md").write_text(
                "# Corrections\n",
                encoding="utf-8",
            )
            (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(
                root_memory_paths(root),
                [
                    "MEMORY.md",
                    "PURSUITS.md",
                    "MEMORY_agent-corrections-writing.md",
                    "MEMORY_agent-corrections-design.md",
                ],
            )

    def test_daily_snapshot_records_present_agent_correction_collections(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "MEMORY_agent-corrections-writing.md").write_text(
                "# Corrections\n\n"
                "### First entry\n\nFirst body.\n\n"
                "```md\n### Not an entry\n```\n\n"
                "### Second entry\n\nSecond body.\n",
                encoding="utf-8",
            )

            snapshot = load_daily_snapshot(root)

        self.assertEqual(
            snapshot.paths,
            ["MEMORY.md", "MEMORY_agent-corrections-writing.md"],
        )

    def test_memory_diff_tracks_fixed_agent_correction_collections(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            correction = root / "MEMORY_agent-corrections-design.md"
            correction.write_text(
                "# Corrections\n\n### Existing entry\n\nOld body.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "MEMORY.md", correction.name], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=RightMemory Tests",
                    "-c",
                    "user.email=rightmemory-tests@example.invalid",
                    "commit",
                    "-qm",
                    "initial",
                ],
                cwd=root,
                check=True,
            )
            old_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            correction.write_text(
                "# Corrections\n\n"
                "### New first entry\n\nNew body.\n\n"
                "### Existing entry\n\nCurrent body.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", correction.name], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=RightMemory Tests",
                    "-c",
                    "user.email=rightmemory-tests@example.invalid",
                    "commit",
                    "-qm",
                    "update correction",
                ],
                cwd=root,
                check=True,
            )
            new_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            changed = _changed_root_memory_paths(root, old_commit, new_commit)

        self.assertEqual(changed, ["MEMORY_agent-corrections-design.md"])


class RetrieveContextStoreTests(unittest.TestCase):
    def test_retrieve_context_store_persists_native_history_and_cursors(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = RetrieveContextStore(root)
            history = json.dumps(
                [
                    {
                        "kind": "response",
                        "parts": [{"part_kind": "tool-return", "content": "detail body"}],
                    }
                ]
            ).encode()

            state = store.load("retrieve-a")
            self.assertIsNone(state.model_history_json)
            self.assertIsNone(state.delivered_memory_commit)

            store.record_success(
                "retrieve-a",
                memory_commit="abc123",
                model_history_json=history,
                visible_recent_candidates={"candidate-key": "update-a:1"},
                delivery=RetrieveDeliveryCoverage(local_items={"alpha": "hash-a"}),
            )
            state = store.load("retrieve-a")

        self.assertEqual(state.delivered_memory_commit, "abc123")
        self.assertEqual(json.loads(state.model_history_json or b"null"), json.loads(history))
        self.assertEqual(
            state.visible_recent_candidates,
            {"candidate-key": "update-a:1"},
        )
        self.assertEqual(state.delivery_coverage.local_items, {"alpha": "hash-a"})

    def test_retrieve_context_store_reset_removes_complete_session_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = RetrieveContextStore(Path(tempdir))
            store.record_success(
                "retrieve-a",
                memory_commit="abc123",
                model_history_json=b"[]",
                visible_recent_candidates={},
            )

            self.assertTrue(store.reset("retrieve-a"))
            state = store.load("retrieve-a")

        self.assertIsNone(state.model_history_json)
        self.assertIsNone(state.delivered_memory_commit)

    def test_legacy_synthetic_turn_state_is_not_silently_reused(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            path = (
                root
                / ".runtime"
                / "retrieve_context"
                / "sessions"
                / "retrieve-a.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "session_id": "retrieve-a",
                        "delivered_memory_commit": "abc123",
                        "turns": [{"query": "old", "answer": "old"}],
                        "delivery_coverage": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported field.*turns"):
                RetrieveContextStore(root).load("retrieve-a")
