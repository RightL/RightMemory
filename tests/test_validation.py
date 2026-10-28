from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.tools import MemoryTools
from rightmemory.validation import validate_memory_root


class CompleteMemoryValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

    def test_valid_root_is_model_free_readonly_and_json_serializable(self) -> None:
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}

        result = validate_memory_root(self.root)

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, ())
        self.assertIsNotNone(result.memory_report)
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["errors"], [])
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_required_document_failure_prevents_graph_validation(self) -> None:
        (self.root / "PURSUITS.md").unlink()
        with patch.object(MemoryTools, "validate_memory", side_effect=AssertionError("invalid root read")):
            result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.document_errors), 1)
        self.assertIsNone(result.memory_report)

    def test_nonregular_corrections_are_rejected(self) -> None:
        (self.root / "corrections.md").mkdir()

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.document_errors), 1)

    def test_symlinked_required_root_is_rejected(self) -> None:
        memory = self.root / "MEMORY.md"
        target = self.root / "other.md"
        memory.rename(target)
        try:
            memory.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation failed: {exc}")

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.document_errors), 1)

    def test_graph_failure_retains_original_report_and_structured_errors(self) -> None:
        (self.root / "MEMORY.md").write_text("# Missing {F#missing}\n", encoding="utf-8")

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertTrue(result.errors)
        self.assertEqual(result.document_errors, ())
        self.assertIsNotNone(result.memory_report)


    def test_correction_collection_limits_remain_enforced(self) -> None:
        (self.root / "MEMORY_agent-corrections-writing.md").write_text(
            "# Agent Corrections\n\n### 1. Entry\n\n" + "x" * 201 + "\n",
            encoding="utf-8",
        )

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertTrue(result.errors)
        self.assertIsNotNone(result.memory_report)

    def test_unreadable_encoding_is_a_structured_failure(self) -> None:
        (self.root / "MEMORY.md").write_bytes(b"\xff")

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)

    def test_invalid_current_learning_state_is_readonly_failure(self) -> None:
        candidate = self.root / "learning" / "candidates" / "invalid.json"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"not a candidate")
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertTrue(any("learning" in error for error in result.errors))
        self.assertEqual({path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
