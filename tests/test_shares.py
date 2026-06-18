import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.share_models import (
    ShareFilePart,
    ShareQuestionPart,
    ShareRelationship,
    load_shares,
    save_shares,
    validate_share_id,
)
from rightmemory.shared_view_models import load_shared_view_credential, save_shared_view_credential
from rightmemory.shares import approve_share, create_share, join_share, publish_share, share_status


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


def _write_canonical_file_and_question_parts(root: Path):
    from rightmemory.shared_view_files import write_file_view_recipe
    from rightmemory.shared_view_questions import write_question_view

    write_file_view_recipe(
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
