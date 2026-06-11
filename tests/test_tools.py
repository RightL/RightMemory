import subprocess
import shutil
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

    def test_read_tools_exclude_runtime_shared_view_content(self):
        runtime_import = self.root / ".runtime" / "shared_views" / "imports" / "alice-auth-api"
        runtime_import.mkdir(parents=True)
        (runtime_import / "MEMORY.md").write_text("external shared context\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        with self.assertRaises(ValueError):
            tools.read(".runtime/shared_views/imports/alice-auth-api/MEMORY.md")
        with self.assertRaises(ValueError):
            tools.read_command("rg external .runtime/shared_views/imports/alice-auth-api/MEMORY.md")

        self.assertNotIn(".runtime/shared_views/imports/alice-auth-api/MEMORY.md", tools.glob("**/*"))
        self.assertEqual(tools.read_command("rg external"), "no matches")

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

    def test_claude_shaped_read_grep_and_glob_tools(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `alpha` stores model config → [rel:beta]\n"
            "- `beta` stores runtime notes → [rel:alpha]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_extra.md").write_text("- `gamma` unrelated\n", encoding="utf-8")

        self.assertEqual(set(self.tools.glob("MEMORY*.md")), {"MEMORY.md", "MEMORY_extra.md"})
        self.assertEqual(
            self.tools.read("MEMORY.md", offset=3, limit=2),
            "3: - `alpha` stores model config → [rel:beta]\n4: - `beta` stores runtime notes → [rel:alpha]",
        )

        result = self.tools.grep(r"runtime\s+notes", context_lines=1)

        self.assertIn("MEMORY.md:4 match", result)
        self.assertIn("3: - `alpha` stores model config", result)

    def test_read_command_cat_and_sed_satisfy_edit_read_gate(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        self.assertEqual(self.tools.read_command("sed -n '2,3p' MEMORY.md"), "beta\ngamma\n")
        result = self.tools.edit_file("MEMORY.md", "beta\ngamma\n", "BETA\ngamma\n")

        self.assertEqual(result, "edited MEMORY.md: replaced 1 occurrence")
        self.assertEqual(memory.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")

        self.assertEqual(self.tools.read_command("cat MEMORY.md"), "alpha\nBETA\ngamma\n")

    def test_read_command_rejects_shell_operators(self):
        (self.root / "MEMORY.md").write_text("alpha\n", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            self.tools.read_command("cat MEMORY.md > copy.md")

        self.assertIn("does not support shell operators", str(caught.exception))

    def test_read_command_restricts_git_diff_forms(self):
        self._git("init")
        (self.root / "MEMORY.md").write_text("alpha\n", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            self.tools.read_command("git diff --output diff.txt")

        self.assertIn("supported git diff form", str(caught.exception))

    @unittest.skipIf(shutil.which("rg") is None, "rg is not installed")
    def test_read_command_runs_ripgrep_read_only(self):
        (self.root / "MEMORY.md").write_text("alpha\nbeta\n", encoding="utf-8")

        self.assertEqual(self.tools.read_command("rg beta MEMORY.md"), "MEMORY.md:beta")

    @unittest.skipIf(shutil.which("rg") is None, "rg is not installed")
    def test_read_command_expands_ripgrep_path_globs_without_shell(self):
        (self.root / "MEMORY.md").write_text("alpha\nbeta\n", encoding="utf-8")
        (self.root / "MEMORY_extra.md").write_text("beta detail\n", encoding="utf-8")
        (self.root / "NOTES.md").write_text("beta notes\n", encoding="utf-8")

        self.assertEqual(
            set(self.tools.read_command("rg beta MEMORY*.md").splitlines()),
            {"MEMORY.md:beta", "MEMORY_extra.md:beta detail"},
        )

    @unittest.skipIf(shutil.which("rg") is None, "rg is not installed")
    def test_read_command_preserves_ripgrep_iglob_filter_values(self):
        (self.root / "MEMORY.md").write_text("alpha\n", encoding="utf-8")
        (self.root / "MEMORY_extra.md").write_text("beta\n", encoding="utf-8")
        (self.root / "NOTES.md").write_text("gamma\n", encoding="utf-8")

        self.assertEqual(
            set(self.tools.read_command("rg --files --iglob MEMORY*.md").splitlines()),
            {"MEMORY.md", "MEMORY_extra.md"},
        )

    @unittest.skipIf(shutil.which("rg") is None, "rg is not installed")
    def test_read_command_unmatched_ripgrep_path_glob_returns_no_matches(self):
        (self.root / "MEMORY.md").write_text("beta\n", encoding="utf-8")

        self.assertEqual(self.tools.read_command("rg beta MEMORY_*.md"), "no matches")

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

    def test_edit_file_changes_requested_region(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        self.tools.read_file("MEMORY.md")

        result = self.tools.edit_file("MEMORY.md", "alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\n")

        self.assertEqual(result, "edited MEMORY.md: replaced 1 occurrence")
        self.assertEqual(memory.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")

    def test_edit_file_requires_recent_read(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\n", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            self.tools.edit_file("MEMORY.md", "beta", "BETA")

        self.assertIn("read MEMORY.md", str(caught.exception))

    def test_edit_file_rejects_ambiguous_old_string(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")
        self.tools.read_file("MEMORY.md")

        with self.assertRaises(ValueError) as caught:
            self.tools.edit_file("MEMORY.md", "alpha\nbeta\n", "ALPHA\nBETA\n")

        self.assertIn("old_string matched 2 times", str(caught.exception))
        self.assertIn("line(s) 1, 3", str(caught.exception))

    def test_edit_file_replace_all_changes_multiple_matches(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\nalpha\nbeta\n", encoding="utf-8")
        self.tools.read_file("MEMORY.md")

        result = self.tools.edit_file("MEMORY.md", "beta", "BETA", replace_all=True)

        self.assertEqual(result, "edited MEMORY.md: replaced 2 occurrences")
        self.assertEqual(memory.read_text(encoding="utf-8"), "alpha\nBETA\nalpha\nBETA\n")

    def test_edit_file_reports_closest_match(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        self.tools.read_file("MEMORY.md")

        with self.assertRaises(ValueError) as caught:
            self.tools.edit_file("MEMORY.md", "alpha\nBETTA\ngamma\n", "alpha\nBETA\ngamma\n")

        self.assertIn("old_string not found in MEMORY.md", str(caught.exception))
        self.assertIn("closest inspected text", str(caught.exception))
        self.assertIn("line 1", str(caught.exception))

    def test_edit_file_normalizes_line_endings(self):
        memory = self.root / "MEMORY.md"
        memory.write_text("alpha\r\nbeta\r\n", encoding="utf-8")
        self.tools.read_file("MEMORY.md")

        result = self.tools.edit_file("MEMORY.md", "alpha\nbeta\n", "alpha\nBETA\n")

        self.assertEqual(result, "edited MEMORY.md: replaced 1 occurrence after normalizing line endings")
        self.assertEqual(memory.read_bytes(), b"alpha\r\nBETA\r\n")

    def test_create_delete_and_rename_file(self):
        create_result = self.tools.create_file("MEMORY_extra.md", "# Extra\n\n- `extra` note\n")

        self.assertEqual(create_result, "created MEMORY_extra.md")
        self.assertEqual((self.root / "MEMORY_extra.md").read_text(encoding="utf-8"), "# Extra\n\n- `extra` note\n")

        rename_result = self.tools.rename_file("MEMORY_extra.md", "MEMORY_renamed.md")

        self.assertEqual(rename_result, "renamed MEMORY_extra.md to MEMORY_renamed.md")
        self.assertFalse((self.root / "MEMORY_extra.md").exists())
        self.assertTrue((self.root / "MEMORY_renamed.md").exists())

        delete_result = self.tools.delete_file("MEMORY_renamed.md")

        self.assertEqual(delete_result, "deleted MEMORY_renamed.md")
        self.assertFalse((self.root / "MEMORY_renamed.md").exists())

    def test_git_add_accepts_active_memory_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        (self.root / "MEMORY_detail.md").write_text("# Detail\n", encoding="utf-8")
        (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")

        result = self.tools.git_add(["MEMORY.md", "MEMORY_detail.md"])

        self.assertEqual(result, "staged: MEMORY.md, MEMORY_detail.md")
        status = self.tools.git_status()
        self.assertIn("A  MEMORY.md", status)
        self.assertIn("A  MEMORY_detail.md", status)
        self.assertIn("?? rightmemory.toml", status)

        with self.assertRaises(ValueError):
            self.tools.git_add(["rightmemory.toml"])

    def test_insight_tools_commit_only_insight_logs(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        (self.root / "insight_logs").mkdir()
        (self.root / "insight_logs" / "2026-05-30-143012.md").write_text("# Insight\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="insight")

        result = tools.git_add(["insight_logs/2026-05-30-143012.md"])

        self.assertEqual(result, "staged: insight_logs/2026-05-30-143012.md")
        with self.assertRaisesRegex(ValueError, r"insight_logs/\*\.md"):
            tools.git_add(["MEMORY.md"])

    def test_dreamer_tools_reject_dream_logs(self):
        self._git("init")
        (self.root / "dream_logs").mkdir()
        (self.root / "dream_logs" / "2026-05-30.md").write_text("# Dream\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="dreamer")

        with self.assertRaisesRegex(ValueError, r"MEMORY.md or MEMORY_\*\.md"):
            tools.git_add(["dream_logs/2026-05-30.md"])

    def test_insight_create_file_rejects_active_memory(self):
        tools = MemoryTools(self.root, role="insight")

        with self.assertRaisesRegex(ValueError, r"can only write insight_logs/\*\.md"):
            tools.create_file("MEMORY_new.md", "# New\n")

    def test_sync_reconciler_can_repair_active_memory_and_insight_logs(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        insight = self.root / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="sync-reconciler")

        result = tools.git_add(["MEMORY.md", "insight_logs/2026-05-30-143012.md"])

        self.assertEqual(result, "staged: MEMORY.md, insight_logs/2026-05-30-143012.md")
        with self.assertRaisesRegex(
            ValueError,
            r"MEMORY.md, MEMORY_\*\.md, shared_views\.toml, shared_views/<id> source files, or insight_logs/\*\.md",
        ):
            tools.git_add(["rightmemory.toml"])

    def test_sync_reconciler_can_repair_shared_view_registry(self):
        self._git("init")
        registry = self.root / "shared_views.toml"
        registry.write_text('[connections.alice-auth-api]\nref = "rightmemory://view/old"\n', encoding="utf-8")
        tools = MemoryTools(self.root, role="sync-reconciler")
        tools.read_file("shared_views.toml")
        edit_result = tools.edit_file(
            "shared_views.toml",
            'ref = "rightmemory://view/old"',
            'ref = "rightmemory://view/new"',
        )
        add_result = tools.git_add(["shared_views.toml"])
        self.assertEqual(edit_result, "edited shared_views.toml: replaced 1 occurrence")
        self.assertEqual(add_result, "staged: shared_views.toml")

    def test_sync_reconciler_can_repair_shared_view_definition_source(self):
        self._git("init")
        view_dir = self.root / "shared_views" / "alice-auth-api"
        view_dir.mkdir(parents=True)
        (view_dir / "view.md").write_text("# Old Title\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="sync-reconciler")

        tools.read_file("shared_views/alice-auth-api/view.md")
        edit_result = tools.edit_file("shared_views/alice-auth-api/view.md", "# Old Title", "# Alice Auth API")
        add_result = tools.git_add(["shared_views/alice-auth-api/view.md"])

        self.assertEqual(edit_result, "edited shared_views/alice-auth-api/view.md: replaced 1 occurrence")
        self.assertEqual(add_result, "staged: shared_views/alice-auth-api/view.md")

    def test_insight_read_tools_are_limited_to_active_memory_and_insight_logs(self):
        (self.root / "MEMORY.md").write_text("# Domain\n\nmemory beta\n", encoding="utf-8")
        (self.root / "MEMORY_detail.md").write_text("# Detail\n", encoding="utf-8")
        insight = self.root / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n\nreflection beta\n", encoding="utf-8")
        dream = self.root / "dream_logs" / "2026-05-30.md"
        dream.parent.mkdir()
        dream.write_text("# Dream\n\nsecret beta\n", encoding="utf-8")
        runtime = self.root / ".runtime" / "state.json"
        runtime.parent.mkdir()
        runtime.write_text('{"secret":"beta"}\n', encoding="utf-8")
        (self.root / "rightmemory.toml").write_text("secret = 'beta'\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="insight")

        self.assertEqual(
            set(tools.glob("**/*")),
            {"MEMORY.md", "MEMORY_detail.md", "insight_logs/2026-05-30-143012.md"},
        )
        self.assertIn("MEMORY.md:3: memory beta", tools.grep("beta", glob="**/*.md"))
        self.assertIn("insight_logs/2026-05-30-143012.md:3: reflection beta", tools.grep("reflection", glob="**/*.md"))
        self.assertEqual(tools.grep("secret", glob="**/*.md"), "no matches")
        self.assertIn("reflection beta", tools.read("insight_logs/2026-05-30-143012.md"))
        with self.assertRaisesRegex(ValueError, r"can only read MEMORY.md, MEMORY_\*\.md, or insight_logs/\*\.md"):
            tools.read("rightmemory.toml")
        with self.assertRaisesRegex(ValueError, r"can only read MEMORY.md, MEMORY_\*\.md, or insight_logs/\*\.md"):
            tools.outline_file("dream_logs/2026-05-30.md")
        with self.assertRaisesRegex(ValueError, r"can only read MEMORY.md, MEMORY_\*\.md, or insight_logs/\*\.md"):
            tools.read_command("cat rightmemory.toml")

    @unittest.skipIf(shutil.which("rg") is None, "rg is not installed")
    def test_insight_read_command_rg_uses_limited_read_scope(self):
        (self.root / "MEMORY.md").write_text("# Domain\n\nmemory beta\n", encoding="utf-8")
        insight = self.root / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n\nreflection beta\n", encoding="utf-8")
        dream = self.root / "dream_logs" / "2026-05-30.md"
        dream.parent.mkdir()
        dream.write_text("# Dream\n\nsecret beta\n", encoding="utf-8")
        runtime = self.root / ".runtime" / "state.json"
        runtime.parent.mkdir()
        runtime.write_text('{"secret":"beta"}\n', encoding="utf-8")
        (self.root / "rightmemory.toml").write_text("secret = 'beta'\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="insight")

        files = set(tools.read_command("rg --files").splitlines())
        self.assertEqual(files, {"MEMORY.md", "insight_logs/2026-05-30-143012.md"})
        explicit_files = set(tools.read_command("rg --files .").splitlines())
        self.assertEqual(explicit_files, {"MEMORY.md", "insight_logs/2026-05-30-143012.md"})
        result = tools.read_command("rg beta")
        self.assertIn("MEMORY.md:memory beta", result)
        self.assertIn("insight_logs/2026-05-30-143012.md:reflection beta", result)
        self.assertNotIn("rightmemory.toml", result)
        self.assertNotIn(".runtime", result)
        self.assertNotIn("dream_logs", result)
        explicit_result = tools.read_command("rg beta .")
        self.assertIn("MEMORY.md:memory beta", explicit_result)
        self.assertIn("insight_logs/2026-05-30-143012.md:reflection beta", explicit_result)
        self.assertNotIn("rightmemory.toml", explicit_result)
        self.assertNotIn(".runtime", explicit_result)
        self.assertNotIn("dream_logs", explicit_result)
        with self.assertRaisesRegex(ValueError, r"can only read MEMORY.md, MEMORY_\*\.md, or insight_logs/\*\.md"):
            tools.read_command("rg beta dream_logs/2026-05-30.md")

    def test_git_add_accepts_memory_skill_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text("# Two-Side Review\n", encoding="utf-8")

        result = self.tools.git_add(["MEMORY.md", "MEMORY_SKILL_two-side-review.md"])

        self.assertEqual(result, "staged: MEMORY.md, MEMORY_SKILL_two-side-review.md")
        status = self.tools.git_status()
        self.assertIn("A  MEMORY.md", status)
        self.assertIn("A  MEMORY_SKILL_two-side-review.md", status)

    def test_git_add_rejects_directory_path_before_git_mutation(self):
        self._git("init")
        memory_dir = self.root / "MEMORY_tree.md"
        memory_dir.mkdir()
        (memory_dir / "child").write_text("# Child\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot stage directory path"):
            self.tools.git_add(["MEMORY_tree.md"])

        self.assertEqual(self.tools.git_status(), status_before)

    def test_git_add_rejects_deleted_head_directory_before_git_mutation(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory_dir = self.root / "MEMORY_tree.md"
        memory_dir.mkdir()
        (memory_dir / "child").write_text("# Child\n", encoding="utf-8")
        self._git("add", "MEMORY_tree.md/child")
        self._git("commit", "-m", "initial tree")
        shutil.rmtree(memory_dir)
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot stage directory path"):
            self.tools.git_add(["MEMORY_tree.md"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertFalse(memory_dir.exists())

    def test_git_add_rejects_index_only_directory_before_git_mutation(self):
        self._git("init")
        memory_dir = self.root / "MEMORY_tree.md"
        memory_dir.mkdir()
        (memory_dir / "child").write_text("# Child\n", encoding="utf-8")
        self._git("add", "MEMORY_tree.md/child")
        shutil.rmtree(memory_dir)
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot stage directory path"):
            self.tools.git_add(["MEMORY_tree.md"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertFalse(memory_dir.exists())

    def test_git_commit_rejects_non_memory_staged_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")
        self._git("add", "rightmemory.toml")

        with self.assertRaises(ValueError):
            self.tools.git_commit("memory: should fail")

    def test_git_commit_rejects_rename_from_disallowed_source(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")
        self._git("add", "rightmemory.toml")
        self._git("commit", "-m", "initial config")
        self._git("mv", "rightmemory.toml", "MEMORY_renamed.md")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "rightmemory\\.toml"):
            self.tools.git_commit("memory: rename config")

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "initial config")

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

    def test_git_commit_accepts_optional_body(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        result = self.tools.git_commit(
            "memory: review codex transcript s1",
            body="Durable memory updated after transcript review.",
        )
        log = self._git("log", "-1", "--format=%B")

        self.assertIn("committed", result)
        self.assertIn("memory: review codex transcript s1", log)
        self.assertIn("Durable memory updated", log)

    def test_git_commit_allows_empty_prune_checkpoint(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])
        self.tools.git_commit("memory: add domain")

        result = self.tools.git_commit(
            "prune: checkpoint",
            body="Boundary: HEAD\n\nRemoved:\n(none)",
            allow_empty=True,
        )

        self.assertIn("committed", result)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_commit_rejects_non_prune_empty_commit(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")

        with self.assertRaisesRegex(ValueError, "empty commits are limited"):
            self.tools.git_commit("memory: empty", allow_empty=True)

        with self.assertRaisesRegex(ValueError, "empty commits are limited"):
            self.tools.git_commit("prune: expired active memory", allow_empty=True)

    def test_git_log_and_show_file_read_prune_history(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n\n- `old` old value\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])
        self.tools.git_commit("memory: add old")
        old_head = self._git("rev-parse", "HEAD")
        self._git("commit", "--allow-empty", "-m", "memory: mentions prune", "-m", "prune: not a checkpoint")
        self.tools.git_commit(
            "prune: checkpoint",
            body="Removed:\n- MEMORY.md#old | old value\n",
            allow_empty=True,
        )

        log = self.tools.git_log()
        shown = self.tools.git_show_file(old_head, "MEMORY.md")

        self.assertIn("subject prune: checkpoint", log)
        self.assertIn("MEMORY.md#old", log)
        self.assertNotIn("memory: mentions prune", log)
        self.assertIn("1: # Domain", shown)
        self.assertIn("3: - `old` old value", shown)

    def test_git_show_file_reports_file_missing_at_revision(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])
        self.tools.git_commit("memory: add root")
        old_head = self._git("rev-parse", "HEAD")
        (self.root / "MEMORY_new.md").write_text("# New Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY_new.md"])
        self.tools.git_commit("memory: add new detail")

        shown = self.tools.git_show_file(old_head, "MEMORY_new.md")

        self.assertEqual(shown, f"[file not present at revision: {old_head}:MEMORY_new.md]")

    def test_git_show_file_rejects_unsafe_revision_and_path(self):
        self._git("init")

        with self.assertRaisesRegex(ValueError, "revision"):
            self.tools.git_show_file("--help", "MEMORY.md")
        with self.assertRaisesRegex(ValueError, "history paths"):
            self.tools.git_show_file("HEAD", "../MEMORY.md")

    def test_git_commit_rejects_nul_in_subject(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        with self.assertRaisesRegex(ValueError, "commit subject must not contain NUL bytes"):
            self.tools.git_commit("memory: add\x00domain")

    def test_git_commit_rejects_nul_in_body(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        with self.assertRaisesRegex(ValueError, "commit body must not contain NUL bytes"):
            self.tools.git_commit("memory: add domain", body="body\x00text")

    def test_git_discard_reverts_allowed_tracked_memory_changes(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        detail = self.root / "MEMORY_detail.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        detail.write_text("# Detail\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "MEMORY_detail.md")
        self._git("commit", "-m", "initial memory")
        memory.write_text("# Broken\n", encoding="utf-8")
        detail.write_text("# Broken\n", encoding="utf-8")

        result = self.tools.git_discard(["MEMORY.md", "MEMORY_detail.md"])

        self.assertEqual(result, "discarded: MEMORY.md, MEMORY_detail.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
        self.assertEqual(detail.read_text(encoding="utf-8"), "# Detail\n")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_reverts_staged_and_unstaged_changes(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")

        memory.write_text("# Staged broken\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        memory.write_text("# Unstaged broken\n", encoding="utf-8")

        result = self.tools.git_discard(["MEMORY.md"])

        self.assertEqual(result, "discarded: MEMORY.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_file(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        detail = self.root / "MEMORY_detail.md"
        detail.write_text("# New staged detail\n", encoding="utf-8")
        self._git("add", "MEMORY_detail.md")

        result = self.tools.git_discard(["MEMORY_detail.md"])

        self.assertEqual(result, "discarded: MEMORY_detail.md")
        self.assertFalse(detail.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_file_with_unstaged_edits(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        detail = self.root / "MEMORY_detail.md"
        detail.write_text("# New staged detail\n", encoding="utf-8")
        self._git("add", "MEMORY_detail.md")
        detail.write_text("# Edited staged detail\n", encoding="utf-8")
        self.assertEqual(self.tools.git_status(), "AM MEMORY_detail.md")

        result = self.tools.git_discard(["MEMORY_detail.md"])

        self.assertEqual(result, "discarded: MEMORY_detail.md")
        self.assertFalse(detail.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_file_without_head(self):
        self._git("init")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")

        result = self.tools.git_discard(["MEMORY.md"])

        self.assertEqual(result, "discarded: MEMORY.md")
        self.assertFalse(memory.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_file_with_unstaged_edits_without_head(self):
        self._git("init")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        memory.write_text("# Edited domain\n", encoding="utf-8")
        self.assertEqual(self.tools.git_status(), "AM MEMORY.md")

        result = self.tools.git_discard(["MEMORY.md"])

        self.assertEqual(result, "discarded: MEMORY.md")
        self.assertFalse(memory.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_rejects_unstaged_untracked_memory_file(self):
        self._git("init")
        detail = self.root / "MEMORY_detail.md"
        detail.write_text("# Untracked detail\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard untracked path"):
            self.tools.git_discard(["MEMORY_detail.md"])

        self.assertTrue(detail.exists())
        self.assertEqual(detail.read_text(encoding="utf-8"), "# Untracked detail\n")
        self.assertEqual(self.tools.git_status(), status_before)

    def test_git_discard_rejects_staged_deletion_with_recreated_memory_file(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        detail = self.root / "MEMORY_detail.md"
        detail.write_text("# Detail\n", encoding="utf-8")
        self._git("add", "MEMORY_detail.md")
        self._git("commit", "-m", "initial detail")
        self._git("rm", "MEMORY_detail.md")
        detail.write_text("# Replacement detail\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard staged deletion with replacement"):
            self.tools.git_discard(["MEMORY_detail.md"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertEqual(detail.read_text(encoding="utf-8"), "# Replacement detail\n")

    def test_git_discard_rejects_directory_path_before_git_mutation(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        memory_dir = self.root / "MEMORY_tree.md"
        memory_dir.mkdir()
        (memory_dir / "child").write_text("# Child\n", encoding="utf-8")
        memory.write_text("# Staged broken\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard directory path"):
            self.tools.git_discard(["MEMORY.md", "MEMORY_tree.md"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Staged broken\n")

    def test_git_discard_rejects_deleted_head_directory_before_git_mutation(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory_dir = self.root / "MEMORY_tree.md"
        memory_dir.mkdir()
        (memory_dir / "child").write_text("# Child\n", encoding="utf-8")
        self._git("add", "MEMORY_tree.md/child")
        self._git("commit", "-m", "initial tree")
        shutil.rmtree(memory_dir)
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard directory path"):
            self.tools.git_discard(["MEMORY_tree.md"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertFalse(memory_dir.exists())

    def test_git_discard_rejects_non_memory_paths(self):
        self._git("init")
        (self.root / "rightmemory.toml").write_text("[review]\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.tools.git_discard(["rightmemory.toml"])

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

    def test_validate_memory_accepts_skill_heading_marker(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Two-Side Review {S#two-side-review} → [rel:domain]\n\n"
            "A reusable instruction asset for opposing review passes.\n\n"
            "- `review-signal` Use the skill when the user asks for two-side review. → [rel:two-side-review]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text(
            "# Two-Side Review\n\nRun support and risk passes, then summarize.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_accepts_shared_view_heading_marker(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Alice Auth API {M#alice-auth-api} → [rel:project]\n\n"
            "Alice owns auth API collaboration context.\n\n"
            "- `frontend-login` Frontend login work uses Alice's shared view. → [rel:alice-auth-api]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed: 3 ids", result)

    def test_validate_memory_requires_skill_backing_file(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Two-Side Review {S#two-side-review} → [rel:domain]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("missing skill file `MEMORY_SKILL_two-side-review.md`", result)

    def test_validate_memory_treats_skill_file_body_as_freeform_markdown(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Two-Side Review {S#two-side-review} → [rel:domain]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text(
            "# Two-Side Review\n\n"
            "#### When to use\n\n"
            "- `trigger` this is a prompt label, not a memory graph node.\n"
            "- `missing` this should not create a duplicate id or dangling edge → [rel:no-such-id]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_allows_four_hash_skill_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "### Agent Behavior\n\n"
            "#### Two-Side Review {S#two-side-review}\n\n"
            "A compact skill description can live under the pointer.\n\n"
            "---\n\n"
            "### Other Topic\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text(
            "# Two-Side Review\n\nRun support and risk passes, then summarize.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_rejects_four_hash_shared_view_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "### Integrations\n\n"
            "#### Alice Auth API {M#alice-auth-api}\n\n"
            "This should be a normal #/##/### shared-view heading, not a pointer.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("#### pointer must use `{F#slug}` or `{S#slug}`", result)

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

    def test_validate_memory_catches_malformed_edge_entries(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` first → [rel:two, bad-edge, cfg:]\n"
            "- `two` second → []\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("malformed edge `bad-edge`", result)
        self.assertIn("malformed edge `cfg:`", result)

    def test_validate_memory_catches_heading_depth_and_pointer_shape(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Area\n\n"
            "#### Wrong Parent {F#wrong-parent}\n\n"
            "### Topic\n\n"
            "#### Plain Pointer {#plain-pointer}\n\n"
            "##### Too Deep {F#too-deep}\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("#### pointer must be under a ### heading", result)
        self.assertIn("#### pointer must use `{F#slug}`", result)
        self.assertIn("heading deeper than ####", result)

    def test_validate_memory_allows_body_under_four_hash_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "### Topic\n\n"
            "#### Detail Pointer {F#detail}\n\n"
            "This paragraph summarizes the detail file.\n\n"
            "---\n\n"
            "### Next Topic\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_catches_nodes_and_child_headings_under_four_hash_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "### Topic\n\n"
            "#### Detail Pointer {F#detail}\n\n"
            "Paragraph text is allowed.\n\n"
            "- `detail-node` child nodes stay in the detail file. → []\n\n"
            "##### Child Heading\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("#### pointer cannot contain child node", result)
        self.assertIn("#### pointer cannot contain child heading", result)

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
        return process.stdout.strip()
