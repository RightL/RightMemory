from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rightmemory.graph import build_graph_manifest
from rightmemory.pursuit_tree import PursuitOperationError, apply_operation, load_pursuit_tree
from rightmemory.tools import MemoryTools


class PursuitTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write("MEMORY.md", "# Memory\n\n## Entry {#entry}\n")
        self.write("PURSUITS.md", "# Pursuits\n")

    def write(self, name: str, value: str):
        (self.root / name).write_bytes(value.encode("utf-8"))

    def files(self):
        return {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}

    def apply(self, **operation):
        result = apply_operation(self.root, operation)
        self.assertEqual(build_graph_manifest(self.root).errors, [])
        self.assertIn("validation passed", MemoryTools(self.root).validate_memory())
        return result

    def test_snapshot_uses_logical_ancestry_across_nested_backings(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Focus\n\n- `leaf`\n\n## Parent {F#parent} → [doc:entry]\n\nParent body.\n")
        self.write("PURSUIT_parent.md", "# First {#first}\n\n# Deep {F#deep}\n")
        self.write("PURSUIT_deep.md", "# Leaf {#leaf}\n\nLeaf body.\n")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.root_ids, ("parent",))
        self.assertEqual(tree.items["parent"].child_ids, ("first", "deep"))
        self.assertEqual(tree.items["deep"].child_ids, ("leaf",))
        self.assertEqual(tree.items["leaf"].parent_id, "deep")
        self.assertEqual(tree.items["parent"].edges, (("doc", "entry"),))
        self.assertEqual(tree.items["parent"].body, "Parent body.")
        self.assertEqual(tree.focus_ids, ("leaf",))
        self.assertTrue(tree.items["leaf"].focused)
        self.assertEqual(tree.items["leaf"].source_path, "PURSUIT_deep.md")
        self.assertEqual([item["id"] for item in tree.to_dict()["items"]], ["parent", "first", "deep", "leaf"])

    def test_rename_patches_only_title_and_preserves_backings_and_body_bytes(self):
        source = "# Pursuits\r\n\r\n##   Parent  {F#parent}  -> [doc:entry]\r\n\r\nKeep  spaces.  \r\n\r\n## Other {#other}\r\n"
        self.write("PURSUITS.md", source)
        self.write("PURSUIT_parent.md", "# Child {#child}\n\nUnchanged child body.\n")
        before = self.files()
        edit = self.apply(type="rename", id="parent", title="中文 renamed")
        self.assertEqual(edit.changed_paths, ("PURSUITS.md",))
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), source.replace("Parent  {F#", "中文 renamed  {F#").encode())
        self.assertEqual((self.root / "PURSUIT_parent.md").read_bytes(), before["PURSUIT_parent.md"])
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["parent"].child_ids, ("child",))
        self.assertEqual(tree.items["parent"].edges, (("doc", "entry"),))

    def test_body_edit_keeps_other_sections_and_newline_style(self):
        source = "# Pursuits\r\n\r\n## Parent {#parent}\r\n\r\nOld note.\r\n\r\n### Child {#child}\r\n\r\n  Child body.  \r\n\r\n## Other {#other}\r\n"
        self.write("PURSUITS.md", source)
        self.apply(type="edit_body", id="parent", body="A **free** note.\n\n- A list item\n\n```md\n## Example\n```")
        after = (self.root / "PURSUITS.md").read_bytes().decode()
        self.assertEqual(after[after.index("### Child"):], source[source.index("### Child"):])
        self.assertNotIn("\n", after.replace("\r\n", ""))
        self.assertEqual(load_pursuit_tree(self.root).items["parent"].child_ids, ("child",))

    def test_create_ids_are_global_stable_and_accept_chinese_titles(self):
        first = self.apply(type="create", title="Entry")
        second = self.apply(type="create", title="中文方向")
        third = self.apply(type="create", title="Entry")
        self.assertEqual(first.selected_id, "entry-2")
        self.assertTrue(second.selected_id.startswith("p-"))
        self.assertEqual(third.selected_id, "entry-3")
        self.apply(type="rename", id=second.selected_id, title="English now")
        self.assertIn(second.selected_id, load_pursuit_tree(self.root).items)
        self.assertEqual(load_pursuit_tree(self.root).root_ids, (first.selected_id, second.selected_id, third.selected_id))

    def test_new_id_avoids_orphan_backing_and_reserved_filename(self):
        self.write("PURSUIT_orphan.md", "Unrelated existing file.\n")
        before = (self.root / "PURSUIT_orphan.md").read_bytes()
        result = self.apply(type="create", title="Orphan")
        reserved = self.apply(type="create", title="Rules")
        self.assertEqual(result.selected_id, "orphan-2")
        self.assertNotEqual(reserved.selected_id.casefold(), "rules")
        self.assertEqual((self.root / "PURSUIT_orphan.md").read_bytes(), before)

    def test_seven_levels_create_nested_f_boundaries_with_direct_child_layout(self):
        parent = None
        ids = []
        for number in range(1, 8):
            result = self.apply(type="create", parent_id=parent, title=f"Level {number}")
            ids.append(result.selected_id)
            parent = result.selected_id
        tree = load_pursuit_tree(self.root)
        for index, item_id in enumerate(ids):
            self.assertEqual(tree.items[item_id].parent_id, ids[index - 1] if index else None)
            self.assertEqual(tree.items[item_id].child_ids, (ids[index + 1],) if index < 6 else ())
        self.assertEqual(tree.items[ids[1]].anchor_kind, "F#")
        self.assertEqual(tree.items[ids[4]].anchor_kind, "F#")
        self.assertTrue((self.root / "PURSUIT_level-2.md").read_text(encoding="utf-8").startswith("# Level 3 {#level-3}"))
        self.assertTrue((self.root / "PURSUIT_level-5.md").read_text(encoding="utf-8").startswith("# Level 6 {#level-6}"))
        self.assertEqual({path.name for path in self.root.glob("PURSUIT_*.md")}, {"PURSUIT_level-2.md", "PURSUIT_level-5.md"})

    def test_move_reorders_subtree_and_keeps_nonempty_backing_identity(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a}\n\n### Child {F#child}\n\nChild body.\n\n## B {#b}\n\n### Existing {#existing}\n")
        self.write("PURSUIT_child.md", "# Deep {#deep}\n\nDeep  content.  \n")
        before = (self.root / "PURSUIT_child.md").read_bytes()
        self.apply(type="move", id="child", parent_id="b", after_id=None)
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["a"].child_ids, ())
        self.assertEqual(tree.items["b"].child_ids, ("child", "existing"))
        self.assertEqual(tree.items["child"].child_ids, ("deep",))
        self.assertEqual((self.root / "PURSUIT_child.md").read_bytes(), before)
        self.apply(type="reorder", id="child", after_id="existing")
        self.assertEqual(load_pursuit_tree(self.root).items["b"].child_ids, ("existing", "child"))

    def test_move_normalizes_deep_subtree_and_removes_empty_source_backing(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Source {F#source}\n\n## Dest {#dest}\n\n### Place {#place}\n")
        self.write("PURSUIT_source.md", "# Child {#child}\n\nChild body.\n\n## Grand {#grand}\n\n### Great {#great}\n")
        self.apply(type="move", id="child", parent_id="place")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["source"].anchor_kind, "#")
        self.assertFalse((self.root / "PURSUIT_source.md").exists())
        self.assertEqual(tree.items["place"].anchor_kind, "F#")
        self.assertEqual(tree.items["child"].parent_id, "place")
        self.assertEqual(tree.items["great"].parent_id, "grand")
        self.assertTrue((self.root / "PURSUIT_place.md").read_text(encoding="utf-8").startswith("# Child {#child}"))

    def test_move_to_depth_three_splits_existing_child_headings_recursively(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Source {F#source}\n\n## Dest {#dest}\n")
        self.write("PURSUIT_source.md", "# Child {#child}\n\n## Grand {#grand}\n\n### Great {#great}\n")
        self.apply(type="move", id="child", parent_id="dest")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["child"].anchor_kind, "F#")
        self.assertEqual(tree.items["grand"].parent_id, "child")
        self.assertEqual(tree.items["great"].parent_id, "grand")
        self.assertEqual(tree.items["source"].anchor_kind, "#")
        self.assertTrue((self.root / "PURSUIT_child.md").read_text(encoding="utf-8").startswith("# Grand {#grand}"))

    def test_cycle_and_invalid_title_fail_without_any_file_change(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Parent {F#parent}\n")
        self.write("PURSUIT_parent.md", "# Child {#child}\n")
        for operation in (
            {"type": "move", "id": "parent", "parent_id": "child"},
            {"type": "move", "id": "parent", "parent_id": "parent"},
            {"type": "rename", "id": "parent", "title": "Break\n## Added"},
        ):
            with self.subTest(operation=operation):
                before = self.files()
                with self.assertRaises(PursuitOperationError):
                    apply_operation(self.root, operation)
                self.assertEqual(self.files(), before)

    def test_invalid_candidate_is_rolled_back_with_new_backing_removed(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a}\n\n### Place {#place}\n\n## B {#b} → [rel:place]\n")
        before = self.files()
        with self.assertRaisesRegex(PursuitOperationError, "containment-only"):
            apply_operation(self.root, {"type": "move", "id": "b", "parent_id": "place"})
        self.assertEqual(self.files(), before)

    def test_delete_repairs_both_families_focus_and_removes_all_subtree_backings(self):
        self.write("MEMORY.md", "# Memory\n\n## Entry {#entry} -> [doc:parent, dep:keep]\n\nMention parent in prose.\n\n- `fact` Child mentioned. → [rel:child]\n\n## Keep {#keep}\n")
        self.write("PURSUITS.md", "# Pursuits\n\n## Focus\n\n- `parent`\n- `leaf`\n- `other`\n\n## Parent {F#parent}\n\n## Other {#other} -> [doc:child, doc:entry]\n\nMention leaf in prose.\n")
        self.write("PURSUIT_parent.md", "# Child {F#child}\n")
        self.write("PURSUIT_child.md", "# Leaf {#leaf}\n")
        self.write("untouched.txt", "A file outside the graph.\r\n")
        before = (self.root / "untouched.txt").read_bytes()
        edit = self.apply(type="delete", id="parent")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.root_ids, ("other",))
        self.assertEqual(tree.focus_ids, ("other",))
        self.assertFalse((self.root / "PURSUIT_parent.md").exists())
        self.assertFalse((self.root / "PURSUIT_child.md").exists())
        self.assertEqual(tree.items["other"].edges, (("doc", "entry"),))
        self.assertIn("Mention parent in prose.", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertIn("Mention leaf in prose.", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "untouched.txt").read_bytes(), before)
        self.assertEqual(len(edit.repaired_references), 5)
        self.assertEqual(set(edit.changed_paths), {"MEMORY.md", "PURSUITS.md", "PURSUIT_parent.md", "PURSUIT_child.md"})

    def test_last_child_delete_downgrades_empty_backing_but_keeps_prose_backing(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Empty {F#empty}\n\n## Prose {F#prose}\n")
        self.write("PURSUIT_empty.md", "\n# Last {#last}\n")
        self.write("PURSUIT_prose.md", "Keep this backing preamble.\n\n# Other {#other}\n")
        self.apply(type="delete", id="last")
        self.apply(type="delete", id="other")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["empty"].anchor_kind, "#")
        self.assertFalse((self.root / "PURSUIT_empty.md").exists())
        self.assertEqual(tree.items["prose"].anchor_kind, "F#")
        self.assertEqual((self.root / "PURSUIT_prose.md").read_text(encoding="utf-8"), "Keep this backing preamble.\n\n")

    def test_focus_order_changes_without_rewriting_bodies(self):
        source = "# Pursuits\r\n\r\n## A {#a}\r\n\r\nA note.  \r\n\r\n## B {#b}\r\n"
        self.write("PURSUITS.md", source)
        self.apply(type="set_focus", id="b", focused=True)
        self.apply(type="set_focus", id="a", focused=True)
        self.assertEqual(load_pursuit_tree(self.root).focus_ids, ("b", "a"))
        self.assertTrue((self.root / "PURSUITS.md").read_bytes().endswith(source[source.index("## A"):].encode()))
        before = self.files()
        self.assertEqual(self.apply(type="set_focus", id="a", focused=True).changed_paths, ())
        self.assertEqual(self.files(), before)
        self.apply(type="set_focus", id="b", focused=False)
        self.assertEqual(load_pursuit_tree(self.root).focus_ids, ("a",))

    def test_first_focus_removes_exact_old_starter_placeholder(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                source = (
                    "# Pursuits\n\n## Focus\n\n  No Pursuit is focused yet. \n\n"
                    "## Example Application {#sample-pursuit-application}\n\nKeep this body.\n"
                ).replace("\n", newline)
                self.write("PURSUITS.md", source)
                self.apply(type="set_focus", id="sample-pursuit-application", focused=True)
                expected = source.replace(
                    "  No Pursuit is focused yet. ", "- `sample-pursuit-application`"
                )
                self.assertEqual((self.root / "PURSUITS.md").read_bytes(), expected.encode("utf-8"))
                self.assertEqual(load_pursuit_tree(self.root).focus_ids, ("sample-pursuit-application",))

    def test_first_focus_preserves_user_body(self):
        for body in (
            "Keep this attention note.",
            "No Pursuit is focused yet.\n\nKeep this user note too.",
            "No Pursuit is focused yet!",
        ):
            with self.subTest(body=body):
                source = f"# Pursuits\n\n## Focus\n\n{body}\n\n## A {{#a}}\n"
                self.write("PURSUITS.md", source)
                self.apply(type="set_focus", id="a", focused=True)
                self.assertEqual(
                    (self.root / "PURSUITS.md").read_bytes(),
                    source.replace("## A {#a}", "- `a`\n\n## A {#a}").encode("utf-8"),
                )

    def test_plain_group_is_visible_and_receives_id_when_edited(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Plain Group\n\nPlain body.\n\n### Child {#child}\n")
        tree = load_pursuit_tree(self.root)
        plain = tree.root_ids[0]
        self.assertTrue(tree.items[plain].editable)
        self.assertEqual(tree.items[plain].anchor_kind, "plain")
        edit = self.apply(type="rename", id=plain, title="Named group")
        self.assertEqual(edit.selected_id, "named-group")
        self.assertEqual(load_pursuit_tree(self.root).items["child"].parent_id, "named-group")

    def test_legacy_bullet_is_read_only_preserved_on_move_and_removed_with_subtree(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a}\n\nA body.\n\n- `legacy` Keep this leaf. → [doc:entry]\n\n## B {#b}\n")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["a"].child_ids, ("legacy",))
        self.assertFalse(tree.items["legacy"].editable)
        self.assertTrue(any("legacy" in diagnostic for diagnostic in tree.diagnostics))
        for kind in ("rename", "delete", "move", "set_focus"):
            with self.subTest(kind=kind), self.assertRaises(PursuitOperationError):
                apply_operation(self.root, {"type": kind, "id": "legacy", "title": "No", "focused": True})
        self.apply(type="move", id="a", parent_id="b")
        self.assertIn("- `legacy` Keep this leaf. → [doc:entry]", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))
        self.apply(type="delete", id="a")
        self.assertNotIn("legacy", load_pursuit_tree(self.root).items)

    def test_body_edit_cannot_silently_delete_or_change_legacy_graph_leaves(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a}\n\nA note.\n\n- `legacy` Keep. → []\n")
        before = self.files()
        for body in ("Replacement note.", "A note.\n\n- `legacy` Changed. → []", "## Another heading"):
            with self.subTest(body=body), self.assertRaises(PursuitOperationError):
                apply_operation(self.root, {"type": "edit_body", "id": "a", "body": body})
            self.assertEqual(self.files(), before)
        self.apply(type="edit_body", id="a", body="Replacement note.\n\n- `legacy` Keep. → []")
        self.assertIn("legacy", load_pursuit_tree(self.root).items)

    def test_legacy_next_bullets_are_raw_note_text_not_legacy_leaves(self):
        body = "**State:** Old state.\n\n**Next:**\n- `research` Anything.\n- `do` Another.\n\n**Done when:** Old outcome."
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a}\n\n" + body + "\n")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["a"].body, body)
        self.assertEqual(tree.items["a"].child_ids, ())
        self.apply(type="edit_body", id="a", body="Only meaning remains.")
        self.assertEqual(load_pursuit_tree(self.root).items["a"].body, "Only meaning remains.")

    def test_invalid_root_still_has_snapshot_and_refuses_writes(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## A {#a} → [rel:missing]\n")
        tree = load_pursuit_tree(self.root)
        self.assertIn("a", tree.items)
        self.assertTrue(tree.diagnostics)
        before = self.files()
        with self.assertRaises(PursuitOperationError):
            apply_operation(self.root, {"type": "rename", "id": "a", "title": "New"})
        self.assertEqual(self.files(), before)

    def test_focus_handles_missing_final_newline_in_existing_control_block(self):
        for ending in ("## Focus", "## Focus\n\n- `a`"):
            with self.subTest(ending=ending):
                self.write("PURSUITS.md", "# Pursuits\n\n## A {F#a}\n\n" + ending)
                self.write("PURSUIT_a.md", "# B {#b}\n")
                self.apply(type="set_focus", id="b", focused=True)
                expected = ("a", "b") if "`a`" in ending else ("b",)
                self.assertEqual(load_pursuit_tree(self.root).focus_ids, expected)

    def test_new_child_handles_parent_document_without_final_newline(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Parent {#parent}")
        result = self.apply(type="create", parent_id="parent", title="Child")
        self.assertEqual(load_pursuit_tree(self.root).items[result.selected_id].parent_id, "parent")

    def test_new_focus_section_does_not_capture_existing_root_legacy_nodes(self):
        self.write("PURSUITS.md", "# Pursuits\n\n- `legacy` Root leaf. → []\n\n## A {#a}\n")
        self.apply(type="set_focus", id="a", focused=True)
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.root_ids, ("legacy", "a"))
        self.assertEqual(tree.focus_ids, ("a",))

    def test_unchanged_plain_group_note_is_a_noop(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Group\n\nExisting note.\n")
        tree = load_pursuit_tree(self.root)
        before = self.files()
        self.assertEqual(self.apply(type="edit_body", id=tree.root_ids[0], body="Existing note.").changed_paths, ())
        self.assertEqual(self.files(), before)

    def test_automatic_split_preserves_legacy_leaves_and_owner_prose(self):
        self.write("PURSUITS.md", "# Pursuits\r\n\r\n## Parent {#parent}\r\n\r\n### Child {#child}\r\n\r\nBefore leaf.  \r\n\r\n- `legacy` Leaf. → []\r\n\r\nAfter leaf.  \r\n")
        self.apply(type="create", parent_id="child", title="Grandchild")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["child"].child_ids, ("legacy", "grandchild"))
        source = (self.root / "PURSUITS.md").read_bytes()
        backing = (self.root / "PURSUIT_child.md").read_bytes()
        self.assertIn(b"Before leaf.  \r\n", source)
        self.assertIn(b"After leaf.  \r\n", source)
        self.assertIn("- `legacy` Leaf. → []\r\n".encode(), backing)
        self.assertNotIn(b"\n", backing.replace(b"\r\n", b""))

    def test_insert_after_legacy_leaf_preserves_body_and_rejects_reparenting(self):
        self.write("PURSUITS.md", "# Pursuits\n\n## Parent {#parent}\n\n- `legacy` Leaf. → []\n\nOwner text after leaf.\n")
        before = self.files()
        with self.assertRaisesRegex(PursuitOperationError, "legacy graph leaves"):
            apply_operation(self.root, {"type": "create", "parent_id": "parent", "after_id": None, "title": "First"})
        self.assertEqual(self.files(), before)
        self.apply(type="create", parent_id="parent", after_id="legacy", title="Child")
        tree = load_pursuit_tree(self.root)
        self.assertEqual(tree.items["parent"].child_ids, ("legacy", "child"))
        self.assertIn("Owner text after leaf.", tree.items["parent"].body)


if __name__ == "__main__":
    unittest.main()
