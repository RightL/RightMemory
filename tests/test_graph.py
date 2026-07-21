from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rightmemory.graph import build_graph_manifest, build_mf_manifest
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
