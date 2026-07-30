import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class IsolatedWriteTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._seed_tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._seed_tempdir.cleanup)
        cls._seed_root = Path(cls._seed_tempdir.name) / "repository"
        cls._seed_root.mkdir()

        def seed_git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args],
                cwd=cls._seed_root,
                text=True,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
            return result.stdout.strip()

        seed_git("init")
        seed_git("config", "user.email", "test@example.com")
        seed_git("config", "user.name", "Test User")
        (cls._seed_root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "- `one` initial memory → []\n",
            encoding="utf-8",
        )
        (cls._seed_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        seed_git("add", "MEMORY.md", "PURSUITS.md")
        seed_git("commit", "-m", "initial memory")
        cls._seed_head = seed_git("rev-parse", "HEAD")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        shutil.copytree(self._seed_root, self.root, dirs_exist_ok=True)
        self.initial_head = self._seed_head

    def _add_isolated_worktree(self, role: str, identifier: str) -> tuple[str, Path]:
        branch = f"rightmemory-isolated-{role}-{identifier}"
        worktree = self.root / ".runtime" / "worktrees" / f"{role}-{identifier}"
        self._git("worktree", "add", "-b", branch, str(worktree), self.initial_head)
        return branch, worktree

    def _write_lease(self, role: str, identifier: str, *, pid: int, identity: str) -> Path:
        path = self.root / ".runtime" / "worktree-leases" / f"{role}-{identifier}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "process_identity": identity}) + "\n",
            encoding="utf-8",
        )
        return path

    def _append_memory(self, root: Path, text: str) -> None:
        memory = root / "MEMORY.md"
        memory.write_text(memory.read_text(encoding="utf-8") + text, encoding="utf-8")

    def _add_fixed_agent_correction_collections(self) -> None:
        memory = self.root / "MEMORY.md"
        memory.write_text(
            memory.read_text(encoding="utf-8")
            + "\n### Agent corrections\n\n"
            "#### Writing corrections {M#agent-corrections-writing}\n\n"
            "#### Design corrections {M#agent-corrections-design}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_agent-corrections-writing.md").write_text(
            "# Curated writing corrections\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_agent-corrections-design.md").write_text(
            "# Curated design corrections\n",
            encoding="utf-8",
        )
        self._git(
            "add",
            "MEMORY.md",
            "MEMORY_agent-corrections-writing.md",
            "MEMORY_agent-corrections-design.md",
        )
        self._git("commit", "-m", "memory: add fixed agent correction collections")

    def _corrections_markdown(self, count: int) -> str:
        entries = []
        for index in range(count):
            entries.append(
                f"## Entry {index}\n\n"
                "### Candidate\n\nCandidate.\n\n"
                "### Proposed edit\n\nProposed.\n\n"
                "### Accepted edit\n\nAccepted.\n"
            )
        return "# RightMemory Edit Corrections\n\n" + "\n".join(entries)

    def _assert_isolated_cleanup(self) -> None:
        worktrees = self._git("worktree", "list", "--porcelain")
        branches = self._git("branch", "--list", "rightmemory-isolated-*")
        leases = list((self.root / ".runtime" / "worktree-leases").glob("*.json"))
        self.assertNotIn(".runtime/worktrees/", worktrees)
        self.assertEqual(branches, "")
        self.assertEqual(leases, [])

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()
