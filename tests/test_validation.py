from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.guidance import GUIDANCE_INBOX_PATH
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

    def test_legacy_reference_and_nonregular_corrections_are_rejected(self) -> None:
        (self.root / "PURSUIT_RULES.md").write_text("test", encoding="utf-8")
        (self.root / "corrections.md").mkdir()

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.document_errors), 2)

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
        self.assertEqual(result.guidance_errors, ())
        self.assertIsNotNone(result.memory_report)

    def test_guidance_is_validated_even_when_memory_is_valid(self) -> None:
        (self.root / GUIDANCE_INBOX_PATH).write_text(
            "# Pending Agent Guidance\n\n## GI-20260815-a1b2c3d4\n\nMissing provenance.\n",
            encoding="utf-8",
        )

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertTrue(result.guidance_errors)
        self.assertEqual(result.errors, result.guidance_errors)
        self.assertIsNotNone(result.memory_report)

    def test_guidance_regular_file_check_is_included_with_root_errors(self) -> None:
        (self.root / "MEMORY.md").unlink()
        (self.root / GUIDANCE_INBOX_PATH).mkdir()

        result = validate_memory_root(self.root)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.document_errors), 1)
        self.assertEqual(len(result.guidance_errors), 1)
        self.assertEqual(len(result.errors), 2)

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


if __name__ == "__main__":
    unittest.main()
