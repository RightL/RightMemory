import io
import json
import shutil
import subprocess
import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rightmemory.hub.client import HubClientError
from rightmemory.shared_view_builder import refresh_file_view, run_file_view_builder, run_question_view_builder
from rightmemory.shared_view_files import (
    FileViewPullResult,
    FileViewPublishResult,
    approve_file_view,
    export_file_view_package,
    invite_file_view,
    list_file_view_publish_events,
    load_file_view_recipe,
    prepare_file_view_publish_outbox,
    publish_file_view_package,
    publish_approved_file_views,
    publish_file_view_outbox,
    pull_file_view,
    record_file_view_publish_results,
    render_file_view,
    validate_file_view_recipe_source,
    write_extractive_file_view_recipe,
    write_generative_file_view,
)
from rightmemory.shared_view_models import (
    SharedViewConnection,
    SharedViewTarget,
    load_connections,
    load_shared_view_credential,
    list_shared_view_credentials,
    save_connections,
    validate_connection,
)
from rightmemory.shared_view_questions import (
    _run_provider_question,
    answer_question_view,
    ask_question_view,
    load_question_view,
    publish_question_view,
    question_token_hash,
    register_question_view_with_hub,
    verify_question_view_token,
    write_question_view,
)
from rightmemory.shared_views import (
    accept_shared_view,
    accept_http_shared_view_invitation,
    list_shared_view_notes,
    record_shared_view_note,
    save_shared_view_credential,
    shared_view_connection_status,
)


GENERATED_MEMORY = (
    "# Auth API {#auth-api} → []\n\n"
    "- `token-expiry` Tokens expire after one hour. → []\n"
)


def _write_valid_import_package(root: Path, *, memory: str = GENERATED_MEMORY) -> None:
    (root / "dist").mkdir(parents=True, exist_ok=True)
    (root / "view.md").write_text("# Auth API Files\n", encoding="utf-8")
    (root / "recipe.toml").write_text(
        'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
        encoding="utf-8",
    )
    (root / "rightmemory-shared-view.toml").write_text(
        'version = 2\nview_id = "auth-api-files"\nkind = "file"\n',
        encoding="utf-8",
    )
    (root / "dist" / "MEMORY.md").write_text(memory, encoding="utf-8")
    (root / "dist" / "manifest.toml").write_text(
        'version = 2\nview_id = "auth-api-files"\ndocument_kind = "rightmemory-memory"\n',
        encoding="utf-8",
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

    def test_git_file_target_round_trips(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="git-file",
                        view_id="auth-api-files",
                        git_url="https://github.com/user/rightmemory-shares.git",
                        git_branch="gh-pages",
                        git_share_id="auth-api",
                        accepted_from_url=(
                            "https://github.com/user/rightmemory-shares.git"
                            "#share=auth-api&branch=gh-pages"
                        ),
                    ),
                )
            },
        )

        target = load_connections(self.root)["auth-api-files"].target

        self.assertEqual(target.kind, "git-file")
        self.assertEqual(target.git_url, "https://github.com/user/rightmemory-shares.git")
        self.assertEqual(target.git_branch, "gh-pages")
        self.assertEqual(target.git_share_id, "auth-api")

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

    def test_list_shared_view_credentials_omits_tokens(self):
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="secret-token",
            base_url="https://hub.example.test",
            provider_id="alice",
            view_id="auth-api-files",
        )

        credentials = list_shared_view_credentials(self.root)

        self.assertEqual(
            credentials,
            [
                {
                    "credential_id": "alice-publish",
                    "kind": "http-publish",
                    "base_url": "https://hub.example.test",
                    "provider_id": "alice",
                    "view_id": "auth-api-files",
                    "created_at": credentials[0]["created_at"],
                }
            ],
        )
        self.assertNotIn("token", credentials[0])

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

    def test_accept_shared_view_rejects_id_already_owned_by_pursuit(self):
        (self.root / "MEMORY.md").write_text("# Memory {#memory}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Auth API Work {#auth-api-ask}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, r"shared view id `auth-api-ask` already exists as a pursuit heading"):
            accept_shared_view(
                self.root,
                heading_id="auth-api-ask",
                view_type="question",
                title="Auth API Questions",
                body="Ask the provider.",
                ref="rightmemory://mq/auth-api-ask",
            )

        self.assertNotIn("{MQ#auth-api-ask}", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertNotIn("auth-api-ask", load_connections(self.root))


class SharedFileViewRecipeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [doc:auth-api]\n"
            "- `private-payroll` Payroll details stay private. -> [doc:auth-api]\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits {#pursuits} → []\n", encoding="utf-8")

    def test_file_recipe_renders_selected_context_without_excluded_ids(self):
        write_extractive_file_view_recipe(
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

    def test_exact_node_preserves_ancestor_bodies_without_sibling_leaks(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "Project context.\n\n"
            "## Auth API {#auth-api}\n\n"
            "Auth context.\n\n"
            "### Public Tokens {#public-tokens}\n\n"
            "Public token context.\n\n"
            "- `token-expiry` Tokens expire after one hour. -> []\n"
            "- `token-shape` Tokens use JWT. -> []\n\n"
            "### Private Tokens {#private-tokens}\n\n"
            "Private token context.\n\n"
            "- `private-token` Private token material. -> []\n\n"
            "## Billing {#billing}\n\n"
            "Billing context.\n\n"
            "- `invoice-format` Invoices use JSON. -> []\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="token-expiry",
            title="Token Expiry",
            intent="Expose only token expiry with its interpreting context.",
            include_nodes=["token-expiry", "private-token"],
            exclude_ids=["private-tokens"],
            approved=True,
        )

        render_file_view(self.root, "token-expiry")

        exported = (self.root / "shared_views" / "token-expiry" / "dist" / "MEMORY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Project context.", exported)
        self.assertIn("Auth context.", exported)
        self.assertIn("Public token context.", exported)
        self.assertIn("- `token-expiry`", exported)
        self.assertNotIn("token-shape", exported)
        self.assertNotIn("Private Tokens", exported)
        self.assertNotIn("Private token material", exported)
        self.assertNotIn("Billing", exported)
        self.assertNotIn("invoice-format", exported)

    def test_exact_f_detail_node_preserves_logical_ancestor_bodies(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "Project context.\n\n"
            "## Auth Detail {F#auth-detail}\n\n"
            "Auth detail summary.\n\n"
            "## Unrelated {#unrelated}\n\n"
            "Unrelated context.\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_auth-detail.md").write_text(
            "### Token Rules {#token-rules}\n\n"
            "Token rule context.\n\n"
            "- `detail-expiry` Detail tokens expire hourly. -> []\n"
            "- `detail-shape` Detail tokens use JWT. -> []\n\n"
            "### Internal Rules {#internal-rules}\n\n"
            "Internal context.\n\n"
            "- `internal-detail` Internal only. -> []\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-detail-expiry",
            title="Auth Detail Expiry",
            intent="Expose one F-backed detail node with logical context.",
            include_nodes=["detail-expiry"],
            approved=True,
        )

        render_file_view(self.root, "auth-detail-expiry")

        dist = self.root / "shared_views" / "auth-detail-expiry" / "dist"
        exported = (dist / "MEMORY.md").read_text(encoding="utf-8")
        detail = (dist / "MEMORY_auth-detail.md").read_text(encoding="utf-8")
        self.assertIn("Project context.", exported)
        self.assertIn("Auth detail summary.", exported)
        self.assertNotIn("Unrelated context.", exported)
        self.assertIn("Token rule context.", detail)
        self.assertIn("- `detail-expiry`", detail)
        self.assertNotIn("detail-shape", detail)
        self.assertNotIn("Internal Rules", detail)
        self.assertNotIn("internal-detail", detail)

    def test_file_recipe_discovers_both_graphs_without_parsing_m_or_s_bodies(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Public Context {#public-context}\n\n"
            "Public memory context.\n\n"
            "## Curated Notes {M#curated-notes}\n\n"
            "## Review Skill {S#review-skill}\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Live Work {#live-work}\n\nLive Pursuit context.\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_curated-notes.md").write_text(
            "## Forged Public Context {#public-context}\n\nsecret M body\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_review-skill.md").write_text(
            "## Forged Public Context {#public-context}\n\nsecret S body\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="combined-context",
            title="Combined Context",
            intent="Expose selected global graph context.",
            include_headings=["public-context", "live-work"],
            approved=True,
        )

        render_file_view(self.root, "combined-context")

        exported = (self.root / "shared_views" / "combined-context" / "dist" / "MEMORY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Public memory context.", exported)
        self.assertIn("Live Pursuit context.", exported)
        self.assertNotIn("secret M body", exported)
        self.assertNotIn("secret S body", exported)

    def test_file_recipe_rejects_m_and_s_backing_files_as_graph_sources(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Curated Notes {M#curated-notes}\n\n"
            "## Review Skill {S#review-skill}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_curated-notes.md").write_text("secret M body\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_review-skill.md").write_text("secret S body\n", encoding="utf-8")

        for path in ("MEMORY_curated-notes.md", "MEMORY_SKILL_review-skill.md"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "RightMemory graph file"):
                    write_extractive_file_view_recipe(
                        self.root,
                        view_id="bad-source",
                        title="Bad Source",
                        intent="Must not expose backing bodies.",
                        include_files=[path],
                    )

    def test_extractive_recipe_writes_clean_render_and_refresh_metadata(self):
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
            last_semantic_refresh_memory_commit="abc123",
        )

        recipe = load_file_view_recipe(self.root, "auth-api-files")
        text = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")

        self.assertEqual(recipe.render, "extractive")
        self.assertEqual(recipe.semantic_refresh_days, 7)
        self.assertEqual(recipe.last_semantic_refresh_memory_commit, "abc123")
        self.assertIn('render = "extractive"', text)
        self.assertNotIn("expanded-heading-subtrees", text)

    def test_generative_recipe_forbids_selection_fields(self):
        view_dir = self.root / "shared_views" / "auth-api-files"
        view_dir.mkdir(parents=True)
        (view_dir / "recipe.toml").write_text(
            'version = 1\n'
            'view_id = "auth-api-files"\n'
            'kind = "file"\n'
            'title = "Auth API Files"\n'
            'approved = true\n'
            'intent = "Expose auth context."\n'
            'render = "generative"\n'
            'semantic_refresh_days = 7\n'
            'last_semantic_refresh_at = ""\n'
            'last_semantic_refresh_memory_commit = ""\n'
            'include_nodes = ["token-expiry"]\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "generative file view recipe must not include selection field"):
            validate_file_view_recipe_source(self.root, "auth-api-files")

    def test_old_expanded_heading_render_is_rejected(self):
        view_dir = self.root / "shared_views" / "auth-api-files"
        view_dir.mkdir(parents=True)
        (view_dir / "recipe.toml").write_text(
            'version = 1\n'
            'view_id = "auth-api-files"\n'
            'kind = "file"\n'
            'title = "Auth API Files"\n'
            'approved = true\n'
            'intent = "Expose auth context."\n'
            'render = "expanded-heading-subtrees"\n'
            'include_headings = ["auth-api"]\n'
            'include_nodes = []\n'
            'include_files = []\n'
            'exclude_ids = []\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, 'render must be "extractive" or "generative"'):
            validate_file_view_recipe_source(self.root, "auth-api-files")

    def test_file_recipe_excludes_nested_heading_subtree(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "Public auth context.\n\n"
            "### Internal Tokens {#internal-tokens}\n\n"
            "- `secret-token` Private token shape. -> [doc:internal-tokens]\n\n"
            "### Public Tokens {#public-tokens}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [doc:public-tokens]\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
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

    def test_file_recipe_excludes_managed_example_template_block(self):
        (self.root / "MEMORY.md").write_text(
            "# Alice Auth API {#alice-auth-api}\n\n"
            "## Session Model {#alice-session-model}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [doc:alice-session-model]\n\n"
            "---\n\n"
            "> Starter template. <!-- rightmemory:example:start -->\n\n"
            "# Sample Project Graph {#sample-project-graph}\n\n"
            "- `sample-node` This is example content, not user memory. -> []\n\n"
            "<!-- rightmemory:example:end -->\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["alice-auth-api"],
            approved=True,
        )

        render_file_view(self.root, "auth-api-files")

        exported = (self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Tokens expire after one hour.", exported)
        self.assertNotIn("rightmemory:example", exported)
        self.assertNotIn("Sample Project Graph", exported)
        self.assertNotIn("sample-node", exported)

    def test_generative_recipe_exports_existing_generated_memory(self):
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            memory_document=GENERATED_MEMORY,
            approved=True,
        )
        package = self.root / "package"

        export_file_view_package(self.root, "auth-api-files", package)

        exported = (package / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        recipe = (package / "recipe.toml").read_text(encoding="utf-8")
        self.assertIn('render = "generative"', recipe)
        self.assertIn("# Auth API {#auth-api}", exported)
        self.assertIn("Tokens expire after one hour.", exported)

    def test_generative_package_fails_when_generated_memory_missing(self):
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            memory_document=GENERATED_MEMORY,
            approved=True,
        )
        shutil.rmtree(self.root / "shared_views" / "auth-api-files" / "dist")

        with self.assertRaisesRegex(ValueError, "generative file view output is missing"):
            export_file_view_package(self.root, "auth-api-files", self.root / "package")

    def test_approve_file_view_sets_approved_true(self):
        write_extractive_file_view_recipe(
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
            write_extractive_file_view_recipe(
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

    def test_file_view_builder_accepts_generative_output(self):
        def fake_builder(memory_root, view_id, message):
            write_generative_file_view(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose sanitized auth context.",
                memory_document=GENERATED_MEMORY,
                approved=False,
                publish_hub_url="https://hub.example.test",
                publish_credential_id="alice-publish",
            )
            return "built generative file view auth-api-files"

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
        self.assertEqual(result, "built generative file view auth-api-files")
        self.assertIn("Tokens expire after one hour.", preview.read_text(encoding="utf-8"))

    def test_refresh_file_view_preserves_approval_and_publish_settings(self):
        self._init_git_memory()
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
            last_semantic_refresh_at="2000-01-01T00:00:00+00:00",
            last_semantic_refresh_memory_commit="old",
        )

        def fake_builder(memory_root, view_id, message):
            write_generative_file_view(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose sanitized auth context.",
                memory_document=GENERATED_MEMORY,
                approved=False,
            )
            return "refreshed"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            result = refresh_file_view(self.root, "auth-api-files", force=True)

        recipe = load_file_view_recipe(self.root, "auth-api-files")
        self.assertIn("refreshed file view auth-api-files", result)
        self.assertTrue(recipe.approved)
        self.assertEqual(recipe.publish_hub_url, "https://hub.example.test")
        self.assertEqual(recipe.publish_credential_id, "alice-publish")
        self.assertEqual(recipe.render, "generative")
        self.assertNotEqual(recipe.last_semantic_refresh_memory_commit, "old")

    def test_refresh_file_view_restores_previous_files_on_builder_failure(self):
        self._init_git_memory()
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
        )
        original = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")

        def fake_builder(memory_root, view_id, message):
            view_dir = memory_root / "shared_views" / view_id
            (view_dir / "recipe.toml").write_text("broken = true\n", encoding="utf-8")
            return "broken"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            with self.assertRaisesRegex(ValueError, "unsupported field"):
                refresh_file_view(self.root, "auth-api-files", force=True)

        restored = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        self.assertEqual(restored, original)

    def test_refresh_file_view_publish_failure_keeps_refresh_commit_clean(self):
        self._init_git_memory()
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
            last_semantic_refresh_at="2000-01-01T00:00:00+00:00",
            last_semantic_refresh_memory_commit="old",
        )
        self._git(
            "add",
            "shared_views/auth-api-files/.gitignore",
            "shared_views/auth-api-files/recipe.toml",
            "shared_views/auth-api-files/view.md",
        )
        self._git("commit", "-m", "shared-view: initial")
        old_head = self._git("rev-parse", "HEAD")

        def fake_builder(memory_root, view_id, message):
            write_generative_file_view(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose sanitized auth context.",
                memory_document=GENERATED_MEMORY,
                approved=False,
            )
            return "refreshed"

        with (
            patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder),
            patch("rightmemory.shared_view_builder.publish_file_view_package", side_effect=RuntimeError("publish down")),
        ):
            with self.assertRaisesRegex(RuntimeError, "publish down"):
                refresh_file_view(self.root, "auth-api-files", force=True, publish=True)

        self.assertNotEqual(self._git("rev-parse", "HEAD"), old_head)
        self.assertEqual(self._git("status", "--short"), "")
        recipe = load_file_view_recipe(self.root, "auth-api-files")
        self.assertEqual(recipe.render, "generative")

    def test_file_view_builder_rejects_noncanonical_model_recipe(self):
        def fake_builder(memory_root, view_id, message):
            view_dir = memory_root / "shared_views" / view_id
            view_dir.mkdir(parents=True)
            (view_dir / "view.md").write_text("# Auth API Files\n", encoding="utf-8")
            (view_dir / "recipe.toml").write_text(
                'kind = "file"\n'
                'approved = false\n'
                'title = "Auth API Files"\n'
                'intent = "Expose auth context."\n'
                'include = ["auth-api"]\n'
                'exclude = []\n',
                encoding="utf-8",
            )
            return "built file view auth-api-files"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            with self.assertRaisesRegex(ValueError, "unsupported field\\(s\\): exclude, include"):
                run_file_view_builder(
                    self.root,
                    view_id="auth-api-files",
                    title="Auth API Files",
                    intent="Expose auth context.",
                    hub_url="https://hub.example.test",
                    credential_id="alice-publish",
                )

    def _init_git_memory(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "MEMORY.md", "PURSUITS.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "memory: initial"], cwd=self.root, check=True, stdout=subprocess.PIPE)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def test_question_view_builder_rejects_noncanonical_model_config(self):
        def fake_builder(memory_root, view_id, message):
            view_dir = memory_root / "shared_views" / view_id
            view_dir.mkdir(parents=True)
            (view_dir / "view.md").write_text("# Auth API Questions\n", encoding="utf-8")
            (view_dir / "retriever.md").write_text("Answer from auth memory.\n", encoding="utf-8")
            (view_dir / "question.toml").write_text(
                'kind = "question"\n'
                'approved = false\n'
                'title = "Auth API Questions"\n'
                'intent = "Answer auth questions."\n'
                'include = ["auth-api"]\n',
                encoding="utf-8",
            )
            return "built question view auth-api-ask"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            with self.assertRaisesRegex(ValueError, "unsupported field\\(s\\): include"):
                run_question_view_builder(
                    self.root,
                    view_id="auth-api-ask",
                    title="Auth API Questions",
                    intent="Answer auth questions.",
                )


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
                "rightmemory-shared-view.toml": 'version = 2\nview_id = "auth-api-files"\nkind = "file"\n',
                "dist/MEMORY.md": GENERATED_MEMORY,
                "dist/manifest.toml": (
                    'version = 2\nview_id = "auth-api-files"\n'
                    'document_kind = "rightmemory-memory"\n'
                ),
            }
        )

        with patch("rightmemory.shared_view_files.HubClient") as client_type:
            client_type.return_value.download_package.return_value = archive
            result = pull_file_view(self.root, "auth-api-files")

        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
        self.assertEqual(result.status, "pulled")
        self.assertIn("Tokens expire", (imported / "dist" / "MEMORY.md").read_text(encoding="utf-8"))

    def test_pull_file_view_falls_back_to_stale_import(self):
        package = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
        _write_valid_import_package(
            package,
            memory="# Auth API {#auth-api} → []\n\n- `stale` stale but usable → []\n",
        )
        imported = package / "dist"

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
            "# Project {#project}\n\n## Auth API {#auth-api}\n\n- `token-expiry` Tokens expire. -> [doc:auth-api]\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits {#pursuits} → []\n", encoding="utf-8")
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        write_extractive_file_view_recipe(
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

    def test_automatic_publish_uses_stable_per_view_idempotency_key(self):
        clients = []

        with patch(
            "rightmemory.shared_view_files.HubClient",
            side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token),
        ):
            publish_approved_file_views(self.root, operation_id="update-operation-1")

        self.assertEqual(
            clients[0].publish_calls[0]["idempotency_key"],
            "update-operation-1:auth-api-files",
        )

    def test_automatic_publish_reads_content_from_snapshot_and_credentials_from_live_root(self):
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        shutil.copy2(self.root / "MEMORY.md", snapshot / "MEMORY.md")
        shutil.copy2(self.root / "PURSUITS.md", snapshot / "PURSUITS.md")
        shutil.copytree(self.root / "shared_views", snapshot / "shared_views")
        clients = []

        with patch(
            "rightmemory.shared_view_files.HubClient",
            side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token),
        ):
            results = publish_approved_file_views(
                snapshot,
                operation_id="update-operation-1",
                credential_root=self.root,
            )

        self.assertEqual(results[0].status, "published")
        self.assertEqual(clients[0].token, "publish-token")

    def test_publish_file_view_package_does_not_create_invitation(self):
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_nodes=("token-expiry",),
            approved=True,
        )
        (self.root / "MEMORY.md").write_text(
            "# Auth {#auth} → []\n\n- `token-expiry` Tokens expire. → []\n",
            encoding="utf-8",
        )
        clients = []

        with patch("rightmemory.shared_view_files.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            result = publish_file_view_package(
                self.root,
                "auth-api-files",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
            )

        self.assertEqual(result["version_id"], "ver_1")
        self.assertEqual(clients[0].publish_calls[0]["view_id"], "auth-api-files")
        self.assertEqual(clients[0].invitation_calls, [])

    def test_invite_file_view_publishes_current_package_and_creates_invitation(self):
        clients = []

        with patch("rightmemory.shared_view_files.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            result = invite_file_view(self.root, "auth-api-files", label="frontend")

        client = clients[0]
        self.assertIn("invited file view auth-api-files", result)
        self.assertIn("invitation_url\thttps://hub.example.test/i/invite-token", result)
        self.assertEqual(client.base_url, "https://hub.example.test")
        self.assertEqual(client.token, "publish-token")
        self.assertEqual(client.publish_calls[0]["view_id"], "auth-api-files")
        self.assertIn("dist/MEMORY.md", client.publish_calls[0]["files"])
        self.assertEqual(client.invitation_calls[0]["view_id"], "auth-api-files")
        self.assertEqual(client.invitation_calls[0]["label"], "frontend")

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

    def test_list_file_view_publish_events_returns_newest_valid_events(self):
        events = self.root / ".runtime" / "shared_views" / "publish-events.jsonl"
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text(
            '{"created_at":"2026-06-17T10:00:00+00:00","view_id":"old","status":"published","message":"old","trigger":"memory-write"}\n'
            "not json\n"
            '{"created_at":"2026-06-17T11:00:00+00:00","view_id":"new","status":"failed","message":"boom","trigger":"memory-write"}\n',
            encoding="utf-8",
        )

        listed = list_file_view_publish_events(self.root)

        self.assertEqual([event["view_id"] for event in listed], ["new", "old"])
        self.assertEqual(listed[0]["status"], "failed")

    def test_publish_approved_generative_view_fails_closed_when_output_missing(self):
        shutil.rmtree(self.root / "shared_views" / "auth-api-files")
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            memory_document=GENERATED_MEMORY,
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        shutil.rmtree(self.root / "shared_views" / "auth-api-files" / "dist")

        with patch("rightmemory.shared_view_files.HubClient", side_effect=AssertionError("publish should not run")):
            results = publish_approved_file_views(self.root)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("generative file view output is missing", results[0].message)

    def test_generative_publish_retry_uses_frozen_outbox_package(self):
        shutil.rmtree(self.root / "shared_views" / "auth-api-files")
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            memory_document=(
                "# Auth API {#auth-api} → []\n\n"
                "- `original-generated` Original generated context. → []\n"
            ),
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        outbox = self.root / ".runtime" / "test-publish-outbox"
        prepare_file_view_publish_outbox(self.root, outbox)
        generated = self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md"
        generated.write_text("# Later generated context\n", encoding="utf-8")
        clients = []

        with patch(
            "rightmemory.shared_view_files.HubClient",
            side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token),
        ):
            results = publish_file_view_outbox(
                outbox,
                credential_root=self.root,
                operation_id="dreamer-operation-1",
            )

        self.assertEqual(results[0].status, "published")
        self.assertIn("Original generated context", clients[0].publish_calls[0]["memory"])
        self.assertNotIn("Later generated context", clients[0].publish_calls[0]["memory"])


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

    def test_register_question_view_with_hub_does_not_create_invitation(self):
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth questions.",
            retriever_instructions="Answer from auth memory.",
            approved=True,
        )
        clients = []

        with patch("rightmemory.shared_view_questions.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            result = register_question_view_with_hub(
                self.root,
                "auth-api-ask",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                question_base_url="https://provider.example.test",
            )

        self.assertEqual(result["view_id"], "auth-api-ask")
        self.assertEqual(clients[0].question_registrations[0]["view_id"], "auth-api-ask")
        self.assertEqual(clients[0].invitation_calls, [])
        config = load_question_view(self.root, "auth-api-ask")
        self.assertEqual(len(config.access_token_hashes), 1)

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

        with (
            patch("rightmemory.shared_view_questions.Event") as event_type,
            patch("rightmemory.shared_view_questions.ThreadPoolExecutor") as executor_type,
        ):
            event_type.return_value.wait.return_value = False
            executor_type.return_value.submit.return_value.done.return_value = False
            result = answer_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider did not start within 1 seconds", result)
        event_type.return_value.wait.assert_called_once_with(timeout=1)
        executor_type.return_value.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_answer_question_view_times_out_after_provider_starts(self):
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

        with (
            patch("rightmemory.shared_view_questions.Event") as event_type,
            patch("rightmemory.shared_view_questions.ThreadPoolExecutor") as executor_type,
        ):
            event_type.return_value.wait.return_value = True
            executor_type.return_value.submit.return_value.result.side_effect = TimeoutError
            result = answer_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider answer timed out after 1 seconds", result)
        event_type.return_value.wait.assert_called_once_with(timeout=1)
        executor_type.return_value.submit.return_value.result.assert_called_once_with(timeout=1)
        executor_type.return_value.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

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
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
        _write_valid_import_package(imported)

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

    def publish_package(self, view_id: str, package_root: Path, *, idempotency_key: str | None = None):
        self.publish_calls.append(
            {
                "view_id": view_id,
                "idempotency_key": idempotency_key,
                "files": sorted(path.relative_to(package_root).as_posix() for path in package_root.rglob("*") if path.is_file()),
                "memory": (package_root / "dist" / "MEMORY.md").read_text(encoding="utf-8"),
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
