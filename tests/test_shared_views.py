import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.shared_views import (
    SharedViewConnection,
    SharedViewTarget,
    accept_shared_view,
    accept_shared_view_invitation,
    build_shared_view,
    define_shared_view,
    export_shared_view,
    list_shared_view_inbox,
    load_connections,
    publish_shared_view,
    record_shared_view_note,
    retrieve_shared_view,
    save_connections,
)


class SharedViewRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_save_and_load_connections(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            relationship="human",
            maintainer="Alice",
            description="Auth API collaboration context",
            accepted_from="rightmemory://view/invite/abc123",
            target=SharedViewTarget(kind="package", path=".runtime/shared_views/imports/alice-auth-api"),
        )

        save_connections(self.root, {"alice-auth-api": connection})
        loaded = load_connections(self.root)

        self.assertEqual(loaded["alice-auth-api"], connection)

    def test_save_and_load_connection_with_dotted_heading_id(self):
        connection = SharedViewConnection(
            heading_id="team.auth-api",
            ref="rightmemory://view/team.auth-api",
            relationship="team-space",
            maintainer="Auth Team",
            description="Team auth API collaboration context",
            target=SharedViewTarget(kind="package", path=".runtime/shared_views/imports/team.auth-api"),
        )

        save_connections(self.root, {"team.auth-api": connection})
        loaded = load_connections(self.root)

        self.assertIn("team.auth-api", loaded)
        self.assertEqual(loaded["team.auth-api"], connection)

    def test_save_connections_rejects_unknown_relationship(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            relationship="mystery",
        )

        with self.assertRaises(ValueError) as caught:
            save_connections(self.root, {"alice-auth-api": connection})

        self.assertIn("unknown shared view relationship", str(caught.exception))

    def test_save_connections_rejects_unknown_target_kind(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            target=SharedViewTarget(kind="remote_cache"),
        )

        with self.assertRaises(ValueError) as caught:
            save_connections(self.root, {"alice-auth-api": connection})

        self.assertIn("unknown shared view target kind", str(caught.exception))

    def test_save_connections_rejects_package_without_path(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            target=SharedViewTarget(kind="package"),
        )

        with self.assertRaises(ValueError) as caught:
            save_connections(self.root, {"alice-auth-api": connection})

        self.assertIn("package shared view target requires path", str(caught.exception))

    def test_save_connections_rejects_target_paths_outside_memory_root(self):
        paths = [str(self.root.parent / "outside"), "../outside"]
        for path in paths:
            with self.subTest(path=path):
                connection = SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="local_markdown", path=path),
                )

                with self.assertRaises(ValueError) as caught:
                    save_connections(self.root, {"alice-auth-api": connection})

                self.assertIn("shared view target path must stay under the memory root", str(caught.exception))

    def test_load_connections_rejects_unknown_relationship(self):
        (self.root / "shared_views.toml").write_text(
            """
            [connections.alice-auth-api]
            ref = "rightmemory://view/alice-auth-api"
            relationship = "mystery"
            """,
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_connections(self.root)

        self.assertIn("unknown shared view relationship", str(caught.exception))

    def test_load_connections_normalizes_old_local_markdown_target_to_package(self):
        (self.root / ".runtime/shared_views/imports/alice-auth-api").mkdir(parents=True)
        (self.root / "shared_views.toml").write_text(
            """
            [connections.alice-auth-api]
            ref = "rightmemory://view/alice-auth-api"
            relationship = "human"

            [connections.alice-auth-api.target]
            kind = "local_markdown"
            path = ".runtime/shared_views/imports/alice-auth-api"
            """,
            encoding="utf-8",
        )

        loaded = load_connections(self.root)

        self.assertEqual(loaded["alice-auth-api"].target.kind, "package")

    def test_load_connections_rejects_target_outside_memory_root(self):
        (self.root / "shared_views.toml").write_text(
            """
            [connections.alice-auth-api]
            ref = "rightmemory://view/alice-auth-api"
            relationship = "human"

            [connections.alice-auth-api.target]
            kind = "local_markdown"
            path = "../outside"
            """,
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_connections(self.root)

        self.assertIn("shared view target path must stay under the memory root", str(caught.exception))


class SharedViewBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "Auth API accepts signed tokens.\n"
            "Private payroll note should stay internal.\n"
            "Token rotation happens monthly.\n",
            encoding="utf-8",
        )

    def test_define_and_build_shared_view_materializes_filtered_markdown(self):
        define_result = define_shared_view(
            self.root,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            audience="Frontend integration team",
            maintainer="Alice",
            retriever_instructions="Answer with API contract facts and omit unrelated private notes.",
            filter_terms=["auth", "token"],
        )
        build_result = build_shared_view(self.root, "alice-auth-api")

        view_dir = self.root / "shared_views" / "alice-auth-api"
        exported = (view_dir / "dist" / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("defined shared view alice-auth-api", define_result)
        self.assertIn("built shared view alice-auth-api", build_result)
        self.assertIn("# Alice Auth API", (view_dir / "view.md").read_text(encoding="utf-8"))
        self.assertIn("omit unrelated private notes", (view_dir / "retriever.md").read_text(encoding="utf-8"))
        self.assertIn('filter_terms = ["auth", "token"]', (view_dir / "export.toml").read_text(encoding="utf-8"))
        self.assertEqual((view_dir / ".gitignore").read_text(encoding="utf-8"), "dist/\n")
        self.assertIn("Auth API accepts signed tokens.", exported)
        self.assertIn("Token rotation happens monthly.", exported)
        self.assertNotIn("Private payroll note", exported)
        self.assertIn("memory_sha256", (view_dir / "dist" / "manifest.toml").read_text(encoding="utf-8"))

    def test_export_shared_view_writes_package_and_invitation(self):
        define_shared_view(
            self.root,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            maintainer="Alice",
            filter_terms=["auth"],
        )
        target = self.root / "exported-auth-view"

        result = export_shared_view(self.root, "alice-auth-api", target)

        self.assertIn("exported shared view alice-auth-api", result)
        self.assertTrue((target / "view.md").exists())
        self.assertTrue((target / "dist" / "MEMORY.md").exists())
        invitation = (target / "rightmemory-shared-view.toml").read_text(encoding="utf-8")
        self.assertIn('view_id = "alice-auth-api"', invitation)
        self.assertIn('kind = "package"', invitation)

    def test_export_shared_view_rebuilds_stale_dist(self):
        define_shared_view(
            self.root,
            view_id="alice-auth-api",
            title="Alice Auth API",
            filter_terms=["auth"],
        )
        view_dir = self.root / "shared_views" / "alice-auth-api"
        (view_dir / "dist").mkdir()
        (view_dir / "dist" / "MEMORY.md").write_text("stale generated output\n", encoding="utf-8")
        target = self.root / "exported-auth-view"

        export_shared_view(self.root, "alice-auth-api", target)

        exported = (target / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("Auth API accepts signed tokens.", exported)
        self.assertNotIn("stale generated output", exported)

    def test_publish_shared_view_writes_minimal_hub_registry(self):
        define_shared_view(
            self.root,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            maintainer="Alice",
            filter_terms=["auth"],
        )
        hub = self.root / "hub"

        result = publish_shared_view(self.root, "alice-auth-api", hub)

        self.assertIn("published shared view alice-auth-api", result)
        registry = (hub / "registry.toml").read_text(encoding="utf-8")
        self.assertIn('[views."alice-auth-api"]', registry)
        self.assertIn('package_path = "views/alice-auth-api"', registry)
        self.assertTrue((hub / "views" / "alice-auth-api" / "dist" / "MEMORY.md").exists())
        self.assertIn('kind = "hub"', (hub / "invitations" / "alice-auth-api.toml").read_text(encoding="utf-8"))


class SharedViewAcceptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")

    def test_accept_shared_view_creates_heading_and_registry_entry(self):
        result = accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
            relationship="human",
            maintainer="Alice",
            description="Auth API collaboration context",
            accepted_from="rightmemory://view/invite/abc123",
            target_path=".runtime/shared_views/imports/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        loaded = load_connections(self.root)

        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)
        self.assertIn("Alice owns auth API collaboration context.", memory)
        self.assertEqual(loaded["alice-auth-api"].ref, "rightmemory://view/alice-auth-api")
        self.assertIn("accepted shared view alice-auth-api", result)

    def test_accept_shared_view_does_not_duplicate_existing_heading(self):
        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )
        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertEqual(memory.count("{M#alice-auth-api}"), 1)

    def test_accept_shared_view_uses_existing_m_heading_in_detail_file(self):
        (self.root / "MEMORY_ALICE.md").write_text("## Existing Alice View {M#alice-auth-api}\n", encoding="utf-8")

        result = accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        detail = (self.root / "MEMORY_ALICE.md").read_text(encoding="utf-8")

        self.assertIn("accepted shared view alice-auth-api", result)
        self.assertNotIn("### Alice Auth API {M#alice-auth-api}", memory)
        self.assertEqual(detail.count("{M#alice-auth-api}"), 1)

    def test_accept_shared_view_rejects_conflicting_graph_id(self):
        before = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            accept_shared_view(
                self.root,
                heading_id="project",
                title="Project Shared View",
                body="Invalid duplicate graph id.",
                ref="rightmemory://view/project",
            )

        after = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("graph id `project` already exists", str(caught.exception))
        self.assertEqual(after, before)

    def test_accept_shared_view_rejects_conflicting_graph_id_in_detail_file(self):
        (self.root / "MEMORY_ALICE.md").write_text("## Alice Skill {S#alice-auth-api}\n", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            accept_shared_view(
                self.root,
                heading_id="alice-auth-api",
                title="Alice Auth API",
                body="Invalid duplicate graph id.",
                ref="rightmemory://view/alice-auth-api",
            )

        self.assertIn("MEMORY_ALICE.md", str(caught.exception))
        self.assertIn("{S#alice-auth-api}", str(caught.exception))

    def test_accept_shared_view_rejects_conflicting_bullet_node_id(self):
        before = "# Project {#project}\n\n- `alice-auth-api` existing node\n"
        (self.root / "MEMORY.md").write_text(before, encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            accept_shared_view(
                self.root,
                heading_id="alice-auth-api",
                title="Alice Auth API",
                body="Invalid duplicate graph id.",
                ref="rightmemory://view/alice-auth-api",
            )

        after = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("bullet node `alice-auth-api`", str(caught.exception))
        self.assertEqual(after, before)
        self.assertFalse((self.root / "shared_views.toml").exists())

    def test_accept_shared_view_ignores_conflicting_graph_id_in_skill_file(self):
        (self.root / "MEMORY_SKILL_auth.md").write_text("## Freeform Skill {#alice-auth-api}\n", encoding="utf-8")

        result = accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("accepted shared view alice-auth-api", result)
        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)

    def test_accept_shared_view_ignores_m_marker_in_skill_file(self):
        (self.root / "MEMORY_SKILL_auth.md").write_text("## Freeform Skill {M#alice-auth-api}\n", encoding="utf-8")

        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)

    def test_rejected_accept_leaves_memory_unchanged(self):
        before = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        cases = (
            {"ref": "   "},
            {"ref": "rightmemory://view/alice-auth-api", "target_path": "   "},
        )

        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    accept_shared_view(
                        self.root,
                        heading_id="alice-auth-api",
                        title="Alice Auth API",
                        body="Alice owns auth API collaboration context.",
                        **kwargs,
                    )

                after = (self.root / "MEMORY.md").read_text(encoding="utf-8")

                self.assertEqual(after, before)
                self.assertFalse((self.root / "shared_views.toml").exists())

    def test_accept_shared_view_normalizes_generated_title(self):
        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice\n{#project} Auth   API {M#bad}",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)
        self.assertNotIn("{M#bad}", memory)

    def test_accept_shared_view_inserts_into_existing_shared_views_section(self):
        (self.root / "MEMORY.md").write_text(
            "# Shared Views\n\nExisting shared context.\n\n# Work Context\n\nWork notes stay here.\n",
            encoding="utf-8",
        )

        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertLess(memory.index("### Alice Auth API {M#alice-auth-api}"), memory.index("# Work Context"))
        self.assertTrue(memory.rstrip().endswith("Work notes stay here."))

    def test_accept_shared_view_invitation_copies_package_and_records_resolver(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        provider.mkdir()
        consumer.mkdir()
        (provider / "MEMORY.md").write_text(
            "# Provider\n\nAuth API accepts signed tokens.\n",
            encoding="utf-8",
        )
        (consumer / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        define_shared_view(
            provider,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            maintainer="Alice",
            filter_terms=["auth"],
        )
        package = self.root / "package"
        export_shared_view(provider, "alice-auth-api", package)

        result = accept_shared_view_invitation(consumer, package)

        memory = (consumer / "MEMORY.md").read_text(encoding="utf-8")
        connections = load_connections(consumer)
        imported = consumer / ".runtime" / "shared_views" / "imports" / "alice-auth-api"
        self.assertIn("accepted shared view alice-auth-api", result)
        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)
        self.assertEqual(connections["alice-auth-api"].target.kind, "package")
        self.assertEqual(connections["alice-auth-api"].target.view_id, "alice-auth-api")
        self.assertTrue((imported / "rightmemory-shared-view.toml").exists())


class SharedViewInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_human_connection_requires_confirmation_before_note(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                )
            },
        )

        result = record_shared_view_note(self.root, "alice-auth-api", "Docs are stale")

        self.assertIn("confirmation required", result)
        self.assertFalse((self.root / ".runtime/shared_views/interactions").exists())

    def test_owned_agent_connection_records_note_without_confirmation(self):
        save_connections(
            self.root,
            {
                "auth-agent": SharedViewConnection(
                    heading_id="auth-agent",
                    ref="rightmemory://view/auth-agent",
                    relationship="owned-agent",
                )
            },
        )

        result = record_shared_view_note(self.root, "auth-agent", "Sync docs need a refresh")

        interaction_path = self.root / ".runtime/shared_views/interactions/auth-agent.jsonl"
        records = [json.loads(line) for line in interaction_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relationship"], "owned-agent")
        self.assertEqual(records[0]["status"], "queued")
        self.assertEqual(records[0]["message"], "Sync docs need a refresh")
        self.assertIn("queued shared view note", result)

    def test_confirmed_human_note_is_recorded(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                )
            },
        )

        result = record_shared_view_note(self.root, "alice-auth-api", "Confirmed docs update", confirmed=True)

        interaction_path = self.root / ".runtime/shared_views/interactions/alice-auth-api.jsonl"
        records = [json.loads(line) for line in interaction_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["relationship"], "human")
        self.assertEqual(records[0]["status"], "queued")
        self.assertEqual(records[0]["message"], "Confirmed docs update")
        self.assertIn("queued shared view note", result)

    def test_local_provider_connection_delivers_confirmed_note_to_provider_inbox(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        provider.mkdir()
        consumer.mkdir()
        save_connections(
            consumer,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                    target=SharedViewTarget(kind="local", path=str(provider), view_id="alice-auth-api"),
                )
            },
        )

        result = record_shared_view_note(
            consumer,
            "alice-auth-api",
            "Docs are missing token_expires_at.",
            confirmed=True,
            actor="assistant",
            task_context="frontend login migration",
        )

        inbox = list_shared_view_inbox(provider, "alice-auth-api")
        self.assertIn("recorded shared view note", result)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["message"], "Docs are missing token_expires_at.")
        self.assertEqual(inbox[0]["task_context"], "frontend login migration")
        self.assertEqual(inbox[0]["actor"], "assistant")

    def test_package_connection_queues_confirmed_note_locally(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    target=SharedViewTarget(kind="package", path=".runtime/shared_views/imports/alice-auth-api"),
                )
            },
        )
        (self.root / ".runtime/shared_views/imports/alice-auth-api").mkdir(parents=True)

        result = record_shared_view_note(self.root, "alice-auth-api", "Package docs are stale.", confirmed=True)

        interaction_path = self.root / ".runtime/shared_views/interactions/alice-auth-api.jsonl"
        records = [json.loads(line) for line in interaction_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("queued shared view note", result)
        self.assertEqual(records[0]["status"], "queued")


class SharedViewRetrieveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_retrieve_shared_view_returns_fresh_markdown_matches(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nAuth API accepts signed tokens.\nAn unrelated note.\n",
            encoding="utf-8",
        )
        (target / "MEMORY_EXTRA.md").write_text(
            "Token rotation happens monthly.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    maintainer="Alice",
                    description="Auth collaboration context",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "the auth token")

        self.assertIn("Shared view: alice-auth-api", result)
        self.assertIn("Status: fresh", result)
        self.assertIn("Ref: rightmemory://view/alice-auth-api", result)
        self.assertIn("Maintainer: Alice", result)
        self.assertIn("Description: Auth collaboration context", result)
        self.assertIn("Freshness:", result)
        self.assertIn("Matches:", result)
        self.assertIn("- MEMORY.md:3: Auth API accepts signed tokens.", result)
        self.assertIn("- MEMORY_EXTRA.md:1: Token rotation happens monthly.", result)
        self.assertNotIn("An unrelated note.", result)

        cache = self.root / ".runtime/shared_views/cache/alice-auth-api.txt"
        self.assertTrue(cache.exists())
        self.assertIn("Auth API accepts signed tokens.", cache.read_text(encoding="utf-8"))
        self.assertEqual((self.root / ".runtime/.gitignore").read_text(encoding="utf-8"), "*\n")

    def test_retrieve_shared_view_returns_fresh_when_cache_write_fails(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nToken expiry metadata includes token_expires_at.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )

        with patch("rightmemory.shared_views._write_shared_view_cache", side_effect=PermissionError("read-only")):
            result = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")

        self.assertIn("Status: fresh", result)
        self.assertIn("- MEMORY.md:3: Token expiry metadata includes token_expires_at.", result)

    def test_retrieve_shared_view_skips_symlinked_markdown_files(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text("# Alice Auth API\n\nPublic shared note.\n", encoding="utf-8")
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside_file = Path(outside_dir.name) / "MEMORY_leak_source.md"
        outside_file.write_text("Outside root secret phrase should not be imported.\n", encoding="utf-8")
        try:
            (target / "MEMORY_leak.md").symlink_to(outside_file)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "outside secret phrase")

        self.assertIn("Status: fresh", result)
        self.assertIn("- no strong match in published shared memory", result)
        self.assertNotIn("Outside root secret phrase", result)

    def test_retrieve_shared_view_uses_cache_when_target_disappears(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nDurable cache phrase lives here.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )
        retrieve_shared_view(self.root, "alice-auth-api", "durable cache")
        (target / "MEMORY.md").unlink()
        target.rmdir()

        result = retrieve_shared_view(self.root, "alice-auth-api", "durable cache")

        self.assertIn("Shared view: alice-auth-api", result)
        self.assertIn("Status: cached", result)
        self.assertNotIn("Status: fresh", result)
        self.assertIn("- MEMORY.md:3: Durable cache phrase lives here.", result)

    def test_retrieve_shared_view_cache_matches_later_query(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nAlpha deployment note.\nBeta rollback note.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )
        fresh = retrieve_shared_view(self.root, "alice-auth-api", "alpha")
        (target / "MEMORY.md").unlink()
        target.rmdir()

        cached = retrieve_shared_view(self.root, "alice-auth-api", "beta")

        self.assertIn("Status: fresh", fresh)
        self.assertIn("- MEMORY.md:3: Alpha deployment note.", fresh)
        self.assertIn("Status: cached", cached)
        self.assertIn("- MEMORY.md:4: Beta rollback note.", cached)
        self.assertNotIn("Alpha deployment note.", cached)

    def test_retrieve_shared_view_termless_query_has_no_strong_match(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nUI layout decisions live in the shared view.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "UI")

        self.assertIn("Status: fresh", result)
        self.assertIn("- no strong match in published shared memory", result)
        self.assertNotIn("- MEMORY.md:3: UI layout decisions live in the shared view.", result)

    def test_retrieve_shared_view_does_not_use_cache_after_revocation(self):
        target = self.root / ".runtime/shared_views/imports/alice-auth-api"
        target.mkdir(parents=True)
        (target / "MEMORY.md").write_text(
            "# Alice Auth API\n\nRevoked cache phrase should disappear.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                    ),
                )
            },
        )
        retrieve_shared_view(self.root, "alice-auth-api", "revoked cache")
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="revoked"),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "revoked cache")

        self.assertIn("Shared view: alice-auth-api", result)
        self.assertIn("Status: unavailable", result)
        self.assertIn("Reason: access revoked", result)
        self.assertNotIn("Status: cached", result)
        self.assertNotIn("Revoked cache phrase should disappear.", result)

    def test_retrieve_shared_view_reads_accepted_package_endpoint(self):
        package = self.root / ".runtime/shared_views/imports/alice-auth-api"
        (package / "dist").mkdir(parents=True)
        (package / "export.toml").write_text(
            """
            version = 1
            view_id = "alice-auth-api"
            ref = "rightmemory://view/alice-auth-api"
            title = "Alice Auth API"
            """,
            encoding="utf-8",
        )
        (package / "dist" / "MEMORY.md").write_text(
            "# Alice Auth API Shared View\n\nToken expiry metadata includes token_expires_at.\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(
                        kind="package",
                        path=".runtime/shared_views/imports/alice-auth-api",
                        view_id="alice-auth-api",
                    ),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")

        self.assertIn("Status: fresh", result)
        self.assertIn("Provenance: Alice Auth API", result)
        self.assertIn("Backing: filtered Markdown", result)
        self.assertIn("token_expires_at", result)

    def test_retrieve_shared_view_uses_local_provider_retriever_prompt_backing(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        provider.mkdir()
        consumer.mkdir()
        (provider / "MEMORY.md").write_text(
            "# Provider\n\n"
            "Auth API returns token_expires_at in login responses.\n"
            "Payroll API returns salary bands.\n",
            encoding="utf-8",
        )
        define_shared_view(
            provider,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            retriever_instructions="Answer from auth API collaboration context.",
            filter_terms=["auth", "token"],
        )
        save_connections(
            consumer,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="local", path=str(provider), view_id="alice-auth-api"),
                )
            },
        )

        result = retrieve_shared_view(consumer, "alice-auth-api", "token expiry salary")

        self.assertIn("Status: fresh", result)
        self.assertIn("Backing: retriever prompt", result)
        self.assertIn("Auth API returns token_expires_at", result)
        self.assertNotIn("Payroll API returns salary bands", result)

    def test_retrieve_shared_view_does_not_scan_provider_memory_without_filter_scope(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        provider.mkdir()
        consumer.mkdir()
        (provider / "MEMORY.md").write_text(
            "# Provider\n\n"
            "Payroll API returns salary bands.\n",
            encoding="utf-8",
        )
        define_shared_view(
            provider,
            view_id="alice-auth-api",
            title="Alice Auth API",
            retriever_instructions="Answer only auth API collaboration context.",
        )
        save_connections(
            consumer,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="local", path=str(provider), view_id="alice-auth-api"),
                )
            },
        )

        result = retrieve_shared_view(consumer, "alice-auth-api", "salary")

        self.assertIn("Status: unavailable", result)
        self.assertNotIn("Payroll API returns salary bands", result)

    def test_retrieve_shared_view_infers_prompt_scope_without_explicit_filter_terms(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        provider.mkdir()
        consumer.mkdir()
        (provider / "MEMORY.md").write_text(
            "# Provider\n\n"
            "Auth API returns token_expires_at in login responses.\n"
            "Payroll API returns salary bands.\n",
            encoding="utf-8",
        )
        define_shared_view(
            provider,
            view_id="alice-auth-api",
            title="Alice Auth API",
            retriever_instructions="Answer only auth API collaboration context.",
        )
        save_connections(
            consumer,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="local", path=str(provider), view_id="alice-auth-api"),
                )
            },
        )

        result = retrieve_shared_view(consumer, "alice-auth-api", "token salary")

        self.assertIn("Status: fresh", result)
        self.assertIn("Backing: retriever prompt with inferred scope", result)
        self.assertIn("Auth API returns token_expires_at", result)
        self.assertNotIn("Payroll API returns salary bands", result)

    def test_retrieve_shared_view_uses_hub_hosted_package(self):
        provider = self.root / "provider"
        consumer = self.root / "consumer"
        hub = self.root / "hub"
        provider.mkdir()
        consumer.mkdir()
        (provider / "MEMORY.md").write_text(
            "# Provider\n\nAuth API accepts signed tokens.\n",
            encoding="utf-8",
        )
        define_shared_view(
            provider,
            view_id="alice-auth-api",
            title="Alice Auth API",
            description="Auth API collaboration context.",
            filter_terms=["auth"],
        )
        publish_shared_view(provider, "alice-auth-api", hub)
        save_connections(
            consumer,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    target=SharedViewTarget(kind="hub", path=str(hub), view_id="alice-auth-api"),
                )
            },
        )

        result = retrieve_shared_view(consumer, "alice-auth-api", "signed token")

        self.assertIn("Status: fresh", result)
        self.assertIn("Provenance: hub hosted Alice Auth API", result)
        self.assertIn("Auth API accepts signed tokens.", result)
