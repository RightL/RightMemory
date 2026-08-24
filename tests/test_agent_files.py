from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rightmemory.agent_files import (
    collect_catalog,
    find_content,
    load_catalog,
    main,
    register_paths,
)


class AgentFilesTests(unittest.TestCase):
    def test_collect_stores_normalized_duplicate_content_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "AGENTS.md").write_bytes(b"\xef\xbb\xbf# Rules\r\n\r\nBe clear.\r\n")
            (second / "CLAUDE.md").write_text("# Rules\n\nBe clear.\n", encoding="utf-8")

            register_paths(state, [first, second])
            result = collect_catalog(state)
            catalog = load_catalog(state)
            stored = json.loads((state / "catalog.json").read_text(encoding="utf-8"))

        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.content_count, 1)
        self.assertEqual(len(catalog.contents), 1)
        self.assertEqual(catalog.contents[0].content, "# Rules\n\nBe clear.\n")
        self.assertEqual(len(catalog.contents[0].sources), 2)
        self.assertEqual(len(stored["contents"]), 1)

    def test_collect_recurses_and_skips_ignored_directories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            nested = workspace / "src" / "feature"
            nested.mkdir(parents=True)
            (nested / "AGENTS.md").write_text("nested\n", encoding="utf-8")
            (workspace / "README.md").write_text("not an instruction\n", encoding="utf-8")
            ignored = workspace / ".git"
            ignored.mkdir()
            (ignored / "AGENTS.md").write_text("ignored\n", encoding="utf-8")

            register_paths(state, [workspace])
            result = collect_catalog(state)
            catalog = load_catalog(state)

        self.assertEqual(result.file_count, 1)
        self.assertEqual(len(catalog.contents), 1)
        self.assertEqual(Path(catalog.contents[0].sources[0]).parent.name, "feature")

    def test_overlapping_registrations_do_not_duplicate_a_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            nested = workspace / "nested"
            nested.mkdir(parents=True)
            instruction = nested / "AGENTS.md"
            instruction.write_text("one source\n", encoding="utf-8")

            register_paths(state, [workspace, instruction])
            result = collect_catalog(state)
            catalog = load_catalog(state)

        self.assertEqual(result.file_count, 1)
        self.assertEqual(catalog.contents[0].sources, (str(instruction.resolve()),))

    def test_registration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()

            first = register_paths(state, [workspace])
            second = register_paths(state, [workspace])
            catalog = load_catalog(state)

        self.assertEqual(first.added, (str(workspace.resolve()),))
        self.assertEqual(second.existing, (str(workspace.resolve()),))
        self.assertEqual(catalog.targets, (str(workspace.resolve()),))

    def test_collect_rebuilds_current_inventory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            instruction = workspace / "AGENTS.md"
            instruction.write_text("present\n", encoding="utf-8")
            register_paths(state, [workspace])
            collect_catalog(state)
            instruction.unlink()

            result = collect_catalog(state)
            catalog = load_catalog(state)

        self.assertEqual(result.file_count, 0)
        self.assertEqual(catalog.contents, ())

    def test_missing_registered_path_is_reported_and_retained(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            register_paths(state, [workspace])
            workspace.rmdir()

            result = collect_catalog(state)
            catalog = load_catalog(state)

        self.assertEqual(len(result.warnings), 1)
        self.assertIn("registered path does not exist", result.warnings[0])
        self.assertEqual(catalog.targets, (str(workspace.resolve()),))

    def test_show_accepts_hash_prefix(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("# One\n", encoding="utf-8")
            register_paths(state, [workspace])
            collect_catalog(state)
            content = load_catalog(state).contents[0]
            stdout = io.StringIO()

            with patch("sys.stdout", stdout):
                result = main(["show", content.short_id], state_root=state)

        self.assertEqual(result, 0)
        self.assertIn(content.digest, stdout.getvalue())
        self.assertIn("# One", stdout.getvalue())

    def test_register_rejects_unrelated_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            unrelated = root / "README.md"
            unrelated.write_text("text\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "registered path must be a directory or file named",
            ):
                register_paths(state, [unrelated])

    def test_find_content_rejects_unknown_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state = Path(tempdir) / "state"
            with self.assertRaisesRegex(ValueError, "unknown agent-file content id"):
                find_content(load_catalog(state), "missing")


class AgentFilesEntrypointTests(unittest.TestCase):
    def test_entrypoint_routes_without_resolving_a_memory_root(self):
        from rightmemory.entrypoint import main as entrypoint_main

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / "state"
            workspace = root / "workspace"
            workspace.mkdir()
            stdout = io.StringIO()
            with (
                patch("rightmemory.agent_files.default_agent_files_root", return_value=state),
                patch(
                    "rightmemory.entrypoint.resolve_memory_root",
                    side_effect=AssertionError("agent-files must not resolve Memory state"),
                ),
                patch("sys.stdout", stdout),
            ):
                result = entrypoint_main(["agent-files", "register", str(workspace)])

        self.assertEqual(result, 0)
        self.assertIn("registered:", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
