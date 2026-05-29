import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.status import (
    DashboardStatus,
    GitStatus,
    SectionStatus,
    collect_git_status,
    format_status_dashboard,
    read_log_preview,
)


class StatusDashboardTests(unittest.TestCase):
    def test_collect_git_status_reports_clean_branch_and_head(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")
            head = self._git(root, "rev-parse", "--short", "HEAD")
            branch = self._git(root, "branch", "--show-current")

            status = collect_git_status(root)

        self.assertEqual(status.summary, f"clean on {branch} @ {head}")
        self.assertIsNone(status.issue)

    def test_collect_git_status_reports_dirty_count(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            self._git(root, "init")
            self._git(root, "config", "user.email", "test@example.com")
            self._git(root, "config", "user.name", "Test User")
            (root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "initial memory")
            (root / "MEMORY.md").write_text("# Dirty\n", encoding="utf-8")

            status = collect_git_status(root)

        self.assertIn("dirty: 1 path", status.summary)
        self.assertEqual(status.issue, "dirty worktree: 1 path")

    def test_collect_git_status_reports_unavailable_repo(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status = collect_git_status(Path(tempdir))

        self.assertIn("unavailable", status.summary)
        self.assertIsNotNone(status.issue)

    def test_read_log_preview_prefers_recent_failure_line(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "dreamer.log"
            log.write_text(
                "[2026-05-29T08:00:00+00:00] rightmemory dreamer cycle\n"
                "ordinary success message\n"
                "rightmemory dreamer cycle failed: RuntimeError: boom\n"
                "rightmemory dreamer watch stopped\n",
                encoding="utf-8",
            )

            preview = read_log_preview(log)

        self.assertEqual(preview, "rightmemory dreamer cycle failed: RuntimeError: boom")

    def test_read_log_preview_caps_long_preview(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = Path(tempdir) / "pruner.log"
            log.write_text("x" * 400 + "\n", encoding="utf-8")

            preview = read_log_preview(log)

        self.assertEqual(len(preview), 300)

    def test_format_status_dashboard_renders_grouped_sections(self):
        dashboard = DashboardStatus(
            root=Path("/memory/root"),
            git=GitStatus(summary="clean on main @ abc1234"),
            watches=[
                SectionStatus(
                    name="review",
                    state="running pid 123",
                    log_path=".runtime/watch/review.log",
                    last="reviewed 3 sessions",
                ),
                SectionStatus(
                    name="pruner",
                    state="stopped",
                    log_path=".runtime/watch/pruner.log",
                    last="failed: boom",
                    issue="pruner failed",
                ),
            ],
            dreamer=SectionStatus(
                name="dreamer",
                state="running pid 456",
                log_path=".runtime/watch/dreamer.log",
                detail="trigger: 12.5/50.0 points",
            ),
            update=SectionStatus(
                name="update",
                state="worker: idle",
                log_path=".runtime/async/update/",
                detail="pending: 0 candidates across 0 sessions",
            ),
            issues=["pruner failed"],
        )

        output = format_status_dashboard(dashboard)

        self.assertIn("RightMemory\n  root: /memory/root\n  git: clean on main @ abc1234", output)
        self.assertIn("Managed Watches", output)
        self.assertIn("review: running pid 123", output)
        self.assertIn("Async Update", output)
        self.assertIn("Recent Issues\n  pruner failed", output)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
