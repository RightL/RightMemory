import subprocess
import tempfile
import unittest
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_START = "rightmemory:example:start"
EXAMPLE_END = "rightmemory:example:end"


class InstallScriptTests(unittest.TestCase):
    def test_memory_example_includes_skill_creation_guidance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)
            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")

        block_start = memory.index(EXAMPLE_START)
        block_end = memory.index(EXAMPLE_END, block_start)
        managed_example = memory[block_start:block_end]

        self.assertIn("Skill Creation Guidance {F#sample-skill-creation-guidance}", managed_example)
        self.assertIn("skill_artifacts/sample-skill-creation-guidance/", managed_example)
        self.assertIn("class-level", managed_example)
        self.assertIn("support material", managed_example)

    def test_initial_install_copies_managed_example(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)

            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
            gitignore = (memory_root / ".gitignore").read_text(encoding="utf-8")
            install_stamp_exists = (memory_root / ".runtime" / "install.stamp").exists()

        self.assertIn(EXAMPLE_START, memory)
        self.assertIn(EXAMPLE_END, memory)
        self.assertIn("# User Pending Task and Thoughts", memory)
        self.assertIn("!skill_artifacts/\n", gitignore)
        self.assertIn("!skill_artifacts/**\n", gitignore)
        self.assertTrue(install_stamp_exists)

    def test_install_preserves_existing_memory_gitignore(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            custom_gitignore = "# user rules\n*\n!custom.md\n"
            memory_root.mkdir()
            (memory_root / ".gitignore").write_text(custom_gitignore, encoding="utf-8")

            self._install(memory_root, skills_target)
            preserved = (memory_root / ".gitignore").read_text(encoding="utf-8")

        self.assertEqual(preserved, custom_gitignore)

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
                "- `real-node` keep me. → []\n\n"
                "---\n\n"
                "# User Pending Task and Thoughts (user-edited only - AI agents must not modify this section)\n",
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
                "  printf '#!/usr/bin/env sh\\n' > \"$2/bin/python\"\n"
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
            self.assertIn("rightmemory is installed", result.stdout)

    def _install(self, memory_root: Path, skills_target: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "install.sh", "--mode", "subagent", str(memory_root), str(skills_target)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
