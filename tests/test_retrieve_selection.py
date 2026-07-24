from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import rightmemory.retrieve_selection as retrieve_selection_module
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry
from rightmemory.retrieve_selection import (
    LineRange,
    RetrieveDeliveryCoverage,
    RetrieveSelection,
    RetrieveSelectionError,
    RetrieveSelectionRenderer,
    SourceSelection,
    parse_retrieve_selection_json,
)


class RetrieveSelectionRendererTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n"
            "## Project {#project}\n\n"
            "Project body.\n\n"
            "- `project-fact` A project fact. → []\n\n"
            "### Child {#child}\n\n"
            "Child body.\n\n"
            "- `child-fact` A child fact. → []\n\n"
            "## Detail {F#detail}\n\n"
            "Detail summary.\n\n"
            "## Notes {M#notes}\n\n"
            "Free-form evidence.\n\n"
            "## Review Skill {S#review-skill}\n\n"
            "Reusable instructions.\n\n"
            "## External API {MF#external-api}\n\n"
            "Mirrored relationship.\n\n"
            "## Provider Context {MQ#provider-context}\n\n"
            "Question relationship.\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_detail.md").write_text(
            "### Detail Topic {#detail-topic}\n\n"
            "Detail body.\n\n"
            "- `detail-fact` A detail fact. → []\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_notes.md").write_text(
            "# Notes\n\n"
            "Intro.\n\n"
            "## Example\n\n"
            "Before.\n\n"
            "```python\n"
            "print('one')\n"
            "print('two')\n"
            "```\n\n"
            "After.\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_review-skill.md").write_text(
            "# Review Skill\n\nUse every instruction.\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n"
            "## Focus\n\n"
            "- `active-work` resume first\n\n"
            "## Active\n\n"
            "### Active Work {#active-work}\n\n"
            "**State:** in progress\n\n"
            "**Next:**\n\n"
            "- `do` continue implementation\n",
            encoding="utf-8",
        )
        canonical = self.root / ".runtime" / "shared_views" / "imports" / "external-api" / "dist"
        canonical.mkdir(parents=True)
        (canonical / "MEMORY.md").write_text(
            "# External\n\n"
            "## Tokens {#tokens} → []\n\n"
            "Token body.\n\n"
            "- `token-expiry` Tokens expire hourly. → []\n\n"
            "## Detail {F#mf-detail} → []\n\n"
            "## Incident Evidence {M#incident-evidence} → []\n\n"
            "## Review Checklist {S#review-checklist} → []\n",
            encoding="utf-8",
        )
        (canonical / "MEMORY_mf-detail.md").write_text(
            "# Deep Detail {#deep-detail} → []\n\nDeep body.\n",
            encoding="utf-8",
        )
        (canonical / "MEMORY_incident-evidence.md").write_text(
            "# Evidence\n\nLine one.\nLine two.\nLine three.\n",
            encoding="utf-8",
        )
        (canonical / "MEMORY_SKILL_review-checklist.md").write_text(
            "# Checklist\n\nReview every item.\n",
            encoding="utf-8",
        )
        (canonical / "manifest.toml").write_text(
            'version = 2\nview_id = "external-api"\ndocument_kind = "rightmemory-memory"\n',
            encoding="utf-8",
        )
        (canonical.parent / "view.md").write_text("# External API\n", encoding="utf-8")
        (canonical.parent / "recipe.toml").write_text(
            'version = 1\nview_id = "external-api"\nkind = "file"\nsecret = true\n',
            encoding="utf-8",
        )
        (canonical.parent / "rightmemory-shared-view.toml").write_text(
            'version = 2\nview_id = "external-api"\nkind = "file"\n',
            encoding="utf-8",
        )
        self.renderer = RetrieveSelectionRenderer(self.root, max_output_chars=100_000)

    def test_heading_and_node_selection_have_tree_semantics(self):
        heading = self.renderer.render(RetrieveSelection(ids=["project"]))
        node = self.renderer.render(RetrieveSelection(ids=["child-fact"]))

        self.assertIn("## Project {#project}", heading.text)
        self.assertIn("Project body.", heading.text)
        self.assertIn("### Child {#child}", heading.text)
        self.assertIn("- `child-fact`", heading.text)
        self.assertNotIn("## Detail {F#detail}", heading.text)
        self.assertIn("# Memory", node.text)
        self.assertIn("## Project {#project}", node.text)
        self.assertIn("### Child {#child}", node.text)
        self.assertIn("- `child-fact`", node.text)
        self.assertIn("Project body.", node.text)
        self.assertIn("Child body.", node.text)
        self.assertNotIn("- `project-fact`", node.text)
        self.assertNotIn("## Detail {F#detail}", node.text)

    def test_local_rendering_uses_the_canonical_index_without_shadow_parsers(self):
        rendered = self.renderer.render(RetrieveSelection(ids=["child-fact"]))

        self.assertEqual(
            rendered.text,
            "# Memory\n\n"
            "## Project {#project}\n\n"
            "Project body.\n\n"
            "### Child {#child}\n\n"
            "Child body.\n\n"
            "- `child-fact` A child fact. → []",
        )
        self.assertFalse(hasattr(retrieve_selection_module, "_TreeParser"))
        self.assertFalse(hasattr(retrieve_selection_module, "_LogicalGraph"))

    def test_f_detail_and_pursuit_focus_are_resolved(self):
        detail = self.renderer.render(RetrieveSelection(ids=["detail"]))
        pursuit = self.renderer.render(RetrieveSelection(ids=["active-work"]))

        self.assertIn("Detail summary.", detail.text)
        self.assertIn("### Detail Topic {#detail-topic}", detail.text)
        self.assertIn("- `detail-fact`", detail.text)
        self.assertIn("## Focus", pursuit.text)
        self.assertIn("- `active-work` resume first", pursuit.text)
        self.assertIn("### Active Work {#active-work}", pursuit.text)
        self.assertIn("**State:** in progress", pursuit.text)

    def test_m_range_adds_heading_and_completes_fence_without_line_numbers(self):
        rendered = self.renderer.render(
            RetrieveSelection(
                sources=[
                    SourceSelection(
                        source_id="M#notes",
                        ranges=[LineRange(start=10, end=10)],
                    )
                ]
            )
        )

        self.assertIn("Source: `M#notes`", rendered.text)
        self.assertIn("## Example", rendered.text)
        self.assertIn("```python", rendered.text)
        self.assertIn("print('one')", rendered.text)
        self.assertIn("print('two')", rendered.text)
        self.assertNotRegex(rendered.text, r"(?m)^\d+: ")

    def test_skill_is_whole_and_ranges_are_rejected(self):
        rendered = self.renderer.render(
            RetrieveSelection(sources=[SourceSelection(source_id="S#review-skill")])
        )
        self.assertIn(
            "# Review Skill\n\nUse every instruction.",
            rendered.text.replace("\r\n", "\n"),
        )

        with self.assertRaisesRegex(RetrieveSelectionError, "complete skill"):
            self.renderer.render(
                RetrieveSelection(
                    sources=[
                        SourceSelection(
                            source_id="S#review-skill",
                            ranges=[LineRange(start=1, end=1)],
                        )
                    ]
                )
            )

    def test_mf_ids_are_scoped_and_metadata_is_not_exposed(self):
        rendered = self.renderer.render(
            RetrieveSelection(
                sources=[SourceSelection(source_id="MF#external-api", ids=["tokens"])]
            )
        )

        self.assertIn("## Tokens {#tokens}", rendered.text)
        self.assertIn("Token body.", rendered.text)
        self.assertIn("- `token-expiry`", rendered.text)
        self.assertNotIn("recipe.toml", rendered.text)
        self.assertNotIn("secret = true", rendered.text)

    def test_mf_delivery_is_namespaced_by_view(self):
        memory = self.root / "MEMORY.md"
        memory.write_text(
            memory.read_text(encoding="utf-8")
            + "\n## Billing API {MF#billing-api}\n\nMirrored billing relationship.\n",
            encoding="utf-8",
        )
        package = self.root / ".runtime" / "shared_views" / "imports" / "billing-api"
        dist = package / "dist"
        dist.mkdir(parents=True)
        (package / "view.md").write_text("# Billing API\n", encoding="utf-8")
        (package / "recipe.toml").write_text(
            'version = 1\nview_id = "billing-api"\nkind = "file"\n',
            encoding="utf-8",
        )
        (package / "rightmemory-shared-view.toml").write_text(
            'version = 2\nview_id = "billing-api"\nkind = "file"\n',
            encoding="utf-8",
        )
        (dist / "manifest.toml").write_text(
            'version = 2\nview_id = "billing-api"\ndocument_kind = "rightmemory-memory"\n',
            encoding="utf-8",
        )
        (dist / "MEMORY.md").write_text(
            "# Billing Tokens {#tokens} → []\n\nBilling token body.\n",
            encoding="utf-8",
        )
        first = self.renderer.render(
            RetrieveSelection(
                sources=[SourceSelection(source_id="MF#external-api", ids=["tokens"])]
            )
        )

        second = self.renderer.render(
            RetrieveSelection(
                sources=[SourceSelection(source_id="MF#billing-api", ids=["tokens"])]
            ),
            delivered=first.delivery,
        )

        self.assertIn("Billing token body.", second.text)

    def test_mf_f_detail_and_qualified_resources(self):
        detail = self.renderer.render(
            RetrieveSelection(
                sources=[SourceSelection(source_id="MF#external-api", ids=["deep-detail"])]
            )
        )
        evidence = self.renderer.render(
            RetrieveSelection(
                sources=[
                    SourceSelection(
                        source_id="MF#external-api/M#incident-evidence",
                        ranges=[LineRange(start=3, end=4)],
                    )
                ]
            )
        )
        skill = self.renderer.render(
            RetrieveSelection(
                sources=[SourceSelection(source_id="MF#external-api/S#review-checklist")]
            )
        )

        self.assertIn("# Deep Detail {#deep-detail}", detail.text)
        self.assertIn("Line one.", evidence.text)
        self.assertIn("Line two.", evidence.text)
        self.assertIn("Review every item.", skill.text)

    def test_direct_mf_ranges_and_invalid_qualification_are_rejected(self):
        with self.assertRaisesRegex(RetrieveSelectionError, "not line ranges"):
            self.renderer.render(
                RetrieveSelection(
                    sources=[
                        SourceSelection(
                            source_id="MF#external-api",
                            ranges=[LineRange(start=1, end=1)],
                        )
                    ]
                )
            )
        with self.assertRaisesRegex(ValueError, "qualified"):
            SourceSelection(source_id="MF#external-api/M#incident-evidence/deeper")
        with self.assertRaisesRegex(RetrieveSelectionError, "mismatched"):
            self.renderer.render(
                RetrieveSelection(
                    sources=[SourceSelection(source_id="MF#external-api/S#incident-evidence")]
                )
            )

    def test_selecting_outer_mf_heading_does_not_expand_import(self):
        rendered = self.renderer.render(RetrieveSelection(ids=["external-api"]))

        self.assertIn("Mirrored relationship.", rendered.text)
        self.assertNotIn("Token body.", rendered.text)

    def test_locally_modified_mf_package_fails_closed(self):
        path = (
            self.root
            / ".runtime"
            / "shared_views"
            / "imports"
            / "external-api"
            / "dist"
            / "MEMORY.md"
        )
        path.write_text("# Broken wrapper\n\nunaddressed text\n", encoding="utf-8")

        with self.assertRaisesRegex(RetrieveSelectionError, "invalid canonical mirrored view"):
            self.renderer.render(
                RetrieveSelection(
                    sources=[SourceSelection(source_id="MF#external-api", ids=["tokens"])]
                )
            )

    def test_mq_selection_is_local_and_deterministic(self):
        rendered = self.renderer.render(RetrieveSelection(ids=["provider-context"]))

        self.assertIn("Question relationship.", rendered.text)
        self.assertIn("Provider question context is available for `MQ#provider-context`.", rendered.text)
        with self.assertRaisesRegex(ValueError, "source_id must be an M#, S#, MF#"):
            SourceSelection(source_id="MQ#provider-context")

    def test_delivery_omits_unchanged_items_and_override_repeats_them(self):
        first = self.renderer.render(RetrieveSelection(ids=["project-fact"]))
        second = self.renderer.render(
            RetrieveSelection(ids=["project-fact"]),
            delivered=first.delivery,
        )
        repeated = self.renderer.render(
            RetrieveSelection(ids=["project-fact"]),
            delivered=first.delivery,
            include_returned=True,
        )

        self.assertEqual(second.text, "no strong match")
        self.assertIn("- `project-fact`", repeated.text)

    def test_changed_item_with_same_id_can_return(self):
        first = self.renderer.render(RetrieveSelection(ids=["project-fact"]))
        memory = self.root / "MEMORY.md"
        memory.write_text(
            memory.read_text(encoding="utf-8").replace("A project fact.", "A revised project fact."),
            encoding="utf-8",
        )

        revised = self.renderer.render(
            RetrieveSelection(ids=["project-fact"]),
            delivered=first.delivery,
        )
        self.assertIn("A revised project fact.", revised.text)

    def test_recent_candidates_use_composite_ids_and_preserve_order(self):
        entries = [
            RecentSubmittedMemoryEntry("session-a", 1, "2026-07-18T00:00:00+00:00", "First evidence."),
            RecentSubmittedMemoryEntry("session-a", 2, "2026-07-18T00:01:00+00:00", "Second evidence."),
        ]
        rendered = self.renderer.render(
            RetrieveSelection(recent_candidates=["session-a:2", "session-a:1"]),
            recent_entries=entries,
        )

        self.assertLess(rendered.text.index("First evidence."), rendered.text.index("Second evidence."))
        self.assertEqual(
            rendered.delivery.recent_candidates,
            ["session-a:1", "session-a:2"],
        )

    def test_strict_json_rejects_prose_and_unknown_fields(self):
        parsed = parse_retrieve_selection_json(
            '{"ids": ["project"], "sources": [], "recent_candidates": []}'
        )
        self.assertEqual(parsed.ids, ["project"])
        with self.assertRaises(RetrieveSelectionError):
            parse_retrieve_selection_json(
                'Here: {"ids": [], "sources": [], "recent_candidates": []}'
            )
        with self.assertRaises(RetrieveSelectionError):
            parse_retrieve_selection_json(
                '{"ids": [], "sources": [], "recent_candidates": [], "reason": "x"}'
            )
        with self.assertRaises(RetrieveSelectionError):
            parse_retrieve_selection_json('{"ids": []}')

    def test_output_limit_rejects_instead_of_truncating(self):
        renderer = RetrieveSelectionRenderer(self.root, max_output_chars=20)
        with self.assertRaisesRegex(RetrieveSelectionError, "select less content"):
            renderer.render(RetrieveSelection(ids=["project"]))


class RetrieveDeliveryCoverageTests(unittest.TestCase):
    def test_merge_updates_versions_without_dropping_other_kinds(self):
        old = RetrieveDeliveryCoverage(local_items={"one": "old"}, recent_candidates=["s:1"])
        newer = RetrieveDeliveryCoverage(local_items={"one": "new", "two": "v"})

        merged = old.merged(newer)

        self.assertEqual(merged.local_items, {"one": "new", "two": "v"})
        self.assertEqual(merged.recent_candidates, ["s:1"])


if __name__ == "__main__":
    unittest.main()
