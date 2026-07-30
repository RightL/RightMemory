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
            pursuits = (memory_root / "PURSUITS.md").read_text(encoding="utf-8")
            gitignore = (memory_root / ".gitignore").read_text(encoding="utf-8")
            tracked_gitignore = self._git(memory_root, "ls-files", "--", ".gitignore")
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            git_status = self._git(memory_root, "status", "--short")
            schema_exists = (skills_target / "rightmemory-schema.md").exists()
            edit_correction_rules_exist = (
                skills_target / "rightmemory-edit-correction-rules.md"
            ).exists()
            retriever_exists = (skills_target / "memory-retriever" / "SKILL.md").exists()
            orchestrator_exists = (skills_target / "rightmemory-orchestrator" / "SKILL.md").exists()
            maintainer = (skills_target / "maintain-rightmemory" / "SKILL.md")
            maintainer_exists = maintainer.exists()
            maintainer_text = maintainer.read_text(encoding="utf-8")
            legacy_orchestrator_exists = (skills_target / "memory-orchestrator").exists()
            leaked_requirement_file = (root / "3.11").exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("rightmemory package into", result.stdout)
        self.assertIn("rightmemory is available in this PowerShell session", result.stdout)
        self.assertIn("SetEnvironmentVariable", result.stdout)
        self.assertIn("rightmemory:example:start", memory)
        self.assertIn("rightmemory:pursuit-example:start", pursuits)
        self.assertFalse((memory_root / "PURSUIT_RULES.md").exists())
        self.assertFalse((memory_root / "AGENT_CORRECTION_MEMORY_RULES.md").exists())
        self.assertTrue(gitignore.startswith("*\n!.gitignore\n"))
        self.assertEqual(tracked_gitignore, ".gitignore")
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertEqual(git_status, "")
        self.assertFalse(schema_exists)
        self.assertFalse(edit_correction_rules_exist)
        self.assertTrue(retriever_exists)
        self.assertTrue(orchestrator_exists)
        self.assertTrue(maintainer_exists)
        self.assertNotIn(str(memory_root), maintainer_text)
        self.assertIn("rightmemory reference schema", maintainer_text)
        self.assertIn("`rightmemory status`", maintainer_text)
        self.assertIn("`rightmemory --profile <name> status`", maintainer_text)
        self.assertIn("use the reported `root:`", maintainer_text)
        self.assertIn("do not infer or guess it", maintainer_text)
        self.assertNotIn("{{MEMORY_ROOT}}", maintainer_text)
        self.assertFalse(legacy_orchestrator_exists)
        self.assertIn('set "PYTHONUTF8=1"', wrapper_text)
        self.assertIn('set "RIGHTMEMORY_ROOT=', wrapper_text)
        self.assertIn(str(memory_root), wrapper_text)
        self.assertIn(' -m rightmemory.cli %*', wrapper_text)
        self.assertFalse(leaked_requirement_file)

    def test_windows_installer_bootstraps_both_modes_with_tracked_gitignore(self):
        for mode in ("cli-agent", "standalone"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                memory_root = root / "memory"
                skills_target = root / "skills"
                env = self._env_with_fake_uv(root)

                result = self._run_installer(
                    ["--mode", mode, str(memory_root), str(skills_target)],
                    root,
                    env,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self._git(memory_root, "status", "--short"), "")
                self.assertEqual(
                    self._git(memory_root, "ls-files", "--", ".gitignore"),
                    ".gitignore",
                )
                self.assertTrue(
                    (memory_root / ".gitignore")
                    .read_text(encoding="utf-8")
                    .startswith("*\n!.gitignore\n")
                )
                self.assertIn(
                    f"mode={mode}",
                    (memory_root / ".runtime" / "install.stamp").read_text(
                        encoding="utf-8"
                    ),
                )

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

    def test_windows_installer_propagates_install_core_failure(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            bootstrap_python = root / "failing-python.cmd"
            bootstrap_python.write_text("@exit /b 7\r\n", encoding="ascii")
            env = self._env_with_fake_uv(root)
            env["RIGHTMEMORY_TEST_PYTHON"] = str(bootstrap_python)

            result = self._install(memory_root, skills_target, env)

        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn("rightmemory is available in this PowerShell session", result.stdout)
        self.assertFalse(memory_root.exists())
        self.assertFalse(skills_target.exists())

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
