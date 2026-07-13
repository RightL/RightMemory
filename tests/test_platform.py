import errno
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rightmemory.platform as rm_platform


class PlatformHelperTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows file locks require msvcrt")
    def test_windows_nonblocking_lock_reports_contention(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "state.lock"
            with path.open("a+", encoding="utf-8") as first, path.open("a+", encoding="utf-8") as second:
                rm_platform.lock_file_nonblocking(first)
                try:
                    with self.assertRaises(BlockingIOError):
                        rm_platform.lock_file_nonblocking(second)
                finally:
                    rm_platform.unlock_file(first)

    def test_windows_blocking_lock_retries_without_a_fixed_attempt_limit(self):
        contention = OSError(errno.EACCES, "locked")
        with (
            patch.object(rm_platform, "IS_WINDOWS", True),
            patch.object(rm_platform, "_lock_windows_file_nonblocking", side_effect=[contention, contention, None]) as lock,
            patch.object(rm_platform.time, "sleep") as sleep,
        ):
            rm_platform.lock_file(object())

        self.assertEqual(lock.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @unittest.skipUnless(os.name == "nt", "PowerShell command shims are Windows-specific")
    def test_windows_npm_command_uses_matching_powershell_shim_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cmd = root / "codex.cmd"
            ps1 = root / "codex.ps1"
            cmd.write_text("@exit /b 99\r\n", encoding="utf-8")
            ps1.write_text(
                "[Console]::Out.Write((ConvertTo-Json -Compress -InputObject @($args)))\n",
                encoding="utf-8",
            )
            arguments = ["two words", "100% literal", "a&b", "中文", "line one\nline two"]

            path = f"{root}{os.pathsep}{os.environ.get('PATH', '')}"
            with patch.dict(os.environ, {"PATH": path}):
                command = rm_platform.prepare_command(["codex", *arguments])
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    check=True,
                )

        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertTrue(any(part.lower().endswith("codex.ps1") for part in command))

    @unittest.skipUnless(os.name == "nt", "Windows process lookup requires Win32 APIs")
    def test_windows_process_exists_recognizes_current_process(self):
        self.assertTrue(rm_platform.process_exists(os.getpid()))
        self.assertTrue(rm_platform.process_identity(os.getpid()).startswith("win:"))

    def test_posix_process_command_falls_back_to_ps_without_proc(self):
        result = subprocess.CompletedProcess(
            ["ps"],
            0,
            stdout="python -m rightmemory.cli review watch\n",
            stderr="",
        )
        with (
            patch.object(rm_platform, "IS_WINDOWS", False),
            patch.object(rm_platform.Path, "read_bytes", side_effect=FileNotFoundError),
            patch.object(rm_platform.subprocess, "run", return_value=result) as run,
        ):
            command = rm_platform.process_command(123)

        self.assertEqual(command, "python -m rightmemory.cli review watch")
        self.assertEqual(run.call_args.args[0], ["ps", "-p", "123", "-o", "command="])

    def test_windows_process_command_reads_cim_command_line(self):
        result = subprocess.CompletedProcess(
            ["powershell"],
            0,
            stdout="python -m rightmemory.web.app --serve\r\n",
            stderr="",
        )
        with (
            patch.object(rm_platform, "IS_WINDOWS", True),
            patch.object(rm_platform.subprocess, "run", return_value=result) as run,
        ):
            command = rm_platform.process_command(456)

        self.assertEqual(command, "python -m rightmemory.web.app --serve")
        self.assertIn("Get-CimInstance Win32_Process", run.call_args.args[0][-1])
        self.assertIn("ProcessId = 456", run.call_args.args[0][-1])

    def test_windows_detached_process_kwargs_use_creation_flags(self):
        with (
            patch.object(rm_platform, "IS_WINDOWS", True),
            patch.object(rm_platform.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch.object(rm_platform.subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True),
        ):
            kwargs = rm_platform.detached_process_kwargs()

        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["creationflags"], 0x8000200)

    def test_windows_restart_spawns_replacement_and_exits_current_process(self):
        with (
            patch.object(rm_platform, "IS_WINDOWS", True),
            patch.object(rm_platform, "detached_process_kwargs", return_value={"creationflags": 123}),
            patch.object(rm_platform.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(SystemExit) as caught:
                rm_platform.restart_current_process(["python", "-m", "rightmemory.cli", "review", "watch"])

        self.assertEqual(caught.exception.code, 0)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][-2:], ["review", "watch"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], 123)


if __name__ == "__main__":
    unittest.main()
