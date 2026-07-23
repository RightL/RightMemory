import shutil
import subprocess
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.update_queue import UpdateCandidate, UpdateQueueStore
from rightmemory.update_queue_git import GitUpdateQueueCoordinator
from rightmemory.update_review import UpdateReviewStore, tracked_review_blob_oid


class GitUpdateQueueTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed_tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._seed_tempdir.cleanup)
        cls._seed_root = Path(cls._seed_tempdir.name) / "topology"
        cls._seed_root.mkdir()
        remote = cls._seed_root / "remote.git"
        first = cls._seed_root / "first"
        second = cls._seed_root / "second"
        cls._run_git(cls._seed_root, "init", "--bare", str(remote))
        cls._run_git(cls._seed_root, "clone", str(remote), str(first))
        cls._run_git(cls._seed_root, "clone", str(remote), str(second))
        for repo in (first, second):
            cls._run_git(repo, "config", "user.email", "test@example.com")
            cls._run_git(repo, "config", "user.name", "Test User")
            cls._run_git(repo, "remote", "set-url", "origin", "../remote.git")
        (first / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n",
            encoding="utf-8",
        )
        (first / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (first / "PURSUIT_RULES.md").write_text("# Pursuit Rules\n", encoding="utf-8")
        cls._run_git(first, "add", "MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md")
        cls._run_git(first, "commit", "-m", "initial memory")
        cls._run_git(first, "push", "-u", "origin", "HEAD:main")
        cls._run_git(first, "branch", "--set-upstream-to", "origin/main")
        cls._run_git(second, "fetch", "origin")
        cls._run_git(second, "checkout", "-B", "main", "origin/main")
        cls._run_git(second, "branch", "--set-upstream-to", "origin/main")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "topology"
        shutil.copytree(self._seed_root, self.root)
        self.remote = self.root / "remote.git"
        self.first = self.root / "first"
        self.second = self.root / "second"

    def _outbox_candidate(self, root: Path, uid: str) -> UpdateCandidate:
        candidate = UpdateCandidate(
            uid=uid,
            session_id="session-a",
            display_id=1,
            message="remember this",
            submitted_at=datetime(2026, 7, 21, tzinfo=UTC).isoformat(),
        )
        UpdateQueueStore(root).write_outbox(candidate)
        return candidate

    def _review_candidate(
        self,
        root: Path,
        review_id: str,
        message: str,
        *,
        uid: str | None = None,
        submitted_at: str = "2026-07-21T00:00:00+00:00",
        review_commit: str | None = None,
    ) -> UpdateCandidate:
        review_blob_oid = tracked_review_blob_oid(root, review_id)
        self.assertIsNotNone(review_blob_oid)
        candidate = UpdateCandidate(
            uid=uid or uuid.uuid4().hex,
            session_id=review_id,
            display_id=1,
            message=message,
            submitted_at=submitted_at,
            kind="review",
            review_id=review_id,
            review_commit=review_commit or self._git(root, "rev-parse", "HEAD"),
            review_blob_oid=review_blob_oid,
        )
        UpdateQueueStore(root).write_outbox(candidate)
        return candidate

    def _create_tracked_review(self) -> str:
        operation_id = "update-review-origin"
        base = self._git(self.first, "rev-parse", "HEAD")
        (self.first / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` reviewed → []\n",
            encoding="utf-8",
        )
        store = UpdateReviewStore(self.first)
        record = store.create_review(
            origin_operation_id=operation_id,
            base_commit=base,
            write_surface="Memory",
            summary="Added reviewed memory.",
            diff=self._git(self.first, "diff", "--", "MEMORY.md"),
        )
        self._git(
            self.first,
            "add",
            "MEMORY.md",
            str(store.review_path(record.review_id).relative_to(self.first)),
        )
        self._git(
            self.first,
            "commit",
            "-m",
            f"memory: reviewed update\n\nRightMemory-Operation: {operation_id}",
        )
        self._git(self.first, "push", "origin", "HEAD:main")
        return record.review_id

    def _coordinator(self, root: Path, device_id: str) -> GitUpdateQueueCoordinator:
        return GitUpdateQueueCoordinator(
            SyncConfig(memory_root=root, enabled=True),
            device_id=device_id,
        )

    def _manual_recovery_coordinator(self) -> GitUpdateQueueCoordinator:
        self._outbox_candidate(self.first, "a" * 32)
        coordinator = self._coordinator(self.first, "1" * 32)
        coordinator.publish_outbox()
        first_claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
        ).claim
        self.assertIsNotNone(first_claim)
        coordinator.fail(first_claim, reason_code="processing_failed")
        second_claim = coordinator.claim_next(
            target_batch_candidates=1,
            max_wait_seconds=0,
            now=datetime(2030, 1, 1, tzinfo=UTC),
        ).claim
        self.assertIsNotNone(second_claim)
        recovery = coordinator.fail(second_claim, reason_code="processing_failed")
        self.assertTrue(recovery.manual_recovery)
        return coordinator

    def _reset_to_remote(self, root: Path) -> None:
        self._git(root, "fetch", "origin")
        self._git(root, "reset", "--hard", "origin/main")

    def _git(self, cwd: Path, *args: str) -> str:
        return self._run_git(cwd, *args)

    @staticmethod
    def _run_git(cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()
