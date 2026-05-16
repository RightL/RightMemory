import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.tools import MemoryTools


class MemoryToolsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.tools = MemoryTools(self.root)

    def test_rejects_paths_outside_memory_root(self):
        outside = self.root.parent / "outside.md"

        with self.assertRaises(ValueError):
            self.tools.read_file(str(outside))

        with self.assertRaises(ValueError):
            self.tools.list_files("../*.md")

    def test_read_file_truncates_large_full_reads(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("\n".join(f"line {index}" for index in range(250)), encoding="utf-8")

        result = self.tools.read_file("MEMORY.md")

        self.assertIn("1: line 0", result)
        self.assertIn("200: line 199", result)
        self.assertIn("[truncated: showing lines 1-200 of 250", result)
        self.assertEqual(
            self.tools.read_file("MEMORY.md", start_line=10, end_line=12),
            "10: line 9\n11: line 10\n12: line 11",
        )

    def test_read_around_returns_context_window(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("\n".join(f"line {index}" for index in range(10)), encoding="utf-8")

        self.assertEqual(
            self.tools.read_around("MEMORY.md", line_number=5, context_lines=2),
            "3: line 2\n4: line 3\n5: line 4\n6: line 5\n7: line 6",
        )

    def test_search_files_returns_matches_with_context(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `alpha` stores model config → [rel:beta]\n"
            "- `beta` stores runtime notes → [rel:alpha]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_extra.md").write_text("- `gamma` unrelated\n", encoding="utf-8")

        result = self.tools.search_files("runtime", context_lines=1)

        self.assertIn("MEMORY.md:4 match", result)
        self.assertIn("3: - `alpha` stores model config", result)
        self.assertIn("4: - `beta` stores runtime notes", result)

    def test_outline_file_returns_heading_map(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Project {#project} → [rel:domain]\n\n"
            "### Topic\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.tools.outline_file("MEMORY.md"),
            "1: # Domain {#domain}\n3:   ## Project {#project} → [rel:domain]\n5:     ### Topic",
        )

    def test_apply_patch_changes_requested_region(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        patch = """*** Begin Patch
*** Update File: MEMORY.md
@@
 alpha
-beta
+BETA
 gamma
*** End Patch"""

        result = self.tools.apply_patch(patch)

        self.assertEqual(result, "applied patch: MEMORY.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")

    def test_apply_patch_adds_and_deletes_files(self):
        add_patch = """*** Begin Patch
*** Add File: MEMORY_extra.md
+# Extra
+
+- `extra` note
*** End Patch"""

        add_result = self.tools.apply_patch(add_patch)

        self.assertEqual(add_result, "applied patch: MEMORY_extra.md")
        self.assertEqual((self.root / "MEMORY_extra.md").read_text(encoding="utf-8"), "# Extra\n\n- `extra` note\n")

        delete_patch = """*** Begin Patch
*** Delete File: MEMORY_extra.md
*** End Patch"""

        delete_result = self.tools.apply_patch(delete_patch)

        self.assertEqual(delete_result, "applied patch: MEMORY_extra.md")
        self.assertFalse((self.root / "MEMORY_extra.md").exists())

    def test_apply_patch_rolls_back_on_later_failure(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\n", encoding="utf-8")
        patch = """*** Begin Patch
*** Update File: MEMORY.md
@@
 alpha
-beta
+BETA
*** Update File: MISSING.md
@@
 missing
+new
*** End Patch"""

        with self.assertRaises(FileNotFoundError):
            self.tools.apply_patch(patch)

        self.assertEqual(memory.read_text(encoding="utf-8"), "alpha\nbeta\n")

    def test_git_add_accepts_memory_files_and_dream_logs(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        (self.root / "MEMORY_detail.md").write_text("# Detail\n", encoding="utf-8")
        dream_logs = self.root / "dream_logs"
        dream_logs.mkdir()
        (dream_logs / "2026-05-08.md").write_text("# Dream\n", encoding="utf-8")
        (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")

        result = self.tools.git_add(["MEMORY.md", "MEMORY_detail.md", "dream_logs/2026-05-08.md"])

        self.assertEqual(result, "staged: MEMORY.md, MEMORY_detail.md, dream_logs/2026-05-08.md")
        status = self.tools.git_status()
        self.assertIn("A  MEMORY.md", status)
        self.assertIn("A  MEMORY_detail.md", status)
        self.assertIn("A  dream_logs/2026-05-08.md", status)
        self.assertIn("?? rightmemory.toml", status)

        with self.assertRaises(ValueError):
            self.tools.git_add(["rightmemory.toml"])

    def test_git_commit_rejects_non_memory_staged_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")
        self._git("add", "rightmemory.toml")

        with self.assertRaises(ValueError):
            self.tools.git_commit("memory: should fail")

    def test_git_commit_commits_staged_memory_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        result = self.tools.git_commit("memory: add domain")

        self.assertIn("committed", result)
        self.assertIn("memory: add domain", result)
        self.assertEqual(self.tools.git_status(), "")

    def test_validate_memory_catches_duplicate_ids_and_dangling_edges(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` first → [rel:missing]\n"
            "- `one` duplicate → []\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("duplicate id `one`", result)
        self.assertIn("dangling edge `rel:missing`", result)

    def test_validate_memory_allows_one_way_edges(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` first → [rel:two]\n"
            "- `two` second → []\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_accepts_file_backed_heading_marker(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Runtime {F#runtime} → [rel:domain]\n"
            "- `runtime-python` Uses Python 3.11. → [cfg:runtime]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_runtime.md").write_text(
            "# Runtime Details\n\n"
            "- `runtime-install` Install dependencies with uv. → [rel:runtime-python]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_catches_self_and_duplicate_edges(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` first → [rel:one, rel:two, rel:two]\n"
            "- `two` second → []\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("self-edge `rel:one`", result)
        self.assertIn("duplicate edge `rel:two`", result)

    def test_validate_memory_catches_pending_section_changes_from_git(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n\n# User Pending Task and Thoughts\nold\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        memory.write_text("# Domain\n\n# User Pending Task and Thoughts\nnew\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("protected pending-task section changed", result)

    def _git(self, *args):
        process = subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr)
