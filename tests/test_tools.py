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

    def test_git_add_accepts_skill_artifacts(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        (self.root / "skill_artifacts" / "loose.md").parent.mkdir(exist_ok=True)
        (self.root / "skill_artifacts" / "loose.md").write_text("not slug scoped\n", encoding="utf-8")

        result = self.tools.git_add(["skill_artifacts/skill-creator/references/authoring.md"])

        self.assertEqual(result, "staged: skill_artifacts/skill-creator/references/authoring.md")
        self.assertIn("A  skill_artifacts/skill-creator/references/authoring.md", self.tools.git_status())
        with self.assertRaises(ValueError):
            self.tools.git_add(["skill_artifacts/loose.md"])

    def test_git_add_rejects_skill_artifact_directory_before_git_mutation(self):
        self._git("init")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot stage directory path"):
            self.tools.git_add(["skill_artifacts/skill-creator/references"])

        self.assertEqual(self.tools.git_status(), status_before)

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

    def test_git_commit_accepts_optional_body(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        result = self.tools.git_commit(
            "memory: review codex transcript s1",
            body="Distilled skill signal: skill authoring guidance belongs in memory-backed skills.",
        )
        log = self._git("log", "-1", "--format=%B")

        self.assertIn("committed", result)
        self.assertIn("memory: review codex transcript s1", log)
        self.assertIn("Distilled skill signal", log)

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

    def test_git_discard_reverts_allowed_tracked_changes(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "skill_artifacts/skill-creator/references/authoring.md")
        self._git("commit", "-m", "initial memory")
        memory.write_text("# Broken\n", encoding="utf-8")
        artifact.write_text("# Broken\n", encoding="utf-8")

        result = self.tools.git_discard([
            "MEMORY.md",
            "skill_artifacts/skill-creator/references/authoring.md",
        ])

        self.assertEqual(result, "discarded: MEMORY.md, skill_artifacts/skill-creator/references/authoring.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# Skill authoring\n")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_reverts_staged_and_unstaged_changes(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "skill_artifacts/skill-creator/references/authoring.md")
        self._git("commit", "-m", "initial memory")

        memory.write_text("# Staged broken\n", encoding="utf-8")
        artifact.write_text("# Staged broken\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "skill_artifacts/skill-creator/references/authoring.md")
        memory.write_text("# Unstaged broken\n", encoding="utf-8")
        artifact.write_text("# Unstaged broken\n", encoding="utf-8")

        result = self.tools.git_discard([
            "MEMORY.md",
            "skill_artifacts/skill-creator/references/authoring.md",
        ])

        self.assertEqual(result, "discarded: MEMORY.md, skill_artifacts/skill-creator/references/authoring.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# Skill authoring\n")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_skill_artifact(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# New staged artifact\n", encoding="utf-8")
        self._git("add", "skill_artifacts/skill-creator/references/authoring.md")

        result = self.tools.git_discard(["skill_artifacts/skill-creator/references/authoring.md"])

        self.assertEqual(result, "discarded: skill_artifacts/skill-creator/references/authoring.md")
        self.assertFalse(artifact.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_removes_staged_added_skill_artifact_with_unstaged_edits(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# New staged artifact\n", encoding="utf-8")
        self._git("add", "skill_artifacts/skill-creator/references/authoring.md")
        artifact.write_text("# Edited staged artifact\n", encoding="utf-8")
        self.assertEqual(
            self.tools.git_status(),
            "AM skill_artifacts/skill-creator/references/authoring.md",
        )

        result = self.tools.git_discard(["skill_artifacts/skill-creator/references/authoring.md"])

        self.assertEqual(result, "discarded: skill_artifacts/skill-creator/references/authoring.md")
        self.assertFalse(artifact.exists())
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_rejects_unstaged_untracked_skill_artifact(self):
        self._git("init")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Untracked artifact\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard untracked path"):
            self.tools.git_discard(["skill_artifacts/skill-creator/references/authoring.md"])

        self.assertTrue(artifact.exists())
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# Untracked artifact\n")
        self.assertEqual(self.tools.git_status(), status_before)

    def test_git_discard_rejects_staged_deletion_with_recreated_skill_artifact(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        artifact_path = "skill_artifacts/skill-creator/references/authoring.md"
        artifact = self.root / artifact_path
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        self._git("add", artifact_path)
        self._git("commit", "-m", "initial artifact")
        self._git("rm", artifact_path)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("# Replacement artifact\n", encoding="utf-8")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard staged deletion with replacement"):
            self.tools.git_discard([artifact_path])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# Replacement artifact\n")

    def test_git_discard_rejects_directory_path_before_git_mutation(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "initial memory")
        (self.root / "skill_artifacts" / "skill-creator" / "references").mkdir(parents=True)
        memory.write_text("# Staged broken\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard directory path"):
            self.tools.git_discard([
                "MEMORY.md",
                "skill_artifacts/skill-creator/references",
            ])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Staged broken\n")

    def test_git_discard_rejects_deleted_head_directory_before_git_mutation(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        self._git("add", "skill_artifacts/skill-creator/references/authoring.md")
        self._git("commit", "-m", "initial artifact")
        shutil.rmtree(artifact.parent)
        status_before = self.tools.git_status()

        with self.assertRaisesRegex(ValueError, "cannot discard directory path"):
            self.tools.git_discard(["skill_artifacts/skill-creator/references"])

        self.assertEqual(self.tools.git_status(), status_before)
        self.assertFalse(artifact.exists())

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
        return process.stdout.strip()
