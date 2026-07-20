import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.watch import (
    MANAGED_WATCH_TARGETS,
    MANAGED_WATCH_ENV,
    WATCH_COMMANDS,
    WATCH_HANDOFF_PID_ENV,
    ManagedWatchStatus,
    WatchLock,
    consume_watch_stop_request,
    start_managed_watch,
    stop_managed_watch,
    watch_log_path,
    watch_identity_path,
    watch_pid_path,
    watch_stop_path,
)


class WatchControlTests(unittest.TestCase):
    def test_transcript_and_update_reviews_are_independent_managed_targets(self):
        self.assertIn("review", MANAGED_WATCH_TARGETS)
        self.assertIn("update-review", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["review"], ("review", "watch"))
        self.assertEqual(WATCH_COMMANDS["update-review"], ("update-review", "watch"))

    def test_agent_cli_cleanup_has_its_own_managed_target(self):
        self.assertIn("agent-cli-cleanup", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["agent-cli-cleanup"], ("agent-cli", "cleanup", "--watch"))

    def test_starting_update_review_does_not_clean_update_worktrees(self):
        class FakeProcess:
            pid = 456

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale") as cleanup,
                patch("rightmemory.watch.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                status = start_managed_watch(root, "update-review", "python")

        self.assertEqual(status.state, "running")
        self.assertEqual(status.pid, 456)
        cleanup.assert_not_called()
        self.assertEqual(popen.call_args.args[0], ["python", "-m", "rightmemory.cli", "update-review", "watch"])

    def test_managed_watch_registers_and_cleans_its_own_pid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch.dict(os.environ, {MANAGED_WATCH_ENV: "review"}, clear=False):
                with WatchLock(root, "review"):
                    self.assertEqual(watch_pid_path(root, "review").read_text(encoding="utf-8"), f"{os.getpid()}\n")
                    if watch_identity_path(root, "review").exists():
                        self.assertRegex(
                            watch_identity_path(root, "review").read_text(encoding="utf-8"),
                            r"^(win|proc):",
                        )
                self.assertFalse(watch_pid_path(root, "review").exists())
                self.assertFalse(watch_identity_path(root, "review").exists())

    def test_handoff_retargets_pending_stop_request_to_replacement_pid(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            stop_path = watch_stop_path(root, "review")
            stop_path.parent.mkdir(parents=True)
            stop_path.write_text("123\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {MANAGED_WATCH_ENV: "review", WATCH_HANDOFF_PID_ENV: "123"},
                clear=False,
            ):
                with WatchLock(root, "review"):
                    self.assertEqual(stop_path.read_text(encoding="utf-8"), f"{os.getpid()}\n")

    def test_stop_request_is_consumed_only_by_target_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            stop_path = watch_stop_path(root, "review")
            stop_path.parent.mkdir(parents=True)
            stop_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

            self.assertTrue(consume_watch_stop_request(root, "review", os.getpid()))
            self.assertFalse(stop_path.exists())

    def test_managed_stop_persists_request_while_current_work_finishes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            status = ManagedWatchStatus("review", "running", 456, watch_log_path(root, "review"))
            with (
                patch("rightmemory.watch.managed_watch_status", return_value=status),
                patch("rightmemory.watch._wait_for_exit", return_value=False),
            ):
                result = stop_managed_watch(root, "review", timeout_seconds=0)

            self.assertEqual(result.state, "stopping")
            self.assertEqual(watch_stop_path(root, "review").read_text(encoding="utf-8"), "456\n")


if __name__ == "__main__":
    unittest.main()
