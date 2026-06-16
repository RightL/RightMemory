import io
import json
import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rightmemory.hub.client import HubClientError
from rightmemory.shared_view_builder import run_file_view_builder
from rightmemory.shared_view_files import (
    FileViewPullResult,
    FileViewPublishResult,
    approve_file_view,
    export_file_view_package,
    publish_approved_file_views,
    pull_file_view,
    record_file_view_publish_results,
    render_file_view,
    write_file_view_recipe,
)
from rightmemory.shared_view_models import (
    SharedViewConnection,
    SharedViewTarget,
    load_connections,
    load_shared_view_credential,
    save_connections,
    validate_connection,
)
from rightmemory.shared_view_questions import (
    _run_provider_question,
    answer_question_view,
    ask_question_view,
    publish_question_view,
    question_token_hash,
    verify_question_view_token,
    write_question_view,
)
from rightmemory.shared_views import (
    accept_http_shared_view_invitation,
    list_shared_view_notes,
    record_shared_view_note,
    save_shared_view_credential,
    shared_view_connection_status,
)


class SharedViewModelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_save_and_load_file_and_question_connections(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        view_id="auth-api-files",
                        credential_id="http-auth-api-files",
                    ),
                ),
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://hub.example.test",
                        view_id="auth-api-ask",
                        credential_id="http-auth-api-ask",
                        question_base_url="https://provider.example.test",
                        question_credential_id="http-auth-api-ask-question",
                    ),
                ),
            },
        )

        loaded = load_connections(self.root)

        self.assertEqual(loaded["auth-api-files"].view_type, "file")
        self.assertEqual(loaded["auth-api-files"].target.kind, "http-file")
        self.assertEqual(loaded["auth-api-ask"].view_type, "question")
        self.assertEqual(loaded["auth-api-ask"].target.kind, "http-question")

    def test_provider_root_targets_are_rejected(self):
        with self.assertRaises(ValueError) as caught:
            validate_connection(
                self.root,
                "auth-api-files",
                SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(kind="local", path="/tmp/alice"),
                ),
            )

        self.assertIn("unknown shared view target kind `local`", str(caught.exception))

    def test_accept_http_question_invitation_uses_direct_provider_endpoint(self):
        with patch("rightmemory.shared_views.HubClient") as client_type:
            client = client_type.return_value
            client.get_invitation_view.return_value = {
                "view_id": "auth-api-ask",
                "kind": "question",
                "title": "Auth API Questions",
                "ref": "rightmemory://mq/auth-api-ask",
                "question_base_url": "https://provider.example.test",
            }
            client.accept_invitation.return_value = {
                "view_id": "auth-api-ask",
                "connection_token": "connection-token",
                "question_token": "question-token",
            }
            result = accept_http_shared_view_invitation(self.root, "https://hub.example.test/i/invite-token")

        connection = load_connections(self.root)["auth-api-ask"]
        credential = load_shared_view_credential(self.root, "http-auth-api-ask")
        self.assertEqual(result, "accepted shared view auth-api-ask")
        self.assertEqual(connection.view_type, "question")
        self.assertEqual(connection.target.kind, "http-question")
        self.assertEqual(connection.target.base_url, "https://hub.example.test")
        self.assertEqual(connection.target.question_base_url, "https://provider.example.test")
        self.assertEqual(connection.target.credential_id, "http-auth-api-ask")
        self.assertEqual(connection.target.question_credential_id, "http-auth-api-ask-question")
        question_credential = load_shared_view_credential(self.root, "http-auth-api-ask-question")
        self.assertEqual(credential["base_url"], "https://hub.example.test")
        self.assertEqual(credential["token"], "connection-token")
        self.assertEqual(question_credential["base_url"], "https://provider.example.test")
        self.assertEqual(question_credential["token"], "question-token")
        self.assertIn("{MQ#auth-api-ask}", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

        with patch("rightmemory.shared_views.HubClient") as note_client_type:
            note_client_type.return_value.post_interaction.return_value = {"status": "recorded"}
            note_result = record_shared_view_note(self.root, "auth-api-ask", "Docs are stale.", confirmed=True)

        self.assertIn("recorded shared view note", note_result)
        self.assertEqual(note_client_type.call_args.args, ("https://hub.example.test", "connection-token"))

    def test_accept_http_question_invitation_requires_question_endpoint(self):
        with patch("rightmemory.shared_views.HubClient") as client_type:
            client = client_type.return_value
            client.get_invitation_view.return_value = {
                "view_id": "auth-api-ask",
                "kind": "question",
                "title": "Auth API Questions",
                "ref": "rightmemory://mq/auth-api-ask",
            }
            client.accept_invitation.return_value = {
                "view_id": "auth-api-ask",
                "connection_token": "connection-token",
                "question_token": "question-token",
            }

            with self.assertRaises(ValueError) as caught:
                accept_http_shared_view_invitation(self.root, "https://hub.example.test/i/invite-token")

        self.assertIn("question_base_url", str(caught.exception))
        client.accept_invitation.assert_not_called()

    def test_accept_http_question_invitation_requires_question_token(self):
        with patch("rightmemory.shared_views.HubClient") as client_type:
            client = client_type.return_value
            client.get_invitation_view.return_value = {
                "view_id": "auth-api-ask",
                "kind": "question",
                "title": "Auth API Questions",
                "ref": "rightmemory://mq/auth-api-ask",
                "question_base_url": "https://provider.example.test",
            }
            client.accept_invitation.return_value = {
                "view_id": "auth-api-ask",
                "connection_token": "connection-token",
            }

            with self.assertRaises(ValueError) as caught:
                accept_http_shared_view_invitation(self.root, "https://hub.example.test/i/invite-token")

        self.assertIn("question_token", str(caught.exception))


class SharedFileViewRecipeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [rel:auth-api]\n"
            "- `private-payroll` Payroll details stay private. -> [rel:auth-api]\n",
            encoding="utf-8",
        )

    def test_file_recipe_renders_selected_context_without_excluded_ids(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            include_nodes=["token-expiry"],
            exclude_ids=["private-payroll"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        result = render_file_view(self.root, "auth-api-files")

        exported = self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md"
        recipe = self.root / "shared_views" / "auth-api-files" / "recipe.toml"
        self.assertIn("rendered file view auth-api-files", result)
        self.assertIn("Tokens expire after one hour.", exported.read_text(encoding="utf-8"))
        self.assertNotIn("Payroll details", exported.read_text(encoding="utf-8"))
        self.assertIn('kind = "file"', recipe.read_text(encoding="utf-8"))

    def test_file_recipe_excludes_nested_heading_subtree(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "Public auth context.\n\n"
            "### Internal Tokens {#internal-tokens}\n\n"
            "- `secret-token` Private token shape. -> [rel:internal-tokens]\n\n"
            "### Public Tokens {#public-tokens}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [rel:public-tokens]\n",
            encoding="utf-8",
        )
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            exclude_ids=["internal-tokens"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        render_file_view(self.root, "auth-api-files")

        exported = (self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Public auth context.", exported)
        self.assertIn("Tokens expire after one hour.", exported)
        self.assertNotIn("Internal Tokens", exported)
        self.assertNotIn("Private token shape", exported)

    def test_file_package_does_not_include_retriever_prompt(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        package = self.root / "package"

        export_file_view_package(self.root, "auth-api-files", package)

        self.assertTrue((package / "view.md").exists())
        self.assertTrue((package / "recipe.toml").exists())
        self.assertTrue((package / "dist" / "MEMORY.md").exists())
        self.assertFalse((package / "retriever.md").exists())

    def test_approve_file_view_sets_approved_true(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth context.",
            include_headings=["auth-api"],
            approved=False,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        result = approve_file_view(self.root, "auth-api-files")

        self.assertIn("approved file view auth-api-files", result)
        recipe = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        self.assertIn("approved = true", recipe)

    def test_file_view_builder_renders_generated_dist_preview(self):
        def fake_builder(memory_root, view_id, message):
            write_file_view_recipe(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose auth API integration context.",
                include_headings=["auth-api"],
                approved=False,
                publish_hub_url="https://hub.example.test",
                publish_credential_id="alice-publish",
            )
            return "built file view auth-api-files"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            result = run_file_view_builder(
                self.root,
                view_id="auth-api-files",
                title="Auth API Files",
                intent="Expose auth API integration context.",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
            )

        preview = self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md"
        self.assertEqual(result, "built file view auth-api-files")
        self.assertTrue(preview.is_file())
        self.assertIn("Tokens expire after one hour.", preview.read_text(encoding="utf-8"))


class SharedFileViewPullTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        view_id="auth-api-files",
                        credential_id="http-auth-api-files",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-files",
            kind="http-connection",
            token="connection-token",
            base_url="https://hub.example.test",
            view_id="auth-api-files",
        )

    def test_pull_file_view_replaces_import_atomically(self):
        archive = _zip_bytes(
            {
                "view.md": "# Auth API Files\n",
                "recipe.toml": 'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
                "rightmemory-shared-view.toml": 'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
                "dist/MEMORY.md": "# Published Context\n\nTokens expire after one hour.\n",
                "dist/manifest.toml": 'version = 1\nview_id = "auth-api-files"\n',
            }
        )

        with patch("rightmemory.shared_view_files.HubClient") as client_type:
            client_type.return_value.download_package.return_value = archive
            result = pull_file_view(self.root, "auth-api-files")

        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
        self.assertEqual(result.status, "pulled")
        self.assertIn("Tokens expire", (imported / "dist" / "MEMORY.md").read_text(encoding="utf-8"))

    def test_pull_file_view_falls_back_to_stale_import(self):
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("stale but usable\n", encoding="utf-8")

        with patch("rightmemory.shared_view_files.HubClient") as client_type:
            client_type.return_value.download_package.side_effect = HubClientError("offline")
            result = pull_file_view(self.root, "auth-api-files")

        self.assertEqual(result.status, "stale")
        self.assertIn("stale but usable", (imported / "MEMORY.md").read_text(encoding="utf-8"))


class SharedFileViewAutoPublishTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n## Auth API {#auth-api}\n\n- `token-expiry` Tokens expire. -> [rel:auth-api]\n",
            encoding="utf-8",
        )
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

    def test_publish_approved_file_views_renders_and_uploads(self):
        clients = []

        with patch("rightmemory.shared_view_files.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            results = publish_approved_file_views(self.root)

        self.assertEqual(results[0].status, "published")
        self.assertEqual(clients[0].base_url, "https://hub.example.test")
        self.assertIn("dist/MEMORY.md", clients[0].publish_calls[0]["files"])

    def test_record_file_view_publish_results_logs_failures(self):
        record_file_view_publish_results(
            self.root,
            [FileViewPublishResult("auth-api-files", "failed", "hub offline")],
            trigger="update-write",
        )

        path = self.root / ".runtime" / "shared_views" / "publish-events.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["view_id"], "auth-api-files")
        self.assertEqual(records[0]["status"], "failed")
        self.assertEqual(records[0]["message"], "hub offline")
        self.assertEqual(records[0]["trigger"], "update-write")


class SharedQuestionViewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_write_question_view_config(self):
        result = write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            access_tokens=["question-token"],
        )

        view_dir = self.root / "shared_views" / "auth-api-ask"
        self.assertIn("wrote question view auth-api-ask", result)
        self.assertTrue((view_dir / "view.md").exists())
        self.assertTrue((view_dir / "retriever.md").exists())
        question_toml = (view_dir / "question.toml").read_text(encoding="utf-8")
        self.assertIn('kind = "question"', question_toml)
        self.assertIn(question_token_hash("question-token"), question_toml)
        self.assertTrue(verify_question_view_token(self.root, "auth-api-ask", "question-token"))
        self.assertFalse(verify_question_view_token(self.root, "auth-api-ask", "wrong-token"))

    def test_publish_question_view_registers_question_metadata_and_hashes_token(self):
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
        )
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        clients = []

        with patch("rightmemory.shared_view_questions.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            result = publish_question_view(
                self.root,
                "auth-api-ask",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                question_base_url="https://provider.example.test",
                label="frontend",
                expires_at="2026-07-01T00:00:00+00:00",
            )

        client = clients[0]
        question_token = client.question_registrations[0]["question_token"]
        question_toml = (self.root / "shared_views" / "auth-api-ask" / "question.toml").read_text(encoding="utf-8")
        self.assertIn("published question view auth-api-ask", result)
        self.assertIn("invitation_url\thttps://hub.example.test/i/invite-token", result)
        self.assertEqual(client.base_url, "https://hub.example.test")
        self.assertEqual(client.token, "publish-token")
        self.assertEqual(client.question_registrations[0]["view_id"], "auth-api-ask")
        self.assertEqual(client.question_registrations[0]["question_base_url"], "https://provider.example.test")
        self.assertEqual(client.invitation_calls[0]["label"], "frontend")
        self.assertEqual(client.invitation_calls[0]["expires_at"], "2026-07-01T00:00:00+00:00")
        self.assertIn(question_token_hash(question_token), question_toml)
        self.assertNotIn(question_token, question_toml)

    def test_run_provider_question_sets_started_from_runtime_callback(self):
        started = threading.Event()
        observations = []

        class FakeRuntime:
            def __init__(self, config):
                self.config = config

            def run_session_turn(self, session_id, prompt, *, on_started=None):
                observations.append(started.is_set())
                if on_started is not None:
                    on_started()
                observations.append(started.is_set())
                return "Use token_expires_at."

            def cleanup(self):
                observations.append("cleanup")

        with (
            patch("rightmemory.config.load_config", return_value=object()),
            patch("rightmemory.runtime.RightMemoryRuntime", FakeRuntime),
        ):
            answer = _run_provider_question(
                self.root,
                "retrieve",
                "auth-api-ask",
                "Provider question:\nHow do tokens refresh?",
                started,
            )

        self.assertEqual(answer, "Use token_expires_at.")
        self.assertEqual(observations, [False, True, "cleanup"])

    def test_ask_question_view_returns_unavailable_when_provider_does_not_start(self):
        save_connections(
            self.root,
            {
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://hub.example.test",
                        view_id="auth-api-ask",
                        credential_id="http-auth-api-ask",
                        question_base_url="https://provider.example.test",
                        question_credential_id="http-auth-api-ask-question",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-ask-question",
            kind="http-question",
            token="question-token",
            base_url="https://provider.example.test",
            view_id="auth-api-ask",
        )

        with patch("rightmemory.shared_view_questions.HubClient") as client_type:
            client_type.return_value.ask_question.side_effect = HubClientError("provider did not start")
            result = ask_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider did not start", result)

    def test_ask_question_view_preserves_provider_unavailable_payload(self):
        save_connections(
            self.root,
            {
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://hub.example.test",
                        view_id="remote-auth-api-ask",
                        credential_id="http-auth-api-ask",
                        question_base_url="https://provider.example.test",
                        question_credential_id="http-auth-api-ask-question",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-ask-question",
            kind="http-question",
            token="question-token",
            base_url="https://provider.example.test",
            view_id="remote-auth-api-ask",
        )
        provider_text = "Shared question: remote-auth-api-ask\nStatus: unavailable\nReason: provider is busy\n"

        with patch("rightmemory.shared_view_questions.HubClient") as client_type:
            client_type.return_value.ask_question.return_value = {
                "status": "ok",
                "data": {
                    "status": "unavailable",
                    "reason": "provider is busy",
                    "text": provider_text,
                },
            }
            result = ask_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Shared question: auth-api-ask", result)
        self.assertIn("Status: unavailable", result)
        self.assertIn("provider is busy", result)

    def test_answer_question_view_times_out_when_provider_does_not_start(self):
        release = threading.Event()
        self.addCleanup(release.set)
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            start_timeout_seconds=1,
            answer_timeout_seconds=1,
        )

        def blocked_start(root, provider_role, view_id, prompt, started):
            release.wait(5)
            return "too late"

        with patch("rightmemory.shared_view_questions._run_provider_question", side_effect=blocked_start):
            result = answer_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider did not start within 1 seconds", result)

    def test_answer_question_view_times_out_after_provider_starts(self):
        release = threading.Event()
        self.addCleanup(release.set)
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
            start_timeout_seconds=1,
            answer_timeout_seconds=1,
        )

        def slow_answer(root, provider_role, view_id, prompt, started):
            started.set()
            release.wait(5)
            return "too late"

        with patch("rightmemory.shared_view_questions._run_provider_question", side_effect=slow_answer):
            result = answer_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider answer timed out after 1 seconds", result)

    def test_connection_status_reports_file_imports_and_question_targets(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        credential_id="http-auth-api-files",
                    ),
                ),
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://hub.example.test",
                        credential_id="http-auth-api-ask",
                        question_base_url="https://provider.example.test",
                        question_credential_id="http-auth-api-ask-question",
                    ),
                ),
            },
        )
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("published context\n", encoding="utf-8")

        file_status = shared_view_connection_status(self.root, "auth-api-files")
        question_status = shared_view_connection_status(self.root, "auth-api-ask")

        self.assertEqual(file_status["status"], "imported")
        self.assertEqual(question_status["status"], "configured")


class SharedViewInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_note_requires_http_target(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(kind="none"),
                )
            },
        )

        result = record_shared_view_note(self.root, "auth-api-files", "Docs are stale.", confirmed=True)

        self.assertEqual(result, "shared view auth-api-files does not have an HTTP interaction target")

    def test_note_posts_to_http_for_file_and_question_views(self):
        for view_type, target_kind, heading_id, ref in (
            ("file", "http-file", "auth-api-files", "rightmemory://mf/auth-api-files"),
            ("question", "http-question", "auth-api-ask", "rightmemory://mq/auth-api-ask"),
        ):
            target = SharedViewTarget(
                kind=target_kind,
                base_url="https://hub.example.test",
                view_id=heading_id,
                credential_id=f"http-{heading_id}",
            )
            if view_type == "question":
                target = SharedViewTarget(
                    kind=target_kind,
                    base_url="https://hub.example.test",
                    view_id=heading_id,
                    credential_id=f"http-{heading_id}",
                    question_base_url="https://provider.example.test",
                    question_credential_id=f"http-{heading_id}-question",
                )
            save_connections(
                self.root,
                {
                    heading_id: SharedViewConnection(
                        heading_id=heading_id,
                        view_type=view_type,
                        ref=ref,
                        target=target,
                    )
                },
            )
            save_shared_view_credential(
                self.root,
                f"http-{heading_id}",
                kind="http-connection",
                token="connection-token",
                base_url="https://hub.example.test",
                view_id=heading_id,
            )

            with patch("rightmemory.shared_views.HubClient") as client_type:
                client_type.return_value.post_interaction.return_value = {"status": "recorded"}
                result = record_shared_view_note(self.root, heading_id, "Docs are stale.", confirmed=True)

            self.assertIn("recorded shared view note", result)
            self.assertEqual(client_type.call_args.args, ("https://hub.example.test", "connection-token"))
            self.assertEqual(len(list_shared_view_notes(self.root, heading_id)), 1)

    def test_note_http_failure_is_recorded_as_failed_not_queued(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        view_id="auth-api-files",
                        credential_id="http-auth-api-files",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-files",
            kind="http-connection",
            token="connection-token",
            base_url="https://hub.example.test",
            view_id="auth-api-files",
        )

        with patch("rightmemory.shared_views.HubClient") as client_type:
            client_type.return_value.post_interaction.side_effect = HubClientError("offline")
            result = record_shared_view_note(self.root, "auth-api-files", "Docs are stale.", confirmed=True)

        notes = list_shared_view_notes(self.root, "auth-api-files")
        self.assertEqual(result, "failed to send shared view note for auth-api-files")
        self.assertEqual(notes[0]["status"], "failed")
        self.assertNotEqual(notes[0]["status"], "queued")


class _FakeHubClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token
        self.publish_calls = []
        self.question_registrations = []
        self.invitation_calls = []

    def publish_package(self, view_id: str, package_root: Path):
        self.publish_calls.append(
            {
                "view_id": view_id,
                "files": sorted(path.relative_to(package_root).as_posix() for path in package_root.rglob("*") if path.is_file()),
            }
        )
        return {"version_id": "ver_1"}

    def register_question_view(self, view_id: str, **kwargs):
        self.question_registrations.append({"view_id": view_id, **kwargs})
        return {"version_id": "ver_1", "view_id": view_id}

    def create_invitation(self, view_id: str, *, label: str | None = None, expires_at: str | None = None):
        self.invitation_calls.append({"view_id": view_id, "label": label, "expires_at": expires_at})
        return {"invitation_url": "https://hub.example.test/i/invite-token"}


def _record_fake_client(clients: list[_FakeHubClient], base_url: str, token: str) -> _FakeHubClient:
    client = _FakeHubClient(base_url, token)
    clients.append(client)
    return client


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()
