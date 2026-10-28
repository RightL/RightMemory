import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "install.sh tests exercise the POSIX installer")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        # Fixture commits must not leave detached maintenance changing .git
        # while preservation tests take complete byte-for-byte snapshots.
        config_count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_COUNT": str(config_count + 1),
                    f"GIT_CONFIG_KEY_{config_count}": "maintenance.auto",
                    f"GIT_CONFIG_VALUE_{config_count}": "false",
                },
            )
        )

    def _env_with_fake_uv(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"python\" ] && [ \"$2\" = \"find\" ]; then\n"
            "  echo \"$RIGHTMEMORY_TEST_PYTHON\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"venv\" ]; then\n"
            "  target=''\n"
            "  for arg in \"$@\"; do target=\"$arg\"; done\n"
            "  mkdir -p \"$target/bin\"\n"
            "  cat > \"$target/bin/python\" <<'PYEOF'\n"
            "#!/usr/bin/env sh\n"
            "exit 0\n"
            "PYEOF\n"
            "  chmod 755 \"$target/bin/python\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "RIGHTMEMORY_TEST_PYTHON": sys.executable,
        }

    def _env_with_fake_git_no_uv(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/bin",
        }

    def _env_with_failing_uv_python(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_git = fake_bin / "git"
        fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o755)
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"python\" ] && [ \"$2\" = \"find\" ]; then\n"
            "  echo 'no Python 3.11 available' >&2\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        return {
            **os.environ,
            "HOME": str(root / "home"),
            "XDG_DATA_HOME": str(root / "data"),
            "PATH": f"{fake_bin}:/bin",
        }

    def test_initial_install_creates_state_and_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)

            self.assertTrue((memory_root / "MEMORY.md").is_file())
            self.assertTrue((memory_root / "PURSUITS.md").is_file())
            self.assertTrue((memory_root / ".runtime" / "install.stamp").is_file())
            self.assertTrue((memory_root / "insight_logs").is_dir())
            self.assertFalse((memory_root / "PURSUIT_RULES.md").exists())
            self.assertFalse((memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").exists())

    def test_initial_install_configures_git_author_and_baseline_commit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)
            git_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            git_email = self._git(memory_root, "config", "--local", "--get", "user.email")
            head = self._git(memory_root, "log", "--oneline", "-1")
            status = self._git(memory_root, "status", "--short")

        self.assertEqual(git_name, "RightMemory")
        self.assertEqual(git_email, "rightmemory@localhost")
        self.assertIn("memory: initial baseline", head)
        self.assertEqual(status, "")
        self.assertIn("git user.name = RightMemory", result.stdout)
        self.assertIn("initial memory baseline commit", result.stdout)

    def test_empty_git_repository_without_head_is_bootstrapped_as_new(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)

            result = self._install(memory_root, skills_target)

            self.assertTrue((memory_root / "MEMORY.md").is_file())
            self.assertTrue((memory_root / "PURSUITS.md").is_file())
            self.assertFalse((memory_root / "PURSUIT_RULES.md").exists())
            self.assertFalse((memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").exists())
            self.assertIn("memory: initial baseline", self._git(memory_root, "log", "--oneline", "-1"))
            self.assertEqual(self._git(memory_root, "status", "--short"), "")
            self.assertIn("from MEMORY.example.md", result.stdout)

    def test_complete_non_git_root_is_preserved_and_baseline_committed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            expected = {
                "MEMORY.md": "# Existing Memory\r\n\n保留现有内容。\n".encode("utf-8"),
                "PURSUITS.md": b"# Existing Pursuits\r\n",
            }
            for name, content in expected.items():
                (memory_root / name).write_bytes(content)

            self._install(memory_root, skills_target)

            self.assertEqual({name: (memory_root / name).read_bytes() for name in expected}, expected)
            self.assertIn("memory: initial baseline", self._git(memory_root, "log", "--oneline", "-1"))
            self.assertEqual(self._git(memory_root, "status", "--short"), "")

    def test_initial_install_baselines_existing_shared_view_registry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (memory_root / "shared_views.toml").write_text(
                '[connections.alice-auth-api]\ntype = "file"\nref = "rightmemory://mf/current"\n',
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            status = self._git(memory_root, "status", "--short")
            committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

        self.assertEqual(status, "")
        self.assertIn("shared_views.toml", committed_files)

    def test_initial_install_baselines_existing_share_registry(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            (memory_root / "shares.toml").write_text(
                '[shares.auth-api]\n'
                'version = 1\n'
                'role = "provider"\n'
                'title = "Auth API"\n'
                'state = "draft"\n'
                'parts = ["file"]\n'
                '[shares.auth-api.file]\n'
                'view_id = "auth-api-files"\n'
                'intent = "Expose auth context."\n'
                'approved = false\n',
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            status = self._git(memory_root, "status", "--short")
            committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

        self.assertEqual(status, "")
        self.assertIn("shares.toml", committed_files)


    def test_install_preserves_existing_memory_repo_author(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.name", "Existing User"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.email", "existing@example.com"], cwd=memory_root, check=True)

            result = self._install(memory_root, skills_target)
            git_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            git_email = self._git(memory_root, "config", "--local", "--get", "user.email")

        self.assertEqual(git_name, "Existing User")
        self.assertEqual(git_email, "existing@example.com")
        self.assertIn("git author configured", result.stdout)

    def test_install_baselines_existing_learning_without_rewriting_ignore(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            gitignore = b"*\n!MEMORY.md\n!PURSUITS.md\n"
            (memory_root / ".gitignore").write_bytes(gitignore)
            retained = {
                "learning/guidance/example.md": b"scoped guidance\n",
                "learning/skills/example/SKILL.md": b"reusable method\n",
                "learning/skills/example/assets/template.txt": b"supporting resource\n",
            }
            for name, contents in retained.items():
                path = memory_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)

            self._install(memory_root, root / "skills")

            self.assertEqual((memory_root / ".gitignore").read_bytes(), gitignore)
            self.assertEqual({name: (memory_root / name).read_bytes() for name in retained}, retained)
            self.assertEqual(set(self._git(memory_root, "ls-files", "--", "learning").splitlines()), set(retained))

    def test_install_refuses_incomplete_committed_root_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            runtime_home = root / "data" / "rightmemory"
            runtime_command = root / "home" / ".local" / "bin" / "rightmemory"
            memory_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)
            subprocess.run(["git", "config", "--local", "user.name", "Existing User"], cwd=memory_root, check=True)
            subprocess.run(
                ["git", "config", "--local", "user.email", "existing@example.com"], cwd=memory_root, check=True
            )
            (memory_root / "MEMORY.md").write_text("# Existing Memory\n", encoding="utf-8")
            subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "memory: user baseline"], cwd=memory_root, check=True)
            runtime_dir = memory_root / ".runtime"
            runtime_dir.mkdir()
            (runtime_dir / "install.stamp").write_text("old stamp\n", encoding="utf-8")
            skills_target.mkdir()
            (skills_target / "keep.txt").write_text("skills stay\n", encoding="utf-8")
            runtime_home.mkdir(parents=True)
            (runtime_home / "keep.txt").write_text("runtime stays\n", encoding="utf-8")
            runtime_command.parent.mkdir(parents=True)
            runtime_command.write_text("old wrapper\n", encoding="utf-8")

            before_head = self._git(memory_root, "rev-parse", "HEAD")
            before_status = self._git(memory_root, "status", "--short")
            before_name = self._git(memory_root, "config", "--local", "--get", "user.name")
            before_email = self._git(memory_root, "config", "--local", "--get", "user.email")
            before_memory = self._snapshot(memory_root)
            before_skills = self._snapshot(skills_target)
            before_runtime = self._snapshot(runtime_home)
            before_wrapper = runtime_command.read_bytes()

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing RightMemory root is incomplete", result.stderr)
            self.assertIn(
                "missing required files: PURSUITS.md",
                result.stderr,
            )
            self.assertIn("installation made no changes", result.stderr)
            self.assertIn("inspect and repair this root using the current schema", result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), before_head)
            self.assertEqual(self._git(memory_root, "status", "--short"), before_status)
            self.assertEqual(self._git(memory_root, "config", "--local", "--get", "user.name"), before_name)
            self.assertEqual(self._git(memory_root, "config", "--local", "--get", "user.email"), before_email)
            self.assertEqual(self._snapshot(memory_root), before_memory)
            self.assertEqual(self._snapshot(skills_target), before_skills)
            self.assertEqual(self._snapshot(runtime_home), before_runtime)
            self.assertEqual(runtime_command.read_bytes(), before_wrapper)

    def test_install_refuses_directory_and_symlink_required_paths_as_non_regular(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cases = ("directory", "symlink")
            for case in cases:
                with self.subTest(case=case):
                    memory_root = root / case / "memory"
                    skills_target = root / case / "skills"
                    memory_root.mkdir(parents=True)
                    (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
                    pursuits = memory_root / "PURSUITS.md"
                    if case == "directory":
                        pursuits.mkdir()
                    else:
                        target = memory_root / "pursuits-target.md"
                        target.write_text("# Pursuits\n", encoding="utf-8")
                        pursuits.symlink_to(target.name)
                    before = self._snapshot(memory_root)

                    result = self._run_install(memory_root, skills_target, check=False)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("non-regular required files: PURSUITS.md", result.stderr)
                    self.assertIn("installation made no changes", result.stderr)
                    self.assertEqual(self._snapshot(memory_root), before)
                    self.assertFalse(skills_target.exists())
                    self.assertFalse((memory_root / ".runtime" / "install.stamp").exists())

    def test_shared_view_gitignore_alone_marks_existing_root_incomplete(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            view_dir = memory_root / "shared_views" / "auth-api"
            view_dir.mkdir(parents=True)
            (view_dir / ".gitignore").write_text("dist/\n", encoding="utf-8")
            before = self._snapshot(memory_root)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing RightMemory root is incomplete", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(memory_root), before)
            self.assertFalse(skills_target.exists())

    def test_install_refuses_bare_git_target_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory.git"
            skills_target = root / "skills"
            subprocess.run(["git", "init", "--bare", "-q", str(memory_root)], check=True)
            before = self._snapshot(memory_root)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("bare Git repository", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(memory_root), before)
            self.assertFalse(skills_target.exists())

    def test_install_refuses_target_nested_in_another_git_worktree(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            outer = root / "outer"
            memory_root = outer / "memory"
            skills_target = root / "skills"
            outer.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
            subprocess.run(["git", "config", "user.name", "Outer User"], cwd=outer, check=True)
            subprocess.run(["git", "config", "user.email", "outer@example.com"], cwd=outer, check=True)
            (outer / "README.md").write_text("outer\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=outer, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "outer baseline"], cwd=outer, check=True)
            memory_root.mkdir()
            self._env_with_fake_uv(outer)
            before = self._snapshot(outer)

            result = self._run_install(memory_root, skills_target, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inside another Git working tree", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._snapshot(outer), before)
            self.assertFalse(skills_target.exists())

    def test_complete_committed_root_preserves_all_semantic_state_bytes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)
            semantic_files = {
                "MEMORY.md": b"# Custom Memory\r\n",
                "MEMORY_detail.md": b"detail\n",
                "PURSUITS.md": b"# Custom Pursuits\n",
                "PURSUIT_work.md": b"work\r\n",
                "corrections.md": b"# Corrections\n",
                "shared_views.toml": b"[connections]\n",
                "shares.toml": b"[shares]\n",
                "shared_views/example/view.md": b"view\n",
                "shared_views/example/recipe.toml": b"version = 1\n",
                "insight_logs/one.md": b"insight\n",
            }
            for relative, content in semantic_files.items():
                path = memory_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            subprocess.run(["git", "add", "--", *semantic_files], cwd=memory_root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "memory: custom semantic state"], cwd=memory_root, check=True)
            expected_head = self._git(memory_root, "rev-parse", "HEAD")

            result = self._install(memory_root, skills_target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), expected_head)
            self.assertEqual(
                {relative: (memory_root / relative).read_bytes() for relative in semantic_files},
                semantic_files,
            )

    def test_reinstall_preserves_complete_root_gitignore_bytes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)
            custom_gitignore = (
                b"*\r\n"
                b"!MEMORY.md\r\n"
                b"!PURSUITS.md\r\n"
                b"!local-control-plane-entry\r\n"
            )
            (memory_root / ".gitignore").write_bytes(custom_gitignore)
            subprocess.run(["git", "add", ".gitignore"], cwd=memory_root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "memory: explicit gitignore update"],
                cwd=memory_root,
                check=True,
            )
            expected_head = self._git(memory_root, "rev-parse", "HEAD")

            result = self._install(memory_root, skills_target)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), expected_head)
            self.assertEqual((memory_root / ".gitignore").read_bytes(), custom_gitignore)
            self.assertEqual(self._git(memory_root, "status", "--short"), "")

    def test_new_root_installs_current_memory_gitignore_allowlist(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / ".gitignore").write_text(
                "*\n!MEMORY.md\n!dream_logs/\n!dream_logs/*.md\n",
                encoding="utf-8",
            )

            result = self._install(memory_root, skills_target)
            gitignore = (memory_root / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            gitignore,
            "*\n"
            "!.gitignore\n"
            "!MEMORY.md\n"
            "!MEMORY_*.md\n"
            "!PURSUITS.md\n"
            "!PURSUIT_*.md\n"
            "!corrections.md\n"
            "!shared_views.toml\n"
            "!shares.toml\n"
            "!shared_views/\n"
            "!shared_views/*/\n"
            "!shared_views/*/view.md\n"
            "!shared_views/*/retriever.md\n"
            "!shared_views/*/recipe.toml\n"
            "!shared_views/*/question.toml\n"
            "!shared_views/*/.gitignore\n"
            "!insight_logs/\n"
            "!insight_logs/*.md\n"
            "!learning/\n"
            "!learning/**\n",
        )

    def test_cli_agent_installs_automatic_orchestration_and_independent_skills(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)
            retriever_exists = (skills_target / "memory-retriever" / "SKILL.md").is_file()
            orchestrator_exists = (skills_target / "rightmemory-orchestrator" / "SKILL.md").is_file()
            auto_orchestrator_exists = (
                skills_target / "rightmemory-auto-orchestrator" / "SKILL.md"
            ).is_file()
            maintainer_exists = (skills_target / "maintain-rightmemory" / "SKILL.md").is_file()
            guidance_maintainer_exists = (
                skills_target / "maintain-agent-guidance" / "SKILL.md"
            ).is_file()
            map_maintainer_exists = (skills_target / "maintain-pursuit-map" / "SKILL.md").is_file()
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            wrapper = (root / "home" / ".local" / "bin" / "rightmemory").read_text(encoding="utf-8")
            installed_skill_directories = sorted(path.name for path in skills_target.iterdir() if path.is_dir())

        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("Write [agent_cli], [retrieve.agent_cli], and a default writer [update.agent_cli] config", result.stdout)
        self.assertNotIn("Write [retrieve.model] and a default writer [update.model] config", result.stdout)
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertFalse(retriever_exists)
        self.assertFalse(orchestrator_exists)
        self.assertTrue(auto_orchestrator_exists)
        self.assertTrue(maintainer_exists)
        self.assertTrue(guidance_maintainer_exists)
        self.assertTrue(map_maintainer_exists)
        self.assertIn("rightmemory-auto-orchestrator", result.stdout)
        self.assertIn("export PYTHONUTF8=1", wrapper)
        self.assertIn('export RIGHTMEMORY_ROOT="', wrapper)
        self.assertIn('exec "', wrapper)
        self.assertIn(' -m rightmemory.entrypoint "$@"', wrapper)
        self.assertFalse((skills_target / "rightmemory-edit-correction-rules.md").exists())
        self.assertEqual(
            installed_skill_directories,
            [
                "maintain-agent-guidance",
                "maintain-pursuit-map",
                "maintain-rightmemory",
                "maintain-work-skills",
                "rightmemory-auto-orchestrator",
                "rightmemory-manager",
            ],
        )

    def test_rerun_preserves_managed_examples_and_user_state_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)

            memory_path = memory_root / "MEMORY.md"
            memory = memory_path.read_text(encoding="utf-8")
            memory_path.write_text(
                "# Real Memory {#real-memory}\n\n- `real-node` keep me. → []\n\n"
                + memory,
                encoding="utf-8",
            )
            pursuits_path = memory_root / "PURSUITS.md"
            pursuits = pursuits_path.read_text(encoding="utf-8")
            pursuits_path.write_text(
                "# User Pursuits\n\n## Continue Release {#continue-release}\n\nKeep this live intent.\n\n"
                + pursuits,
                encoding="utf-8",
            )
            expected_memory = memory_path.read_bytes()
            expected_pursuits = pursuits_path.read_bytes()

            self._install(memory_root, skills_target)
            actual_memory = memory_path.read_bytes()
            actual_pursuits = pursuits_path.read_bytes()

        self.assertEqual(actual_memory, expected_memory)
        self.assertEqual(actual_pursuits, expected_pursuits)



    def test_default_install_uses_standalone_and_default_skill_targets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            env = self._env_with_fake_uv(root)

            result = subprocess.run(
                ["bash", "install.sh"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            home = root / "home"
            self.assertTrue((home / ".rightmemory" / "MEMORY.md").exists())
            self.assertTrue((home / ".rightmemory" / "PURSUITS.md").exists())
            self.assertFalse((home / ".rightmemory" / "PURSUIT_RULES.md").exists())
            self.assertFalse((home / ".rightmemory" / "AGENT_CORRECTION_MEMORY_RULES.md").exists())
            self.assertTrue((home / ".rightmemory" / ".runtime" / "install.stamp").exists())
            for target in (home / ".codex" / "skills", home / ".claude" / "skills"):
                self.assertFalse((target / "rightmemory-edit-correction-rules.md").exists())
                self.assertTrue((target / "rightmemory-auto-orchestrator" / "SKILL.md").exists())
                self.assertTrue((target / "maintain-rightmemory" / "SKILL.md").is_file())
                self.assertTrue((target / "maintain-pursuit-map" / "SKILL.md").is_file())
                self.assertTrue((target / "rightmemory-manager" / "SKILL.md").is_file())
                self.assertFalse((target / "memory-retriever").exists())
                self.assertFalse((target / "rightmemory-orchestrator").exists())
                self.assertTrue((target / "maintain-agent-guidance" / "SKILL.md").is_file())
                self.assertTrue((target / "maintain-work-skills" / "SKILL.md").is_file())
                self.assertFalse((target / "memory-orchestrator").exists())
            self.assertFalse((home / ".codex" / "skills" / "memory-curator").exists())
            self.assertIn("MODE         = standalone", result.stdout)
            self.assertIn("Write [retrieve.model] and a default writer [update.model] config", result.stdout)
            self.assertIn("rightmemory-auto-orchestrator", result.stdout)
            self.assertIn("rightmemory is installed", result.stdout)

    def test_install_reports_missing_git_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            fake_git = root / "bin" / "git"
            fake_git.write_text("#!/usr/bin/env sh\nexit 1\n", encoding="utf-8")
            fake_git.chmod(0o755)
            env["PATH"] = f"{root / 'bin'}:/bin"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: git", result.stderr)
        self.assertIn("macOS:", result.stderr)
        self.assertIn("Linux / WSL", result.stderr)
        self.assertIn("Official git install guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_reports_missing_uv_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_fake_git_no_uv(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: uv", result.stderr)
        self.assertIn("macOS:", result.stderr)
        self.assertIn("Linux / WSL", result.stderr)
        self.assertIn("Official uv install guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_reports_uv_python_failure_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_failing_uv_python(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            memory_exists = memory_root.exists()
            skills_exists = skills_target.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not find or provision Python >=3.11 with uv", result.stderr)
        self.assertIn("uv Python guide", result.stderr)
        self.assertFalse(memory_exists)
        self.assertFalse(skills_exists)

    def test_install_preserves_unowned_runtime_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            unowned = {
                ".runtime/user-notes/one.txt": b"a saved note\n",
                ".runtime/user-notes/two.json": b'{"important": true}',
            }
            for name, contents in unowned.items():
                path = memory_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
            self._install(memory_root, skills_target)

            self.assertEqual({name: (memory_root / name).read_bytes() for name in unowned}, unowned)

    def test_install_warns_when_stale_rightmemory_precedes_installed_wrapper(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            stale_rightmemory = root / "bin" / "rightmemory"
            stale_rightmemory.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
            stale_rightmemory.chmod(0o755)

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

            installed_wrapper = root / "home" / ".local" / "bin" / "rightmemory"

        self.assertIn(f"rightmemory is installed at {installed_wrapper}", result.stdout)
        self.assertIn(f"PATH currently resolves rightmemory to:\n\n              {stale_rightmemory}", result.stdout)
        self.assertIn("stale code or use the wrong RIGHTMEMORY_ROOT", result.stdout)

    def test_invalid_mode_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "unsupported", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                env=self._env_with_fake_uv(root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertFalse(memory_root.exists())
            self.assertFalse(skills_target.exists())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice: 'unsupported'", result.stderr)


    def _install(self, memory_root: Path, skills_target: Path) -> subprocess.CompletedProcess[str]:
        return self._run_install(memory_root, skills_target, check=True)

    def _run_install(
        self,
        memory_root: Path,
        skills_target: Path,
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        root = memory_root.parent
        return subprocess.run(
            ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
            cwd=REPO_ROOT,
            env=self._env_with_fake_uv(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def _snapshot(self, root: Path) -> tuple[tuple[str, str, bytes | str], ...] | None:
        if not os.path.lexists(root):
            return None
        entries: list[tuple[str, str, bytes | str]] = []
        for path in sorted((root, *root.rglob("*")), key=lambda item: str(item.relative_to(root))):
            relative = "." if path == root else path.relative_to(root).as_posix()
            if path.is_symlink():
                entries.append((relative, "symlink", os.readlink(path)))
            elif path.is_dir():
                entries.append((relative, "directory", b""))
            else:
                entries.append((relative, "file", path.read_bytes()))
        return tuple(entries)

    def _git(self, memory_root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
