import subprocess
import tempfile
import unittest
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_START = "rightmemory:example:start"
EXAMPLE_END = "rightmemory:example:end"


class InstallScriptTests(unittest.TestCase):
    def _env_with_fake_uv(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"venv\" ]; then\n"
            "  mkdir -p \"$2/bin\"\n"
            "  cat > \"$2/bin/python\" <<'PYEOF'\n"
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"rightmemory.semantic_upgrades\" ]; then\n"
            "  command=\"$3\"\n"
            "  memory_root=''\n"
            "  previous=''\n"
            "  for arg in \"$@\"; do\n"
            "    if [ \"$previous\" = \"--memory-root\" ]; then memory_root=\"$arg\"; fi\n"
            "    previous=\"$arg\"\n"
            "  done\n"
            "  mkdir -p \"$memory_root/.runtime\"\n"
            "  state=\"$memory_root/.runtime/semantic-upgrades.json\"\n"
            "  if [ \"$command\" = \"baseline\" ]; then\n"
            "    echo '  [keep]    semantic upgrade baseline recorded for 2 current note(s):'\n"
            "    echo '            user-context-agent-behavior-split'\n"
            "    echo '            open-context-questions'\n"
            "    printf '{\"absorbed\":{\"user-context-agent-behavior-split\":{},\"open-context-questions\":{}}}\\n' > \"$state\"\n"
            "  elif grep -q 'user-context-agent-behavior-split' \"$state\" 2>/dev/null; then\n"
            "    echo '  [keep]    no semantic upgrade notes pending'\n"
            "  else\n"
            "    echo '  [notice]  2 semantic upgrade note(s) pending for the next dreamer cycle:'\n"
            "    echo '            user-context-agent-behavior-split'\n"
            "    echo '            open-context-questions'\n"
            "    printf '{\"absorbed\": {}}\\n' > \"$state\"\n"
            "  fi\n"
            "fi\n"
            "exit 0\n"
            "PYEOF\n"
            "  chmod 755 \"$2/bin/python\"\n"
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
        }

    def test_initial_install_copies_managed_example(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)

            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
            state = (memory_root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8")
            install_stamp_exists = (memory_root / ".runtime" / "install.stamp").exists()

        self.assertIn(EXAMPLE_START, memory)
        self.assertIn(EXAMPLE_END, memory)
        self.assertIn("# Open Context Questions {#open-context-questions}", memory)
        self.assertIn("q-rightmemory-project-context", memory)
        self.assertIn("user-context-agent-behavior-split", state)
        self.assertIn("open-context-questions", state)
        self.assertTrue(install_stamp_exists)

    def test_cli_agent_installs_command_backed_orchestrator_without_role_skills(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)
            orchestrator = (skills_target / "memory-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            curator_exists = (skills_target / "memory-curator").exists()
            dreamer_exists = (skills_target / "memory-dreamer").exists()

        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("Write [agent_cli] and [<role>.agent_cli] provider/model config", result.stdout)
        self.assertNotIn("Write role model config", result.stdout)
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertIn("installed `rightmemory` command", orchestrator)
        self.assertIn("Open context questions", orchestrator)
        self.assertNotIn("standalone mode", orchestrator)
        self.assertNotIn("standalone runtime", orchestrator)
        self.assertFalse(curator_exists)
        self.assertFalse(dreamer_exists)

    def test_rerun_refreshes_marked_example_and_preserves_user_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)

            memory_path = memory_root / "MEMORY.md"
            memory = memory_path.read_text(encoding="utf-8")
            memory_path.write_text(
                "# Real Memory {#real-memory}\n\n- `real-node` keep me. → []\n\n"
                + memory.replace("Example Application", "Stale Example Application"),
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            refreshed = memory_path.read_text(encoding="utf-8")

        self.assertIn("# Real Memory {#real-memory}", refreshed)
        self.assertIn("- `real-node` keep me. → []", refreshed)
        self.assertIn("Example Application", refreshed)
        self.assertNotIn("Stale Example Application", refreshed)
        self.assertEqual(refreshed.count(EXAMPLE_START), 1)
        self.assertEqual(refreshed.count(EXAMPLE_END), 1)

    def test_rerun_migrates_known_old_starter_block(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text(
                "# Starter Knowledge Base {#starter-knowledge-base}\n\n"
                "> Old starter text.\n\n"
                "---\n\n"
                "# Real Memory {#real-memory}\n\n"
                "- `real-node` keep me. → []\n",
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            migrated = (memory_root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn(EXAMPLE_START, migrated)
        self.assertIn("# Sample Project Graph", migrated)
        self.assertNotIn("# Starter Knowledge Base", migrated)
        self.assertIn("# Real Memory {#real-memory}", migrated)
        self.assertIn("- `real-node` keep me. → []", migrated)

    def test_default_install_uses_standalone_and_default_skill_targets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = \"venv\" ]; then\n"
                "  mkdir -p \"$2/bin\"\n"
                "  cat > \"$2/bin/python\" <<'PYEOF'\n"
                "#!/usr/bin/env sh\n"
                "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"rightmemory.semantic_upgrades\" ]; then\n"
                "  command=\"$3\"\n"
                "  memory_root=''\n"
                "  previous=''\n"
                "  for arg in \"$@\"; do\n"
                "    if [ \"$previous\" = \"--memory-root\" ]; then memory_root=\"$arg\"; fi\n"
                "    previous=\"$arg\"\n"
                "  done\n"
                "  mkdir -p \"$memory_root/.runtime\"\n"
                "  state=\"$memory_root/.runtime/semantic-upgrades.json\"\n"
                "  if [ \"$command\" = \"baseline\" ]; then\n"
                "    echo '  [keep]    semantic upgrade baseline recorded for 2 current note(s):'\n"
                "    echo '            user-context-agent-behavior-split'\n"
                "    echo '            open-context-questions'\n"
                "    printf '{\"absorbed\":{\"user-context-agent-behavior-split\":{},\"open-context-questions\":{}}}\\n' > \"$state\"\n"
                "  elif grep -q 'user-context-agent-behavior-split' \"$state\" 2>/dev/null; then\n"
                "    echo '  [keep]    no semantic upgrade notes pending'\n"
                "  else\n"
                "    echo '  [notice]  2 semantic upgrade note(s) pending for the next dreamer cycle:'\n"
                "    echo '            user-context-agent-behavior-split'\n"
                "    echo '            open-context-questions'\n"
                "    printf '{\"absorbed\": {}}\\n' > \"$state\"\n"
                "  fi\n"
                "fi\n"
                "exit 0\n"
                "PYEOF\n"
                "  chmod 755 \"$2/bin/python\"\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(root / "home"),
                "XDG_DATA_HOME": str(root / "data"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            }

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
            self.assertTrue((home / ".rightmemory" / ".runtime" / "install.stamp").exists())
            self.assertTrue((home / ".codex" / "skills" / "memory-orchestrator" / "SKILL.md").exists())
            self.assertTrue((home / ".claude" / "skills" / "memory-orchestrator" / "SKILL.md").exists())
            self.assertFalse((home / ".codex" / "skills" / "memory-curator").exists())
            self.assertFalse((home / ".claude" / "skills" / "memory-dreamer").exists())
            self.assertIn("MODE         = standalone", result.stdout)
            self.assertIn("Write role model config", result.stdout)
            self.assertIn("rightmemory is installed", result.stdout)
            self.assertIn("semantic upgrade baseline recorded", result.stdout)
            self.assertNotIn("pending for the next dreamer cycle", result.stdout)

    def test_install_reports_pending_semantic_upgrade_notes_for_existing_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text(
                "# Existing Memory {#existing-memory}\n\n- `existing-node` keep me. → []\n",
                encoding="utf-8",
            )

            result = self._install(memory_root, skills_target)
            state_exists = (memory_root / ".runtime" / "semantic-upgrades.json").exists()

        self.assertIn("semantic upgrade note(s) pending", result.stdout)
        self.assertIn("user-context-agent-behavior-split", result.stdout)
        self.assertIn("open-context-questions", result.stdout)
        self.assertTrue(state_exists)

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

    def test_subagent_mode_is_rejected_with_cli_agent_guidance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = subprocess.run(
                ["bash", "install.sh", "--mode", "subagent", str(memory_root), str(skills_target)],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported --mode: subagent", result.stderr)
        self.assertIn("--mode cli-agent", result.stderr)

    def test_install_removes_old_rightmemory_role_skills_and_preserves_user_dirs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            old_curator = skills_target / "memory-curator"
            old_dreamer = skills_target / "memory-dreamer"
            old_curator.mkdir(parents=True)
            old_dreamer.mkdir(parents=True)
            old_curator.joinpath("SKILL.md").write_text(
                "---\nname: memory-curator\n---\n"
                "You are the subagent execution wrapper for RightMemory retrieval and update work.\n",
                encoding="utf-8",
            )
            old_dreamer.joinpath("SKILL.md").write_text(
                "---\nname: memory-dreamer\n---\n"
                "You are the subagent execution wrapper for RightMemory dream cycles.\n",
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            old_curator_exists = old_curator.exists()
            old_dreamer_exists = old_dreamer.exists()

            user_memory_root = root / "user-memory"
            user_skills_target = root / "user-skills"
            user_curator = user_skills_target / "memory-curator"
            user_dreamer = user_skills_target / "memory-dreamer"
            user_curator.mkdir(parents=True)
            user_dreamer.mkdir(parents=True)
            user_curator.joinpath("SKILL.md").write_text(
                "---\nname: memory-curator\n---\nUser-owned memory-curator helper.\n",
                encoding="utf-8",
            )
            user_dreamer.joinpath("SKILL.md").write_text(
                "---\nname: memory-dreamer\n---\nUser-owned memory-dreamer helper.\n",
                encoding="utf-8",
            )

            self._install(user_memory_root, user_skills_target)
            user_curator_exists = user_curator.exists()
            user_dreamer_exists = user_dreamer.exists()
            user_curator_text = user_curator.joinpath("SKILL.md").read_text(encoding="utf-8")
            user_dreamer_text = user_dreamer.joinpath("SKILL.md").read_text(encoding="utf-8")

        self.assertFalse(old_curator_exists)
        self.assertFalse(old_dreamer_exists)
        self.assertTrue(user_curator_exists)
        self.assertTrue(user_dreamer_exists)
        self.assertIn("User-owned", user_curator_text)
        self.assertIn("User-owned", user_dreamer_text)

    def _install(self, memory_root: Path, skills_target: Path) -> subprocess.CompletedProcess[str]:
        root = memory_root.parent
        return subprocess.run(
            ["bash", "install.sh", "--mode", "cli-agent", str(memory_root), str(skills_target)],
            cwd=REPO_ROOT,
            env=self._env_with_fake_uv(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
