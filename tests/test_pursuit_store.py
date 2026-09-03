import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rightmemory.graph import build_graph_manifest
from rightmemory.pursuit_store import PursuitStore, PursuitStoreError, _PursuitSupervisor
from rightmemory.pursuit_tree import PursuitEdit, apply_operation
from tests.isolated_write_test_base import IsolatedWriteTestBase


class PursuitStoreTests(IsolatedWriteTestBase):
    session_id = "test-editor-session-private-identity"

    def setUp(self):
        super().setUp()
        self._git("config", "core.autocrlf", "false")
        self._git("config", "commit.gpgSign", "false")
        (self.root / "MEMORY.md").write_bytes(
            "# Memory\n\n## Context {#context}\n\n"
            "- `memory-one` Stable context. → [dep:alpha]\n\n"
            "## Other {#other}\n\nUnchanged body.\n".encode("utf-8")
        )
        (self.root / "PURSUITS.md").write_bytes(
            "# Pursuits\n\n## Focus\n\n- `alpha`\n\n"
            "## Alpha {#alpha} → [dep:context]\n\nAlpha body.\n\n"
            "### Child {#alpha-child}\n\nChild body.\n\n"
            "## Beta {#beta}\n\nBeta body.\n".encode("utf-8")
        )
        self._git("add", "MEMORY.md", "PURSUITS.md")
        self._git("commit", "-m", "seed editable map")
        self.initial_head = self._git("rev-parse", "HEAD")
        self.store = PursuitStore(self.root)

    def _apply(self, operation, *, revision=None, session_id=None):
        result = self._pending(operation, revision=revision, session_id=session_id)
        return self._flush_result(result, session_id=session_id)

    def _pending(self, operation, *, revision=None, session_id=None):
        owner = session_id or self.session_id
        return self.store.apply(
            operation,
            revision or self.store.snapshot(owner)["revision"],
            owner,
        )

    def _flush_result(self, result, *, session_id=None):
        flushed = self.store.flush(session_id or self.session_id)
        return {**result, "commit": flushed["commit"], "snapshot": flushed["snapshot"]}

    def _undo(self, result):
        return self._flush_result(self._pending_history("undo", result))

    def _redo(self, result):
        return self._flush_result(self._pending_history("redo", result))

    def _pending_history(self, direction, result):
        return getattr(self.store, direction)(
            result["operation_id"], self.store.snapshot(self.session_id)["revision"], self.session_id,
        )

    def _bytes(self):
        return {path.name: path.read_bytes() for path in self.root.glob("*.md")}

    def _assert_unchanged(self, before, head=None):
        self.assertEqual(self._bytes(), before)
        self.assertEqual(self._git("rev-parse", "HEAD"), head or self.initial_head)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_isolated_cleanup()

    def test_snapshot_contains_order_focus_revision_and_writability(self):
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["root_ids"], ["alpha", "beta"])
        items = {item["id"]: item for item in snapshot["items"]}
        self.assertEqual(items["alpha"]["child_ids"], ["alpha-child"])
        self.assertEqual(items["alpha-child"]["parent_id"], "alpha")
        self.assertEqual(snapshot["focus_ids"], ["alpha"])
        self.assertTrue(snapshot["valid"])
        self.assertTrue(snapshot["writable"])
        self.assertEqual(snapshot["diagnostics"], [])
        self.assertEqual(snapshot["git_head"], self.initial_head)
        self.assertEqual(snapshot["revision"], PursuitStore(self.root).snapshot()["revision"])
        self.assertFalse((self.root / ".runtime").exists())

    def test_rename_lands_exactly_one_commit_and_preserves_memory_bytes(self):
        before = self._bytes()
        previous = self.store.snapshot()
        result = self._apply({"type": "rename", "id": "alpha", "title": "Alpha 中文"}, revision=previous["revision"])
        self.assertTrue(result["undoable"])
        self.assertEqual(result["commit"], self._git("rev-parse", "HEAD"))
        self.assertEqual(result["snapshot"]["git_head"], result["commit"])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertNotEqual(previous["revision"], result["snapshot"]["revision"])
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), before["MEMORY.md"])
        item = next(item for item in result["snapshot"]["items"] if item["id"] == "alpha")
        self.assertEqual(item["title"], "Alpha 中文")
        self.assertEqual(item["body"], "Alpha body.")
        self.assertEqual(item["edges"], [["dep", "context"]])
        self.assertNotIn(self.session_id, self._git("show", "--no-patch", "--format=%B", "HEAD"))
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_isolated_cleanup()

    def test_rename_many_lands_one_commit_and_one_undo_restores_every_title(self):
        before = self._bytes()
        previous = self.store.snapshot()

        result = self._apply(
            {
                "type": "rename_many",
                "renames": [
                    {"id": "alpha", "title": "Alpha renamed"},
                    {"id": "beta", "title": "Beta renamed"},
                ],
            },
            revision=previous["revision"],
        )

        self.assertEqual(result["selected_id"], "alpha")
        self.assertEqual(result["id_remaps"], [])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        items = {item["id"]: item for item in result["snapshot"]["items"]}
        self.assertEqual(items["alpha"]["title"], "Alpha renamed")
        self.assertEqual(items["beta"]["title"], "Beta renamed")

        undone = self._undo(result)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "2")
        self.assertEqual(undone["id_remaps"], [])
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_isolated_cleanup()

    def test_rename_many_history_returns_ordered_id_remaps_in_both_directions(self):
        pursuits = self.root / "PURSUITS.md"
        pursuits.write_bytes(
            pursuits.read_bytes()
            + b"\n## First plain\n\nFirst body.\n\n## Second plain\n\nSecond body.\n"
        )
        self._git("add", "PURSUITS.md")
        self._git("commit", "-m", "seed plain headings")
        self.initial_head = self._git("rev-parse", "HEAD")
        plain = {
            item["title"]: item["id"]
            for item in self.store.snapshot()["items"]
            if item["title"] in {"First plain", "Second plain"}
        }

        renamed = self._apply(
            {
                "type": "rename_many",
                "renames": [
                    {"id": plain["Second plain"], "title": "Second renamed"},
                    {"id": plain["First plain"], "title": "First renamed"},
                ],
            }
        )
        forward = [
            {"from": plain["Second plain"], "to": "second-renamed"},
            {"from": plain["First plain"], "to": "first-renamed"},
        ]
        self.assertEqual(renamed["id_remaps"], forward)
        self.assertEqual(renamed["selected_id"], "second-renamed")
        renamed_bytes = self._bytes()

        undone = self._undo(renamed)
        inverse = [
            {"from": mapping["to"], "to": mapping["from"]}
            for mapping in forward
        ]
        self.assertEqual(undone["id_remaps"], inverse)
        self.assertEqual(
            {item["title"]: item["id"] for item in undone["snapshot"]["items"] if item["title"].endswith("plain")},
            plain,
        )

        redone = self._redo(undone)
        self.assertEqual(redone["id_remaps"], forward)
        self.assertEqual(self._bytes(), renamed_bytes)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "3")
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_isolated_cleanup()

    def test_rename_many_rejects_invalid_or_duplicate_entries_without_partial_write(self):
        before = self._bytes()
        operations = (
            {
                "type": "rename_many",
                "renames": [
                    {"id": "alpha", "title": "Changed"},
                    {"id": "beta", "title": "Bad\nheading"},
                ],
            },
            {
                "type": "rename_many",
                "renames": [
                    {"id": "alpha", "title": "Changed"},
                    {"id": "alpha", "title": "Changed again"},
                ],
            },
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(PursuitStoreError) as caught:
                self._apply(operation)
            self.assertEqual(caught.exception.code, "invalid_operation")
            self._assert_unchanged(before)

    def test_git_line_ending_conversion_is_rejected_before_publication(self):
        before = self._bytes()
        self._git("config", "core.autocrlf", "true")
        self.assertEqual(self._git("status", "--porcelain"), "")
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "delete", "id": "alpha"})
        self.assertEqual(caught.exception.code, "read_only")
        self.assertIn("line-ending", str(caught.exception))
        self._assert_unchanged(before)

    def test_consistent_crlf_root_preserves_bytes_through_delete_and_undo(self):
        self._git("config", "core.autocrlf", "true")
        for path in self.root.glob("*.md"):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self._git("add", "MEMORY.md", "PURSUITS.md")
        before = self._bytes()
        self.assertEqual(self._git("status", "--porcelain"), "")
        deleted = self._apply({"type": "delete", "id": "alpha"})
        self.assertEqual(
            (self.root / "MEMORY.md").read_bytes(),
            before["MEMORY.md"].replace(b"[dep:alpha]", b"[]"),
        )
        self._undo(deleted)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_isolated_cleanup()

    def test_all_operations_validate_and_leave_the_root_clean(self):
        create = self._apply({"type": "create", "parent_id": "alpha-child", "title": "Mixed 中文"})
        item_id = create["selected_id"]
        self.assertIn("PURSUIT_alpha-child.md", self._bytes())
        results = [create]
        for operation in (
            {"type": "edit_body", "id": item_id, "body": "A **free-form** note.\n\n第二行。"},
            {"type": "set_focus", "id": item_id, "focused": True},
            {"type": "move", "id": item_id, "parent_id": "beta", "after_id": None},
            {"type": "delete", "id": item_id},
        ):
            results.append(self._apply(operation))
            self.assertEqual(build_graph_manifest(self.root).errors, [])
            self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(len({result["commit"] for result in results}), 5)
        self.assertNotIn("PURSUIT_alpha-child.md", self._bytes())
        self.assertNotIn(item_id, build_graph_manifest(self.root).items)
        self._assert_isolated_cleanup()

    def test_no_change_does_not_create_a_history_entry(self):
        snapshot = self.store.snapshot()
        result = self._apply({"type": "rename", "id": "alpha", "title": "Alpha"}, revision=snapshot["revision"])
        self.assertIsNone(result["commit"])
        self.assertFalse(result["undoable"])
        self.assertEqual(result["snapshot"]["revision"], snapshot["revision"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self._assert_isolated_cleanup()

    def test_multiple_actions_are_durable_and_visible_before_one_flush_commit(self):
        before = self._bytes()
        renamed = self._pending({"type": "rename", "id": "alpha", "title": "Renamed alpha"})
        created = self._pending({"type": "create", "parent_id": "beta", "title": "New child"})
        focused = self._pending({"type": "set_focus", "id": created["selected_id"], "focused": True})
        for result in (renamed, created, focused):
            self.assertIsNone(result["commit"])
            self.assertTrue(result["undoable"])
            self.assertTrue(result["snapshot"]["pending"])
        self.assertEqual(len({result["operation_id"] for result in (renamed, created, focused)}), 3)
        snapshot = self.store.snapshot(self.session_id)
        items = {item["id"]: item for item in snapshot["items"]}
        self.assertEqual(items["alpha"]["title"], "Renamed alpha")
        self.assertEqual(items[created["selected_id"]]["parent_id"], "beta")
        self.assertIn(created["selected_id"], snapshot["focus_ids"])
        self.assertEqual(snapshot["history"], {
            "undo": [result["operation_id"] for result in (renamed, created, focused)], "redo": [],
        })
        self.assertEqual(self.store.snapshot()["git_head"], self.initial_head)
        self._assert_unchanged(before)

        flushed = self.store.flush(self.session_id, snapshot["revision"])
        self.assertEqual(flushed["commit"], self._git("rev-parse", "HEAD"))
        self.assertFalse(flushed["snapshot"]["pending"])
        self.assertEqual(flushed["snapshot"]["history"], snapshot["history"])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(build_graph_manifest(self.root).errors, [])
        self.assertNotIn(".runtime", self._git("show", "--pretty=", "--name-only", "HEAD"))
        self._assert_isolated_cleanup()

    def test_pending_undo_redo_changes_one_action_and_net_zero_creates_no_commit(self):
        before = self._bytes()
        first = self._pending({"type": "rename", "id": "alpha", "title": "Renamed alpha"})
        second = self._pending({"type": "rename", "id": "beta", "title": "Renamed beta"})
        undone = self._pending_history("undo", second)
        self.assertEqual(undone["operation_id"], second["operation_id"])
        items = {item["id"]: item for item in undone["snapshot"]["items"]}
        self.assertEqual(items["alpha"]["title"], "Renamed alpha")
        self.assertEqual(items["beta"]["title"], "Beta")
        self.assertEqual(undone["snapshot"]["history"], {
            "undo": [first["operation_id"]], "redo": [second["operation_id"]],
        })
        redone = self._pending_history("redo", undone)
        self.assertEqual(redone["operation_id"], second["operation_id"])
        self.assertEqual(
            next(item["title"] for item in redone["snapshot"]["items"] if item["id"] == "beta"),
            "Renamed beta",
        )
        self._pending_history("undo", second)
        self._pending_history("undo", first)
        flushed = self.store.flush(self.session_id)
        self.assertIsNone(flushed["commit"])
        self.assertEqual(flushed["snapshot"]["history"], {
            "undo": [], "redo": [second["operation_id"], first["operation_id"]],
        })
        self._assert_unchanged(before)

    def test_landed_batch_undo_creates_compensation_for_only_last_action(self):
        first = self._pending({"type": "rename", "id": "alpha", "title": "Renamed alpha"})
        second = self._pending({"type": "rename", "id": "beta", "title": "Renamed beta"})
        checkpoint = self.store.flush(self.session_id)["commit"]
        original_commit = self._git("cat-file", "commit", checkpoint)
        undone = self._pending_history("undo", second)
        self.assertIsNone(undone["commit"])
        self.assertEqual(self._git("rev-parse", "HEAD"), checkpoint)
        items = {item["id"]: item for item in undone["snapshot"]["items"]}
        self.assertEqual(items["alpha"]["title"], "Renamed alpha")
        self.assertEqual(items["beta"]["title"], "Beta")
        compensation = self.store.flush(self.session_id)
        self.assertEqual(self._git("rev-parse", "HEAD^"), checkpoint)
        self.assertEqual(self._git("cat-file", "commit", checkpoint), original_commit)
        self.assertEqual(compensation["snapshot"]["history"]["undo"], [first["operation_id"]])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "2")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_undo_redo_cross_batch_boundaries_preserves_action_order(self):
        before = self._bytes()
        first = self._pending({"type": "rename", "id": "alpha", "title": "Renamed alpha"})
        second = self._pending({"type": "rename", "id": "beta", "title": "Renamed beta"})
        first_checkpoint = self.store.flush(self.session_id)["commit"]
        third = self._pending({"type": "edit_body", "id": "alpha", "body": "A later note."})
        self.store.flush(self.session_id)
        edited = self._bytes()
        self.assertEqual(self._git("rev-parse", "HEAD^"), first_checkpoint)
        for action in (third, second, first):
            self._pending_history("undo", action)
        self.store.flush(self.session_id)
        self.assertEqual(self._bytes(), before)
        for action in (first, second, third):
            self._pending_history("redo", action)
        self.store.flush(self.session_id)
        self.assertEqual(self._bytes(), edited)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "4")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_pending_create_parent_child_rename_move_and_history_keep_ids(self):
        parent = self._pending({"type": "create", "parent_id": "beta", "title": "Created parent"})
        parent_id = parent["selected_id"]
        child = self._pending({"type": "create", "parent_id": parent_id, "title": "Created child"})
        child_id = child["selected_id"]
        renamed = self._pending({"type": "rename", "id": child_id, "title": "Renamed child"})
        moved = self._pending({"type": "move", "id": child_id, "parent_id": "alpha", "after_id": None})
        for action in (moved, renamed, child, parent):
            self._pending_history("undo", action)
        self.assertNotIn(parent_id, {item["id"] for item in self.store.snapshot(self.session_id)["items"]})
        for action in (parent, child, renamed, moved):
            self._pending_history("redo", action)
        snapshot = self.store.snapshot(self.session_id)
        items = {item["id"]: item for item in snapshot["items"]}
        self.assertEqual(items[parent_id]["parent_id"], "beta")
        self.assertEqual(items[child_id]["title"], "Renamed child")
        self.assertEqual(items[child_id]["parent_id"], "alpha")
        self.store.flush(self.session_id)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertEqual(build_graph_manifest(self.root).errors, [])

    def test_pending_delete_restores_subtree_backing_files_edges_focus_and_bytes(self):
        self._apply({"type": "create", "parent_id": "alpha-child", "title": "Grandchild"})
        before, base = self._bytes(), self._git("rev-parse", "HEAD")
        self.assertIn("PURSUIT_alpha-child.md", before)
        deleted = self._pending({"type": "delete", "id": "alpha"})
        self.assertEqual(deleted["snapshot"]["root_ids"], ["beta"])
        self.assertEqual(deleted["snapshot"]["focus_ids"], [])
        self.assertTrue(any(reference["kind"] == "edge" for reference in deleted["repaired_references"]))
        self._pending_history("undo", deleted)
        self.assertIsNone(self.store.flush(self.session_id)["commit"])
        self._assert_unchanged(before, base)
        self._pending_history("redo", deleted)
        self.store.flush(self.session_id)
        self.assertNotIn("PURSUIT_alpha-child.md", self._bytes())
        restored = self._pending_history("undo", deleted)
        self.assertTrue(any(reference.get("action") == "restored" for reference in restored["repaired_references"]))
        self.store.flush(self.session_id)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(build_graph_manifest(self.root).errors, [])
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_restart_recovers_acknowledged_pending_actions_and_history(self):
        before = self._bytes()
        renamed = self._pending({"type": "rename", "id": "alpha", "title": "Recovered title"})
        created = self._pending({"type": "create", "parent_id": "beta", "title": "Recovered child"})
        self._pending_history("undo", created)
        pending = self.store.snapshot(self.session_id)
        self._assert_unchanged(before)
        self.store = PursuitStore(self.root)
        recovered = self.store.snapshot(self.session_id)
        for field in ("items", "root_ids", "focus_ids", "revision", "history", "pending"):
            self.assertEqual(recovered[field], pending[field], field)
        self._pending_history("redo", created)
        self.store.flush(self.session_id)
        self.store = PursuitStore(self.root)
        self.assertEqual(self.store.snapshot(self.session_id)["history"]["undo"], [
            renamed["operation_id"], created["operation_id"],
        ])
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertIn(created["selected_id"], build_graph_manifest(self.root).items)

    def test_second_session_cannot_fork_pending_map(self):
        first = self._pending({"type": "rename", "id": "alpha", "title": "Owner title"})
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.apply(
                {"type": "rename", "id": "beta", "title": "Other title"},
                self.store.snapshot()["revision"], "another-session",
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.snapshot(self.session_id)["revision"], first["snapshot"]["revision"])
        self.store.flush(self.session_id)
        self.assertEqual(
            next(item["title"] for item in self.store.snapshot()["items"] if item["id"] == "beta"),
            "Beta",
        )

    def test_external_base_change_preserves_recovery_without_replay(self):
        self._pending({"type": "rename", "id": "alpha", "title": "Pending title"})
        self._append_memory(self.root, "\nExternal committed content.\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "external change while editor has pending work")
        before, head = self._bytes(), self._git("rev-parse", "HEAD")
        for store in (self.store, PursuitStore(self.root)):
            with self.assertRaises(PursuitStoreError) as caught:
                store.flush(self.session_id)
            self.assertEqual((caught.exception.code, caught.exception.status), ("conflict", 409))
            self.assertTrue(store.pending_state()["pending"])
            recovery = store.snapshot(self.session_id)
            self.assertFalse(recovery["writable"])
            self.assertEqual(recovery["error_code"], "conflict")
            self.assertTrue(recovery["recovery"])
            self.assertEqual(
                next(item["title"] for item in recovery["items"] if item["id"] == "alpha"),
                "Pending title",
            )
            other_session = store.snapshot("another-session")
            self.assertFalse(other_session["writable"])
            self.assertEqual(other_session["error_code"], "conflict")
            self.assertTrue(other_session["recovery"])
            self._assert_unchanged(before, head)
        self.assertNotIn(b"Pending title", before["PURSUITS.md"])

    def test_flush_requires_current_pending_revision(self):
        first = self._pending({"type": "rename", "id": "alpha", "title": "First title"})
        second = self._pending({"type": "rename", "id": "beta", "title": "Second title"})
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.flush(self.session_id, first["snapshot"]["revision"])
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self.store.snapshot(self.session_id)["revision"], second["snapshot"]["revision"])
        self.store.flush(self.session_id, second["snapshot"]["revision"])

    def test_rejected_later_action_preserves_acknowledged_pending_state(self):
        accepted = self._pending({"type": "rename", "id": "alpha", "title": "Saved title"})
        with self.assertRaises(PursuitStoreError) as caught:
            self._pending({"type": "rename", "id": "beta", "title": "Invalid\nheading"})
        self.assertEqual(caught.exception.code, "invalid_operation")
        recovered = PursuitStore(self.root).snapshot(self.session_id)
        self.assertEqual(recovered["revision"], accepted["snapshot"]["revision"])
        self.assertEqual(recovered["history"], accepted["snapshot"]["history"])
        self.store.flush(self.session_id)
        titles = {item["id"]: item["title"] for item in self.store.snapshot()["items"]}
        self.assertEqual((titles["alpha"], titles["beta"]), ("Saved title", "Beta"))

    def test_new_action_after_undo_discards_redo_but_preserves_older_actions(self):
        first = self._pending({"type": "rename", "id": "alpha", "title": "First title"})
        abandoned = self._pending({"type": "rename", "id": "beta", "title": "Abandoned title"})
        self._pending_history("undo", abandoned)
        latest = self._pending({"type": "edit_body", "id": "alpha", "body": "New direction."})
        self.assertEqual(latest["snapshot"]["history"], {
            "undo": [first["operation_id"], latest["operation_id"]], "redo": [],
        })
        with self.assertRaises(PursuitStoreError):
            self._pending_history("redo", abandoned)
        self.store.flush(self.session_id)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")

    def test_failed_journal_save_does_not_acknowledge_or_replace_earlier_action(self):
        accepted = self._pending({"type": "rename", "id": "alpha", "title": "Saved title"})
        before = self._bytes()
        with patch.object(self.store._journal, "save", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self._pending({"type": "rename", "id": "beta", "title": "Unacknowledged title"})
        self.store = PursuitStore(self.root)
        recovered = self.store.snapshot(self.session_id)
        self.assertEqual(recovered["revision"], accepted["snapshot"]["revision"])
        self.assertEqual(recovered["history"], accepted["snapshot"]["history"])
        self.assertEqual(
            next(item["title"] for item in recovered["items"] if item["id"] == "beta"), "Beta",
        )
        self._assert_unchanged(before)
        self.store.flush(self.session_id)

    def test_restart_after_publication_before_journal_cleanup_does_not_duplicate_commit(self):
        accepted = self._pending({"type": "rename", "id": "alpha", "title": "Published title"})
        with patch.object(self.store, "_finish_checkpoint", side_effect=OSError("simulated process exit")):
            with self.assertRaisesRegex(PursuitStoreError, "simulated process exit"):
                self.store.flush(self.session_id)
        published = self._git("rev-parse", "HEAD")
        self.assertNotEqual(published, self.initial_head)
        self.store = PursuitStore(self.root)
        recovered = self.store.snapshot(self.session_id)
        self.assertFalse(recovered["pending"])
        self.assertEqual(recovered["history"]["undo"], [accepted["operation_id"]])
        self.assertEqual(
            next(item["title"] for item in recovered["items"] if item["id"] == "alpha"), "Published title",
        )
        self.assertIsNone(self.store.flush(self.session_id)["commit"])
        self.assertEqual(self._git("rev-parse", "HEAD"), published)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_pending_map_preserves_typed_memory_backings_and_shared_connections(self):
        self._append_memory(
            self.root,
            "\n## Evidence {M#evidence} → [dep:alpha]\n\n"
            "## Instructions {S#instructions} → [doc:context]\n\n"
            "## External Files {MF#external-files} → [dep:alpha]\n\n"
            "## External Questions {MQ#external-questions} → [rel:external-files]\n",
        )
        (self.root / "MEMORY_evidence.md").write_bytes(b"# Free-form evidence\n\n- `alpha` ordinary raw prose.\n")
        (self.root / "MEMORY_SKILL_instructions.md").write_bytes(b"# Instructions\n\nReview the available evidence.\n")
        self._git("add", "MEMORY.md", "MEMORY_evidence.md", "MEMORY_SKILL_instructions.md")
        self._git("commit", "-m", "seed typed memory backing fixtures")
        self.initial_head = self._git("rev-parse", "HEAD")
        before = self._bytes()
        self.assertEqual(build_graph_manifest(self.root).errors, [])
        deleted = self._pending({"type": "delete", "id": "alpha"})
        self.store.flush(self.session_id)
        self.assertEqual((self.root / "MEMORY_evidence.md").read_bytes(), before["MEMORY_evidence.md"])
        self.assertEqual((self.root / "MEMORY_SKILL_instructions.md").read_bytes(), before["MEMORY_SKILL_instructions.md"])
        self._pending_history("undo", deleted)
        self.store.flush(self.session_id)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(build_graph_manifest(self.root).errors, [])

    def test_external_commit_after_supervisor_returns_is_not_adopted_as_saved_action_base(self):
        self._pending({"type": "rename", "id": "alpha", "title": "Editor title"})
        original_run = _PursuitSupervisor.run
        external = {}

        def after_publication(supervisor, *args, **kwargs):
            result = original_run(supervisor, *args, **kwargs)
            external["editor_head"] = self._git("rev-parse", "HEAD")
            self._append_memory(self.root, "\nExternal update after editor publication.\n")
            self._git("add", "MEMORY.md")
            self._git("commit", "-m", "external change immediately after editor transaction")
            external["head"] = self._git("rev-parse", "HEAD")
            external["memory"] = (self.root / "MEMORY.md").read_bytes()
            return result

        with patch.object(_PursuitSupervisor, "run", after_publication):
            flushed = self.store.flush(self.session_id)
        self.assertEqual(flushed["commit"], external["editor_head"])
        self.assertEqual(self._git("rev-parse", "HEAD"), external["head"])
        current = self.store.snapshot(self.session_id)
        self.assertFalse(current["pending"])
        self.assertEqual(current["history"], {"undo": [], "redo": []})
        self._pending({"type": "rename", "id": "beta", "title": "Later editor title"})
        self.store.flush(self.session_id)
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), external["memory"])
        self.assertEqual(self._git("rev-parse", "HEAD^"), external["head"])
        titles = {item["id"]: item["title"] for item in self.store.snapshot()["items"]}
        self.assertEqual((titles["alpha"], titles["beta"]), ("Editor title", "Later editor title"))
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_delete_undo_restores_executable_backing_file_mode(self):
        self._apply({"type": "create", "parent_id": "alpha-child", "title": "Grandchild"})
        backing = "PURSUIT_alpha-child.md"
        self._git("update-index", "--chmod=+x", backing)
        self._git("commit", "-m", "mark backing executable")
        before = (self.root / backing).read_bytes()
        self.assertTrue(self._git("ls-tree", "HEAD", "--", backing).startswith("100755 "))
        deleted = self._pending({"type": "delete", "id": "alpha"})
        self.store.flush(self.session_id)
        self.assertEqual(self._git("ls-tree", "HEAD", "--", backing), "")
        self._pending_history("undo", deleted)
        self.store.flush(self.session_id)
        self.assertEqual((self.root / backing).read_bytes(), before)
        self.assertTrue(self._git("ls-tree", "HEAD", "--", backing).startswith("100755 "))
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_net_zero_checkpoint_restarts_batch_duration_for_next_edit(self):
        with patch("rightmemory.pursuit_store.time.time", return_value=1000.0):
            action = self._pending({"type": "rename", "id": "alpha", "title": "Temporary title"})
            self._pending_history("undo", action)
            self.assertIsNone(self.store.flush(self.session_id)["commit"])
        with patch("rightmemory.pursuit_store.time.time", return_value=2000.0):
            self._pending({"type": "rename", "id": "beta", "title": "Later title"})
        self.assertEqual(self.store.pending_state()["started_at"], 2000.0)
        self.store.flush(self.session_id)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "1")

    def test_malformed_operation_type_is_rejected_without_runtime_state(self):
        before = self._bytes()
        for value in (None, [], {}, 1):
            with self.subTest(value=value):
                with self.assertRaises(PursuitStoreError) as caught:
                    self._apply({"type": value})
                self.assertEqual(caught.exception.code, "invalid_operation")
        self._assert_unchanged(before)
        self.assertFalse((self.root / ".runtime").exists())

    def test_stale_revision_after_memory_commit_is_a_conflict(self):
        revision = self.store.snapshot()["revision"]
        self._append_memory(self.root, "\nAnother durable sentence.\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "external memory change")
        before, head = self._bytes(), self._git("rev-parse", "HEAD")
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=revision)
        self.assertEqual((caught.exception.code, caught.exception.status), ("conflict", 409))
        self._assert_unchanged(before, head)

    def test_branch_switch_at_same_commit_invalidates_revision(self):
        revision = self.store.snapshot()["revision"]
        self._git("checkout", "-b", "different-active-branch")
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=revision)
        self.assertEqual(caught.exception.code, "conflict")
        self._assert_unchanged(before)

    def test_untracked_unrelated_unicode_path_is_not_absorbed(self):
        revision = self.store.snapshot()["revision"]
        unrelated = self.root / "other notes 中文.txt"
        unrelated.write_bytes(b"private unfinished work")
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=revision)
        self.assertEqual(caught.exception.code, "dirty_root")
        self.assertEqual(self._bytes(), before)
        self.assertEqual(unrelated.read_bytes(), b"private unfinished work")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("diff", "--cached", "--name-only"), "")

    def test_staged_unrelated_change_is_not_absorbed(self):
        revision = self.store.snapshot()["revision"]
        (self.root / "other.txt").write_bytes(b"staged work")
        self._git("add", "other.txt")
        index_tree = self._git("write-tree")
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=revision)
        self.assertEqual(caught.exception.code, "dirty_root")
        self.assertEqual(self._git("write-tree"), index_tree)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_invalid_root_is_readable_but_cannot_be_edited(self):
        with (self.root / "MEMORY.md").open("ab") as handle:
            handle.write(b"\n## Duplicate {#alpha}\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "external invalid graph")
        snapshot = self.store.snapshot()
        before, head = self._bytes(), self._git("rev-parse", "HEAD")
        self.assertFalse(snapshot["writable"])
        self.assertFalse(snapshot["valid"])
        self.assertTrue(snapshot["diagnostics"])
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "beta", "title": "Changed"}, revision=snapshot["revision"])
        self.assertEqual(caught.exception.code, "invalid_root")
        self._assert_unchanged(before, head)

    def test_candidate_validation_failure_leaves_active_root_unchanged(self):
        before = self._bytes()

        def invalid_edit(candidate, _operation):
            with (candidate / "PURSUITS.md").open("ab") as handle:
                handle.write(b"\n## Duplicate {#alpha}\n")
            return PursuitEdit(("PURSUITS.md",), (), "alpha", "pursuit: invalid fixture")

        with patch("rightmemory.pursuit_store.apply_operation", side_effect=invalid_edit):
            with self.assertRaises(PursuitStoreError) as caught:
                self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self.assertEqual(caught.exception.code, "invalid_operation")
        self._assert_unchanged(before)

    def test_editor_cannot_smuggle_memory_curation_into_a_valid_commit(self):
        before = self._bytes()

        def unsafe_edit(candidate, operation):
            edit = apply_operation(candidate, operation)
            with (candidate / "MEMORY.md").open("ab") as handle:
                handle.write(b"\nUnrelated semantic curation.\n")
            return replace(edit, changed_paths=(*edit.changed_paths, "MEMORY.md"))

        with patch("rightmemory.pursuit_store.apply_operation", side_effect=unsafe_edit):
            with self.assertRaisesRegex(PursuitStoreError, "may change Memory only"):
                self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self._assert_unchanged(before)

    def test_editor_cannot_change_undeclared_paths(self):
        before = self._bytes()

        def unsafe_edit(candidate, operation):
            edit = apply_operation(candidate, operation)
            (candidate / "other.txt").write_bytes(b"unexpected")
            return edit

        with patch("rightmemory.pursuit_store.apply_operation", side_effect=unsafe_edit):
            with self.assertRaisesRegex(PursuitStoreError, "outside its declared operation"):
                self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self._assert_unchanged(before)
        self.assertFalse((self.root / "other.txt").exists())

    def test_publication_rejects_external_head_change_without_overwriting_it(self):
        external = {}

        def race(candidate, operation):
            result = apply_operation(candidate, operation)
            self._append_memory(self.root, "\nExternal committed prose.\n")
            self._git("add", "MEMORY.md")
            self._git("commit", "-m", "external concurrent change")
            external["bytes"] = self._bytes()
            external["head"] = self._git("rev-parse", "HEAD")
            return result

        with patch("rightmemory.pursuit_store.apply_operation", side_effect=race):
            with self.assertRaises(PursuitStoreError) as caught:
                self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self.assertEqual(caught.exception.code, "conflict")
        self._assert_unchanged(external["bytes"], external["head"])

    def test_byte_revision_fence_catches_worktree_bytes_not_reported_by_git(self):
        # Simulate a transient filesystem edit that Git's cached status misses;
        # the independent graph byte fence must still reject publication.
        external = {}
        original_state = self.store._repository_state

        def cached_state():
            return replace(original_state(), dirty_paths=())

        def race(candidate, operation):
            result = apply_operation(candidate, operation)
            self._append_memory(self.root, "\nExternal unindexed prose.\n")
            external["bytes"] = self._bytes()
            return result

        with patch.object(self.store, "_repository_state", side_effect=cached_state), patch(
            "rightmemory.pursuit_store.apply_operation", side_effect=race,
        ):
            with self.assertRaises(PursuitStoreError) as caught:
                self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self._bytes(), external["bytes"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self._assert_isolated_cleanup()

    def test_preexisting_hidden_git_index_changes_are_read_only(self):
        for flag, undo_flag in (("--assume-unchanged", "--no-assume-unchanged"), ("--skip-worktree", "--no-skip-worktree")):
            with self.subTest(flag=flag):
                self._git("update-index", flag, "MEMORY.md")
                snapshot = self.store.snapshot()
                self.assertFalse(snapshot["writable"])
                with self.assertRaises(PursuitStoreError) as caught:
                    self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=snapshot["revision"])
                self.assertEqual(caught.exception.code, "read_only")
                self._git("update-index", undo_flag, "MEMORY.md")

    def test_ignored_untracked_graph_file_cannot_be_overwritten(self):
        (self.root / ".gitignore").write_bytes(b"PURSUIT_alpha-child.md\n")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "external ignore setting")
        backing = self.root / "PURSUIT_alpha-child.md"
        backing.write_bytes(b"# Real untracked memory data\n")
        before, head = self._bytes(), self._git("rev-parse", "HEAD")
        self.assertEqual(self._git("status", "--porcelain"), "")
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "create", "parent_id": "alpha-child", "title": "New child"})
        self.assertEqual(caught.exception.code, "dirty_root")
        self._assert_unchanged(before, head)

    def test_delete_undo_redo_restore_exact_edges_focus_and_bytes(self):
        before = self._bytes()
        deleted = self._apply({"type": "delete", "id": "alpha"})
        deletion_bytes = self._bytes()
        self.assertEqual(deleted["snapshot"]["root_ids"], ["beta"])
        self.assertEqual(deleted["snapshot"]["focus_ids"], [])
        self.assertTrue(any(reference["kind"] == "edge" for reference in deleted["repaired_references"]))
        restored = self._undo(deleted)
        self.assertEqual(self._bytes(), before)
        self.assertNotEqual(restored["commit"], deleted["commit"])
        self.assertTrue(any(reference.get("action") == "restored" for reference in restored["repaired_references"]))
        redone = self._redo(restored)
        self.assertEqual(self._bytes(), deletion_bytes)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "3")
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._undo(redone)
        self.assertEqual(self._bytes(), before)
        self.assertEqual(build_graph_manifest(self.root).errors, [])
        self._assert_isolated_cleanup()

    def test_multiple_undo_redo_use_new_commits_without_rewriting_history(self):
        before = self._bytes()
        first = self._apply({"type": "rename", "id": "alpha", "title": "First change"})
        second = self._apply({"type": "rename", "id": "beta", "title": "Second change"})
        edited = self._bytes()
        undo_second = self._undo(second)
        undo_first = self._undo(first)
        self.assertEqual(self._bytes(), before)
        self._redo(undo_first)
        self._redo(undo_second)
        self.assertEqual(self._bytes(), edited)
        self.assertEqual(self._git("rev-list", "--count", f"{self.initial_head}..HEAD"), "6")
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_history_cannot_undo_another_sessions_action(self):
        result = self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.undo(result["operation_id"], result["snapshot"]["revision"], "another-session")
        self.assertEqual((caught.exception.code, caught.exception.status), ("history_forbidden", 403))
        self._assert_unchanged(before, result["commit"])

    def test_revert_conflict_does_not_change_active_files_or_head(self):
        first = self._apply({"type": "rename", "id": "alpha", "title": "First rename"})
        latest = self._apply({"type": "rename", "id": "alpha", "title": "Later rename"})
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self._undo(first)
        self.assertEqual(caught.exception.code, "history_conflict")
        self._assert_unchanged(before, latest["commit"])

    def test_history_requires_authenticated_action_identity_not_git_revision_syntax(self):
        before = self._bytes()
        for value in ("HEAD", "HEAD~1", "-n", "0" * 40):
            with self.subTest(value=value):
                with self.assertRaises(PursuitStoreError) as caught:
                    self.store.undo(value, self.store.snapshot()["revision"], self.session_id)
                self.assertEqual(caught.exception.code, "history_forbidden")
        self._assert_unchanged(before)

    def test_copied_editor_trailers_do_not_authenticate_another_tree(self):
        result = self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self.store._authenticate_commit(result["commit"], self.session_id)
        message = self._git("show", "--no-patch", "--format=%B", result["commit"])
        self._append_memory(self.root, "\nNot an editor change.\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", message)
        before, forged = self._bytes(), self._git("rev-parse", "HEAD")
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.undo(forged, self.store.snapshot()["revision"], self.session_id)
        self.assertEqual(caught.exception.code, "history_forbidden")
        with self.assertRaises(PursuitStoreError) as caught:
            self.store._authenticate_commit(forged, self.session_id)
        self.assertEqual(caught.exception.code, "history_forbidden")
        self._assert_unchanged(before, forged)

    def test_history_rejects_a_non_editor_commit(self):
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.undo(self.initial_head, self.store.snapshot()["revision"], self.session_id)
        self.assertEqual(caught.exception.code, "history_forbidden")
        self._assert_unchanged(before)

    def test_history_rejects_action_after_external_branch_rewind(self):
        result = self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self._git("reset", "--hard", self.initial_head)
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self.store.undo(result["operation_id"], self.store.snapshot()["revision"], self.session_id)
        self.assertEqual(caught.exception.code, "history_conflict")
        self._assert_unchanged(before)

    def test_history_rejects_repeated_undo_and_external_intervening_commits(self):
        result = self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        undone = self._undo(result)
        before = self._bytes()
        with self.assertRaises(PursuitStoreError) as caught:
            self._undo(result)
        self.assertEqual(caught.exception.code, "history_conflict")
        self._assert_unchanged(before, undone["commit"])
        self._append_memory(self.root, "\nExternal update after undo.\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "external history")
        before, head = self._bytes(), self._git("rev-parse", "HEAD")
        with self.assertRaises(PursuitStoreError) as caught:
            self._redo(undone)
        self.assertEqual(caught.exception.code, "history_conflict")
        self._assert_unchanged(before, head)

    def test_git_hooks_cannot_mutate_candidate_or_published_tree(self):
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-merge"):
            hook = hooks / name
            hook.write_bytes(b"#!/bin/sh\nprintf '\\nHook changed memory.\\n' >> MEMORY.md\n")
            hook.chmod(0o755)
        before_memory = (self.root / "MEMORY.md").read_bytes()
        result = self._apply({"type": "rename", "id": "alpha", "title": "Changed"})
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), before_memory)
        self.assertTrue(result["snapshot"]["writable"])
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_detached_and_in_progress_git_roots_are_read_only(self):
        self._git("checkout", "--detach", self.initial_head)
        snapshot = self.store.snapshot()
        self.assertTrue(snapshot["valid"])
        self.assertFalse(snapshot["writable"])
        with self.assertRaises(PursuitStoreError) as caught:
            self._apply({"type": "rename", "id": "alpha", "title": "Changed"}, revision=snapshot["revision"])
        self.assertEqual(caught.exception.code, "read_only")
        self._git("checkout", "-b", "active-again")
        (self.root / ".git" / "MERGE_HEAD").write_text(self.initial_head + "\n", encoding="ascii")
        self.assertFalse(self.store.snapshot()["writable"])


if __name__ == "__main__":
    unittest.main()
