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
    baseline_packaged_notes,
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

    def test_load_packaged_notes_includes_current_notes(self):
        result = load_packaged_notes()

        notes_by_id = {note.id: note for note in result.notes}
        self.assertIn("user-context-agent-behavior-split", notes_by_id)
        self.assertIn("open-context-questions", notes_by_id)
        self.assertIn("uncertain-memory-marker", notes_by_id)
        self.assertIn("schema-level-memory-skills", notes_by_id)
        self.assertIn("shared-view-headings", notes_by_id)
        self.assertIn("# Open Context Questions {#open-context-questions}", notes_by_id["open-context-questions"].body)
        self.assertIn("not declarative memory facts", notes_by_id["open-context-questions"].body)
        self.assertIn("Uncertain:", notes_by_id["uncertain-memory-marker"].body)
        self.assertIn("S#slug", notes_by_id["schema-level-memory-skills"].body)
        self.assertIn("reusable instruction assets", notes_by_id["schema-level-memory-skills"].body)
        self.assertIn("MF#slug", notes_by_id["shared-view-headings"].body)
        self.assertIn("MQ#slug", notes_by_id["shared-view-headings"].body)
        self.assertIn("shared view", notes_by_id["shared-view-headings"].body)
        self.assertEqual([], result.warnings)


class SemanticUpgradeStateTests(unittest.TestCase):
    def test_pending_context_uses_absorbed_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            mark_absorbed(root, ["user-context-agent-behavior-split"])

            context = pending_context(root)

        self.assertNotIn("user-context-agent-behavior-split", context.ids)
        self.assertIn("open-context-questions", context.ids)

    def test_baseline_packaged_notes_marks_current_notes_absorbed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            baseline = baseline_packaged_notes(root)
            context = pending_context(root)

        self.assertIn("user-context-agent-behavior-split", baseline.ids)
        self.assertIn("open-context-questions", baseline.ids)
        self.assertIn("uncertain-memory-marker", baseline.ids)
        self.assertIn("schema-level-memory-skills", baseline.ids)
        self.assertEqual([], context.ids)

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

    def test_baseline_cli_prints_current_baseline_and_clears_pending_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            exit_code = main(["baseline", "--memory-root", str(root)])
            context = pending_context(root)

            self.assertEqual(exit_code, 0)
            self.assertEqual([], context.ids)
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

    def test_render_prompt_context_includes_note_content(self):
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

        self.assertIn("example-note", rendered)
        self.assertIn("Body.", rendered)


class RuntimeStateRootTests(unittest.TestCase):
    def test_runtime_state_stores_use_state_root_while_tools_use_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            state_root = root / "state"
            memory_root.mkdir()
            config = RuntimeConfig(
                role="retrieve",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=memory_root,
                state_root=state_root,
                fresh_provider_session=False,
            )

            with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
                runtime = RightMemoryRuntime(config)
                try:
                    session_paths = runtime.sessions.paths("agent-1")
                    delivery_path = runtime.recent_submitted_delivery._state_path("agent-1")
                finally:
                    runtime.cleanup()

        self.assertEqual(runtime.tools.memory_root, memory_root.resolve())
        self.assertEqual(session_paths.history, state_root / ".runtime" / "sessions" / "retrieve" / "agent-1.json")
        self.assertEqual(delivery_path, state_root / ".runtime" / "recent_submitted" / "retrieve" / "agent-1.json")
        executor_class.assert_called_once_with(
            memory_root,
            "retrieve",
            AgentCliConfig(provider="codex"),
            state_root=state_root,
            fresh_provider_session=False,
        )


class SemanticUpgradeRuntimeAbsorptionTests(unittest.TestCase):
    def test_dreamer_success_marks_injected_semantic_upgrades_absorbed(self):
        calls = []

        class FakeDreamerExecutor:
            def __init__(
                self,
                memory_root,
                role,
                config,
                semantic_upgrades=None,
                state_root=None,
                fresh_provider_session=False,
            ):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message, self.semantic_upgrades.ids))
                return "dreamed"

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            memory_root.mkdir()
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=memory_root,
                state_root=root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FakeDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    result = runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state = json.loads((root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8"))

        self.assertEqual(result, "dreamed")
        self.assertEqual(
            calls,
            [
                (
                    "dreamer-1",
                    "run",
                    [
                        "user-context-agent-behavior-split",
                        "open-context-questions",
                        "uncertain-memory-marker",
                        "schema-level-memory-skills",
                        "future-facing-behavior-memory",
                        "shared-view-headings",
                    ],
                )
            ],
        )
        self.assertIn("user-context-agent-behavior-split", state["absorbed"])
        self.assertIn("open-context-questions", state["absorbed"])
        self.assertIn("uncertain-memory-marker", state["absorbed"])
        self.assertIn("schema-level-memory-skills", state["absorbed"])
        self.assertIn("future-facing-behavior-memory", state["absorbed"])
        self.assertIn("shared-view-headings", state["absorbed"])

    def test_dreamer_success_marks_semantic_upgrades_absorbed_under_state_root(self):
        class FakeDreamerExecutor:
            def __init__(
                self,
                memory_root,
                role,
                config,
                semantic_upgrades=None,
                state_root=None,
                fresh_provider_session=False,
            ):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                return "dreamed"

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            state_root = root / "state"
            memory_root.mkdir()
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=memory_root,
                state_root=state_root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FakeDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    result = runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state = json.loads((state_root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8"))

        self.assertEqual(result, "dreamed")
        self.assertIn("user-context-agent-behavior-split", state["absorbed"])
        self.assertIn("open-context-questions", state["absorbed"])
        self.assertIn("uncertain-memory-marker", state["absorbed"])
        self.assertIn("schema-level-memory-skills", state["absorbed"])
        self.assertFalse((memory_root / ".runtime" / "semantic-upgrades.json").exists())

    def test_dreamer_failure_leaves_semantic_upgrades_pending(self):
        class FailingDreamerExecutor:
            def __init__(
                self,
                memory_root,
                role,
                config,
                semantic_upgrades=None,
                state_root=None,
                fresh_provider_session=False,
            ):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                raise RuntimeError("dreamer failed")

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            memory_root.mkdir()
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=memory_root,
                state_root=root,
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
