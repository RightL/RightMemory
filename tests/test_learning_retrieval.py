from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from rightmemory.assistant_context import prepare_retrieve_context
from rightmemory.graph import build_graph_manifest
from rightmemory.guidance import GUIDANCE_INBOX_PATH, GuidanceEntry, render_guidance_inbox
from rightmemory.learning import record_learning_processed, submit_learning
from rightmemory.learning_retrieval import get_learning_source, learning_sources, read_learning_page, search_learning_sources
from rightmemory.retrieval_index import build_retrieval_corpus
from rightmemory.retrieve_selection import (
    LineRange, RetrieveSelection, RetrieveSelectionError, RetrieveSelectionRenderer,
    SourceSelection, parse_retrieve_selection_json,
)
from rightmemory.tools import MemoryTools
from tests.isolated_write_test_base import IsolatedWriteTestBase


class LearningRetrievalTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        self.renderer = RetrieveSelectionRenderer(self.root, max_output_chars=100_000)

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _select(self, source_id: str):
        return RetrieveSelection(sources=[SourceSelection(source_id=source_id)])

    def test_new_evidence_is_retrievable_without_graph_admission(self):
        before = (self.root / "MEMORY.md").read_bytes()
        candidate = submit_learning(self.root, "session", "Concrete unfinished experience.")
        source_id = f"L#candidates/{candidate.candidate_id}.json"

        sources = learning_sources(self.root)
        selected = self.renderer.render(self._select(source_id))

        self.assertEqual(set(sources), {source_id})
        self.assertTrue(sources[source_id].whole_only)
        self.assertEqual(set(selected.delivery.complete_sources), {source_id})
        self.assertEqual(selected.delivery.local_items, {})
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), before)
        self.assertEqual(build_graph_manifest(self.root).errors, [])

    def test_domain_processing_keeps_evidence_in_all_retrieval_paths(self):
        candidate = submit_learning(self.root, "session", "Still useful as a case.")
        source_id = f"L#candidates/{candidate.candidate_id}.json"
        before = build_retrieval_corpus(self.root)
        for domain in ("guidance", "skills"):
            record_learning_processed(self.root, domain, [candidate.candidate_id])
            self.assertIn(source_id, learning_sources(self.root))
            self.assertEqual(build_retrieval_corpus(self.root).fingerprint, before.fingerprint)

        prepared = prepare_retrieve_context(self.root, instructions="test instructions")
        self.assertEqual(prepared["learning_source_count"], 1)
        self.assertEqual(prepared["learning_root"], str(self.root / "learning"))
        self.assertTrue((Path(prepared["learning_root"]) / f"candidates/{candidate.candidate_id}.json").is_file())
        self.assertTrue(Path(prepared["learning_index_file"]).is_file())
        self.assertFalse(any("/processed/" in path for path in prepared["source_files"]))

    def test_curated_unicode_paths_use_stable_encoded_source_ids(self):
        relative = "guidance/写作 偏好.md"
        path = self._write("learning/" + relative, "# Scope\n\nConcrete case.\n")
        source_id = "L#" + quote(relative, safe="/-._")
        self.assertEqual(get_learning_source(self.root, source_id).path, path)
        selection = parse_retrieve_selection_json(
            '{"ids":[],"sources":[{"source_id":"' + source_id + '","ids":[],"ranges":[]}],"recent_candidates":[]}'
        )
        self.assertEqual(set(self.renderer.render(selection).delivery.complete_sources), {source_id})

    def test_guidance_ranges_are_selectable_and_tracked(self):
        self._write("learning/guidance/example.md", "# Scope\n\nFirst.\nSecond.\n")
        source_id = "L#guidance/example.md"
        selection = RetrieveSelection(sources=[SourceSelection(
            source_id=source_id, ranges=[LineRange(start=3, end=3)]
        )])
        result = self.renderer.render(selection)
        self.assertEqual(result.delivery.complete_sources, {})
        self.assertEqual([(item.source_id, item.start, item.end) for item in result.delivery.ranges],
                         [(source_id, 3, 3)])
        self.assertEqual(self.renderer.current_delivery_selection(result.delivery).sources, selection.sources)

    def test_raw_evidence_and_skill_bodies_require_whole_selection(self):
        candidate = submit_learning(self.root, "session", "Case.")
        self._write("learning/skills/repeatable/SKILL.md", "# Method\n\nComplete method.\n")
        for source_id in (f"L#candidates/{candidate.candidate_id}.json", "L#skills/repeatable/SKILL.md"):
            with self.subTest(source_id=source_id):
                with self.assertRaises(RetrieveSelectionError):
                    self.renderer.render(RetrieveSelection(sources=[SourceSelection(
                        source_id=source_id, ranges=[LineRange(start=1, end=1)]
                    )]))
                self.assertIn(source_id, self.renderer.render(self._select(source_id)).delivery.complete_sources)

    def test_embedding_corpus_tracks_assets_without_indexing_nontext_resources(self):
        self._write("learning/skills/repeatable/SKILL.md", "# Method\n\n" + "Step.\n" * 500)
        self._write("learning/skills/repeatable/references/example.md", "# Example\n")
        self._write("learning/skills/repeatable/scripts/helper.py", "print('fixture')\n")
        self._write("learning/experiences/partial.md", "# Work case\n")
        entries = {entry.key: entry for entry in build_retrieval_corpus(self.root).entries}
        self.assertEqual({key for key in entries if key.startswith("L#")}, {
            "L#skills/repeatable/SKILL.md", "L#skills/repeatable/references/example.md",
            "L#experiences/partial.md",
        })
        self.assertFalse(entries["L#skills/repeatable/SKILL.md"].pending)

    def test_changed_or_deleted_sources_invalidate_delivered_coverage(self):
        path = self._write("learning/guidance/example.md", "# Original\n")
        source_id = "L#guidance/example.md"
        delivered = self.renderer.render(self._select(source_id)).delivery
        self.assertEqual(len(self.renderer.current_delivery_selection(delivered, unchanged_only=True).sources), 1)
        path.write_text("# Revised\n", encoding="utf-8")
        self.assertEqual(self.renderer.current_delivery_selection(delivered, unchanged_only=True).sources, [])
        self.assertEqual(len(self.renderer.current_delivery_selection(delivered).sources), 1)
        path.unlink()
        self.assertEqual(self.renderer.current_delivery_selection(delivered).sources, [])

    def test_source_ids_cannot_escape_inventory_or_select_graph_ids(self):
        for source_id in ("L#../MEMORY.md", "L#guidance/%2E%2E/MEMORY.md", "L#guidance/bad%.md"):
            with self.subTest(source_id=source_id), self.assertRaises(ValueError):
                SourceSelection(source_id=source_id)
        with self.assertRaises(ValueError):
            SourceSelection(source_id="L#guidance/example.md", ids=["one"])
        with self.assertRaises(RetrieveSelectionError):
            self.renderer.render(self._select("L#guidance/missing.md"))
        with self.assertRaises(ValueError):
            get_learning_source(self.root, "L#processed/guidance/" + "a" * 64 + ".json")

    def test_legacy_inbox_is_readable_without_implicit_migration(self):
        entry = GuidanceEntry("GI-20261027-1234abcd", "session", "2026-10-27T00:00:00Z", "Concrete old case.")
        path = self._write(GUIDANCE_INBOX_PATH, render_guidance_inbox([entry]))
        before = path.read_bytes()
        source_id = f"L#legacy/{entry.entry_id}"
        self.assertIn(source_id, learning_sources(self.root))
        self.assertIn(source_id, self.renderer.render(self._select(source_id)).delivery.complete_sources)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "learning").exists())

    def test_retrieve_file_scope_excludes_processing_receipts(self):
        candidate = submit_learning(self.root, "session", "Case.")
        receipt = record_learning_processed(self.root, "guidance", [candidate.candidate_id])[0]
        tools = MemoryTools(self.root, role="retrieve")
        source = f"learning/candidates/{candidate.candidate_id}.json"
        self.assertIn(source, tools.list_files("learning/**/*"))
        self.assertNotIn(receipt.relative_to(self.root).as_posix(), tools.list_files("learning/**/*"))
        with self.assertRaises(ValueError):
            tools.read(receipt.relative_to(self.root).as_posix())
        with self.assertRaises(ValueError):
            MemoryTools(self.root, role="update").read_learning("L#" + source.removeprefix("learning/"))

    def test_curators_cannot_write_other_domains_or_processing_receipts(self):
        guidance = MemoryTools(self.root, role="guidance-curator")
        skills = MemoryTools(self.root, role="skills-curator")
        guidance.create_file("learning/guidance/example.md", "# Scoped guidance\n")
        skills.create_file("learning/skills/repeatable/SKILL.md", "# Method\n")
        for tools, path in (
            (guidance, "learning/skills/other/SKILL.md"),
            (skills, "learning/guidance/other.md"),
            (guidance, "MEMORY.md"),
            (skills, "MEMORY.md"),
            (guidance, "learning/processed/guidance/" + "a" * 64 + ".json"),
            (skills, "learning/processed/skills/" + "a" * 64 + ".json"),
        ):
            with self.subTest(role=tools.role, path=path), self.assertRaises(ValueError):
                tools.create_file(path, "{}")

    def test_raw_retirement_requires_both_receipts_and_is_committable(self):
        candidate = submit_learning(self.root, "session", "Case.")
        relative = f"learning/candidates/{candidate.candidate_id}.json"
        tools = MemoryTools(self.root, role="guidance-curator")
        record_learning_processed(self.root, "guidance", [candidate.candidate_id])
        with self.assertRaises(ValueError):
            tools.delete_file(relative)
        record_learning_processed(self.root, "skills", [candidate.candidate_id])
        self._git("add", "learning/processed")
        self._git("commit", "-m", "record both domains")
        tools.delete_file(relative)
        tools.git_add([relative])
        tools.git_commit("learning: retire processed case")
        self.assertFalse((self.root / relative).exists())
        self.assertEqual(self._git("status", "--short"), "")

    def test_full_collection_search_and_browsing_are_paged_without_losing_sources(self):
        for index in range(43):
            self._write(f"learning/guidance/case-{index:02d}.md", f"# Case {index}\n\nShared context.\n")
        self._write("learning/guidance/match.md", "# Preferred method\n\n复用 workflow evidence.\n")
        first = search_learning_sources(self.root, "", limit=20)
        second = search_learning_sources(self.root, "", offset=first.next_offset, limit=20)
        last = search_learning_sources(self.root, "", offset=second.next_offset, limit=20)
        found = [source.source_id for page in (first, second, last) for source in page.sources]
        self.assertEqual(first.total, 44)
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(set(found), set(learning_sources(self.root)))
        self.assertIsNone(last.next_offset)
        exact = search_learning_sources(self.root, "复用 WORKFLOW")
        self.assertEqual([source.source_id for source in exact.sources], ["L#guidance/match.md"])
        self.assertEqual(search_learning_sources(self.root, "absent-token").total, 0)

    def test_search_pagination_rejects_unbounded_or_negative_requests(self):
        for offset, limit in ((-1, 20), (0, 0), (0, 51), (True, 20), (0, True)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(ValueError):
                search_learning_sources(self.root, "", offset=offset, limit=limit)
        with self.assertRaises(ValueError):
            MemoryTools(self.root, role="update").search_learning("")

    def test_oversized_single_line_source_can_be_read_without_loss_in_bounded_pages(self):
        original = "# Large case\n\n" + "原始" * 20_000
        self._write("learning/experiences/large.md", original)
        source_id = "L#experiences/large.md"
        pieces = []
        offset = 0
        while True:
            page = read_learning_page(self.root, source_id, offset=offset)
            self.assertLessEqual(len(page.text), 12_000)
            self.assertEqual(page.start_line, original.count("\n", 0, offset) + 1)
            self.assertEqual(page.total_chars, len(original))
            pieces.append(page.text)
            if page.next_offset is None:
                break
            offset = page.next_offset
        self.assertEqual("".join(pieces), original)
        for offset, limit in ((-1, 20), (0, 0), (0, 12_001)):
            with self.subTest(offset=offset, limit=limit), self.assertRaises(ValueError):
                read_learning_page(self.root, source_id, offset=offset, limit=limit)

