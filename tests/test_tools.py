import inspect
import os
import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.share_models import load_shares
from rightmemory.tools import MemoryTools


class MemoryToolsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.tools = MemoryTools(self.root)

    def _validate_complete_graph(self) -> str:
        pursuits = self.root / "PURSUITS.md"
        if not pursuits.exists():
            pursuits.write_text("# Pursuits\n", encoding="utf-8")
        return self.tools.validate_memory()

    def _symlink_or_skip(self, link: Path, target: Path, *, target_is_directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink is not permitted: {exc}")

    def _write_mf_package(
        self,
        view_id: str,
        memory_document: str,
        resources: dict[str, str] | None = None,
    ) -> Path:
        package = self.root / ".runtime" / "shared_views" / "imports" / view_id
        dist = package / "dist"
        dist.mkdir(parents=True)
        (package / "view.md").write_text(f"# {view_id}\n", encoding="utf-8", newline="")
        (package / "recipe.toml").write_text(
            f'version = 1\nview_id = "{view_id}"\nkind = "file"\n',
            encoding="utf-8",
            newline="",
        )
        (package / "rightmemory-shared-view.toml").write_text(
            f'version = 2\nview_id = "{view_id}"\nkind = "file"\n',
            encoding="utf-8",
            newline="",
        )
        (dist / "manifest.toml").write_text(
            f'version = 2\nview_id = "{view_id}"\ndocument_kind = "rightmemory-memory"\n',
            encoding="utf-8",
            newline="",
        )
        (dist / "MEMORY.md").write_text(memory_document, encoding="utf-8", newline="")
        for name, content in (resources or {}).items():
            (dist / name).write_text(content, encoding="utf-8", newline="")
        return package

    def test_rejects_paths_outside_memory_root(self):
        outside = self.root.parent / "outside.md"

        with self.assertRaises(ValueError):
            self.tools.read_file(str(outside))

        with self.assertRaises(ValueError):
            self.tools.list_files("../*.md")

    def test_retrieve_role_cannot_raw_read_mf_import_files(self):
        self._write_mf_package(
            "auth-api-files",
            "# Tokens {#tokens} → []\n\nTokens expire after one hour.\n",
        )
        tools = MemoryTools(self.root, role="retrieve")

        with self.assertRaisesRegex(ValueError, "read_mf"):
            tools.read(".runtime/shared_views/imports/auth-api-files/dist/MEMORY.md")
        with self.assertRaisesRegex(ValueError, "read_mf"):
            tools.grep("Tokens expire", ".runtime/shared_views/imports/auth-api-files")

    def test_retrieve_read_skill_returns_skill_body_by_id(self):
        (self.root / "MEMORY.md").write_text("# Skills\n\n## Alpha {S#alpha}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_alpha.md").write_text("# Alpha Skill\n\nUse alpha.\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_skill("alpha")

        self.assertEqual(result.splitlines(), ["# Alpha Skill", "", "Use alpha."])
        self.assertNotIn("Source:", result)

    def test_retrieve_typed_reads_take_only_their_id(self):
        self.assertEqual(list(inspect.signature(MemoryTools.read_markdown).parameters), ["self", "markdown_id"])
        self.assertEqual(list(inspect.signature(MemoryTools.read_skill).parameters), ["self", "skill_id"])
        self.assertEqual(
            list(inspect.signature(MemoryTools.read_mf).parameters),
            ["self", "mf_id", "resource_id"],
        )

    def test_retrieve_typed_reads_do_not_silently_truncate(self):
        payload = f"{'x' * 35_000}\nEND\n"
        (self.root / "MEMORY.md").write_text(
            "# Root {#root}\n\n"
            "## Detail {F#detail}\n\n"
            "## Markdown {M#markdown}\n\n"
            "## Alpha {S#alpha}\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY_detail.md").write_text(payload, encoding="utf-8")
        (self.root / "MEMORY_markdown.md").write_text(payload, encoding="utf-8")
        (self.root / "MEMORY_SKILL_alpha.md").write_text(payload, encoding="utf-8")
        self._write_mf_package(
            "auth-api",
            f"# Large {{#large}} → []\n\n{payload}",
        )
        tools = MemoryTools(self.root, role="retrieve")

        self.assertTrue(tools.read_detail("detail").endswith("END"))
        self.assertEqual(tools.read_skill("alpha").splitlines()[-1], "END")
        self.assertTrue(tools.read_markdown("markdown").endswith("2: END"))
        self.assertIn("END", tools.read_mf("auth-api"))

    def test_retrieve_read_skill_failure_lists_available_ids_without_paths(self):
        (self.root / "MEMORY.md").write_text("# Skills\n\n## Beta {S#beta}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_beta.md").write_text("# Beta Skill\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_skill("alpha")

        self.assertIn("Skill not found: alpha", result)
        self.assertIn("Available skills:\n- beta", result)
        self.assertNotIn("MEMORY_SKILL_beta.md", result)

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink is not available")
    def test_retrieve_read_skill_does_not_follow_symlink_outside_root(self):
        outside = self.root.parent / f"{self.root.name}-outside-skill.md"
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text("# Outside\n\nsecret\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text("# Skills\n\n## Alpha {S#alpha}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        self._symlink_or_skip(self.root / "MEMORY_SKILL_alpha.md", outside)
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_skill("alpha")

        self.assertIn("Skill not found: alpha", result)
        self.assertNotIn("secret", result)

    def test_retrieve_read_mf_returns_only_line_numbered_canonical_view_by_id(self):
        self._write_mf_package(
            "auth-api",
            "# Auth API {#auth-api} → []\n\nToken expiry.\n",
        )
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("auth-api")

        self.assertIn("Source: MF#auth-api", result)
        self.assertIn("1: # Auth API {#auth-api}", result)
        self.assertIn("3: Token expiry.", result)
        self.assertNotIn("manifest.toml", result)
        self.assertNotIn("view_id", result)

    def test_retrieve_read_mf_failure_lists_available_ids_without_paths(self):
        self._write_mf_package(
            "billing-api",
            "# Billing API {#billing-api} → []\n",
        )
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("auth-api")

        self.assertIn("MF import not found: auth-api", result)
        self.assertIn("Available MF imports:\n- billing-api", result)
        self.assertNotIn(".runtime", result)

    def test_retrieve_read_mf_reads_only_referenced_typed_resources(self):
        self._write_mf_package(
            "auth-api",
            "# Auth {#auth} → []\n\n"
            "## Detail {F#detail} → []\n\n"
            "## Evidence {M#evidence} → []\n\n"
            "## Checklist {S#checklist} → []\n",
            {
                "MEMORY_detail.md": "# Detail Topic {#detail-topic} → []\n",
                "MEMORY_evidence.md": "# Evidence\n\nRaw line.\n",
                "MEMORY_SKILL_checklist.md": "# Checklist\n\nReview all.\n",
            },
        )
        tools = MemoryTools(self.root, role="retrieve")

        direct = tools.read_mf("auth-api")
        detail = tools.read_mf("auth-api", "F#detail")
        evidence = tools.read_mf("auth-api", "M#evidence")
        skill = tools.read_mf("auth-api", "S#checklist")

        self.assertIn("Available MF resources", direct)
        self.assertIn("F#detail", direct)
        self.assertIn("1: # Detail Topic", detail)
        self.assertIn("3: Raw line.", evidence)
        self.assertEqual(skill, "# Checklist\n\nReview all.\n")

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink is not available")
    def test_retrieve_read_mf_does_not_follow_symlink_outside_root(self):
        import_root = self.root / ".runtime" / "shared_views" / "imports" / "auth-api"
        (import_root / "dist").mkdir(parents=True)
        outside = self.root.parent / f"{self.root.name}-outside-mf.md"
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text("external secret\n", encoding="utf-8")
        self._symlink_or_skip(import_root / "dist" / "MEMORY.md", outside)
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("auth-api")

        self.assertIn("MF import not found: auth-api", result)
        self.assertNotIn("external secret", result)

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink is not available")
    def test_retrieve_read_mf_does_not_list_symlinked_import_root(self):
        outside_imports = self.root.parent / f"{self.root.name}-outside-imports"
        self.addCleanup(shutil.rmtree, outside_imports, ignore_errors=True)
        (outside_imports / "auth-api").mkdir(parents=True)
        shared_views_root = self.root / ".runtime" / "shared_views"
        shared_views_root.mkdir(parents=True)
        self._symlink_or_skip(shared_views_root / "imports", outside_imports, target_is_directory=True)
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read_mf("missing")

        self.assertIn("MF import not found: missing", result)
        self.assertIn("Available MF imports:\n- none", result)
        self.assertNotIn("auth-api", result)

    def test_update_role_cannot_read_mf_import_files(self):
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("External context.\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="update")

        with self.assertRaises(ValueError) as caught:
            tools.read(".runtime/shared_views/imports/auth-api-files/dist/MEMORY.md")

        self.assertIn("only readable through read_mf", str(caught.exception))
        self.assertEqual(tools.list_files(".runtime/shared_views/imports/**/*.md"), [])
        self.assertNotIn(".runtime/shared_views/imports", "\n".join(tools.glob("**/*.md")))
        self.assertEqual(tools.grep("External", glob=".runtime/shared_views/imports/**/*.md"), "no matches")
        with self.assertRaises(ValueError) as grep_error:
            tools.grep("External", ".runtime/shared_views/imports/auth-api-files")
        self.assertIn("only readable through read_mf", str(grep_error.exception))

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
        memory.write_bytes(b"alpha\nbeta\ngamma\n")

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

    def test_ripgrep_uses_native_path_separator_without_rewriting_match_text(self):
        (self.root / "MEMORY.md").write_text("memory\n", encoding="utf-8")
        completed = subprocess.CompletedProcess(
            ["rg"],
            0,
            stdout="MEMORY.md:C:\\memory\\value\n",
            stderr="",
        )

        with patch("rightmemory.tools.subprocess.run", return_value=completed) as run:
            output = self.tools.read_command("rg memory MEMORY.md")

        self.assertEqual(output, "MEMORY.md:C:\\memory\\value")
        self.assertIn("--path-separator=/", run.call_args.args[0])

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
        memory.write_bytes(b"alpha\nbeta\ngamma\n")
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
        memory.write_bytes(b"alpha\r\nbeta\r\n")
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

    def test_only_review_correction_tools_can_write_corrections(self):
        self._git("init")
        corrections = self.root / "corrections.md"
        corrections.write_text("# RightMemory Update Corrections\n", encoding="utf-8")

        normal = MemoryTools(self.root, role="update")
        normal.read("corrections.md")
        with self.assertRaisesRegex(ValueError, "PURSUIT_\\*\\.md"):
            normal.edit_file("corrections.md", "RightMemory", "WrongMemory")
        with self.assertRaisesRegex(ValueError, "PURSUIT_\\*\\.md"):
            normal.git_add(["corrections.md"])

        tools = MemoryTools(self.root, role="update-corrector")
        self.assertEqual(tools.git_add(["corrections.md"]), "staged: corrections.md")

    def test_update_roles_treat_pursuit_rules_as_read_only(self):
        rules = self.root / "PURSUIT_RULES.md"
        rules.write_text("# Pursuit Rules\n", encoding="utf-8")

        for role in ("update", "update-corrector"):
            with self.subTest(role=role):
                tools = MemoryTools(self.root, role=role)
                tools.read("PURSUIT_RULES.md")
                with self.assertRaisesRegex(ValueError, "PURSUIT_\\*\\.md"):
                    tools.edit_file("PURSUIT_RULES.md", "Pursuit", "Changed")

    def test_non_updater_roles_cannot_read_updater_corrections(self):
        (self.root / "corrections.md").write_text("private updater feedback\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            MemoryTools(self.root, role="dreamer").read("corrections.md")
        self.assertIn("private updater feedback", MemoryTools(self.root, role="update").read("corrections.md"))

    def test_narrow_memory_roles_cannot_modify_fixed_agent_correction_collections(self):
        paths = (
            "MEMORY_agent-corrections-writing.md",
            "MEMORY_agent-corrections-design.md",
        )
        for path in paths:
            (self.root / path).write_text("# Curated corrections\n", encoding="utf-8")
            for role in (None, "dreamer", "pruner", "reviewer"):
                with self.subTest(path=path, role=role):
                    tools = MemoryTools(self.root, role=role)
                    tools.read(path)
                    with self.assertRaisesRegex(ValueError, r"MEMORY\.md or MEMORY_\*\.md"):
                        tools.edit_file(path, "Curated", "Changed")
                    with self.assertRaisesRegex(ValueError, r"MEMORY\.md or MEMORY_\*\.md"):
                        tools.delete_file(path)

        updater = MemoryTools(self.root, role="update")
        updater.read(paths[0])
        self.assertIn("edited", updater.edit_file(paths[0], "Curated", "Updater-owned"))

        corrector = MemoryTools(self.root, role="update-corrector")
        with self.assertRaisesRegex(ValueError, r"MEMORY_\*\.md"):
            corrector.edit_file(paths[1], "Curated", "Corrector-owned")

    def test_dreamer_can_modify_ordinary_m_and_s_backing_files(self):
        for path in ("MEMORY_research-notes.md", "MEMORY_SKILL_two-pass-review.md"):
            with self.subTest(path=path):
                (self.root / path).write_text("# Linked resource\n", encoding="utf-8")
                tools = MemoryTools(self.root, role="dreamer")
                tools.read(path)
                self.assertIn("edited", tools.edit_file(path, "Linked", "Maintained"))

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
            r"RightMemory state files, shared-view source files, or insight_logs/\*\.md",
        ):
            tools.git_add(["rightmemory.toml"])

    def test_sync_reconciler_can_repair_shared_view_registry(self):
        self._git("init")
        registry = self.root / "shared_views.toml"
        registry.write_text('[connections.alice-auth-api]\ntype = "file"\nref = "rightmemory://mf/old"\n', encoding="utf-8")
        tools = MemoryTools(self.root, role="sync-reconciler")
        tools.read_file("shared_views.toml")
        edit_result = tools.edit_file(
            "shared_views.toml",
            'ref = "rightmemory://mf/old"',
            'ref = "rightmemory://mf/new"',
        )
        add_result = tools.git_add(["shared_views.toml"])
        self.assertEqual(edit_result, "edited shared_views.toml: replaced 1 occurrence")
        self.assertEqual(add_result, "staged: shared_views.toml")

    def test_sync_reconciler_can_repair_share_registry(self):
        self._git("init")
        registry = self.root / "shares.toml"
        registry.write_text(
            '[shares.auth-api]\nversion = 1\nrole = "provider"\ntitle = "Old Auth API"\nstate = "draft"\nparts = ["file"]\n',
            encoding="utf-8",
        )
        tools = MemoryTools(self.root, role="sync-reconciler")
        tools.read_file("shares.toml")
        edit_result = tools.edit_file(
            "shares.toml",
            'title = "Old Auth API"',
            'title = "Auth API"',
        )
        add_result = tools.git_add(["shares.toml"])
        self.assertEqual(edit_result, "edited shares.toml: replaced 1 occurrence")
        self.assertEqual(add_result, "staged: shares.toml")

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

    def test_shared_view_builder_tool_creates_canonical_file_recipe(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [rel:auth-api]\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_extractive_file_view(
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth context.",
            include_headings=["auth-api"],
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        recipe = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        rendered = (self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("success: wrote extractive file view auth-api-files", result)
        self.assertIn('include_headings = ["auth-api"]', recipe)
        self.assertIn('[publish]', recipe)
        self.assertIn("Tokens expire after one hour.", rendered)

    def test_shared_view_builder_tool_reports_bad_file_selection(self):
        (self.root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_extractive_file_view(
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth context.",
            include_headings=["missing-auth-api"],
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        self.assertIn("failed:", result)
        self.assertIn("include_headings id not found in active memory: missing-auth-api", result)

    def test_shared_view_builder_tool_creates_generative_file_view(self):
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_generative_file_view(
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose sanitized auth context.",
            memory_document=(
                "# Auth Tokens {#auth-tokens} → []\n\n"
                "- `token-expiry` Tokens expire after one hour. → []\n"
            ),
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        recipe = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        rendered = (self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("success: wrote generative file view auth-api-files", result)
        self.assertIn('render = "generative"', recipe)
        self.assertNotIn("include_nodes", recipe)
        self.assertIn("Tokens expire after one hour.", rendered)

    def test_shared_view_builder_tool_creates_canonical_question_view(self):
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_question_view(
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth questions.",
            retriever_instructions="Answer from auth API memory only.",
        )

        question_toml = (self.root / "shared_views" / "auth-api-ask" / "question.toml").read_text(encoding="utf-8")
        retriever = (self.root / "shared_views" / "auth-api-ask" / "retriever.md").read_text(encoding="utf-8")
        self.assertIn("success: wrote canonical question view auth-api-ask", result)
        self.assertIn('kind = "question"', question_toml)
        self.assertIn("access_token_hashes = []", question_toml)
        self.assertIn("Answer from auth API memory only.", retriever)

    def test_shared_view_builder_tool_creates_share_relationship(self):
        tools = MemoryTools(self.root, role="shared-view-builder")
        tools.create_generative_file_view(
            "auth-api-files",
            "Auth API Files",
            "Share stable auth API context.",
            "# Auth API {#auth-api} → []\n\nUse POST /auth/refresh on token expiry.\n",
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        tools.create_question_view(
            "auth-api-ask",
            "Auth API Questions",
            "Let consumers ask auth API questions.",
            "Answer only auth API integration questions.",
        )

        result = tools.create_or_update_share_relationship(
            share_id="auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="both",
            file_view_id="auth-api-files",
            file_intent="Share stable auth API context.",
            question_view_id="auth-api-ask",
            question_intent="Let consumers ask auth API questions.",
            question_base_url="https://provider.example.test",
        )

        shares = load_shares(self.root)
        self.assertIn("success: wrote share relationship auth-api", result)
        self.assertEqual(shares["auth-api"].parts, ("file", "question"))
        self.assertEqual(shares["auth-api"].file.view_id, "auth-api-files")
        self.assertEqual(shares["auth-api"].question.view_id, "auth-api-ask")
        self.assertFalse(shares["auth-api"].file.approved)
        self.assertFalse(shares["auth-api"].question.approved)

    def test_shared_view_builder_tool_rejects_missing_selected_artifact(self):
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_or_update_share_relationship(
            share_id="auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="file_context",
            file_view_id="auth-api-files",
            file_intent="Share stable auth API context.",
        )

        self.assertIn("failed:", result)
        self.assertIn("file view source is invalid", result)

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
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` first → [rel:two]\n"
            "- `two` second → []\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_accepts_file_backed_heading_marker(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Runtime {F#runtime} → [rel:domain]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_runtime.md").write_text(
            "# Runtime Details\n\n"
            "- `runtime-python` Uses Python 3.11. → [cfg:runtime]\n"
            "- `runtime-install` Install dependencies with uv. → [rel:runtime-python]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_accepts_skill_heading_marker(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
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

    def test_validate_memory_accepts_file_and_question_shared_view_heading_markers(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API Files {MF#auth-api-files} → [rel:project]\n\n"
            "Use this mirrored file view before login changes.\n\n"
            "## Auth API Questions {MQ#auth-api-ask} → [rel:auth-api-files]\n\n"
            "Use this provider question view for live auth API clarification.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_accepts_m_heading_and_ignores_freeform_body(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Markdown Notes {M#markdown-notes}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_markdown-notes.md").write_text(
            "# Free-form notes\n\n"
            "- `project` is ordinary Markdown here. → [rel:missing]\n",
            encoding="utf-8",
        )

        result = self._validate_complete_graph()

        self.assertIn("validation passed", result)
        self.assertIn("Free-form notes", self.tools.read_markdown("markdown-notes"))

    def test_validate_memory_rejects_case_insensitive_backing_path_collisions(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Detail {F#Notes}\n\n"
            "## Free-form Notes {M#notes}\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY_Notes.md").write_text("# Detail\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("backing file `MEMORY_notes.md` is claimed by", result)
        self.assertIn("F# heading `Notes`", result)
        self.assertIn("M# heading `notes`", result)

    def test_validate_memory_rejects_children_left_under_file_backed_heading(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Runtime {F#runtime}\n\n"
            "- `left-behind` This node should be in the detail file. → []\n\n"
            "### Left Behind Topic {#left-behind-topic}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_runtime.md").write_text("# Runtime Details\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("F# heading cannot retain child node lines", result)
        self.assertIn("F# heading cannot retain child headings", result)

    def test_validate_memory_requires_skill_backing_file(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Two-Side Review {S#two-side-review} → [rel:domain]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("missing skill file `MEMORY_SKILL_two-side-review.md`", result)

    def test_validate_memory_treats_skill_file_body_as_freeform_markdown(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
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
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
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

    def test_validate_memory_allows_four_hash_shared_view_references(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "### Integrations\n\n"
            "#### Auth API Files {MF#auth-api-files}\n\n"
            "#### Auth API Questions {MQ#auth-api-ask}\n",
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

        self.assertIn("`####` terminal reference must be under a `###` heading", result)
        self.assertIn("`####` terminal reference must use", result)
        self.assertIn("headings deeper than `####` are not allowed", result)

    def test_validate_memory_allows_body_under_four_hash_pointer(self):
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "### Topic\n\n"
            "#### Detail Pointer {F#detail}\n\n"
            "This paragraph summarizes the detail file.\n\n"
            "---\n\n"
            "### Next Topic\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_detail.md").write_text("- `detail-fact` Backed detail. → []\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_uses_one_global_namespace_and_cross_root_edges(self):
        (self.root / "MEMORY.md").write_text(
            "# Durable Project {#project}\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n"
            "## Focus\n\n"
            "- `ship-project` Ship the project.\n\n"
            "## Ship Project {#ship-project} → [dep:project]\n",
            encoding="utf-8",
        )

        result = self._validate_complete_graph()

        self.assertIn("validation passed", result)

        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Duplicate {#project}\n",
            encoding="utf-8",
        )
        result = self._validate_complete_graph()
        self.assertIn("duplicate id `project`", result)

    def test_validate_memory_does_not_parse_pursuit_next_actions_as_graph_nodes(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n"
            "## First {#first}\n\n"
            "**Next:**\n"
            "- `do` Continue the first task.\n"
            "- `ask` Confirm the first choice.\n\n"
            "## Second {#second}\n\n"
            "**Next:**\n"
            "- `do` Continue the second task.\n"
            "- `wait` Wait for an external result.\n",
            encoding="utf-8",
        )

        result = self._validate_complete_graph()

        self.assertIn("validation passed", result)

    def test_validate_memory_rejects_unknown_pursuit_next_action(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n"
            "## Investigate {#investigate}\n\n"
            "**Next:**\n"
            "- `research` Inspect the evidence.\n",
            encoding="utf-8",
        )

        result = self._validate_complete_graph()

        self.assertIn("invalid Pursuit Next action `research`", result)

    def test_validate_memory_reserves_pursuit_rules_from_f_backing(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Invalid {F#rules}\n",
            encoding="utf-8",
        )
        (self.root / "PURSUIT_RULES.md").write_text("# Pursuit Rules\n", encoding="utf-8")

        result = self._validate_complete_graph()

        self.assertIn("reserved for PURSUIT_RULES.md", result)

    def test_validate_memory_requires_both_canonical_roots(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("missing canonical RightMemory root `PURSUITS.md`", result)

    def test_validate_memory_ignores_graph_syntax_inside_fenced_examples(self):
        (self.root / "MEMORY.md").write_text(
            "# Durable {#durable}\n\n"
            "```md\n"
            "# Fake Duplicate {#durable}\n"
            "- `fake` ignored → [rel:missing]\n"
            "##### Fake deep heading\n"
            "```\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")

        result = self._validate_complete_graph()

        self.assertIn("validation passed", result)

    def test_packaged_memory_and_pursuit_examples_form_a_valid_graph(self):
        repo_root = Path(__file__).resolve().parents[1]
        shutil.copyfile(repo_root / "MEMORY.example.md", self.root / "MEMORY.md")
        shutil.copyfile(repo_root / "PURSUITS.example.md", self.root / "PURSUITS.md")

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_rejects_overfull_updater_corrections(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        entries = []
        for index in range(16):
            entries.append(
                f"## Correction {index}\n\n"
                "### Background\n\ncontext\n\n"
                "### Proposed edit\n\nproposal\n\n"
                "### Accepted edit\n\naccepted\n"
            )
        (self.root / "corrections.md").write_text("\n".join(entries), encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("at most 15 are allowed", result)
        self.assertIn(
            "validation passed",
            self.tools.validate_memory(enforce_correction_capacity=False),
        )

    def test_validate_memory_resolves_f_backing_relative_to_document_root(self):
        (self.root / "MEMORY.md").write_text("# Durable {F#durable}\n", encoding="utf-8")
        (self.root / "MEMORY_durable.md").write_text("- `durable-fact` Durable. → []\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n\n## Live {F#live}\n", encoding="utf-8")
        (self.root / "PURSUIT_live.md").write_text("- `live-next` Continue. → [dep:durable-fact]\n", encoding="utf-8")

        result = self._validate_complete_graph()

        self.assertIn("validation passed", result)
        self.assertIn("PURSUIT_live.md", self.tools.read_detail("live"))
        self.assertIn("MEMORY_durable.md", self.tools.read_detail("durable"))

    def test_validate_memory_rejects_memory_only_backing_kinds_in_pursuit(self):
        (self.root / "MEMORY.md").write_text("# Durable {#durable}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n\n## Notes {M#notes}\n", encoding="utf-8")
        (self.root / "MEMORY_notes.md").write_text("free form\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("M# heading `notes` is only valid in Memory", result)

    def test_validate_memory_requires_f_backing_file(self):
        (self.root / "MEMORY.md").write_text("# Missing {F#missing}\n", encoding="utf-8")

        result = self.tools.validate_memory()

        self.assertIn("missing F# backing file `MEMORY_missing.md`", result)

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

        self.assertIn("terminal `####` heading cannot contain node lines", result)
        self.assertIn("headings deeper than `####` are not allowed", result)

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
