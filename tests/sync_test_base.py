import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class SyncTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed_tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._seed_tempdir.cleanup)
        cls._seed_root = Path(cls._seed_tempdir.name)
        cls._build_seed(cls._seed_root)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        shutil.copytree(self._seed_root, self.root, dirs_exist_ok=True)
        self.remote = self.root / "remote.git"
        self.device = self.root / "device"
        self.other = self.root / "other"

    def _create_remote_local_conflict(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

    def _commit_candidate_resolution(self, candidate):
        (candidate / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one-remote` remote durable fact → []\n"
            "- `one-local` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(candidate, "add", "MEMORY.md")
        self._git(candidate, "commit", "-m", "memory: resolve staged sync conflict")

    def _assert_no_sync_candidates(self):
        self.assertEqual(self._git(self.device, "branch", "--list", "rightmemory-sync-*"), "")
        worktrees = self._git(self.device, "worktree", "list", "--porcelain")
        self.assertNotIn("/.runtime/worktrees/sync-", worktrees)
        lease_root = self.device / ".runtime" / "worktree-leases"
        leases = (
            sorted(path.name for path in lease_root.glob("sync-*.json"))
            if lease_root.is_dir()
            else []
        )
        self.assertEqual(leases, [])

    @classmethod
    def _build_seed(cls, root: Path) -> None:
        remote = root / "remote.git"
        device = root / "device"
        other = root / "other"
        cls._git(root, "init", "--bare", str(remote))
        cls._git(root, "clone", str(remote), str(device))
        cls._git(root, "clone", str(remote), str(other))
        for repo in (device, other):
            cls._git(repo, "config", "user.email", "test@example.com")
            cls._git(repo, "config", "user.name", "Test User")
            cls._git(repo, "remote", "set-url", "origin", "../remote.git")
        (device / "MEMORY.md").write_text("# Domain\n\n- `one` first → []\n", encoding="utf-8")
        (device / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (device / "PURSUIT_RULES.md").write_text("# Pursuit Rules\n", encoding="utf-8")
        (device / "AGENT_CORRECTION_MEMORY_RULES.md").write_text(
            "# Agent Correction Memory Rules\n",
            encoding="utf-8",
        )
        cls._git(
            device,
            "add",
            "MEMORY.md",
            "PURSUITS.md",
            "PURSUIT_RULES.md",
            "AGENT_CORRECTION_MEMORY_RULES.md",
        )
        cls._git(device, "commit", "-m", "initial memory")
        cls._git(device, "push", "-u", "origin", "HEAD:main")
        cls._git(device, "branch", "--set-upstream-to", "origin/main")
        cls._git(other, "fetch", "origin")
        cls._git(other, "checkout", "-B", "main", "origin/main")
        cls._git(other, "branch", "--set-upstream-to", "origin/main")

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr)
        return process.stdout.strip()

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
