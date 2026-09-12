from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.graph import build_graph_manifest
from rightmemory.retrieve_context import RetrieveContextStore
from rightmemory.retrieve_selection import RetrieveSelection, RetrieveSelectionError, RetrieveSelectionRenderer, SourceSelection
from rightmemory.retrieve_view import ACTIVE_RETRIEVE_VIEW, RetrieveView
from rightmemory.tools import MemoryTools


class RetrieveViewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.memory = self.root / "MEMORY.md"
        self.memory.write_text("##\n\nContext.\n\n- First.\n- Second.\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

    def renderer(self, view):
        return RetrieveSelectionRenderer(self.root, max_output_chars=10000, view=view)

    def selector(self, view, line, namespace="local", file="MEMORY.md"):
        index = view.indexes[namespace]
        return index.block_ids[(index.manifest.root / file, line)]

    def test_fully_anonymous_leaf_heading_and_root_resolve_to_original_content(self):
        view = RetrieveView(self.root)
        rendered = self.renderer(view).render(RetrieveSelection(ids=[self.selector(view, 6)]))
        self.assertEqual(rendered.text, "##\n\nContext.\n\n- Second.")
        whole = self.renderer(view).render(RetrieveSelection(ids=[self.selector(view, 0)]))
        self.assertEqual(whole.text, self.memory.read_text().rstrip())
        heading = self.renderer(view).render(RetrieveSelection(ids=[self.selector(view, 1)]))
        self.assertEqual(heading.text, whole.text)
        self.assertEqual(build_graph_manifest(self.root).items, {})

    def test_identical_siblings_have_distinct_locations_and_coverage(self):
        self.memory.write_text("##\n- Same.\n- Same.\n", encoding="utf-8")
        view = RetrieveView(self.root)
        one, two = self.selector(view, 2), self.selector(view, 3)
        self.assertNotEqual(one, two)
        first = self.renderer(view).render(RetrieveSelection(ids=[one]))
        second = self.renderer(view).render(RetrieveSelection(ids=[two]))
        self.assertTrue(first.delivery.local_items.keys().isdisjoint(second.delivery.local_items))
        self.assertEqual(list(first.delivery.local_items.values()), list(second.delivery.local_items.values()))

    def test_nested_markdown_and_body_fences_create_no_extra_selection_ids(self):
        self.memory.write_text("##\n:::body\n### Formatting\n- Body list\n:::\n\n- Leaf\n\n  ### Nested\n  - Nested list\n", encoding="utf-8")
        view = RetrieveView(self.root)
        numbers = {block.line_number for block in view.local.ids.values() if block.source_path == self.memory}
        self.assertEqual(numbers, {0, 1, 7})

    def test_unchanged_view_keeps_ids_and_changed_view_never_reuses_them(self):
        store = RetrieveContextStore(self.root)
        first = RetrieveView(self.root)
        store.record_selection_state("session", first.state)
        same = RetrieveView(self.root, store.load("session").selection_state)
        self.assertEqual(first.local.block_ids, same.local.block_ids)
        self.memory.write_text("##\n\nChanged.\n\n- First.\n- Second.\n", encoding="utf-8")
        changed = RetrieveView(self.root, same.state)
        old_ids = {key for key, block in first.local.ids.items() if block.source_path == self.memory}
        new_ids = {key for key, block in changed.local.ids.items() if block.source_path == self.memory}
        self.assertTrue(old_ids.isdisjoint(new_ids))
        with self.assertRaisesRegex(RetrieveSelectionError, "expired"):
            self.renderer(changed).render(RetrieveSelection(ids=[self.selector(first, 6)]))
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.renderer(first).render(RetrieveSelection(ids=[self.selector(first, 6)]))

    def test_explicit_ids_survive_and_generated_ids_avoid_collisions(self):
        self.memory.write_text("## {#r1}\n- `r2` Stored. → []\n- Anonymous.\n", encoding="utf-8")
        view = RetrieveView(self.root)
        self.assertEqual(self.selector(view, 1), "r1")
        self.assertEqual(self.selector(view, 2), "r2")
        self.assertNotIn(self.selector(view, 3), {"r1", "r2"})
        generated = self.selector(view, 3)
        self.memory.write_text(f"## {{#{generated}}}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            RetrieveView(self.root, view.state)

    def test_heading_delivery_covers_anonymous_leaves_with_original_versions(self):
        first = RetrieveView(self.root)
        delivered = self.renderer(first).render(RetrieveSelection(ids=[self.selector(first, 1)])).delivery
        leaf = first.local.ids[self.selector(first, 6)]
        self.assertIn(first.local.coverage_key(leaf), delivered.local_items)
        self.assertFalse(set(first.state["issued"]) & delivered.local_items.keys())
        # An unrelated file changes every temporary ID in this local snapshot,
        # while the unchanged original content remains covered.
        (self.root / "PURSUITS.md").write_text("# Different pursuits\n", encoding="utf-8")
        current = RetrieveView(self.root, first.state)
        selection = self.renderer(current).current_delivery_selection(delivered, unchanged_only=True)
        self.assertIn(self.selector(current, 6), selection.ids)
        # Explicit reselection still returns content, as for stored IDs.
        repeated = self.renderer(current).render(RetrieveSelection(ids=[self.selector(current, 6)]))
        self.assertIn("- Second.", repeated.text)
        self.memory.write_text(self.memory.read_text().replace("Context.", "Changed context."), encoding="utf-8")
        changed = RetrieveView(self.root, current.state)
        self.assertEqual(self.renderer(changed).current_delivery_selection(delivered, unchanged_only=True).ids, [])
        self.assertTrue(self.renderer(changed).changed_delivery_labels(delivered))

    def test_anonymous_f_detail_uses_same_index_for_read_and_resolution(self):
        self.memory.write_text("## Detail {F#detail}\n\nOwner context.\n", encoding="utf-8")
        detail = self.root / "MEMORY_detail.md"
        detail.write_text("##\n- Detail leaf.\n", encoding="utf-8")
        view = RetrieveView(self.root)
        selector = self.selector(view, 2, file="MEMORY_detail.md")
        token = ACTIVE_RETRIEVE_VIEW.set(view)
        try:
            with patch.object(view.local, "document_text", wraps=view.local.document_text) as read:
                MemoryTools(self.root, role="retrieve").read_detail("detail")
                read.assert_called_once_with(detail)
        finally:
            ACTIVE_RETRIEVE_VIEW.reset(token)
        result = self.renderer(view).render(RetrieveSelection(ids=[selector]))
        self.assertIn("Owner context.", result.text)
        self.assertIn("- Detail leaf.", result.text)
        self.assertNotIn(selector, result.text)

    def make_import(self, name):
        package = self.root / ".runtime/shared_views/imports" / name
        dist = package / "dist"
        dist.mkdir(parents=True)
        (dist / "MEMORY.md").write_text("##\n- Imported leaf.\n", encoding="utf-8")
        (dist / "manifest.toml").write_text(f'version = 2\nview_id = "{name}"\ndocument_kind = "rightmemory-memory"\n', encoding="utf-8")
        (package / "rightmemory-shared-view.toml").write_text(f'version = 2\nview_id = "{name}"\nkind = "file"\n', encoding="utf-8")
        (package / "view.md").write_text("# Shared\n", encoding="utf-8")
        (package / "recipe.toml").write_text(f'version = 1\nview_id = "{name}"\nkind = "file"\n', encoding="utf-8")

    def test_anonymous_imports_are_scoped_and_independently_selectable(self):
        self.memory.write_text("## A {MF#a}\n## B {MF#b}\n", encoding="utf-8")
        for name in ("a", "b"):
            self.make_import(name)
        view = RetrieveView(self.root)
        a, b = self.selector(view, 2, "MF#a"), self.selector(view, 2, "MF#b")
        result = self.renderer(view).render(RetrieveSelection(sources=[SourceSelection(source_id="MF#a", ids=[a])]))
        self.assertIn("- Imported leaf.", result.text)
        self.assertTrue(all(key.startswith("MF#a:") for key in result.delivery.source_items))
        with self.assertRaisesRegex(RetrieveSelectionError, "source-scoped"):
            self.renderer(view).render(RetrieveSelection(sources=[SourceSelection(source_id="MF#a", ids=[b])]))
        self.assertEqual(self.renderer(view).current_delivery_selection(result.delivery, unchanged_only=True).sources[0].ids, [a])

    def test_reading_copies_do_not_modify_source_or_create_durable_references(self):
        source = self.memory.read_bytes()
        view = RetrieveView(self.root)
        directory = view.write_files("session")
        self.assertTrue((directory / "local/MEMORY.md").is_file())
        self.assertEqual(self.memory.read_bytes(), source)
        selector = self.selector(view, 6)
        MemoryTools(self.root, role="shared-view-builder").create_extractive_file_view(
            view_id="shared", title="Shared", intent="Share leaf", include_nodes=[selector],
        )
        self.assertFalse((self.root / "shared_views/shared/recipe.toml").exists())
        self.assertEqual(self.memory.read_bytes(), source)

    def test_generated_selection_ids_are_not_graph_or_focus_targets(self):
        view = RetrieveView(self.root)
        selector = self.selector(view, 6)
        self.memory.write_text(self.memory.read_text() + f"\n## Edge {{#edge}} → [rel:{selector}]\n", encoding="utf-8")
        self.assertTrue(build_graph_manifest(self.root).errors)
        self.memory.write_text("## Memory\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(f"## Focus\n- `{selector}`\n", encoding="utf-8")
        self.assertTrue(build_graph_manifest(self.root).errors)


if __name__ == "__main__":
    unittest.main()
