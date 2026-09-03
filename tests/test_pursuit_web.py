import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch

from rightmemory.opening_context import build_opening_context
from rightmemory.web.app import create_web_app
from rightmemory.web.service import PursuitBatchLifecycle
from tests.asgi_client import ASGITestClient as TestClient


class PursuitWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self._seed_root(self.root)
        self.app = create_web_app(self.root)
        self.client, self.csrf = self._bootstrap()

    def _bootstrap(self):
        client = TestClient(self.app, request_timeout_seconds=30)
        response = client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        return client, response.json()["csrf_token"]

    @staticmethod
    def _git(root, *args):
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            encoding="utf-8", errors="replace",
        )
        return result.stdout.strip()

    def _seed_root(self, root):
        root.mkdir(parents=True, exist_ok=True)
        (root / ".gitignore").write_text(".runtime/\nother/\n", encoding="utf-8")
        (root / "MEMORY.md").write_text("# Memory\n\n## Context {#context}\n\nStable context.\n", encoding="utf-8")
        (root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Focus\n\n- `alpha`\n\n"
            "## Alpha {#alpha}\n\nA **note**.\n\n"
            "### Child {#child}\n\n## Beta {#beta}\n",
            encoding="utf-8",
        )
        self._git(root, "init", "-q")
        self._git(root, "config", "user.name", "Pursuit Web Test")
        self._git(root, "config", "user.email", "pursuit-web@example.test")
        self._git(root, "config", "core.autocrlf", "false")
        self._git(root, "add", ".gitignore", "MEMORY.md", "PURSUITS.md")
        self._git(root, "commit", "-qm", "initial map")

    def _snapshot(self, client=None):
        response = (client or self.client).get("/api/pursuit-map")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def _operation(self, operation, *, snapshot=None, client=None, csrf=None, **extra):
        snapshot = snapshot or self._snapshot(client)
        return (client or self.client).post(
            "/api/pursuit-map/operations",
            json={"expected_revision": snapshot["revision"], "operation": operation, **extra},
            headers={"x-csrf-token": csrf or self.csrf},
        )

    def _context(self, item_id="alpha", *, snapshot=None, client=None):
        snapshot = snapshot or self._snapshot(client)
        query = urlencode({"item_id": item_id, "expected_revision": snapshot["revision"]})
        return (client or self.client).get(f"/api/pursuit-map/context?{query}")

    def _flush(self, *, snapshot=None):
        response = self.client.post(
            "/api/pursuit-map/flush",
            json={"expected_revision": (snapshot or self._snapshot())["revision"]},
            headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_reads_and_mutations_require_a_bootstrapped_session(self):
        client = TestClient(self.app, request_timeout_seconds=30)
        responses = [
            client.get("/api/pursuit-map"),
            client.get("/api/pursuit-map/context?item_id=alpha&expected_revision=revision"),
        ]
        for endpoint in ("operations", "undo", "redo", "flush", "activity"):
            responses.append(client.post(f"/api/pursuit-map/{endpoint}", json={}))
        for response in responses:
            self.assertEqual(response.status_code, 401)

    def test_all_mutations_require_csrf(self):
        before = self._git(self.root, "rev-parse", "HEAD")
        for endpoint in ("operations", "undo", "redo", "flush", "activity"):
            for headers in ({}, {"x-csrf-token": "wrong"}):
                with self.subTest(endpoint=endpoint, headers=headers):
                    response = self.client.post(f"/api/pursuit-map/{endpoint}", json={}, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["detail"]["message"], "invalid csrf token")
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), before)

    def test_snapshot_contains_ordered_tree_and_revision(self):
        snapshot = self._snapshot()
        self.assertEqual(snapshot["root_ids"], ["alpha", "beta"])
        self.assertEqual(snapshot["focus_ids"], ["alpha"])
        items = {item["id"]: item for item in snapshot["items"]}
        self.assertEqual(items["alpha"]["child_ids"], ["child"])
        self.assertEqual(items["child"]["parent_id"], "alpha")
        self.assertEqual(items["alpha"]["body"].strip(), "A **note**.")
        self.assertTrue(items["alpha"]["focused"])
        self.assertTrue(snapshot["revision"])
        self.assertTrue(snapshot["root_key"])
        self.assertTrue(snapshot["valid"])
        self.assertTrue(snapshot["writable"], snapshot["diagnostics"])
        self.assertEqual(snapshot["git_head"], self._git(self.root, "rev-parse", "HEAD"))

    def test_copy_context_uses_the_current_item_and_single_builder_without_writing(self):
        snapshot = self._snapshot()
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(self.root).parts
        }
        with patch("rightmemory.web.service.build_opening_context", wraps=build_opening_context) as builder:
            response = self._context(snapshot=snapshot)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()["data"]), {"text"})
        self.assertIsInstance(response.json()["data"]["text"], str)
        builder.assert_called_once()
        actual_root, actual_item = builder.call_args.args
        self.assertEqual(actual_root, self.root.resolve())
        self.assertEqual(actual_item["id"], "alpha")
        self.assertEqual(actual_item["source_path"], "PURSUITS.md")
        self.assertEqual(actual_item["source_line"], 7)
        self.assertEqual(builder.call_args.kwargs, {})
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(self.root).parts
        }
        self.assertEqual(after, before)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), snapshot["git_head"])
        self.assertEqual(self._git(self.root, "status", "--porcelain"), "")

    def test_copy_context_rejects_stale_revision_before_calling_builder(self):
        stale = self._snapshot()
        self._operation({"type": "rename", "id": "alpha", "title": "Changed"}, snapshot=stale)
        current = self._snapshot()
        with patch("rightmemory.web.service.build_opening_context") as builder:
            response = self._context(snapshot=stale)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "conflict")
        self.assertEqual(response.json()["detail"]["snapshot"]["revision"], current["revision"])
        builder.assert_not_called()

    def test_copy_context_rejects_a_change_during_the_build(self):
        snapshot = self._snapshot()

        def build_then_change(root, item):
            result = build_opening_context(root, item)
            path = root / "MEMORY.md"
            path.write_text(path.read_text(encoding="utf-8") + "\nConcurrent update.\n", encoding="utf-8")
            return result

        with patch("rightmemory.web.service.build_opening_context", side_effect=build_then_change):
            response = self._context(snapshot=snapshot)

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "conflict")
        self.assertNotEqual(response.json()["detail"]["snapshot"]["revision"], snapshot["revision"])
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), snapshot["git_head"])

    def test_copy_context_rejects_unknown_item_and_missing_parameters(self):
        with patch("rightmemory.web.service.build_opening_context") as builder:
            response = self._context("missing")
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["detail"]["code"], "not_found")
            for query in ("", "item_id=alpha", "expected_revision=revision"):
                with self.subTest(query=query):
                    invalid = self.client.get(f"/api/pursuit-map/context?{query}")
                    self.assertEqual(invalid.status_code, 422)
        builder.assert_not_called()

    def test_copy_context_accepts_a_valid_read_only_root(self):
        unrelated = self.root / "unrelated.txt"
        unrelated.write_bytes(b"uncommitted work")
        snapshot = self._snapshot()
        self.assertFalse(snapshot["writable"])

        response = self._context(snapshot=snapshot)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(unrelated.read_bytes(), b"uncommitted work")
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), snapshot["git_head"])

    def test_copy_context_rejects_invalid_graph_without_calling_builder(self):
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## One {#same}\n\n## Two {#same}\n", encoding="utf-8",
        )
        with patch("rightmemory.web.service.build_opening_context") as builder:
            response = self._context("same")

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_root")
        self.assertFalse(response.json()["detail"]["snapshot"]["valid"])
        builder.assert_not_called()

    def test_copy_context_uses_the_session_active_root(self):
        other_root = self.root / "other"
        self._seed_root(other_root)
        original = self._snapshot()
        selected = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        with patch("rightmemory.web.service.build_opening_context", wraps=build_opening_context) as builder:
            response = self._context()
            conflict = self._context(snapshot=original)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        builder.assert_called_once()
        self.assertEqual(builder.call_args.args[0], other_root.resolve())

    def test_web_studio_preserves_unused_conversation_runtime_state(self):
        database = self.root / ".runtime" / "web" / "conversations.sqlite3"
        self.assertFalse(database.exists())
        database.write_bytes(b"existing conversation state")
        client = TestClient(create_web_app(self.root), request_timeout_seconds=30)
        client.get("/api/session")

        response = self._context(client=client)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(database.read_bytes(), b"existing conversation state")
        self.assertFalse(database.with_name(database.name + "-wal").exists())

    def test_conversation_workspace_and_manager_endpoints_are_removed(self):
        for method, path in (
            ("GET", "/api/conversation-workspace"),
            ("GET", "/api/pursuit-conversations?pursuit_id=alpha"),
            ("POST", "/api/pursuit-conversations"),
            ("POST", "/api/manager-conversations"),
            ("GET", "/api/conversations/previous"),
            ("GET", "/api/conversation-hosts"),
            ("GET", "/api/conversation-projects"),
            ("GET", "/api/conversation-events"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 404)

    def test_operations_are_saved_in_order_and_share_one_checkpoint(self):
        snapshot = self._snapshot()
        initial_head = snapshot["git_head"]
        operations = [
            {"type": "create", "parent_id": "alpha", "after_id": "child", "title": "New direction"},
            {"type": "rename", "title": "中文 and English"},
            {"type": "move", "parent_id": "beta", "after_id": None},
            {"type": "edit_body", "body": "Free-form **Markdown**.\n\nSome context."},
            {"type": "set_focus", "focused": True},
            {"type": "delete"},
        ]
        created_id = None
        for operation in operations:
            if created_id is not None:
                operation = {**operation, "id": created_id}
            with self.subTest(operation=operation["type"]):
                response = self._operation(operation, snapshot=snapshot)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["data"]
                next_snapshot = result["snapshot"]
                self.assertIsNone(result["commit"])
                self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), initial_head)
                self.assertTrue(next_snapshot["pending"])
                self.assertNotEqual(next_snapshot["revision"], snapshot["revision"])
                self.assertTrue(result["operation_id"])
                self.assertTrue(result["undoable"])
                self.assertIsInstance(result["repaired_references"], list)
                self.assertEqual(self._git(self.root, "status", "--porcelain"), "")
                if operation["type"] == "create":
                    created_id = result["selected_id"]
                    self.assertIn(created_id, {item["id"] for item in next_snapshot["items"]})
                snapshot = next_snapshot
        self.assertNotIn(created_id, {item["id"] for item in snapshot["items"]})
        flushed = self._flush()
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), initial_head)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), flushed["commit"])
        self.assertFalse(flushed["snapshot"]["pending"])

    def test_stale_revision_returns_conflict_and_authoritative_snapshot(self):
        stale = self._snapshot()
        first = self._operation({"type": "rename", "id": "alpha", "title": "Changed"}, snapshot=stale)
        self.assertEqual(first.status_code, 200, first.text)
        current = first.json()["data"]["snapshot"]
        before = (self.root / "PURSUITS.md").read_bytes()

        conflict = self._operation({"type": "rename", "id": "beta", "title": "Stale"}, snapshot=stale)

        self.assertEqual(conflict.status_code, 409, conflict.text)
        detail = conflict.json()["detail"]
        self.assertEqual(detail["code"], "conflict")
        self.assertTrue(detail["message"])
        self.assertIsInstance(detail["diagnostics"], list)
        self.assertEqual(detail["snapshot"]["revision"], current["revision"])
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), before)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), current["git_head"])

    def test_rename_many_response_shape_and_stale_revision_conflict(self):
        stale = self._snapshot()
        operation = {
            "type": "rename_many",
            "renames": [
                {"id": "alpha", "title": "Alpha renamed"},
                {"id": "beta", "title": "Beta renamed"},
            ],
        }

        response = self._operation(operation, snapshot=stale)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["data"]
        self.assertEqual(result["selected_id"], "alpha")
        self.assertEqual(result["id_remaps"], [])
        self.assertTrue(result["undoable"])
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), stale["git_head"])
        items = {item["id"]: item for item in result["snapshot"]["items"]}
        self.assertEqual(items["alpha"]["title"], "Alpha renamed")
        self.assertEqual(items["beta"]["title"], "Beta renamed")
        changed = (self.root / "PURSUITS.md").read_bytes()

        conflict = self._operation(operation, snapshot=stale)

        self.assertEqual(conflict.status_code, 409, conflict.text)
        detail = conflict.json()["detail"]
        self.assertEqual(detail["code"], "conflict")
        self.assertEqual(detail["snapshot"]["revision"], result["snapshot"]["revision"])
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), changed)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), stale["git_head"])
        flushed = self._flush()
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), stale["git_head"])
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), flushed["commit"])

    def test_undo_and_redo_add_commits_in_the_same_session(self):
        original = (self.root / "PURSUITS.md").read_bytes()
        applied = self._operation({"type": "rename", "id": "alpha", "title": "Renamed"})
        self.assertEqual(applied.status_code, 200, applied.text)
        checkpoint = self._flush()
        changed = (self.root / "PURSUITS.md").read_bytes()
        result = applied.json()["data"]
        undo = self.client.post(
            "/api/pursuit-map/undo",
            json={"expected_revision": checkpoint["snapshot"]["revision"], "operation_id": result["operation_id"]},
            headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(undo.status_code, 200, undo.text)
        undo_checkpoint = self._flush()
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), original)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), checkpoint["commit"])
        undone = undo.json()["data"]
        redo = self.client.post(
            "/api/pursuit-map/redo",
            json={"expected_revision": undo_checkpoint["snapshot"]["revision"], "operation_id": result["operation_id"]},
            headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(redo.status_code, 200, redo.text)
        self._flush()
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), changed)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), undo_checkpoint["commit"])
        self.assertEqual(self._git(self.root, "status", "--porcelain"), "")

    def test_another_session_cannot_undo_an_editor_action(self):
        applied = self._operation({"type": "rename", "id": "alpha", "title": "Owner edit"})
        self.assertEqual(applied.status_code, 200, applied.text)
        result = applied.json()["data"]
        other_client, other_csrf = self._bootstrap()
        response = other_client.post(
            "/api/pursuit-map/undo",
            json={"expected_revision": result["snapshot"]["revision"], "operation_id": result["operation_id"]},
            headers={"x-csrf-token": other_csrf},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["detail"]["code"], "history_forbidden")
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), result["snapshot"]["git_head"])

    def test_client_session_identifiers_are_not_trusted(self):
        snapshot = self._snapshot()
        with patch("rightmemory.web.service.PursuitStore") as store:
            store.return_value.apply.return_value = {"accepted": True}
            response = self._operation(
                {"type": "rename", "id": "alpha", "title": "Changed"},
                snapshot=snapshot, session_id="client-controlled",
            )
        self.assertEqual(response.status_code, 200, response.text)
        actual = store.return_value.apply.call_args.kwargs["session_id"]
        self.assertTrue(actual)
        self.assertNotEqual(actual, "client-controlled")

    def test_context_finishes_saved_edits_before_reading_canonical_state(self):
        before = self._snapshot()
        response = self._operation({"type": "rename", "id": "alpha", "title": "Updated title"})
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()["data"]["snapshot"]
        observed = []

        def inspect_context_boundary(root, item):
            observed.append((self._git(root, "rev-parse", "HEAD"), item["title"]))
            return build_opening_context(root, item)

        with patch("rightmemory.web.service.build_opening_context", side_effect=inspect_context_boundary):
            copied = self._context(snapshot=pending)
        self.assertEqual(copied.status_code, 200, copied.text)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0][0], before["git_head"])
        self.assertEqual(observed[0][1], "Updated title")
        self.assertFalse(self._snapshot()["pending"])
        self.assertEqual(self._git(self.root, "status", "--porcelain"), "")

    def test_flush_batches_distinct_actions_and_next_edit_makes_another_commit(self):
        original = self._snapshot()
        actions = [
            {"type": "rename", "id": "alpha", "title": "Changed"},
            {"type": "set_focus", "id": "beta", "focused": True},
        ]
        action_ids = []
        for operation in actions:
            response = self._operation(operation)
            self.assertEqual(response.status_code, 200, response.text)
            action_ids.append(response.json()["data"]["operation_id"])
        first = self._flush()
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), original["git_head"])
        self.assertEqual(first["snapshot"]["history"]["undo"], action_ids)
        response = self._operation({"type": "edit_body", "id": "beta", "body": "A saved note."})
        self.assertEqual(response.status_code, 200, response.text)
        second = self._flush()
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), first["commit"])
        self.assertNotEqual(second["commit"], first["commit"])

    def test_root_switch_finishes_current_owner_batch(self):
        other_root = self.root / "other"
        self._seed_root(other_root)
        initial = self._snapshot()
        edited = self._operation({"type": "rename", "id": "alpha", "title": "Before switch"})
        self.assertEqual(edited.status_code, 200, edited.text)
        switched = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD^"), initial["git_head"])
        self.assertIn("Before switch", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_root_switch_conflict_preserves_selection_and_saved_recovery(self):
        other_root = self.root / "other"
        self._seed_root(other_root)
        self._operation({"type": "rename", "id": "alpha", "title": "Pending"})
        (self.root / "MEMORY.md").write_text("# Memory\n\nExternal change.\n", encoding="utf-8")
        self._git(self.root, "add", "MEMORY.md")
        self._git(self.root, "commit", "-qm", "external")
        external = self._git(self.root, "rev-parse", "HEAD")
        switched = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(switched.status_code, 409, switched.text)
        self.assertEqual(self.client.get("/api/session").json()["active_root"], str(self.root.resolve()))
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), external)
        self.assertFalse(self._snapshot()["writable"])
        with self.assertLogs("rightmemory.web.service", level="WARNING"):
            self.app.state.pursuit_batches.flush_due(force=True)
        fresh, _ = self._bootstrap()
        recovery = self._snapshot(fresh)
        self.assertFalse(recovery["writable"])
        self.assertTrue(recovery["recovery"])
        self.assertEqual(recovery["history"], {"undo": [], "redo": []})

    def test_another_browser_cannot_write_or_refresh_owner_activity(self):
        self._operation({"type": "rename", "id": "alpha", "title": "Owner"})
        other, csrf = self._bootstrap()
        snapshot = self._snapshot(other)
        self.assertFalse(snapshot["writable"])
        response = self._operation(
            {"type": "rename", "id": "beta", "title": "Other"}, client=other, csrf=csrf, snapshot=snapshot,
        )
        self.assertEqual(response.status_code, 409, response.text)
        activity = other.post("/api/pursuit-map/activity", json={}, headers={"x-csrf-token": csrf})
        self.assertEqual(activity.status_code, 409, activity.text)
        owner_activity = self.client.post(
            "/api/pursuit-map/activity", json={}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(owner_activity.status_code, 200, owner_activity.text)

    def test_shutdown_and_restart_finish_durable_batches_in_nested_roots(self):
        other_root = self.root / "other"
        self._seed_root(other_root)
        selected = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.csrf = selected.json()["data"]["csrf_token"]
        original = self._snapshot()
        edited = self._operation({"type": "rename", "id": "alpha", "title": "Recovered"})
        self.assertEqual(edited.status_code, 200, edited.text)
        restarted = PursuitBatchLifecycle(self.root)
        try:
            restarted.start()
        finally:
            restarted.stop()
        self.assertEqual(self._git(other_root, "rev-parse", "HEAD^"), original["git_head"])
        self.assertIn("Recovered", (other_root / "PURSUITS.md").read_text(encoding="utf-8"))
        self.assertEqual(self._git(other_root, "status", "--porcelain"), "")
        edited = self._operation({"type": "set_focus", "id": "beta", "focused": True})
        self.assertEqual(edited.status_code, 200, edited.text)
        before_stop = self._git(other_root, "rev-parse", "HEAD")
        self.app.state.pursuit_batches.stop()
        self.assertEqual(self._git(other_root, "rev-parse", "HEAD^"), before_stop)

    def test_missing_revision_or_operation_never_reaches_the_store(self):
        for endpoint in ("operations", "undo", "redo"):
            with self.subTest(endpoint=endpoint), patch("rightmemory.web.service.PursuitStore") as store:
                response = self.client.post(
                    f"/api/pursuit-map/{endpoint}",
                    json={"operation": {"type": "delete", "id": "alpha"}, "operation_id": "action"},
                    headers={"x-csrf-token": self.csrf},
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["detail"]["code"], "invalid_request")
                self.assertFalse(store.return_value.apply.called)
                self.assertFalse(store.return_value.undo.called)
                self.assertFalse(store.return_value.redo.called)
        snapshot = self._snapshot()
        with patch("rightmemory.web.service.PursuitStore") as store:
            response = self.client.post(
                "/api/pursuit-map/operations", json={"expected_revision": snapshot["revision"]},
                headers={"x-csrf-token": self.csrf},
            )
        self.assertEqual(response.status_code, 400, response.text)
        store.return_value.apply.assert_not_called()

    def test_active_root_selection_scopes_reads_and_writes(self):
        other_root = self.root / "other"
        self._seed_root(other_root)
        before = self._snapshot()
        unchanged_client, _ = self._bootstrap()
        selected = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        self.csrf = selected.json()["data"]["csrf_token"]
        other_snapshot = self._snapshot()
        self.assertNotEqual(other_snapshot["root_key"], before["root_key"])
        edited = self._operation({"type": "rename", "id": "alpha", "title": "Other root"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self._flush()
        self.assertEqual(self._snapshot(unchanged_client)["revision"], before["revision"])
        self.assertIn("Other root", (other_root / "PURSUITS.md").read_text(encoding="utf-8"))
        self.assertNotIn("Other root", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_revision_from_an_identical_clone_cannot_write_the_new_active_root(self):
        snapshot = self._snapshot()
        original = (self.root / "PURSUITS.md").read_bytes()
        other_root = self.root / "other"
        self._git(self.root, "clone", "--quiet", "--no-hardlinks", str(self.root), str(other_root))
        self._git(other_root, "config", "user.name", "Pursuit Web Test")
        self._git(other_root, "config", "user.email", "pursuit-web@example.test")
        selected = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": self.csrf},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        self.csrf = selected.json()["data"]["csrf_token"]

        response = self._operation({"type": "rename", "id": "alpha", "title": "Wrong root"}, snapshot=snapshot)

        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "conflict")
        self.assertNotEqual(detail["snapshot"]["root_key"], snapshot["root_key"])
        for root in (self.root, other_root):
            self.assertEqual(self._git(root, "rev-parse", "HEAD"), snapshot["git_head"])
            self.assertEqual((root / "PURSUITS.md").read_bytes(), original)

    def test_invalid_graph_is_readable_but_not_writable(self):
        path = self.root / "PURSUITS.md"
        path.write_text("# Pursuits\n\n## One {#same}\n\n## Two {#same}\n", encoding="utf-8")
        self._git(self.root, "add", "PURSUITS.md")
        self._git(self.root, "commit", "-qm", "invalid external edit")
        before = path.read_bytes()
        snapshot = self._snapshot()
        self.assertFalse(snapshot["valid"])
        self.assertFalse(snapshot["writable"])
        self.assertTrue(snapshot["diagnostics"])
        response = self._operation({"type": "create", "title": "New"}, snapshot=snapshot)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "invalid_root")
        self.assertFalse(response.json()["detail"]["snapshot"]["writable"])
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), snapshot["git_head"])
        self.assertEqual(path.read_bytes(), before)

    def test_dirty_root_is_read_only_and_keeps_unrelated_changes(self):
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("uncommitted work", encoding="utf-8")
        snapshot = self._snapshot()
        self.assertFalse(snapshot["writable"])
        response = self._operation({"type": "rename", "id": "alpha", "title": "Blocked"}, snapshot=snapshot)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "dirty_root")
        self.assertFalse(response.json()["detail"]["snapshot"]["writable"])
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "uncommitted work")
        self.assertEqual(self._git(self.root, "rev-parse", "HEAD"), snapshot["git_head"])

    def test_non_git_root_is_readable_but_not_writable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            for name in ("MEMORY.md", "PURSUITS.md"):
                (root / name).write_bytes((self.root / name).read_bytes())
            client = TestClient(create_web_app(root), request_timeout_seconds=30)
            session = client.get("/api/session")
            snapshot = self._snapshot(client)
            self.assertTrue(snapshot["valid"], snapshot["diagnostics"])
            self.assertFalse(snapshot["writable"])
            self.assertTrue(snapshot["diagnostics"])
            response = self._operation(
                {"type": "create", "title": "Blocked"}, snapshot=snapshot, client=client,
                csrf=session.json()["csrf_token"],
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "read_only")

    def test_store_calls_run_off_the_event_loop(self):
        caller_thread = threading.get_ident()
        with patch("rightmemory.web.service.PursuitStore") as store:
            store.return_value.snapshot.side_effect = lambda *args, **kwargs: {"thread": threading.get_ident()}
            store.return_value.apply.side_effect = lambda *args, **kwargs: {"thread": threading.get_ident()}
            read = self.client.get("/api/pursuit-map")
            write = self._operation({"type": "create", "title": "New"}, snapshot={"revision": "revision"})
        self.assertNotEqual(read.json()["data"]["thread"], caller_thread)
        self.assertNotEqual(write.json()["data"]["thread"], caller_thread)

    def test_pursuit_assets_are_served_without_authentication(self):
        client = TestClient(self.app, request_timeout_seconds=30)
        for asset, media_type in (
            ("pursuit-map.js", "text/javascript"),
            ("pursuit-map.css", "text/css"),
            ("pursuit-map.LICENSE.txt", "text/plain"),
        ):
            with self.subTest(asset=asset):
                response = client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.headers["content-type"].startswith(media_type))
                self.assertTrue(response.content)
        self.assertEqual(client.get("/static/pursuit-map.js.map").status_code, 404)

    def test_task_and_reconciliation_endpoints_do_not_exist(self):
        for endpoint in ("tasks", "run", "reconcile", "task-links"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    f"/api/pursuit-map/{endpoint}", json={}, headers={"x-csrf-token": self.csrf},
                )
                self.assertEqual(response.status_code, 404)


class PursuitBatchLifecycleTests(unittest.TestCase):
    def test_real_activity_defers_idle_flush_but_not_maximum_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lifecycle = PursuitBatchLifecycle(root)
            with patch("rightmemory.web.service.PursuitStore") as stores:
                store = stores.return_value
                store.pending_state.return_value = {"pending": True, "started_at": 100.0, "updated_at": 100.0}
                with patch("rightmemory.web.service.time.time", return_value=104.0):
                    lifecycle.activity(root)
                lifecycle.flush_due(now=108.0)
                store.flush_pending.assert_not_called()
                lifecycle.flush_due(now=110.0)
                store.flush_pending.assert_called_once()
                store.flush_pending.reset_mock()
                with patch("rightmemory.web.service.time.time", return_value=159.0):
                    lifecycle.activity(root)
                lifecycle.flush_due(now=160.0)
                store.flush_pending.assert_called_once()

    def test_flush_failure_remains_visible_and_is_not_retried_every_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            lifecycle = PursuitBatchLifecycle(root)
            with patch("rightmemory.web.service.PursuitStore") as stores:
                store = stores.return_value
                store.pending_state.return_value = {"pending": True, "started_at": 100.0, "updated_at": 100.0}
                store.flush_pending.side_effect = OSError("unavailable")
                with self.assertLogs("rightmemory.web.service", level="WARNING"):
                    lifecycle.flush_due(now=110.0)
                self.assertIsNotNone(lifecycle.failure(root))
                lifecycle.flush_due(now=111.0)
                store.flush_pending.assert_called_once()
                store.flush_pending.side_effect = None
                lifecycle.flush_due(now=171.0)
                self.assertIsNone(lifecycle.failure(root))


if __name__ == "__main__":
    unittest.main()
