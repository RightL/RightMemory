import asyncio
import json
import zipfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlsplit
from unittest.mock import patch

from starlette.requests import Request

from rightmemory.hub.client import HubClientError
from rightmemory.hub.app import create_hub_app
from rightmemory.hub.store import HubStore
from rightmemory.share_models import (
    ShareFilePart,
    ShareQuestionPart,
    ShareRelationship,
    load_shares,
    save_shares,
    validate_share_id,
)
from rightmemory.share_builder import revise_share_builder, run_share_builder
from rightmemory.share_results import ShareCapabilityStatus, ShareOperationResult, format_share_operation_result
from rightmemory.shared_view_models import SharedViewTarget, load_shared_view_credential, save_shared_view_credential
from rightmemory.shared_view_questions import ask_question_view
from rightmemory.shared_views import accept_shared_view, record_shared_view_note
from rightmemory.shares import (
    approve_share,
    create_share,
    create_share_from_request,
    join_share,
    publish_share,
    revise_share,
    share_status,
)
from rightmemory.web.app import create_web_app


def _ensure_mapping(value) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object response, got {type(value).__name__}")
    return value


class ShareResultTests(unittest.TestCase):
    def test_format_share_operation_result_includes_builder_summary_and_next_action(self):
        result = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="both",
            builder_final_message="Selected auth-api docs and enabled live questions.",
            statuses=(
                ShareCapabilityStatus(
                    capability="file_context",
                    artifact_id="auth-api-files",
                    status="draft",
                    preview_path="shared_views/auth-api-files/dist/MEMORY.md",
                    message="file context generated",
                ),
                ShareCapabilityStatus(
                    capability="live_questions",
                    artifact_id="auth-api-ask",
                    status="draft",
                    preview_path="shared_views/auth-api-ask/retriever.md",
                    message="question scope generated",
                ),
            ),
            next_action="rightmemory share approve auth-api",
        )

        text = format_share_operation_result(result)

        self.assertIn("auth-api provider draft capability=both", text)
        self.assertIn("Builder summary:", text)
        self.assertIn("Selected auth-api docs", text)
        self.assertIn("file_context auth-api-files draft", text)
        self.assertIn("live_questions auth-api-ask draft", text)
        self.assertIn("Next:", text)
        self.assertIn("rightmemory share approve auth-api", text)

    def test_operation_result_json_omits_empty_fields(self):
        result = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="consumer",
            state="joined",
            capability="file_context",
            statuses=(),
        )

        payload = result.to_json()

        self.assertEqual(payload["share_id"], "auth-api")
        self.assertEqual(payload["capability"], "file_context")
        self.assertNotIn("builder_final_message", payload)
        self.assertNotIn("invitation_url", payload)


class ShareBuilderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "rightmemory.toml").write_text('[agent_cli]\nprovider = "codex"\n', encoding="utf-8")
        (self.root / "MEMORY.md").write_text("# Project\n\n## Auth API\nUse refresh tokens.\n", encoding="utf-8")

    def test_run_share_builder_uses_share_level_session_and_returns_result(self):
        def fake_run_session_turn(runtime, session_id, message):
            self.assertEqual(session_id, "share-builder-auth-api")
            self.assertIn("<share_build>", message)
            self.assertIn("capability: auto", message)
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
            return "Selected Auth API context."

        with patch("rightmemory.share_builder.RightMemoryRuntime.run_session_turn", fake_run_session_turn):
            result = run_share_builder(
                self.root,
                share_id_hint="auth-api",
                request="Share auth API context.",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                capability="auto",
            )

        self.assertEqual(result.share_id, "auth-api")
        self.assertEqual(result.builder_final_message, "Selected Auth API context.")
        self.assertEqual(result.capability, "file_context")
        self.assertEqual(result.next_action, "rightmemory share approve auth-api")

    def test_run_share_reviser_uses_existing_share_session(self):
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
                    parts=("question",),
                    question=ShareQuestionPart(
                        view_id="auth-api-ask",
                        intent="Answer auth API questions.",
                        question_base_url="https://provider.example.test",
                        approved=False,
                    ),
                )
            },
        )

        def fake_run_session_turn(runtime, session_id, message):
            self.assertEqual(session_id, "share-builder-auth-api")
            self.assertIn("<share_revise>", message)
            self.assertIn("Include profile endpoint.", message)
            return "Updated live question scope."

        with patch("rightmemory.share_builder.RightMemoryRuntime.run_session_turn", fake_run_session_turn):
            result = revise_share_builder(self.root, "auth-api", "Include profile endpoint.")

        self.assertEqual(result.builder_final_message, "Updated live question scope.")
        self.assertEqual(result.capability, "live_questions")


class ShareModelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_validate_share_id_accepts_portable_ids(self):
        self.assertEqual(validate_share_id("auth-api_1.dev"), "auth-api_1.dev")

    def test_validate_share_id_rejects_paths(self):
        with self.assertRaises(ValueError):
            validate_share_id("../auth")

    def test_save_and_load_provider_file_question_share(self):
        share = ShareRelationship(
            share_id="auth-api",
            role="provider",
            title="Auth API",
            provider_id="alice",
            hub_url="http://127.0.0.1:8765",
            credential_id="alice-publish",
            state="draft",
            parts=("file", "question"),
            file=ShareFilePart(
                view_id="auth-api-files",
                intent="Expose auth API integration context for frontend agents",
                approved=False,
            ),
            question=ShareQuestionPart(
                view_id="auth-api-ask",
                intent="Let frontend agents ask temporary auth API questions",
                question_base_url="http://127.0.0.1:8766",
                approved=False,
            ),
        )

        save_shares(self.root, {"auth-api": share})
        loaded = load_shares(self.root)

        self.assertEqual(loaded["auth-api"], share)
        text = (self.root / "shares.toml").read_text(encoding="utf-8")
        self.assertIn("[shares.auth-api]", text)
        self.assertIn('parts = ["file", "question"]', text)

    def test_git_provider_share_round_trips_transport_metadata(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="provider",
                    title="Auth API",
                    provider_id="alice",
                    state="draft",
                    parts=("file",),
                    transport="git",
                    git_url="https://github.com/user/rightmemory-shares.git",
                    git_branch="gh-pages",
                    file=ShareFilePart(view_id="auth-api-files", intent="Share auth API context"),
                )
            },
        )

        loaded = load_shares(self.root)["auth-api"]
        text = (self.root / "shares.toml").read_text(encoding="utf-8")

        self.assertEqual(loaded.transport, "git")
        self.assertEqual(loaded.git_url, "https://github.com/user/rightmemory-shares.git")
        self.assertEqual(loaded.git_branch, "gh-pages")
        self.assertIn('transport = "git"', text)
        self.assertIn('git_url = "https://github.com/user/rightmemory-shares.git"', text)
        self.assertIn('git_branch = "gh-pages"', text)
        self.assertNotIn("hub_url", text)

    def test_load_rejects_part_without_config(self):
        (self.root / "shares.toml").write_text(
            '[shares.auth-api]\n'
            'version = 1\n'
            'role = "provider"\n'
            'title = "Auth API"\n'
            'state = "draft"\n'
            'parts = ["file"]\n',
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_shares(self.root)

        self.assertIn("file part requires [shares.auth-api.file]", str(caught.exception))

    def test_load_rejects_non_boolean_approved(self):
        (self.root / "shares.toml").write_text(
            '[shares.auth-api]\n'
            'version = 1\n'
            'role = "provider"\n'
            'title = "Auth API"\n'
            'state = "draft"\n'
            'parts = ["file"]\n'
            '[shares.auth-api.file]\n'
            'view_id = "auth-api-files"\n'
            'intent = "Expose auth context."\n'
            'approved = "false"\n',
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_shares(self.root)

        self.assertIn("file part approved for auth-api must be a boolean", str(caught.exception))


def _write_canonical_file_and_question_parts(root: Path):
    from rightmemory.shared_view_files import write_extractive_file_view_recipe
    from rightmemory.shared_view_questions import write_question_view

    write_extractive_file_view_recipe(
        root,
        view_id="auth-api-files",
        title="Auth API Files",
        intent="Expose auth API integration context.",
        include_nodes=("token-expiry",),
        approved=False,
        publish_hub_url="https://hub.example.test",
        publish_credential_id="alice-publish",
    )
    write_question_view(
        root,
        view_id="auth-api-ask",
        title="Auth API Questions",
        intent="Let frontend agents ask auth questions.",
        retriever_instructions="Answer from auth memory.",
        approved=False,
    )


class ShareProviderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Auth {#auth}\n\n- `token-expiry` Tokens expire.\n", encoding="utf-8")
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )

    def test_create_share_builds_requested_parts_unapproved(self):
        with (
            patch("rightmemory.shares.run_file_view_builder", return_value="wrote file view recipe auth-api-files") as file_builder,
            patch("rightmemory.shares.run_question_view_builder", return_value="wrote question view auth-api-ask") as question_builder,
        ):
            result = create_share(
                self.root,
                "auth-api",
                title="Auth API",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                file_intent="Expose auth API integration context.",
                question_intent="Let frontend agents ask auth questions.",
                question_base_url="https://provider.example.test",
            )

        share = load_shares(self.root)["auth-api"]
        self.assertIn("created share auth-api", result)
        self.assertEqual(share.parts, ("file", "question"))
        self.assertEqual(share.file.view_id, "auth-api-files")
        self.assertEqual(share.question.view_id, "auth-api-ask")
        self.assertFalse(share.file.approved)
        self.assertFalse(share.question.approved)
        file_builder.assert_called_once()
        question_builder.assert_called_once()

    def test_create_share_from_request_returns_operation_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="both",
            builder_final_message="Built file context and live questions.",
        )

        with patch("rightmemory.shares.run_share_builder", return_value=expected) as builder:
            result = create_share_from_request(
                self.root,
                share_id="auth-api",
                title="Auth API",
                provider_id="alice",
                hub_url="https://hub.example.test/",
                credential_id="alice-publish",
                request="Share the auth API context and allow live questions.",
                capability="both",
                question_base_url="https://provider.example.test",
            )

        self.assertEqual(result, expected)
        builder.assert_called_once_with(
            self.root,
            share_id_hint="auth-api",
            title_hint="Auth API",
            request="Share the auth API context and allow live questions.",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="both",
            question_base_url="https://provider.example.test",
        )

    def test_create_share_request_formats_operation_result(self):
        with patch(
            "rightmemory.shares.create_share_from_request",
            return_value=ShareOperationResult(
                share_id="auth-api",
                title="Auth API",
                role="provider",
                state="draft",
                capability="file_context",
                builder_final_message="Built auth context.",
                next_action="rightmemory share approve auth-api",
            ),
        ):
            result = create_share(
                self.root,
                "auth-api",
                title="Auth API",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                request="Share auth API context.",
            )

        self.assertIn("auth-api provider draft capability=file_context", result)
        self.assertIn("Built auth context.", result)
        self.assertIn("rightmemory share approve auth-api", result)

    def test_revise_share_returns_operation_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="live_questions",
            builder_final_message="Updated live question scope.",
        )

        with patch("rightmemory.shares.revise_share_builder", return_value=expected) as reviser:
            result = revise_share(
                self.root,
                "auth-api",
                "Only answer questions about refresh-token behavior.",
                capability="live-questions",
                question_base_url="https://provider.example.test",
            )

        self.assertEqual(result, expected)
        reviser.assert_called_once_with(
            self.root,
            "auth-api",
            "Only answer questions about refresh-token behavior.",
            capability="live-questions",
            question_base_url="https://provider.example.test",
        )

    def test_approve_share_approves_all_parts(self):
        _write_canonical_file_and_question_parts(self.root)
        create_share(
            self.root,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )

        result = approve_share(self.root, "auth-api")

        share = load_shares(self.root)["auth-api"]
        self.assertEqual(result, "approved share auth-api")
        self.assertEqual(share.state, "approved")
        self.assertTrue(share.file.approved)
        self.assertTrue(share.question.approved)

    def test_publish_share_creates_one_bundled_invite(self):
        _write_canonical_file_and_question_parts(self.root)
        create_share(
            self.root,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )
        approve_share(self.root, "auth-api")

        with (
            patch("rightmemory.shares.publish_file_view_package", return_value={"view_id": "auth-api-files"}),
            patch("rightmemory.shares.register_question_view_with_hub", return_value={"view_id": "auth-api-ask"}),
            patch("rightmemory.shares.HubClient") as client_type,
        ):
            client_type.return_value.create_share_invitation.return_value = {
                "invitation_url": "https://hub.example.test/i/share/share-token"
            }
            result = publish_share(self.root, "auth-api", label="frontend")

        share = load_shares(self.root)["auth-api"]
        self.assertIn("published share auth-api", result)
        self.assertIn("https://hub.example.test/i/share/share-token", result)
        self.assertEqual(share.state, "published")
        client_type.return_value.create_share_invitation.assert_called_once_with(
            "auth-api",
            title="Auth API",
            parts=[
                {"type": "file", "view_id": "auth-api-files"},
                {"type": "question", "view_id": "auth-api-ask"},
            ],
            label="frontend",
            expires_at=None,
        )


class ShareConsumerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

    def test_join_share_accepts_bundle_and_creates_relationship_and_connections(self):
        with (
            patch("rightmemory.shares.HubClient") as client_type,
            patch("rightmemory.shares.pull_file_view") as pull,
        ):
            client_type.return_value.get_share_invitation.return_value = {
                "share_id": "auth-api",
                "title": "Auth API",
                "provider_id": "alice",
                "parts": [
                    {"type": "file", "view_id": "auth-api-files"},
                    {"type": "question", "view_id": "auth-api-ask", "question_base_url": "https://provider.example.test"},
                ],
            }
            client_type.return_value.accept_share_invitation.return_value = {
                "share_id": "auth-api",
                "title": "Auth API",
                "provider_id": "alice",
                "parts": [
                    {
                        "type": "file",
                        "view_id": "auth-api-files",
                        "connection_token": "file-connection-token",
                    },
                    {
                        "type": "question",
                        "view_id": "auth-api-ask",
                        "connection_token": "question-connection-token",
                        "question_token": "question-token",
                    },
                ],
            }
            pull.return_value = type(
                "PullResult",
                (),
                {"heading_id": "auth-api-files", "status": "pulled", "message": "file view pulled"},
            )()

            result = join_share(self.root, "https://hub.example.test/i/share/share-token", consumer_label="frontend")

        shares = load_shares(self.root)
        self.assertIn("joined share auth-api", result)
        self.assertEqual(shares["auth-api"].state, "joined")
        connections_text = (self.root / "shared_views.toml").read_text(encoding="utf-8")
        memory_text = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("[connections.auth-api-files]", connections_text)
        self.assertIn("[connections.auth-api-ask]", connections_text)
        self.assertIn("{MF#auth-api-files}", memory_text)
        self.assertIn("{MQ#auth-api-ask}", memory_text)
        self.assertEqual(load_shared_view_credential(self.root, "http-auth-api-files")["token"], "file-connection-token")
        self.assertEqual(load_shared_view_credential(self.root, "http-auth-api-ask-question")["token"], "question-token")

    def test_share_status_summarizes_relationship(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="consumer",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    state="joined",
                    parts=("file",),
                    file=ShareFilePart(heading_id="auth-api-files"),
                )
            },
        )

        result = share_status(self.root, "auth-api")

        self.assertIn("auth-api provider=alice state=joined parts=file", result)
        self.assertIn("file auth-api-files", result)

    def test_share_status_probes_consumer_question_endpoint(self):
        self._save_consumer_question_share()

        with patch("rightmemory.shares.HubClient") as client_type:
            client_type.return_value.probe_question.return_value = {"data": {"status": "ready"}}
            result = share_status(self.root, "auth-api")

        self.assertIn("question auth-api-ask ready", result)
        client_type.assert_called_once_with("https://provider.example.test", "question-token", timeout=5)
        client_type.return_value.probe_question.assert_called_once_with("auth-api-ask")

    def test_share_status_reports_consumer_question_unreachable_when_probe_fails(self):
        self._save_consumer_question_share()

        with patch("rightmemory.shares.HubClient") as client_type:
            client_type.return_value.probe_question.side_effect = HubClientError("hub request failed: refused")
            result = share_status(self.root, "auth-api")

        self.assertIn("question auth-api-ask unreachable", result)

    def _save_consumer_question_share(self):
        save_shared_view_credential(
            self.root,
            "http-auth-api-ask-question",
            kind="http-question",
            token="question-token",
            base_url="https://provider.example.test",
            view_id="auth-api-ask",
        )
        accept_shared_view(
            self.root,
            heading_id="auth-api-ask",
            view_type="question",
            title="Auth API Questions",
            body="Accepted as part of share auth-api.",
            ref="rightmemory://mq/auth-api-ask",
            maintainer="alice",
            accepted_from="https://hub.example.test/i/share/share-token",
            target=SharedViewTarget(
                kind="http-question",
                base_url="https://hub.example.test",
                view_id="auth-api-ask",
                credential_id="http-auth-api-ask",
                question_base_url="https://provider.example.test",
                question_credential_id="http-auth-api-ask-question",
                accepted_from_url="https://hub.example.test/i/share/share-token",
            ),
        )
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="consumer",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    state="joined",
                    parts=("question",),
                    question=ShareQuestionPart(
                        heading_id="auth-api-ask",
                        question_base_url="https://provider.example.test",
                    ),
                )
            },
        )


class _InProcessHubClient:
    apps_by_base_url: dict[str, object] = {}

    def __init__(self, base_url: str, token: str | None = None, *, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def publish_package(self, view_id: str, package_root: Path) -> dict[str, object]:
        endpoint = self._endpoint("/api/views/{view_id}/versions", "POST")
        response = asyncio.run(
            endpoint(
                view_id=view_id,
                request=self._request_object(
                    "POST",
                    f"/api/views/{quote(view_id)}/versions",
                    body=_zip_package(package_root),
                    headers=self._headers(bearer=True, content_type="application/zip"),
                ),
            )
        )
        return _ensure_mapping(response)

    def register_question_view(
        self,
        view_id: str,
        *,
        title: str,
        description: str,
        question_base_url: str,
        question_token: str,
    ) -> dict[str, object]:
        endpoint = self._endpoint("/api/views/{view_id}/question", "POST")
        response = endpoint(
            view_id,
            self._request_object(
                "POST",
                f"/api/views/{quote(view_id)}/question",
                headers=self._headers(bearer=True),
            ),
            payload={
                "title": title,
                "description": description,
                "question_base_url": question_base_url,
                "question_token": question_token,
            },
        )
        return _ensure_mapping(response)

    def create_share_invitation(
        self,
        share_id: str,
        *,
        title: str,
        parts: list[dict[str, str]],
        label: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"title": title, "parts": parts}
        if label:
            payload["label"] = label
        if expires_at:
            payload["expires_at"] = expires_at
        endpoint = self._endpoint("/api/shares/{share_id}/invitations", "POST")
        response = endpoint(
            share_id,
            self._request_object(
                "POST",
                f"/api/shares/{quote(share_id)}/invitations",
                headers=self._headers(bearer=True),
            ),
            payload=payload,
        )
        return _ensure_mapping(response)

    def get_share_invitation(self, token: str) -> dict[str, object]:
        endpoint = self._endpoint("/api/share-invitations/{token}/view", "GET")
        return _ensure_mapping(endpoint(token))

    def accept_share_invitation(self, token: str, *, consumer_label: str | None = None) -> dict[str, object]:
        payload = {"consumer_label": consumer_label} if consumer_label else {}
        endpoint = self._endpoint("/api/share-invitations/{token}/accept", "POST")
        return _ensure_mapping(endpoint(token, payload=payload))

    def download_package(self, view_id: str) -> bytes:
        endpoint = self._endpoint("/api/views/{view_id}/package", "GET")
        response = endpoint(
            view_id,
            self._request_object(
                "GET",
                f"/api/views/{quote(view_id)}/package",
                headers=self._headers(bearer=True),
            ),
        )
        if not hasattr(response, "body"):
            raise AssertionError("hub package endpoint did not return a response body")
        return bytes(response.body)

    def ask_question(self, view_id: str, question: str) -> dict[str, object]:
        endpoint = self._endpoint("/api/share/questions/{view_id}/ask", "POST")
        response = endpoint(
            view_id,
            self._request_object(
                "POST",
                f"/api/share/questions/{quote(view_id)}/ask",
                headers=self._headers(bearer=True),
            ),
            payload={"question": question},
        )
        return _ensure_mapping(response)

    def probe_question(self, view_id: str) -> dict[str, object]:
        endpoint = self._endpoint("/api/share/questions/{view_id}/ready", "GET")
        response = endpoint(
            view_id,
            self._request_object(
                "GET",
                f"/api/share/questions/{quote(view_id)}/ready",
                headers=self._headers(bearer=True),
            ),
        )
        return _ensure_mapping(response)

    def post_interaction(self, view_id: str, payload: dict[str, object]) -> dict[str, object]:
        endpoint = self._endpoint("/api/views/{view_id}/interactions", "POST")
        response = endpoint(
            view_id,
            self._request_object(
                "POST",
                f"/api/views/{quote(view_id)}/interactions",
                headers=self._headers(bearer=True),
            ),
            payload=payload,
        )
        return _ensure_mapping(response)

    def _headers(self, *, bearer: bool = False, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if bearer:
            if not self.token:
                raise AssertionError("hub token is required")
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["content-type"] = content_type
        return headers

    def _endpoint(self, path_template: str, method: str):
        app = self.apps_by_base_url.get(self.base_url)
        if app is None:
            raise AssertionError(f"no in-process app installed for {self.base_url}")
        for route in app.router.routes:
            if getattr(route, "path", None) == path_template and method in getattr(route, "methods", set()):
                return route.endpoint
        raise AssertionError(f"route not found: {method} {path_template}")

    def _request_object(self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> Request:
        parsed = urlsplit(f"{self.base_url}{path}")
        payload = body or b""
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}

        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": parsed.scheme,
                "path": parsed.path,
                "raw_path": parsed.path.encode("utf-8"),
                "query_string": parsed.query.encode("utf-8"),
                "headers": [
                    (key.lower().encode("utf-8"), value.encode("utf-8")) for key, value in (headers or {}).items()
                ],
                "client": ("127.0.0.1", 1),
                "server": (parsed.hostname or "localhost", parsed.port or 443),
                "root_path": "",
            },
            receive,
        )


def _zip_package(package: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package).as_posix())
    return buffer.getvalue()


class ShareEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.provider = self.root / "provider"
        self.consumer = self.root / "consumer"
        self.hub = self.root / "hub"
        self.provider.mkdir()
        self.consumer.mkdir()
        (self.provider / "MEMORY.md").write_text(
            "# Auth {#auth}\n\n- `token-expiry` Tokens expire after one hour. -> [rel:auth-api]\n",
            encoding="utf-8",
        )
        (self.consumer / "MEMORY.md").write_text("# Frontend\n", encoding="utf-8")
        self.store = HubStore(self.hub)
        self.store.initialize(admin_token="admin-secret", public_base_url="https://hub.example.test")
        self.provider_token = self.store.create_provider_token("alice", label="publish")
        _InProcessHubClient.apps_by_base_url = {
            "https://hub.example.test": create_hub_app(self.hub),
            "https://provider.example.test": create_web_app(self.provider),
        }
        self.addCleanup(_InProcessHubClient.apps_by_base_url.clear)

    def test_file_question_share_join_status(self):
        save_shared_view_credential(
            self.provider,
            "alice-publish",
            kind="http-publish",
            token=self.provider_token.raw_token,
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        _write_canonical_file_and_question_parts(self.provider)
        create_share(
            self.provider,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )
        approve_share(self.provider, "auth-api")

        with (
            patch("rightmemory.shared_view_files.HubClient", _InProcessHubClient),
            patch("rightmemory.shared_view_questions.HubClient", _InProcessHubClient),
            patch("rightmemory.shared_views.HubClient", _InProcessHubClient),
            patch("rightmemory.shares.HubClient", _InProcessHubClient),
            patch(
                "rightmemory.shared_view_questions._run_provider_question",
                side_effect=lambda root, provider_role, view_id, prompt, started: _answer_in_process_question(
                    started,
                    "Tokens expire after one hour.",
                ),
            ),
        ):
            published = publish_share(self.provider, "auth-api", label="frontend")
            self.assertIn("/i/share/", published)
            invitation_url = published.split("invitation_url\t", 1)[1].strip()

            joined = join_share(self.consumer, invitation_url, consumer_label="frontend")
            answer = ask_question_view(self.consumer, "auth-api-ask", "How do tokens refresh?")
            note = record_shared_view_note(self.consumer, "auth-api-files", "Docs are stale.", confirmed=True)

        self.assertIn("joined share auth-api", joined)
        self.assertIn("Status: answered", answer)
        self.assertIn("Tokens expire after one hour.", answer)
        self.assertIn("recorded shared view note", note)
        with patch("rightmemory.shares.HubClient", _InProcessHubClient):
            status = share_status(self.consumer, "auth-api")
        self.assertIn("auth-api provider=alice state=joined parts=file,question", status)
        self.assertIn("file auth-api-files pulled", status)
        self.assertIn("question auth-api-ask ready", status)
        memory_text = (self.consumer / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("{MF#auth-api-files}", memory_text)
        self.assertIn("{MQ#auth-api-ask}", memory_text)
        inbox = self.store.list_provider_inbox("alice")
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["view_id"], "auth-api-files")


def _answer_in_process_question(started, answer: str) -> str:
    started.set()
    return answer
