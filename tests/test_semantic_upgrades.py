from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import AgentCliConfig, RuntimeConfig
from rightmemory.runtime import RightMemoryRuntime
from rightmemory.semantic_upgrades import (
    SemanticUpgradeContext,
    SemanticUpgradeNote,
    format_refresh_summary,
    load_notes_from_directory,
    load_packaged_notes,
    main,
    mark_absorbed,
    parse_note_text,
    pending_context,
    render_prompt_context,
)


VALID_NOTE = """---
id: user-context-agent-behavior-split
introduced_at: 2026-05-20
---

# User Context And Agent Behavior Split

Revisit existing memory that mixes durable user context with agent behavior guidance.
"""


class SemanticUpgradeParserTests(unittest.TestCase):
    def test_parse_note_text_reads_front_matter_title_and_body(self):
        note = parse_note_text("example.md", VALID_NOTE)

        self.assertEqual(note.id, "user-context-agent-behavior-split")
        self.assertEqual(note.introduced_at, date(2026, 5, 20))
        self.assertEqual(note.title, "User Context And Agent Behavior Split")
        self.assertIn("Revisit existing memory", note.body)
        self.assertEqual(note.source, "example.md")

    def test_parse_note_text_rejects_missing_front_matter(self):
        with self.assertRaises(ValueError) as caught:
            parse_note_text("broken.md", "# Missing Front Matter\n")

        self.assertIn("missing front matter", str(caught.exception))

    def test_load_notes_from_directory_sorts_and_warns_for_malformed_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            directory.joinpath("later.md").write_text(
                VALID_NOTE.replace("2026-05-20", "2026-05-21").replace(
                    "user-context-agent-behavior-split", "later-note"
                ),
                encoding="utf-8",
            )
            directory.joinpath("earlier.md").write_text(
                VALID_NOTE.replace("2026-05-20", "2026-05-19").replace(
                    "user-context-agent-behavior-split", "earlier-note"
                ),
                encoding="utf-8",
            )
            directory.joinpath("broken.md").write_text("# Broken\n", encoding="utf-8")

            result = load_notes_from_directory(directory)

        self.assertEqual([note.id for note in result.notes], ["earlier-note", "later-note"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("broken.md", result.warnings[0])

    def test_load_notes_from_directory_skips_duplicate_ids_after_first(self):
        with tempfile.TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            directory.joinpath("one.md").write_text(VALID_NOTE, encoding="utf-8")
            directory.joinpath("two.md").write_text(VALID_NOTE, encoding="utf-8")

            result = load_notes_from_directory(directory)

        self.assertEqual([note.id for note in result.notes], ["user-context-agent-behavior-split"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("duplicate semantic upgrade id", result.warnings[0])

    def test_load_packaged_notes_includes_user_context_split_note(self):
        result = load_packaged_notes()

        self.assertIn("user-context-agent-behavior-split", [note.id for note in result.notes])
        self.assertEqual([], result.warnings)


class SemanticUpgradeStateTests(unittest.TestCase):
    def test_pending_context_uses_absorbed_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            mark_absorbed(root, ["user-context-agent-behavior-split"])

            context = pending_context(root)

        self.assertNotIn("user-context-agent-behavior-split", context.ids)

    def test_corrupt_state_warns_and_treats_notes_as_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / ".runtime" / "semantic-upgrades.json"
            state.parent.mkdir(parents=True)
            state.write_text("{bad json", encoding="utf-8")

            context = pending_context(root)

        self.assertIn("user-context-agent-behavior-split", context.ids)
        self.assertTrue(any("could not read semantic upgrade state" in warning for warning in context.warnings))

    def test_refresh_cli_prints_pending_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            exit_code = main(["refresh", "--memory-root", str(root)])

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / ".runtime" / "semantic-upgrades.json").exists())

    def test_format_refresh_summary_lists_pending_ids(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nBody.",
                    source="example.md",
                )
            ],
            warnings=["broken.md: missing front matter"],
        )

        summary = format_refresh_summary(context)

        self.assertIn("1 semantic upgrade note(s) pending", summary)
        self.assertIn("example-note", summary)
        self.assertIn("broken.md: missing front matter", summary)

    def test_render_prompt_context_preserves_chronological_conflict_rule(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nBody.",
                    source="example.md",
                )
            ],
            warnings=[],
        )

        rendered = render_prompt_context(context)

        self.assertIn("Pending semantic upgrade notes", rendered)
        self.assertIn("later notes refine, narrow, or contradict earlier notes", rendered)
        self.assertIn("Do not copy these notes into memory as maintenance text", rendered)


class SemanticUpgradeRuntimeAbsorptionTests(unittest.TestCase):
    def test_dreamer_success_marks_injected_semantic_upgrades_absorbed(self):
        calls = []

        class FakeDreamerExecutor:
            def __init__(self, memory_root, role, config, semantic_upgrades=None):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message, self.semantic_upgrades.ids))
                return "dreamed"

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FakeDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    result = runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state = json.loads((root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8"))

        self.assertEqual(result, "dreamed")
        self.assertEqual(calls, [("dreamer-1", "run", ["user-context-agent-behavior-split"])])
        self.assertIn("user-context-agent-behavior-split", state["absorbed"])

    def test_dreamer_failure_leaves_semantic_upgrades_pending(self):
        class FailingDreamerExecutor:
            def __init__(self, memory_root, role, config, semantic_upgrades=None):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                raise RuntimeError("dreamer failed")

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FailingDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    with self.assertRaises(RuntimeError):
                        runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state_path = root / ".runtime" / "semantic-upgrades.json"

        self.assertFalse(state_path.exists())


if __name__ == "__main__":
    unittest.main()
