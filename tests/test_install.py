import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_START = "rightmemory:example:start"
EXAMPLE_END = "rightmemory:example:end"


class InstallScriptTests(unittest.TestCase):
    def test_initial_install_copies_managed_example(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            self._install(memory_root, skills_target)

            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertIn(EXAMPLE_START, memory)
        self.assertIn(EXAMPLE_END, memory)
        self.assertIn("# User Pending Task and Thoughts", memory)

    def test_rerun_refreshes_marked_example_and_preserves_user_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            self._install(memory_root, skills_target)

            memory_path = memory_root / "MEMORY.md"
            memory = memory_path.read_text(encoding="utf-8")
            memory_path.write_text(
                "# Real Memory {#real-memory}\n\n- `real-node` keep me. -> []\n\n"
                + memory.replace("Example Application", "Stale Example Application"),
                encoding="utf-8",
            )

            self._install(memory_root, skills_target)
            refreshed = memory_path.read_text(encoding="utf-8")

        self.assertIn("# Real Memory {#real-memory}", refreshed)
        self.assertIn("- `real-node` keep me. -> []", refreshed)
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
                "- `real-node` keep me. -> []\n\n"
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
        self.assertIn("- `real-node` keep me. -> []", migrated)

    def _install(self, memory_root: Path, skills_target: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "install.sh", str(memory_root), str(skills_target)],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )


if __name__ == "__main__":
    unittest.main()
