import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    current_memory_head,
    format_current_material_block,
    format_memory_diff_block,
    format_recent_submitted_context_block,
    format_updated_material_block,
    load_daily_snapshot,
    memory_diff_since,
    root_memory_paths,
)
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry
from rightmemory.retrieve_selection import RetrieveDeliveryCoverage


class RetrieveContextSnapshotTests(unittest.TestCase):
    def test_daily_snapshot_renders_both_roots_without_linked_or_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nroot body\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits {#pursuits}\n\nlive body\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("## Detail {#detail}\n", encoding="utf-8")
            (root / "PURSUIT_detail.md").write_text("## Pursuit Detail {#p-detail}\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("# Skill Body\n", encoding="utf-8")
            (root / ".runtime" / "shared_views" / "imports" / "mf-one").mkdir(parents=True)
            (root / ".runtime" / "shared_views" / "imports" / "mf-one" / "MEMORY.md").write_text(
                "external\n",
                encoding="utf-8",
            )

            snapshot = load_daily_snapshot(root, now=datetime(2026, 6, 29, tzinfo=UTC))

        self.assertEqual(snapshot.day, "2026-06-29")
        self.assertEqual(snapshot.scope, "rightmemory-roots-v2")
        self.assertEqual(snapshot.paths, ["MEMORY.md", "PURSUITS.md"])
        self.assertTrue(snapshot.text.startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("===== MEMORY.md =====", snapshot.text)
        self.assertIn("===== PURSUITS.md =====", snapshot.text)
        self.assertIn("# Root {#root}", snapshot.text)
        self.assertIn("# Pursuits {#pursuits}", snapshot.text)
        self.assertNotIn("MEMORY_detail.md", snapshot.text)
        self.assertNotIn("PURSUIT_detail.md", snapshot.text)
        self.assertNotIn("MEMORY_SKILL_demo.md", snapshot.text)
        self.assertNotIn(".runtime/shared_views/imports", snapshot.text)
        self.assertNotIn("2026-06-29", snapshot.text)

    def test_daily_snapshot_reuses_same_day_text_even_when_memory_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root {#root}\n\nfirst\n", encoding="utf-8")

            first = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))
            (root / "MEMORY.md").write_text("# Root {#root}\n\nsecond\n", encoding="utf-8")
            second = load_daily_snapshot(root, now=datetime(2026, 6, 18, tzinfo=UTC))

        self.assertEqual(first.text, second.text)
        self.assertIn("first", second.text)
        self.assertNotIn("second", second.text)

    def test_root_memory_paths_returns_only_document_roots(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

            self.assertEqual(root_memory_paths(root), ["MEMORY.md", "PURSUITS.md"])


class RetrieveContextDiffTests(unittest.TestCase):
    def test_memory_diff_since_returns_both_root_diffs_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Root\n\nfirst\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n\nactive\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("old detail\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("old skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "PURSUITS.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "initial memory")
            base = current_memory_head(root)

            (root / "MEMORY.md").write_text("# Root\n\nsecond\n", encoding="utf-8")
            (root / "PURSUITS.md").write_text("# Pursuits\n\nblocked\n", encoding="utf-8")
            (root / "MEMORY_detail.md").write_text("new detail\n", encoding="utf-8")
            (root / "MEMORY_SKILL_demo.md").write_text("new skill\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md", "PURSUITS.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
            self._git(root, "commit", "-m", "update memory")
            head = current_memory_head(root)

            diff = memory_diff_since(root, base, head)

        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", diff)
        self.assertIn("diff --git a/PURSUITS.md b/PURSUITS.md", diff)
        self.assertIn("+second", diff)
        self.assertIn("+blocked", diff)
        self.assertNotIn("MEMORY_detail.md", diff)
        self.assertNotIn("MEMORY_SKILL_demo.md", diff)

    def test_format_memory_diff_block_omits_empty_diff(self):
        self.assertEqual(format_memory_diff_block(""), "")
        block = format_memory_diff_block("diff --git a/MEMORY.md b/MEMORY.md\n")
        self.assertIn("Apply this patch mentally", block)
        self.assertIn("diff --git a/MEMORY.md b/MEMORY.md", block)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


class RetrieveContextRequestTests(unittest.TestCase):
    def test_request_text_preserves_context_parts_and_places_query_last(self):
        text = build_retrieve_request_text(
            context_parts=[
                "Daily RightMemory root snapshot\n===== MEMORY.md =====\n# Root\n",
                "# RightMemory root changes since previous retrieve turn\n\n```diff\n+beta\n```",
                "# Recent submitted RightMemory candidates\n\nremember gamma",
            ],
            query="find gamma",
        )

        self.assertTrue(text.startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("# RightMemory root changes since previous retrieve turn", text)
        self.assertIn("Recent submitted RightMemory candidates", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind gamma"))

    def test_request_text_omits_empty_context_parts(self):
        text = build_retrieve_request_text(
            context_parts=[
                "Daily RightMemory root snapshot\n===== MEMORY.md =====\n# Root\n",
                "",
            ],
            query="find root",
        )

        self.assertNotIn("RightMemory root changes since previous retrieve turn", text)
        self.assertNotIn("Recent submitted RightMemory candidates", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind root"))

    def test_resumed_request_can_contain_only_updates_and_query(self):
        text = build_retrieve_request_text(
            context_parts=[
                "# RightMemory root changes since previous retrieve turn\n\n```diff\n+new\n```",
            ],
            query="find new",
        )

        self.assertTrue(text.startswith("# RightMemory root changes since previous retrieve turn\n"))
        self.assertNotIn("Daily RightMemory root snapshot", text)
        self.assertTrue(text.rstrip().endswith("# Query\n\nfind new"))

    def test_recent_submitted_context_block_omits_empty_entries(self):
        self.assertEqual(format_recent_submitted_context_block([]), "")
        block = format_recent_submitted_context_block(
            [
                RecentSubmittedMemoryEntry(
                    update_session_id="update-a",
                    candidate_id=1,
                    submitted_at="2026-06-18T00:00:00+00:00",
                    message="remember delta",
                )
            ]
        )
        self.assertTrue(block.startswith("# Recent submitted RightMemory candidates"))
        self.assertIn("remember delta", block)

    def test_recent_submitted_context_reports_removed_candidates_without_bodies(self):
        block = format_recent_submitted_context_block(
            [],
            no_longer_pending=["update-a:1"],
        )

        self.assertIn("No longer pending:", block)
        self.assertIn("`update-a:1`", block)

    def test_material_blocks_are_model_facing_without_delivery_bookkeeping(self):
        updated = format_updated_material_block(["local item `detail-id`"])
        current = format_current_material_block("## Detail {#detail-id}\n\nCurrent body.")

        self.assertIn("changed since they were last returned", updated)
        self.assertIn("local item `detail-id`", updated)
        self.assertIn("Current retrieval material", current)
        self.assertNotIn("already_returned", updated + current)
        self.assertNotIn("avoid_repeats", updated + current)

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
