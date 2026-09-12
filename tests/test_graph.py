from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rightmemory.graph import (
    KNOWN_EDGE_TYPES,
    block_body_text,
    build_graph_manifest,
    build_mf_manifest,
    parse_addressable_heading,
    remove_edge_targets,
    render_heading_line,
    replace_heading_title,
)
from rightmemory.tools import MemoryTools


class CanonicalGraphManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _write_pursuits(self) -> None:
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

    def test_manifest_retains_documents_spans_and_logical_f_hierarchy(self):
        memory_text = (
            "# Memory\n\n"
            "## Project {#project}\n\n"
            "Project body.\n\n"
            "- `project-fact` Fact. → []\n\n"
            "### Child {#child}\n\n"
            "Child body.\n\n"
            "## Details {F#details}\n\n"
            "Detail summary.\n"
        )
        (self.root / "MEMORY.md").write_text(memory_text, encoding="utf-8", newline="")
        (self.root / "MEMORY_details.md").write_text(
            "### Detail Topic {#detail-topic}\n\n"
            "Detail body.\n\n"
            "- `detail-fact` Evidence. → []\n",
            encoding="utf-8",
        )
        self._write_pursuits()

        manifest = build_graph_manifest(self.root)

        self.assertEqual(manifest.errors, [])
        memory_path = (self.root / "MEMORY.md").resolve()
        self.assertEqual(manifest.documents[memory_path].text, memory_text)
        self.assertEqual(manifest.items["project"].span.start_line, 3)
        self.assertEqual(manifest.items["project"].span.end_line, 12)
        self.assertEqual(manifest.items["project"].body_span.start_line, 4)
        self.assertEqual(manifest.items["project"].body_span.end_line, 8)

        project = manifest.block_for_id("project")
        child = manifest.block_for_id("child")
        details = manifest.block_for_id("details")
        detail_topic = manifest.block_for_id("detail-topic")
        assert project is not None and child is not None and details is not None and detail_topic is not None
        self.assertEqual(child.physical_parent, project.key)
        self.assertEqual(child.logical_parent, project.key)
        self.assertNotEqual(detail_topic.physical_parent, details.key)
        self.assertEqual(detail_topic.logical_parent, details.key)
        self.assertEqual(
            [block.item_id for block in manifest.walk_logical(details.key, include_self=True) if block.item_id],
            ["details", "detail-topic", "detail-fact"],
        )
        self.assertLess(manifest.items["details"].traversal_rank, manifest.items["detail-topic"].traversal_rank)
        self.assertEqual(len(manifest.items["details"].content_hash), 64)

    def test_content_hash_tracks_exact_logical_block(self):
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n## Details {F#details}\n\nSummary.\n",
            encoding="utf-8",
        )
        detail_path = self.root / "MEMORY_details.md"
        detail_path.write_text("- `fact` First. → []\n", encoding="utf-8")
        self._write_pursuits()
        first = build_graph_manifest(self.root).items["details"].content_hash

        detail_path.write_text("- `fact` Revised. → []\n", encoding="utf-8")
        second = build_graph_manifest(self.root).items["details"].content_hash

        self.assertNotEqual(first, second)

    def test_invalid_ids_and_missing_node_edge_lists_share_parser_diagnostics(self):
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n"
            "## Invalid {#bad id}\n\n"
            "- `bad id` Invalid. → []\n"
            "- `missing-edges` Missing suffix.\n\n"
            "```md\n"
            "- `example` Not graph syntax.\n"
            "```\n",
            encoding="utf-8",
        )
        self._write_pursuits()

        manifest = build_graph_manifest(self.root)

        errors = "\n".join(manifest.errors)
        self.assertIn("invalid heading id `bad id`", errors)
        self.assertIn("invalid node id `bad id`", errors)
        self.assertIn("node `missing-edges` must include an edge list", errors)
        self.assertNotIn("node `example`", errors)
        self.assertNotIn("bad id", manifest.items)
        self.assertNotIn("missing-edges", manifest.items)

    def test_terminal_rules_are_owned_by_graph_parser(self):
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n"
            "## Wrong Parent\n\n"
            "#### Plain {#plain}\n\n"
            "- `child` Invalid child. → []\n",
            encoding="utf-8",
        )
        self._write_pursuits()

        manifest = build_graph_manifest(self.root)

        errors = "\n".join(manifest.errors)
        self.assertIn("`####` terminal reference must use", errors)
        self.assertIn("`####` terminal reference must be under a `###` heading", errors)
        self.assertFalse(hasattr(MemoryTools, "_structure_errors"))

    def test_recursive_f_backing_paths_are_diagnosed(self):
        (self.root / "MEMORY.md").write_text("# Memory\n\n## A {F#a}\n", encoding="utf-8")
        (self.root / "MEMORY_a.md").write_text("## B {F#b}\n", encoding="utf-8")
        (self.root / "MEMORY_b.md").write_text("## A Again {F#a}\n", encoding="utf-8")
        self._write_pursuits()

        manifest = build_graph_manifest(self.root)

        self.assertTrue(any("cyclic F# backing path" in error for error in manifest.errors))


class PursuitGrammarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "MEMORY.md").write_bytes(b"# Memory\n")

    def test_title_only_and_free_markdown_body_validate_without_fields(self):
        body = "\r\nA **note**, a [link](https://example.test), and a list.  \r\n\r\n- Ordinary list\r\n\r\n```md\r\n## An example, not a map group\r\n```\r\n"
        (self.root / "PURSUITS.md").write_bytes(
            ("# Pursuits\r\n\r\n## Title only {#first}\r\n\r\n## Notes {#notes}\r\n" + body).encode()
        )
        manifest = build_graph_manifest(self.root)
        self.assertEqual(manifest.errors, [])
        self.assertEqual(set(manifest.items), {"first", "notes"})
        self.assertEqual(block_body_text(manifest, manifest.items["notes"]), body)

    def test_legacy_next_keeps_all_actions_and_graph_looking_bullets_as_body(self):
        body = (
            "\n**State:** Old context.\n\n**Next:**\n"
            "- `do` Continue.\n- `research` Unknown old action.\n"
            "- `not a slug` No validation as a node. -> [rel:missing]\n"
            "- `looks-like-node` Still a Next bullet. -> []\n"
            "- An ordinary bullet.\n\n**Done when:** Older prose.\n"
            "**Status:** Anything here is body.\n"
        )
        (self.root / "PURSUITS.md").write_bytes(("# Pursuits\n\n## Old {#old}\n" + body).encode())
        manifest = build_graph_manifest(self.root)
        self.assertEqual(manifest.errors, [])
        self.assertEqual(set(manifest.items), {"old"})
        self.assertEqual(block_body_text(manifest, manifest.items["old"]), body)

    def test_legacy_next_stops_at_field_or_heading(self):
        for boundary in ("**State:** Another field.", "## Other {#other}"):
            with self.subTest(boundary=boundary):
                (self.root / "PURSUITS.md").write_bytes((
                    "# Pursuits\n\n## Old {#old}\n**Next:**\n"
                    "- `research` Kept.\n" + boundary + "\n- `broken` Missing edge list.\n"
                ).encode())
                manifest = build_graph_manifest(self.root)
                self.assertTrue(any("node `broken` must include an edge list" in error for error in manifest.errors))

    def test_heading_helpers_share_edges_and_preserve_original_suffix(self):
        line = "###   中文 and English  {F#stable}  -> [doc:target,  dep:other ]\r\n"
        heading = parse_addressable_heading(line)
        self.assertEqual((heading.depth, heading.title, heading.id, heading.anchor_kind),
                         (3, "中文 and English", "stable", "F#"))
        self.assertEqual(heading.edges, (("doc", "target"), ("dep", "other")))
        self.assertEqual(replace_heading_title(line, "Changed"),
                         "###   Changed  {F#stable}  -> [doc:target,  dep:other ]\r\n")
        rendered = render_heading_line("中文 and English", "#", "stable", heading.edges, depth=2)
        self.assertEqual(parse_addressable_heading(rendered).edges, heading.edges)
        for invalid in ("", "  ", "two\nlines", "Anchor {#hidden}"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                render_heading_line(invalid, "#", "stable")

    def test_edge_removal_keeps_other_tokens_and_prose_byte_stable(self):
        heading = "## Target mention gone {#source}  -> [rel:gone, dep:keep , doc:gone]\r\n"
        node = "  - `fact` Prose mentions gone. → [doc:gone]\n"
        self.assertEqual(remove_edge_targets(heading, {"gone"}),
                         "## Target mention gone {#source}  -> [ dep:keep ]\r\n")
        self.assertEqual(remove_edge_targets(node, {"gone"}),
                         "  - `fact` Prose mentions gone. → []\n")
        self.assertEqual(remove_edge_targets(heading, {"unrelated"}), heading)


class RelAncestorValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

    def _manifest(self, memory: str, **detail_files: str):
        (self.root / "MEMORY.md").write_text(memory, encoding="utf-8")
        for name, content in detail_files.items():
            (self.root / name).write_text(content, encoding="utf-8")
        return build_graph_manifest(self.root)

    def test_rel_to_direct_parent_is_rejected(self):
        manifest = self._manifest(
            "# Project {#project}\n\n"
            "- `repo-path` Repository location. -> [rel:project]\n"
        )

        self.assertTrue(
            any(
                "source item `repo-path` to ancestor heading `project`" in error
                for error in manifest.errors
            )
        )

    def test_rel_to_indirect_ancestor_is_rejected(self):
        manifest = self._manifest(
            "# Domain {#domain}\n\n"
            "## Project {#project}\n\n"
            "- `repo-path` Repository location. -> [rel:domain]\n"
        )

        self.assertTrue(
            any(
                "source item `repo-path` to ancestor heading `domain`" in error
                for error in manifest.errors
            )
        )

    def test_child_heading_rel_to_ancestor_is_rejected(self):
        manifest = self._manifest(
            "# Domain {#domain}\n\n"
            "## Project {#project}\n\n"
            "### Repository {#repository} -> [rel:domain]\n"
        )

        self.assertTrue(
            any(
                "source item `repository` to ancestor heading `domain`" in error
                for error in manifest.errors
            )
        )

    def test_rel_uses_logical_ancestry_across_f_detail_files(self):
        manifest = self._manifest(
            "# Domain {#domain}\n\n"
            "## Runtime {F#runtime}\n",
            **{
                "MEMORY_runtime.md": (
                    "### Python {#runtime-python}\n\n"
                    "- `runtime-install` Install dependencies. -> [rel:domain]\n"
                )
            },
        )

        self.assertTrue(
            any(
                "source item `runtime-install` to ancestor heading `domain`" in error
                for error in manifest.errors
            )
        )

    def test_rel_to_siblings_descendants_and_unrelated_items_is_allowed(self):
        manifest = self._manifest(
            "# One {#one} -> [rel:child]\n\n"
            "## Child {#child} -> [rel:sibling]\n\n"
            "- `first` First node. -> [rel:second]\n"
            "- `second` Second node. -> []\n\n"
            "## Sibling {#sibling} -> [rel:other]\n\n"
            "# Other {#other}\n"
        )

        self.assertEqual(manifest.errors, [])

    def test_specific_edge_types_to_ancestors_are_allowed(self):
        edges = ", ".join(
            f"{edge_type}:project" for edge_type in sorted(KNOWN_EDGE_TYPES - {"rel"})
        )
        manifest = self._manifest(
            "# Project {#project}\n\n"
            f"- `project-fact` Project fact. -> [{edges}]\n"
        )

        self.assertEqual(manifest.errors, [])

    def test_existing_duplicate_dangling_and_self_edge_errors_are_preserved(self):
        manifest = self._manifest(
            "# Domain {#domain}\n\n"
            "- `one` First. -> [rel:one, rel:two, rel:two, rel:missing]\n"
            "- `two` Second. -> []\n"
            "- `two` Duplicate. -> []\n"
        )
        errors = "\n".join(manifest.errors)

        self.assertIn("duplicate id `two`", errors)
        self.assertIn("self-edge `rel:one`", errors)
        self.assertIn("duplicate edge `rel:two`", errors)
        self.assertIn("dangling edge `rel:missing`", errors)


class MfGraphManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dist = Path(self.tempdir.name)

    def test_mf_profile_uses_local_namespace_and_typed_backings(self):
        (self.dist / "MEMORY.md").write_text(
            "# Shared Memory\n\n"
            "## Topic {#topic}\n\n"
            "Topic body.\n\n"
            "## Detail {F#detail}\n\n"
            "## Evidence {M#evidence}\n\n"
            "## Instructions {S#instructions}\n",
            encoding="utf-8",
        )
        (self.dist / "MEMORY_detail.md").write_text(
            "### Detail Topic {#detail-topic}\n\nDetail body.\n",
            encoding="utf-8",
        )
        (self.dist / "MEMORY_evidence.md").write_text("free form\n", encoding="utf-8")
        (self.dist / "MEMORY_SKILL_instructions.md").write_text("# Instructions\n", encoding="utf-8")

        manifest = build_mf_manifest(self.dist, "auth-api")

        self.assertEqual(manifest.namespace, "MF#auth-api")
        self.assertEqual(manifest.errors, [])
        self.assertEqual(set(manifest.items), {"topic", "detail", "detail-topic", "evidence", "instructions"})
        self.assertEqual(manifest.backing["evidence"].kind, "M#")
        self.assertEqual(manifest.backing["instructions"].kind, "S#")

    def test_mf_profile_rejects_nested_views_and_unaddressed_prose(self):
        (self.dist / "MEMORY.md").write_text(
            "# Shared Memory\n\n"
            "Unaddressed prose.\n\n"
            "## Nested {MF#nested}\n",
            encoding="utf-8",
        )

        manifest = build_mf_manifest(self.dist, "auth-api")

        errors = "\n".join(manifest.errors)
        self.assertIn("MF# heading `nested` is not valid in MF#auth-api", errors)
        self.assertIn("MF document prose must belong to an addressable heading", errors)


if __name__ == "__main__":
    unittest.main()
