import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.codex_app_server import CodexAppServerClient


class RecordingStdin(io.StringIO):
    def close(self):
        self.recorded = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", *, returncode: int = 0):
        self.stdin = RecordingStdin()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class BlockingStream:
    def __init__(self, stopped: threading.Event):
        self.stopped = stopped

    def __iter__(self):
        return self

    def __next__(self):
        self.stopped.wait()
        raise StopIteration


class TimeoutProcess:
    def __init__(self):
        self.stdin = RecordingStdin()
        self.stopped = threading.Event()
        self.stdout = BlockingStream(self.stopped)
        self.stderr = io.StringIO("")
        self.returncode = None

    def wait(self, timeout=None):
        if not self.stopped.is_set():
            raise subprocess.TimeoutExpired(["codex"], timeout)
        self.returncode = 0
        return 0

    def terminate(self):
        self.stopped.set()

    def kill(self):
        self.stopped.set()


class CodexAppServerClientTests(unittest.TestCase):
    def setUp(self):
        self.codex_bin = Path("C:/bundled/codex.exe")
        self.codex_path_dir = Path("bundled/codex-path")
        patcher = patch("codex_cli_bin.bundled_codex_path", return_value=self.codex_bin)
        patcher.start()
        self.addCleanup(patcher.stop)
        path_patcher = patch(
            "codex_cli_bin.bundled_path_dir",
            return_value=self.codex_path_dir,
        )
        path_patcher.start()
        self.addCleanup(path_patcher.stop)

    def test_deletes_batch_through_one_initialized_jsonl_process(self):
        process = FakeProcess(
            (
                '{"id":0,"result":{"serverInfo":{"name":"codex"}}}\n'
                '{"method":"thread/status/changed","params":{}}\n'
                '{"id":1,"result":{}}\n'
                '{"id":2,"result":{}}\n'
            )
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.codex_app_server.subprocess.Popen", return_value=process) as popen:
                results = CodexAppServerClient(root).delete_threads(["thread-1", "thread-2"])

        self.assertTrue(all(result.deleted for result in results))
        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0],
            [str(self.codex_bin), "app-server", "--listen", "stdio://"],
        )
        process_env = popen.call_args.kwargs["env"]
        path_key = next(key for key in process_env if key.upper() == "PATH")
        self.assertEqual(process_env[path_key].split(os.pathsep)[0], str(self.codex_path_dir))
        messages = [json.loads(line) for line in process.stdin.recorded.splitlines()]
        self.assertEqual(messages[0]["method"], "initialize")
        self.assertEqual(messages[1]["method"], "initialized")
        self.assertEqual(
            [(message["id"], message["params"]["threadId"]) for message in messages[2:]],
            [(1, "thread-1"), (2, "thread-2")],
        )

    def test_per_thread_error_is_retryable_without_hiding_successes(self):
        process = FakeProcess(
            (
                '{"id":0,"result":{}}\n'
                '{"id":1,"result":{}}\n'
                '{"id":2,"error":{"code":-32000,"message":"busy"}}\n'
            )
        )
        with tempfile.TemporaryDirectory() as tempdir:
            with patch("rightmemory.codex_app_server.subprocess.Popen", return_value=process):
                results = CodexAppServerClient(Path(tempdir)).delete_threads(["thread-1", "thread-2"])

        self.assertTrue(results[0].deleted)
        self.assertFalse(results[1].deleted)
        self.assertIn("busy", results[1].error)

    def test_already_missing_rollout_counts_as_deleted(self):
        process = FakeProcess(
            '{"id":0,"result":{}}\n'
            '{"id":1,"error":{"code":-32600,"message":"no rollout found for thread id thread-1"}}\n'
        )
        with tempfile.TemporaryDirectory() as tempdir:
            with patch("rightmemory.codex_app_server.subprocess.Popen", return_value=process):
                [result] = CodexAppServerClient(Path(tempdir)).delete_threads(["thread-1"])

        self.assertTrue(result.deleted)
        self.assertIsNone(result.error)

    def test_timeout_returns_failure_for_every_requested_thread(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch("rightmemory.codex_app_server.subprocess.Popen", return_value=TimeoutProcess()):
                results = CodexAppServerClient(Path(tempdir), timeout_seconds=1).delete_threads(
                    ["thread-1", "thread-2"]
                )

        self.assertEqual([result.deleted for result in results], [False, False])
        self.assertTrue(all("TimeoutError" in result.error for result in results))

    def test_missing_initialize_response_fails_closed(self):
        process = FakeProcess('{"id":1,"result":{}}\n')
        with tempfile.TemporaryDirectory() as tempdir:
            with patch("rightmemory.codex_app_server.subprocess.Popen", return_value=process):
                [result] = CodexAppServerClient(Path(tempdir)).delete_threads(["thread-1"])

        self.assertFalse(result.deleted)
        self.assertIn("initialize", result.error)


if __name__ == "__main__":
    unittest.main()
