import tempfile
import unittest
from pathlib import Path

from rightmemory.shared_views import (
    SharedViewConnection,
    SharedViewTarget,
    accept_shared_view,
    load_connections,
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
            target=SharedViewTarget(kind="local_markdown", path=".runtime/shared_views/imports/alice-auth-api"),
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
            target=SharedViewTarget(kind="local_markdown", path=".runtime/shared_views/imports/team.auth-api"),
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

    def test_save_connections_rejects_local_markdown_without_path(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            target=SharedViewTarget(kind="local_markdown"),
        )

        with self.assertRaises(ValueError) as caught:
            save_connections(self.root, {"alice-auth-api": connection})

        self.assertIn("local_markdown shared view target requires path", str(caught.exception))

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
