import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from rightmemory.web.app import create_web_app
from rightmemory.shared_views import load_shared_view_credential


class WebStudioReadApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project {#project}\n\nAuth API notes.\n", encoding="utf-8")
        (self.root / "MEMORY_team.md").write_text("# Team {#team}\n\nTeam notes.\n", encoding="utf-8")
        view = self.root / "shared_views" / "alice-auth-api"
        (view / "dist").mkdir(parents=True)
        (view / "view.md").write_text("# Alice Auth API\n", encoding="utf-8")
        (view / "dist" / "MEMORY.md").write_text("# Published Context\n", encoding="utf-8")
        insight = self.root / "insight_logs"
        insight.mkdir()
        (insight / "2026-06-13.md").write_text("# Insight\n\nUseful pattern.\n", encoding="utf-8")
        log = self.root / ".runtime" / "watch" / "review.log"
        log.parent.mkdir(parents=True)
        log.write_text("old\nrecent review message\n", encoding="utf-8")
        self.client = TestClient(create_web_app(self.root, operator_token="secret-token"))
        login = self.client.post("/api/login", json={"token": "secret-token"})
        self.csrf = login.json()["data"]["csrf_token"]

    def test_overview_and_status_return_structured_data(self):
        overview = self.client.get("/api/overview")
        status = self.client.get("/api/status")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(overview.json()["data"]["active_root"], str(self.root.resolve()))
        self.assertEqual(overview.json()["data"]["shared_views"]["provider_view_count"], 0)
        self.assertEqual(overview.json()["data"]["shared_views"]["connection_count"], 0)
        self.assertIn("git", status.json()["data"])
        self.assertIn("watches", status.json()["data"])

    def test_memory_files_use_server_side_ids(self):
        listing = self.client.get("/api/memory/files")
        files = listing.json()["data"]["files"]
        labels = {item["label"]: item for item in files}

        self.assertEqual(listing.status_code, 200)
        self.assertIn("MEMORY.md", labels)
        self.assertIn("MEMORY_team.md", labels)
        self.assertIn("shared_views/alice-auth-api/view.md", labels)
        self.assertIn("shared_views/alice-auth-api/dist/MEMORY.md", labels)

        content = self.client.get(f"/api/memory/files/{labels['MEMORY.md']['id']}")
        rejected = self.client.get("/api/memory/files/../../MEMORY.md")

        self.assertEqual(content.status_code, 200)
        self.assertIn("Auth API notes.", content.json()["data"]["text"])
        self.assertEqual(rejected.status_code, 404)

    def test_insights_list_and_preview(self):
        listing = self.client.get("/api/insights")
        insight_id = listing.json()["data"]["insights"][0]["id"]
        detail = self.client.get(f"/api/insights/{insight_id}")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Useful pattern.", detail.json()["data"]["text"])

    def test_logs_list_and_tail_known_files(self):
        listing = self.client.get("/api/logs")
        logs = {item["id"]: item for item in listing.json()["data"]["logs"]}
        detail = self.client.get("/api/logs/watch:review")
        rejected = self.client.get("/api/logs/../../review.log")

        self.assertEqual(listing.status_code, 200)
        self.assertIn("watch:review", logs)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("recent review message", detail.json()["data"]["text"])
        self.assertEqual(rejected.status_code, 404)

    def test_missing_known_log_returns_structured_missing_result(self):
        detail = self.client.get("/api/logs/watch:dreamer")

        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["data"]["missing"])
        self.assertEqual(detail.json()["data"]["text"], "")


class WebStudioStaticTests(unittest.TestCase):
    def test_static_shell_loads_assets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
            client = TestClient(create_web_app(root, operator_token="secret-token"))

            index = client.get("/")
            script = client.get("/static/app.js")
            style = client.get("/static/styles.css")

        self.assertEqual(index.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(style.status_code, 200)
        self.assertIn("/static/app.js", index.text)
        self.assertIn("/static/styles.css", index.text)
        self.assertNotIn("ready for the next Web Studio API slice", script.text)
        self.assertIn("define-view-form", script.text)
        self.assertIn("accept-invite-form", script.text)
        self.assertIn("credential-form", script.text)


class WebStudioSharedViewApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Provider {#provider}\n\nAuth API accepts signed tokens.\nPrivate payroll note.\n",
            encoding="utf-8",
        )
        self.client = TestClient(create_web_app(self.root, operator_token="secret-token"))
        login = self.client.post("/api/login", json={"token": "secret-token"})
        self.csrf = login.json()["data"]["csrf_token"]

    def test_provider_define_build_export_and_publish(self):
        define = self.client.post(
            "/api/share/views",
            json={
                "view_id": "alice-auth-api",
                "title": "Alice Auth API",
                "description": "Auth API collaboration context.",
                "maintainer": "Alice",
                "filter_terms": ["auth"],
            },
            headers={"x-csrf-token": self.csrf},
        )
        build = self.client.post(
            "/api/share/views/alice-auth-api/build",
            json={},
            headers={"x-csrf-token": self.csrf},
        )
        package = self.root / "package"
        export = self.client.post(
            "/api/share/views/alice-auth-api/export",
            json={"target": str(package)},
            headers={"x-csrf-token": self.csrf},
        )
        hub = self.root / "hub"
        publish = self.client.post(
            "/api/share/views/alice-auth-api/publish",
            json={"kind": "mounted", "hub": str(hub)},
            headers={"x-csrf-token": self.csrf},
        )
        listing = self.client.get("/api/share/views")

        self.assertEqual(define.status_code, 200)
        self.assertEqual(build.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertEqual(publish.status_code, 200)
        self.assertTrue((package / "rightmemory-shared-view.toml").exists())
        self.assertTrue((hub / "registry.toml").exists())
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["data"]["provider_views"][0]["view_id"], "alice-auth-api")

    def test_provider_publish_dispatches_http_hub(self):
        calls = []

        def fake_publish_http(memory_root, view_id, *, hub_url, credential_id, query=None):
            calls.append((memory_root, view_id, hub_url, credential_id, query))
            return f"published shared view {view_id} to HTTP hub {hub_url}"

        with patch("rightmemory.web.service.publish_http_shared_view", side_effect=fake_publish_http):
            response = self.client.post(
                "/api/share/views/alice-auth-api/publish",
                json={
                    "kind": "http",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "query": "auth",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            calls,
            [(self.root.resolve(), "alice-auth-api", "https://hub.example.test", "alice-publish", "auth")],
        )

    def test_save_http_publish_credential_from_web_studio(self):
        response = self.client.post(
            "/api/share/credentials",
            json={
                "credential_id": "alice-publish",
                "kind": "http-publish",
                "hub_url": "https://hub.example.test",
                "provider_id": "alice",
                "token": "secret-token",
            },
            headers={"x-csrf-token": self.csrf},
        )

        credential = load_shared_view_credential(self.root, "alice-publish")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(credential["kind"], "http-publish")
        self.assertEqual(credential["base_url"], "https://hub.example.test")
        self.assertEqual(credential["provider_id"], "alice")
        self.assertEqual(credential["token"], "secret-token")

    def test_accept_invite_dispatches_http_urls(self):
        calls = []

        def fake_accept_http(memory_root, invitation_url, **kwargs):
            calls.append((memory_root, invitation_url, kwargs["heading_id"]))
            return "accepted shared view remote-auth"

        with patch("rightmemory.web.service.accept_http_shared_view_invitation", side_effect=fake_accept_http):
            response = self.client.post(
                "/api/use/accept-invite",
                json={
                    "invitation": "https://hub.example.test/i/invite-token",
                    "heading_id": "remote-auth",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [(self.root.resolve(), "https://hub.example.test/i/invite-token", "remote-auth")])

    def test_accept_retrieve_note_and_notes(self):
        package = self.root / "package"
        self.client.post(
            "/api/share/views",
            json={"view_id": "alice-auth-api", "title": "Alice Auth API", "filter_terms": ["auth"]},
            headers={"x-csrf-token": self.csrf},
        )
        self.client.post(
            "/api/share/views/alice-auth-api/export",
            json={"target": str(package)},
            headers={"x-csrf-token": self.csrf},
        )

        accept = self.client.post(
            "/api/use/accept-invite",
            json={"invitation": str(package), "heading_id": "alice-auth-api"},
            headers={"x-csrf-token": self.csrf},
        )
        retrieve = self.client.post(
            "/api/use/connections/alice-auth-api/retrieve",
            json={"query": "signed tokens"},
            headers={"x-csrf-token": self.csrf},
        )
        needs_confirm = self.client.post(
            "/api/use/connections/alice-auth-api/note",
            json={"message": "Docs are stale"},
            headers={"x-csrf-token": self.csrf},
        )
        note = self.client.post(
            "/api/use/connections/alice-auth-api/note",
            json={"message": "Docs are stale", "confirmed": True, "actor": "assistant"},
            headers={"x-csrf-token": self.csrf},
        )
        notes = self.client.get("/api/use/connections/alice-auth-api/notes")

        self.assertEqual(accept.status_code, 200)
        self.assertEqual(retrieve.status_code, 200)
        self.assertIn("Auth API accepts signed tokens.", retrieve.json()["data"]["text"])
        self.assertEqual(needs_confirm.status_code, 200)
        self.assertIn("confirmation required", needs_confirm.json()["message"])
        self.assertEqual(note.status_code, 200)
        self.assertEqual(notes.status_code, 200)
        self.assertEqual(notes.json()["data"]["notes"][0]["message"], "Docs are stale")
