import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rightmemory.watch import (
    MANAGED_WATCH_TARGETS,
    MANAGED_WATCH_ENV,
    WATCH_COMMANDS,
    WATCH_HANDOFF_PID_ENV,
    ManagedWatchStatus,
    WatchLock,
    consume_watch_stop_request,
    stop_managed_watch,
    watch_log_path,
    watch_identity_path,
    watch_pid_path,
    watch_stop_path,
    _wait_for_watch_registration,
)


class WatchControlTests(unittest.TestCase):
    def test_registration_retries_temporarily_locked_identity_file(self):
        process = Mock()
        with (
            patch("rightmemory.watch._read_pid", return_value=42),
            patch("rightmemory.watch._read_identity", side_effect=[PermissionError(), "identity"]),
            patch("rightmemory.watch.process_identity", return_value="identity"),
            patch("rightmemory.watch.time.sleep") as sleep,
        ):
            self.assertEqual(_wait_for_watch_registration(Path("fixture"), "sync", process), 42)
        sleep.assert_called_once_with(0.05)
        process.terminate.assert_not_called()

    def test_transcript_review_is_a_managed_target(self):
        self.assertIn("review", MANAGED_WATCH_TARGETS)
        self.assertEqual(WATCH_COMMANDS["review"], ("review", "watch"))

    def test_agent_cli_cleanup_is_not_a_managed_target(self):
        self.assertNotIn("agent-cli-cleanup", MANAGED_WATCH_TARGETS)
        self.assertNotIn("agent-cli-cleanup", WATCH_COMMANDS)

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
