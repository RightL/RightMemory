import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "install.ps1 is the native Windows installer")
class WindowsInstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.git = shutil.which("git")
        if self.powershell is None:
            self.skipTest("PowerShell is not available")
        if self.git is None:
            self.skipTest("git is not available")

    def test_windows_installer_installs_command_wrapper_and_skills(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "用户记忆"
            skills_target = root / "代理技能"
            env = self._env_with_fake_uv(root)

            result = self._install(memory_root, skills_target, env)

            wrapper = root / "local" / "RightMemory" / "bin" / "rightmemory.cmd"
            wrapper_text = wrapper.read_text(encoding="utf-8")
            memory = (memory_root / "MEMORY.md").read_text(encoding="utf-8")
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            git_status = self._git(memory_root, "status", "--short")
            schema_exists = (skills_target / "rightmemory-schema.md").exists()
            orchestrator_exists = (skills_target / "memory-orchestrator" / "SKILL.md").exists()
            leaked_requirement_file = (root / "3.11").exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("rightmemory package into", result.stdout)
        self.assertIn("rightmemory:example:start", memory)
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertEqual(git_status, "")
        self.assertTrue(schema_exists)
        self.assertTrue(orchestrator_exists)
        self.assertIn('set "PYTHONUTF8=1"', wrapper_text)
        self.assertIn('set "RIGHTMEMORY_ROOT=', wrapper_text)
        self.assertIn(str(memory_root), wrapper_text)
        self.assertIn(' -m rightmemory.cli %*', wrapper_text)
        self.assertFalse(leaked_requirement_file)

    def test_windows_installer_reports_missing_uv_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_git_only(root)

            result = self._install(memory_root, skills_target, env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: uv", result.stderr)
        self.assertIn("Windows:", result.stderr)
        self.assertFalse(memory_root.exists())
        self.assertFalse(skills_target.exists())

    def test_windows_default_install_uses_standalone_and_both_skill_targets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            env = self._env_with_fake_uv(root)

            result = self._run_installer([], root, env)

            home = root / "home"
            memory_exists = (home / ".rightmemory" / "MEMORY.md").is_file()
            codex_skill = (home / ".codex" / "skills" / "memory-orchestrator" / "SKILL.md").is_file()
            claude_skill = (home / ".claude" / "skills" / "memory-orchestrator" / "SKILL.md").is_file()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE         = standalone", result.stdout)
        self.assertIn("rightmemory is available in this PowerShell session", result.stdout)
        self.assertIn("SetEnvironmentVariable", result.stdout)
        self.assertTrue(memory_exists)
        self.assertTrue(codex_skill)
        self.assertTrue(claude_skill)

    def test_windows_rerun_refreshes_managed_example_without_corrupting_utf8_memory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            first = self._install(memory_root, skills_target, env)
            self.assertEqual(first.returncode, 0, first.stderr)
            memory_path = memory_root / "MEMORY.md"
            original = memory_path.read_text(encoding="utf-8")
            memory_path.write_text(
                "# Real Memory {#real-memory}\n\n- `user-language` 用户偏好中文。 -> []\n\n"
                + original.replace("Example Application", "Stale Example Application"),
                encoding="utf-8",
            )

            result = self._install(memory_root, skills_target, env)
            refreshed = memory_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("用户偏好中文", refreshed)
        self.assertIn("Example Application", refreshed)
        self.assertNotIn("Stale Example Application", refreshed)

    def test_windows_semantic_upgrade_failure_fails_install_without_success_stamp(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = self._env_with_fake_uv(root)
            env["RIGHTMEMORY_TEST_SEMANTIC_EXIT"] = "7"

            result = self._install(memory_root, skills_target, env)
            stamp_exists = (memory_root / ".runtime" / "install.stamp").exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("semantic upgrade baseline failed with status 7", result.stderr)
        self.assertFalse(stamp_exists)

    def _install(self, memory_root: Path, skills_target: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return self._run_installer(
            ["--mode", "cli-agent", str(memory_root), str(skills_target)],
            memory_root.parent,
            env,
        )

    def _run_installer(
        self,
        arguments: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(REPO_ROOT / "install.ps1"),
                *arguments,
            ],
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _base_env(self, root: Path) -> dict[str, str]:
        env = {**os.environ}
        env["HOME"] = str(root / "home")
        env["USERPROFILE"] = str(root / "home")
        env["LOCALAPPDATA"] = str(root / "local")
        return env

    def _env_with_fake_uv(self, root: Path) -> dict[str, str]:
        env = self._base_env(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        uv_ps1 = fake_bin / "uv.ps1"
        uv_ps1.write_text(
            "if ($args[0] -eq '--version') { Write-Output 'uv 0.0.0'; exit 0 }\n"
            "if ($args[0] -eq 'python' -and $args[1] -eq 'find') { Write-Output $env:RIGHTMEMORY_TEST_PYTHON; exit 0 }\n"
            "if ($args[0] -eq 'venv') {\n"
            "  $target = $args[$args.Count - 1]\n"
            "  New-Item -ItemType Directory -Force -Path (Join-Path $target 'Scripts') | Out-Null\n"
            "  $python = Join-Path $target 'Scripts\\python.cmd'\n"
            "  Set-Content -Encoding ASCII -Path $python -Value @'\n"
            "@echo off\n"
            "set \"command=%3\"\n"
            "set \"memory_root=\"\n"
            ":loop\n"
            "if \"%~1\"==\"\" goto done\n"
            "if \"%~1\"==\"--memory-root\" set \"memory_root=%~2\"\n"
            "shift\n"
            "goto loop\n"
            ":done\n"
            "if not \"%memory_root%\"==\"\" mkdir \"%memory_root%\\.runtime\" 2>nul\n"
            "set \"state=%memory_root%\\.runtime\\semantic-upgrades.json\"\n"
            "if \"%command%\"==\"baseline\" (\n"
            "  echo   [keep]    semantic upgrade baseline recorded for 3 current note(s):\n"
            "  > \"%state%\" echo absorbed\n"
            ") else (\n"
            "  echo   [keep]    no semantic upgrade notes pending\n"
            "  > \"%state%\" echo absorbed\n"
            ")\n"
            "exit /b 0\n"
            "'@\n"
            "  $pythonPs1 = Join-Path $target 'Scripts\\python.ps1'\n"
            "  Set-Content -Encoding UTF8 -Path $pythonPs1 -Value @'\n"
            "$command = $args[2]\n"
            "$memoryRoot = ''\n"
            "for ($index = 0; $index -lt $args.Count; $index++) {\n"
            "  if ($args[$index] -eq '--memory-root') { $memoryRoot = $args[$index + 1] }\n"
            "}\n"
            "if ($memoryRoot) { New-Item -ItemType Directory -Force -Path (Join-Path $memoryRoot '.runtime') | Out-Null }\n"
            "$state = Join-Path $memoryRoot '.runtime\\semantic-upgrades.json'\n"
            "if ($command -eq 'baseline') {\n"
            "  Write-Output '  [keep]    semantic upgrade baseline recorded for 3 current note(s):'\n"
            "} else {\n"
            "  Write-Output '  [keep]    no semantic upgrade notes pending'\n"
            "}\n"
            "Set-Content -Encoding UTF8 -Path $state -Value 'absorbed'\n"
            "if ($env:RIGHTMEMORY_TEST_SEMANTIC_EXIT) { exit [int]$env:RIGHTMEMORY_TEST_SEMANTIC_EXIT }\n"
            "exit 0\n"
            "'@\n"
            "  exit 0\n"
            "}\n"
            "if ($args[0] -eq 'pip') { exit 0 }\n"
            "exit 0\n",
            encoding="utf-8",
        )
        (fake_bin / "uv.cmd").write_text(
            '@powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uv.ps1" %*\r\n',
            encoding="utf-8",
        )
        env["RIGHTMEMORY_TEST_PYTHON"] = sys.executable
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return env

    def _env_with_fake_git_only(self, root: Path) -> dict[str, str]:
        env = self._base_env(root)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "git.cmd").write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" exit /b 0\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
        env["PATH"] = str(fake_bin)
        return env

    def _git(self, memory_root: Path, *args: str) -> str:
        result = subprocess.run(
            [self.git, *args],
            cwd=memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
