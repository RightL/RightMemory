import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.share_models import ShareFilePart, ShareRelationship, save_shares
from rightmemory.share_results import ShareOperationResult
from rightmemory.shared_view_files import FileViewPullResult
from rightmemory.shared_view_models import SharedViewConnection, SharedViewTarget, load_shared_view_credential, save_connections
from rightmemory.shared_view_questions import write_question_view
from rightmemory.web.app import create_web_app
from rightmemory.web.service import WebStudioService
from tests.asgi_client import ASGITestClient as TestClient


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
        (view / "dist" / "MEMORY.md").write_text("# Auth API {#auth-api} → []\n", encoding="utf-8")
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

    def test_settings_summary_reports_runtime_without_secrets(self):
        (self.root / "rightmemory.toml").write_text(
            (
                "[agent_cli]\nprovider = \"codex\"\n\n"
                "[retrieve.agent_cli]\nmodel = \"gpt-5.6-luna\"\nreasoning_effort = \"high\"\n\n"
                "[sync]\nenabled = true\n"
            ),
            encoding="utf-8",
        )

        response = self.client.get("/api/settings")
        data = response.json()["data"]

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["config_exists"])
        self.assertEqual(data["active_root"], str(self.root.resolve()))
        self.assertTrue(data["runtime"]["sync"]["ok"])
        self.assertTrue(data["runtime"]["sync"]["value"]["enabled"])
        retrieve = next(role for role in data["roles"] if role["role"] == "retrieve")
        self.assertTrue(retrieve["ok"])
        self.assertEqual(retrieve["executor"]["mode"], "cli-agent")
        self.assertEqual(retrieve["executor"]["model"], "gpt-5.6-luna")
        self.assertEqual(retrieve["executor"]["reasoning_effort"], "high")
        self.assertNotIn("secret-token", response.text)


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
        self.assertIn("build-file-view-form", script.text)
        self.assertIn("build-question-view-form", script.text)
        self.assertIn("invite-file-view-form", script.text)
        self.assertIn("publish-question-view-form", script.text)
        self.assertIn("file-connection-form", script.text)
        self.assertIn("question-connection-form", script.text)
        self.assertNotIn("consumer-view-form", script.text)
        self.assertIn("accept-invite-form", script.text)
        self.assertIn("credential-form", script.text)
        self.assertIn("provider-inbox-form", script.text)
        self.assertIn("publish-events-panel", script.text)
        self.assertIn("pull-all-connections", script.text)
        self.assertIn("status-all-connections", script.text)
        self.assertIn("credential-select", script.text)


class WebStudioStaticSourceTests(unittest.TestCase):
    def test_share_first_static_source_contains_relationship_ui(self):
        static_root = Path(__file__).resolve().parents[1] / "rightmemory" / "web" / "static"
        script = (static_root / "app.js").read_text(encoding="utf-8")

        self.assertIn("renderShareRelationships", script)
        self.assertIn("create-share-form", script)
        self.assertIn("revise-share-form", script)
        self.assertIn("advanced-shared-view-tools", script)
        self.assertIn("/api/share/relationships", script)
        self.assertIn("Git Repo", script)
        self.assertIn("git_url", script)
        self.assertIn("Copy Join URL", script)


class WebStudioShareRelationshipServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Provider {#provider}\n\nAuth API notes.\n", encoding="utf-8")
        self.service = WebStudioService(self.root)

    def test_share_relationship_service_lists_create_and_revise(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="provider",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    credential_id="alice-publish",
                    state="draft",
                    parts=("file",),
                    file=ShareFilePart(
                        view_id="auth-api-files",
                        intent="Share auth API context.",
                        approved=False,
                    ),
                )
            },
        )

        listing = self.service.share_relationships()

        relationship = listing["relationships"][0]
        self.assertEqual(relationship["share_id"], "auth-api")
        self.assertEqual(relationship["capability"], "file_context")
        self.assertEqual(relationship["file"]["view_id"], "auth-api-files")

        with patch(
            "rightmemory.web.service.create_share_from_request",
            return_value=ShareOperationResult(
                share_id="billing-api",
                title="Billing API",
                role="provider",
                state="draft",
                capability="both",
                builder_final_message="Built billing share.",
                next_action="rightmemory share approve billing-api",
            ),
        ) as create:
            created = self.service.create_share_relationship(
                {
                    "share_id": "billing-api",
                    "title": "Billing API",
                    "provider_id": "alice",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "request": "Share billing API context and questions.",
                    "capability": "both",
                    "question_base_url": "https://provider.example.test",
                }
            )

        self.assertEqual(created["share_id"], "billing-api")
        self.assertEqual(created["capability"], "both")
        create.assert_called_once_with(
            self.root.resolve(),
            share_id="billing-api",
            title="Billing API",
            request="Share billing API context and questions.",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="both",
            question_base_url="https://provider.example.test",
        )

        with patch(
            "rightmemory.web.service.revise_share",
            return_value=ShareOperationResult(
                share_id="auth-api",
                title="Auth API",
                role="provider",
                state="draft",
                capability="file_context",
                builder_final_message="Narrowed auth scope.",
                next_action="rightmemory share approve auth-api",
            ),
        ) as revise:
            revised = self.service.revise_share_relationship(
                "auth-api",
                {"revision": "Only include refresh-token behavior."},
            )

        self.assertIn("Narrowed auth scope.", revised["builder_final_message"])
        revise.assert_called_once_with(
            self.root.resolve(),
            "auth-api",
            "Only include refresh-token behavior.",
            capability=None,
            question_base_url=None,
        )

    def test_create_git_share_relationship_uses_git_target(self):
        with patch(
            "rightmemory.web.service.create_share_from_request",
            return_value=ShareOperationResult(
                share_id="auth-api",
                title="Auth API",
                role="provider",
                state="draft",
                capability="file_context",
                builder_final_message="Built auth context.",
            ),
        ) as create:
            created = self.service.create_share_relationship(
                {
                    "share_id": "auth-api",
                    "title": "Auth API",
                    "provider_id": "alice",
                    "transport": "git",
                    "git_url": "https://github.com/user/rightmemory-shares.git",
                    "git_branch": "gh-pages",
                    "request": "Share auth API context.",
                    "capability": "both",
                    "question_base_url": "https://provider.example.test",
                }
            )

        self.assertEqual(created["capability"], "file_context")
        create.assert_called_once_with(
            self.root.resolve(),
            share_id="auth-api",
            title="Auth API",
            request="Share auth API context.",
            provider_id="alice",
            hub_url=None,
            credential_id=None,
            capability="file_context",
            question_base_url=None,
            git_url="https://github.com/user/rightmemory-shares.git",
            git_branch="gh-pages",
        )

    def test_publish_share_relationship_dispatches_service(self):
        with patch("rightmemory.web.service.publish_share", return_value="published share auth-api") as publish:
            result = self.service.publish_share_relationship("auth-api", {"no_push": True})

        self.assertEqual(result["message"], "published share auth-api")
        publish.assert_called_once_with(
            self.root.resolve(),
            "auth-api",
            label=None,
            expires_at=None,
            git_url=None,
            git_branch=None,
            push=False,
        )


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

    def test_provider_build_file_question_and_approve(self):
        with patch("rightmemory.web.service.run_file_view_builder", return_value="built file view auth-api-files") as build_file:
            build_file_response = self.client.post(
                "/api/share/views/build-file",
                json={
                    "view_id": "auth-api-files",
                    "intent": "Expose auth API integration context.",
                    "title": "Auth API Files",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                },
                headers={"x-csrf-token": self.csrf},
            )

        with patch(
            "rightmemory.web.service.run_question_view_builder",
            return_value="built question view auth-api-ask",
        ) as build_question:
            build_question_response = self.client.post(
                "/api/share/views/build-question",
                json={
                    "view_id": "auth-api-ask",
                    "intent": "Let frontend agents ask auth API questions.",
                    "title": "Auth API Questions",
                },
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.approve_file_view", return_value="approved file view auth-api-files") as approve:
            approve_response = self.client.post(
                "/api/share/views/auth-api-files/approve",
                json={"type": "file"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(build_file_response.status_code, 200)
        self.assertEqual(build_file.call_args.kwargs["intent"], "Expose auth API integration context.")
        self.assertEqual(build_question_response.status_code, 200)
        self.assertEqual(build_question.call_args.kwargs["view_id"], "auth-api-ask")
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve.call_args.args[0], self.root.resolve())
        self.assertEqual(approve.call_args.args[1], "auth-api-files")

    def test_provider_invites_file_view_from_web_studio(self):
        with patch(
            "rightmemory.web.service.invite_file_view",
            return_value="invited file view auth-api-files\ninvitation_url\thttps://hub.example.test/i/invite-token",
        ) as invite:
            response = self.client.post(
                "/api/share/views/auth-api-files/invite",
                json={
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "label": "frontend",
                    "expires_at": "2026-07-01T00:00:00+00:00",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        invite.assert_called_once_with(
            self.root.resolve(),
            "auth-api-files",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            label="frontend",
            expires_at="2026-07-01T00:00:00+00:00",
        )
        self.assertIn("invitation_url", response.json()["message"])

    def test_provider_publishes_question_invitation_from_web_studio(self):
        with patch(
            "rightmemory.web.service.publish_question_view",
            return_value="published question view auth-api-ask\ninvitation_url\thttps://hub.example.test/i/invite-token",
        ) as publish:
            response = self.client.post(
                "/api/share/views/auth-api-ask/publish-question",
                json={
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "question_base_url": "https://provider.example.test",
                    "label": "frontend",
                    "expires_at": "2026-07-01T00:00:00+00:00",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        publish.assert_called_once_with(
            self.root.resolve(),
            "auth-api-ask",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            question_base_url="https://provider.example.test",
            label="frontend",
            expires_at="2026-07-01T00:00:00+00:00",
        )
        self.assertIn("invitation_url", response.json()["message"])

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

    def test_shared_views_include_sanitized_credentials(self):
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

        listing = self.client.get("/api/share/views")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(listing.status_code, 200)
        credentials = listing.json()["data"]["credentials"]
        self.assertEqual(credentials[0]["credential_id"], "alice-publish")
        self.assertEqual(credentials[0]["base_url"], "https://hub.example.test")
        self.assertEqual(credentials[0]["provider_id"], "alice")
        self.assertNotIn("token", credentials[0])

    def test_share_relationships_api_lists_create_and_revise(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="provider",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    credential_id="alice-publish",
                    state="draft",
                    parts=("file",),
                    file=ShareFilePart(
                        view_id="auth-api-files",
                        intent="Share auth API context.",
                        approved=False,
                    ),
                )
            },
        )

        listing = self.client.get("/api/share/relationships")

        self.assertEqual(listing.status_code, 200)
        relationship = listing.json()["data"]["relationships"][0]
        self.assertEqual(relationship["share_id"], "auth-api")
        self.assertEqual(relationship["capability"], "file_context")
        self.assertEqual(relationship["file"]["view_id"], "auth-api-files")

        with patch(
            "rightmemory.web.service.create_share_from_request",
            return_value=ShareOperationResult(
                share_id="billing-api",
                title="Billing API",
                role="provider",
                state="draft",
                capability="both",
                builder_final_message="Built billing share.",
                next_action="rightmemory share approve billing-api",
            ),
        ) as create:
            created = self.client.post(
                "/api/share/relationships",
                json={
                    "share_id": "billing-api",
                    "title": "Billing API",
                    "provider_id": "alice",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "request": "Share billing API context and questions.",
                    "capability": "both",
                    "question_base_url": "https://provider.example.test",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["data"]["share_id"], "billing-api")
        self.assertEqual(created.json()["data"]["capability"], "both")
        create.assert_called_once_with(
            self.root.resolve(),
            share_id="billing-api",
            title="Billing API",
            request="Share billing API context and questions.",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="both",
            question_base_url="https://provider.example.test",
        )

        with patch(
            "rightmemory.web.service.revise_share",
            return_value=ShareOperationResult(
                share_id="auth-api",
                title="Auth API",
                role="provider",
                state="draft",
                capability="file_context",
                builder_final_message="Narrowed auth scope.",
                next_action="rightmemory share approve auth-api",
            ),
        ) as revise:
            revised = self.client.post(
                "/api/share/relationships/auth-api/revise",
                json={"revision": "Only include refresh-token behavior."},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(revised.status_code, 200)
        self.assertIn("Narrowed auth scope.", revised.json()["data"]["builder_final_message"])
        revise.assert_called_once_with(
            self.root.resolve(),
            "auth-api",
            "Only include refresh-token behavior.",
            capability=None,
            question_base_url=None,
        )

        with patch("rightmemory.web.service.publish_share", return_value="published share auth-api") as publish:
            published = self.client.post(
                "/api/share/relationships/auth-api/publish",
                json={"no_push": True},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.json()["data"]["message"], "published share auth-api")
        publish.assert_called_once_with(
            self.root.resolve(),
            "auth-api",
            label=None,
            expires_at=None,
            git_url=None,
            git_branch=None,
            push=False,
        )

    def test_provider_inbox_uses_saved_credential_defaults(self):
        self.client.post(
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
        with patch("rightmemory.web.service.list_http_shared_view_inbox") as inbox:
            inbox.return_value = [
                {
                    "interaction_id": "int-1",
                    "view_id": "auth-api-files",
                    "connection_id": "conn-1",
                    "payload": {"message": "Docs are stale"},
                }
            ]
            response = self.client.post(
                "/api/share/provider-inbox",
                json={"credential_id": "alice-publish"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        inbox.assert_called_once_with(
            self.root.resolve(),
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            provider_id="alice",
        )
        self.assertEqual(response.json()["data"]["interactions"][0]["view_id"], "auth-api-files")

    def test_publish_events_pull_all_and_status_all_api(self):
        with patch("rightmemory.web.service.list_file_view_publish_events") as events:
            events.return_value = [{"view_id": "auth-api-files", "status": "published"}]
            events_response = self.client.get("/api/share/publish-events")

        with patch("rightmemory.web.service.pull_all_file_views") as pull_all:
            pull_all.return_value = [FileViewPullResult("auth-api-files", "pulled", "file view pulled")]
            pull_response = self.client.post(
                "/api/use/connections/pull-all",
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.shared_view_connection_status") as connection_status:
            connection_status.return_value = {
                "heading_id": "auth-api-files",
                "type": "file",
                "target": "http-file",
                "status": "imported",
                "message": "file view import is available",
            }
            with patch("rightmemory.web.service.load_connections") as connections:
                connection = type("Connection", (), {"heading_id": "auth-api-files"})()
                connections.return_value = {"auth-api-files": connection}
                status_response = self.client.get("/api/use/connections/status-all")

        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(events_response.json()["data"]["events"][0]["status"], "published")
        self.assertEqual(pull_response.status_code, 200)
        self.assertEqual(pull_response.json()["data"]["results"][0]["status"], "pulled")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["data"]["statuses"][0]["status"], "imported")

    def test_provider_question_endpoint_accepts_connection_bearer_for_remote_ask(self):
        calls = []
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            access_tokens=["connection-token"],
        )

        def fake_answer(self, view_id, payload):
            calls.append((view_id, payload["question"]))
            return "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"

        remote_client = TestClient(create_web_app(self.root, operator_token="secret-token"))
        with patch("rightmemory.web.service.WebStudioService.answer_question_view", new=fake_answer):
            response = remote_client.post(
                "/api/share/questions/auth-api-ask/ask",
                json={"question": "How do tokens refresh?"},
                headers={"Authorization": "Bearer connection-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("auth-api-ask", "How do tokens refresh?")])
        self.assertEqual(response.json()["data"]["status"], "answered")
        self.assertIn("Status: answered", response.json()["data"]["text"])

    def test_provider_question_endpoint_rejects_wrong_connection_bearer(self):
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            access_tokens=["connection-token"],
        )

        remote_client = TestClient(create_web_app(self.root, operator_token="secret-token"))
        with patch("rightmemory.web.service.WebStudioService.answer_question_view") as answer:
            response = remote_client.post(
                "/api/share/questions/auth-api-ask/ask",
                json={"question": "How do tokens refresh?"},
                headers={"Authorization": "Bearer wrong-token"},
            )

        self.assertEqual(response.status_code, 401)
        answer.assert_not_called()

    def test_provider_question_ready_probe_validates_bearer_without_answering(self):
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            access_tokens=["connection-token"],
        )

        remote_client = TestClient(create_web_app(self.root, operator_token="secret-token"))
        with patch("rightmemory.web.service.WebStudioService.answer_question_view") as answer:
            wrong = remote_client.get(
                "/api/share/questions/auth-api-ask/ready",
                headers={"Authorization": "Bearer wrong-token"},
            )
            ready = remote_client.get(
                "/api/share/questions/auth-api-ask/ready",
                headers={"Authorization": "Bearer connection-token"},
            )

        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["data"]["status"], "ready")
        self.assertEqual(ready.json()["data"]["view_id"], "auth-api-ask")
        answer.assert_not_called()

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

    def test_consumer_file_view_pull_question_ask_note_and_notes(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    view_type="file",
                    ref="rightmemory://mf/alice-auth-api",
                    relationship="external",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        credential_id="alice-auth-api-token",
                        view_id="alice-auth-api",
                    ),
                )
            },
        )

        with patch("rightmemory.web.service.pull_file_view") as pull:
            pull.return_value = FileViewPullResult("auth-api-files", "pulled", "file view pulled")
            pull_response = self.client.post(
                "/api/use/connections/auth-api-files/pull",
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.shared_view_connection_status") as connection_status:
            connection_status.return_value = {
                "heading_id": "auth-api-files",
                "type": "file",
                "target": "http-file",
                "status": "imported",
                "message": "file view import is available",
            }
            status_response = self.client.get("/api/use/connections/auth-api-files/status")

        with patch("rightmemory.web.service.ask_question_view") as ask:
            ask.return_value = "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"
            ask_response = self.client.post(
                "/api/use/connections/auth-api-ask/ask",
                json={"question": "How do tokens refresh?"},
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

        self.assertEqual(pull_response.status_code, 200)
        self.assertIn("pulled", pull_response.json()["message"])
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["data"]["status"], "imported")
        self.assertEqual(ask_response.status_code, 200)
        self.assertEqual(ask_response.json()["data"]["status"], "answered")
        self.assertIn("Status: answered", ask_response.json()["data"]["text"])
        self.assertEqual(needs_confirm.status_code, 200)
        self.assertIn("confirmation required", needs_confirm.json()["message"])
        self.assertEqual(note.status_code, 200)
        self.assertEqual(notes.status_code, 200)
        self.assertEqual(notes.json()["data"]["notes"][0]["message"], "Docs are stale")

    def test_legacy_web_shared_view_endpoints_are_removed(self):
        define_response = self.client.post(
            "/api/share/views",
            json={"view_id": "legacy"},
            headers={"x-csrf-token": self.csrf},
        )
        build_response = self.client.post(
            "/api/share/views/legacy/build",
            json={"query": "tokens"},
            headers={"x-csrf-token": self.csrf},
        )
        export_response = self.client.post(
            "/api/share/views/legacy/export",
            json={"hub": "/tmp/hub"},
            headers={"x-csrf-token": self.csrf},
        )
        publish_response = self.client.post(
            "/api/share/views/legacy/publish",
            json={"kind": "http"},
            headers={"x-csrf-token": self.csrf},
        )
        retrieve_response = self.client.post(
            "/api/use/connections/auth-api-files/retrieve",
            json={"query": "tokens"},
            headers={"x-csrf-token": self.csrf},
        )

        self.assertIn(define_response.status_code, {404, 405})
        self.assertEqual(build_response.status_code, 404)
        self.assertEqual(export_response.status_code, 404)
        self.assertEqual(publish_response.status_code, 404)
        self.assertEqual(retrieve_response.status_code, 404)
