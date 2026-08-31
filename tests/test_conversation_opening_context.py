from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rightmemory.conversations.opening_context import OpeningContextError, build_opening_context
from rightmemory.graph import build_graph_manifest
from rightmemory.pursuit_tree import load_pursuit_tree


class ConversationOpeningContextTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8", newline="")

    def snapshot(self, item_id: str) -> dict[str, object]:
        items = load_pursuit_tree(self.root).to_dict()["items"]
        return next(item for item in items if item["id"] == item_id)

    def build(self, item_id: str):
        return build_opening_context(
            self.root,
            self.snapshot(item_id),
            host_label="Remote host",
            project_label="Execution project",
            execution_cwd="/srv/execution-project",
        )

    def test_selects_only_current_direct_neighbors_and_logical_heading_ancestors(self) -> None:
        self.write(
            "MEMORY.md",
            """# Memory

## Incoming {#incoming} -> [rel:current]

Incoming-owned prose.

### Incoming child {#incoming-child}

Incoming child secret.

## Second outgoing {#second-outgoing}

Second-outgoing-owned prose.

## Outgoing {#outgoing} -> [doc:two-hop]

Outgoing-owned prose.

### Outgoing child {#outgoing-child}

Outgoing child secret.

## Two hop {#two-hop}

Two-hop secret.
""",
        )
        self.write(
            "PURSUITS.md",
            """# Pursuits

## Focus

- `current`

## Current {#current} -> [doc:outgoing, rel:second-outgoing]

Current-owned prose.

### Current child {#current-child}

Current child secret.

## Unrelated {#unrelated}

Unrelated secret.
""",
        )

        context = self.build("current")

        self.assertEqual(context.controller_memory_root, str(self.root))
        self.assertEqual(context.current.selection_id, "current")
        self.assertEqual(
            [section.selection_id for section in context.neighbors],
            ["incoming", "second-outgoing", "outgoing"],
        )
        self.assertEqual(
            context.edge_triples,
            (
                ("incoming", "rel", "current"),
                ("current", "doc", "outgoing"),
                ("current", "rel", "second-outgoing"),
            ),
        )
        selected = {section.selection_id: section for section in context.sections}
        self.assertEqual(
            set(selected),
            {
                "plain:MEMORY.md:1",
                "incoming",
                "second-outgoing",
                "outgoing",
                "plain:PURSUITS.md:1",
                "current",
            },
        )
        self.assertEqual(
            {
                selection_id: (
                    section.block_kind,
                    section.source_path,
                    section.source_line,
                    tuple(
                        (fragment.source_path, fragment.start_line, fragment.end_line)
                        for fragment in section.prose_fragments
                    ),
                )
                for selection_id, section in selected.items()
            },
            {
                "plain:MEMORY.md:1": ("heading", "MEMORY.md", 1, ()),
                "incoming": ("heading", "MEMORY.md", 3, (("MEMORY.md", 5, 5),)),
                "second-outgoing": (
                    "heading",
                    "MEMORY.md",
                    11,
                    (("MEMORY.md", 13, 13),),
                ),
                "outgoing": ("heading", "MEMORY.md", 15, (("MEMORY.md", 17, 17),)),
                "plain:PURSUITS.md:1": ("heading", "PURSUITS.md", 1, ()),
                "current": ("heading", "PURSUITS.md", 7, (("PURSUITS.md", 9, 9),)),
            },
        )
        self.assertTrue(
            set(selected).isdisjoint(
                {
                    "plain:PURSUITS.md:3",
                    "current-child",
                    "incoming-child",
                    "outgoing-child",
                    "two-hop",
                    "unrelated",
                }
            )
        )
        self.assertNotIn(("outgoing", "doc", "two-hop"), context.edge_triples)
        self.assertTrue(all("\\" not in section.source_path for section in context.sections))
        self.assertEqual(context.execution.execution_cwd, "/srv/execution-project")

    def test_rejects_stale_or_non_root_relative_snapshot_locations(self) -> None:
        self.write("MEMORY.md", "# Memory\n")
        self.write("PURSUITS.md", "# Pursuits\n\n## Current {#current}\n")
        snapshot = self.snapshot("current")

        invalid_snapshots = (
            {**snapshot, "source_path": "../PURSUITS.md"},
            {**snapshot, "source_line": 1},
            {**snapshot, "id": "different"},
        )
        for invalid in invalid_snapshots:
            with self.subTest(snapshot=invalid), self.assertRaises(OpeningContextError):
                build_opening_context(
                    self.root,
                    invalid,
                    host_label="Host",
                    project_label="Project",
                    execution_cwd="/srv/project",
                )

    def test_plain_snapshot_id_resolves_through_nested_f_logical_ancestry(self) -> None:
        self.write("MEMORY.md", "# Memory\n")
        self.write("PURSUITS.md", "# Pursuits\n\n## Parent {F#parent}\n")
        self.write(
            "PURSUIT_parent.md",
            "Parent backing-root prose.\n\n# Bridge {F#bridge}\n\nBridge-owned prose.\n",
        )
        self.write(
            "PURSUIT_bridge.md",
            "Bridge backing-root prose.\n\n# Plain leaf\n\nLeaf-owned prose.\n",
        )

        context = self.build("plain:PURSUIT_bridge.md:3")

        self.assertEqual(context.current.selection_id, "plain:PURSUIT_bridge.md:3")
        self.assertEqual(context.current.block_kind, "heading")
        self.assertEqual(
            (context.current.source_path, context.current.source_line),
            ("PURSUIT_bridge.md", 3),
        )
        self.assertEqual(
            [
                (fragment.source_path, fragment.start_line, fragment.end_line)
                for fragment in context.current.prose_fragments
            ],
            [("PURSUIT_bridge.md", 5, 5)],
        )
        self.assertEqual(context.neighbors, ())
        self.assertEqual(context.edge_triples, ())
        self.assertEqual(
            [section.selection_id for section in context.ancestors],
            ["plain:PURSUITS.md:1", "parent", "bridge"],
        )
        self.assertEqual(
            [section.selection_id for section in context.sections],
            ["plain:PURSUITS.md:1", "parent", "bridge", "plain:PURSUIT_bridge.md:3"],
        )
        parent = next(section for section in context.sections if section.selection_id == "parent")
        bridge = next(section for section in context.sections if section.selection_id == "bridge")
        self.assertEqual((parent.source_path, parent.source_line), ("PURSUITS.md", 3))
        self.assertEqual(
            [
                (
                    fragment.source_path,
                    fragment.start_line,
                    fragment.end_line,
                )
                for fragment in parent.prose_fragments
            ],
            [("PURSUIT_parent.md", 1, 1)],
        )
        self.assertEqual((bridge.source_path, bridge.source_line), ("PURSUIT_parent.md", 3))
        self.assertEqual(
            [
                (
                    fragment.source_path,
                    fragment.start_line,
                    fragment.end_line,
                )
                for fragment in bridge.prose_fragments
            ],
            [
                ("PURSUIT_parent.md", 5, 5),
                ("PURSUIT_bridge.md", 1, 1),
            ],
        )
        self.assertTrue(
            {
                (fragment.source_path, fragment.start_line, fragment.end_line)
                for fragment in context.current.prose_fragments
            }.isdisjoint(
                {
                    (fragment.source_path, fragment.start_line, fragment.end_line)
                    for fragment in bridge.prose_fragments
                }
            )
        )

    def test_graph_node_uses_canonically_indexed_prose_without_edge_syntax(self) -> None:
        self.write(
            "MEMORY.md",
            "# Memory\n\n## Target {#memory-target}\n\nTarget-owned prose.\n",
        )
        self.write(
            "PURSUITS.md",
            """# Pursuits

## Direction {#direction}

- `legacy` Inline node prose. -> [doc:memory-target]
""",
        )

        manifest = build_graph_manifest(self.root)
        item = manifest.items["legacy"]
        block = manifest.block_for_id("legacy")
        assert block is not None
        self.assertEqual(block.kind, "node")
        self.assertEqual(block.item_id, "legacy")
        self.assertEqual(block.source_path, self.root / "PURSUITS.md")
        self.assertEqual((block.line_number, block.end_line), (5, 5))
        self.assertEqual(item.block_key, block.key)
        self.assertEqual(item.file, block.source_path)
        self.assertEqual((item.line_number, item.end_line), (5, 5))
        self.assertEqual(item.edges, (("doc", "memory-target"),))
        tree_item = load_pursuit_tree(self.root).items["legacy"]
        self.assertEqual((tree_item.source_path, tree_item.source_line), ("PURSUITS.md", 5))
        self.assertEqual(tree_item.edges, item.edges)

        context = self.build("legacy")

        self.assertEqual(context.current.selection_id, "legacy")
        self.assertEqual(context.current.block_kind, "node")
        self.assertEqual((context.current.source_path, context.current.source_line), ("PURSUITS.md", 5))
        self.assertEqual(
            [
                (
                    fragment.source_path,
                    fragment.start_line,
                    fragment.end_line,
                )
                for fragment in context.current.prose_fragments
            ],
            [("PURSUITS.md", 5, 5)],
        )
        self.assertEqual(context.edge_triples, (("legacy", "doc", "memory-target"),))
        self.assertEqual(
            [section.selection_id for section in context.ancestors],
            ["plain:MEMORY.md:1", "plain:PURSUITS.md:1", "direction"],
        )


if __name__ == "__main__":
    unittest.main()
