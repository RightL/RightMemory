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
            pursuit_rules = (memory_root / "PURSUIT_RULES.md").read_text(encoding="utf-8")
            install_stamp = (memory_root / ".runtime" / "install.stamp").read_text(encoding="utf-8")
            git_status = self._git(memory_root, "status", "--short")
            schema_exists = (skills_target / "rightmemory-schema.md").exists()
            retriever_exists = (skills_target / "memory-retriever" / "SKILL.md").exists()
            orchestrator_exists = (skills_target / "rightmemory-orchestrator" / "SKILL.md").exists()
            legacy_orchestrator_exists = (skills_target / "memory-orchestrator").exists()
            leaked_requirement_file = (root / "3.11").exists()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE         = cli-agent", result.stdout)
        self.assertIn("rightmemory package into", result.stdout)
        self.assertIn("rightmemory:example:start", memory)
        self.assertIn("rightmemory:pursuit-example:start", pursuits)
        self.assertIn("# Pursuit Rules", pursuit_rules)
        self.assertIn("mode=cli-agent", install_stamp)
        self.assertEqual(git_status, "")
        self.assertTrue(schema_exists)
        self.assertTrue(retriever_exists)
        self.assertTrue(orchestrator_exists)
        self.assertFalse(legacy_orchestrator_exists)
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
            pursuit_exists = (home / ".rightmemory" / "PURSUITS.md").is_file()
            rules_exist = (home / ".rightmemory" / "PURSUIT_RULES.md").is_file()
            installed = [
                (
                    (target / "memory-retriever" / "SKILL.md").is_file(),
                    (target / "rightmemory-orchestrator" / "SKILL.md").is_file(),
                    (target / "memory-orchestrator").exists(),
                )
                for target in (home / ".codex" / "skills", home / ".claude" / "skills")
            ]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MODE         = standalone", result.stdout)
        self.assertIn("rightmemory is available in this PowerShell session", result.stdout)
        self.assertIn("SetEnvironmentVariable", result.stdout)
        self.assertTrue(memory_exists)
        self.assertTrue(pursuit_exists)
        self.assertTrue(rules_exist)
        for retriever_exists, orchestrator_exists, legacy_exists in installed:
            self.assertTrue(retriever_exists)
            self.assertTrue(orchestrator_exists)
            self.assertFalse(legacy_exists)

    def test_windows_rerun_preserves_managed_examples_and_utf8_memory(self):
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
            pursuits_path = memory_root / "PURSUITS.md"
            pursuits = pursuits_path.read_text(encoding="utf-8")
            pursuits_path.write_text(
                "# User Pursuits\n\n## Continue {#continue}\n\nKeep this.\n\n"
                + pursuits.replace("Example Release Readiness", "Stale Release Readiness"),
                encoding="utf-8",
            )
            expected_memory = memory_path.read_bytes()
            expected_pursuits = pursuits_path.read_bytes()

            result = self._install(memory_root, skills_target, env)
            actual_memory = memory_path.read_bytes()
            actual_pursuits = pursuits_path.read_bytes()
            refreshed = memory_path.read_text(encoding="utf-8")
            refreshed_pursuits = pursuits_path.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual_memory, expected_memory)
        self.assertEqual(actual_pursuits, expected_pursuits)
        self.assertIn("用户偏好中文", refreshed)
        self.assertIn("Stale Example Application", refreshed)
        self.assertIn("## Continue {#continue}", refreshed_pursuits)
        self.assertIn("Stale Release Readiness", refreshed_pursuits)

    def test_windows_installer_refuses_incomplete_existing_repo_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            runtime_home = root / "local" / "RightMemory"
            memory_root.mkdir()
            self._git(memory_root, "init")
            self._git(memory_root, "config", "user.email", "test@example.com")
            self._git(memory_root, "config", "user.name", "Test User")
            (memory_root / "MEMORY.md").write_text("# Existing Memory\n", encoding="utf-8")
            self._git(memory_root, "add", "MEMORY.md")
            self._git(memory_root, "commit", "-m", "memory: existing baseline")
            (memory_root / ".runtime").mkdir()
            (memory_root / ".runtime" / "install.stamp").write_text("old stamp\n", encoding="utf-8")
            skills_target.mkdir()
            (skills_target / "keep.txt").write_text("skills stay\n", encoding="utf-8")
            runtime_home.mkdir(parents=True)
            (runtime_home / "keep.txt").write_text("runtime stays\n", encoding="utf-8")
            before_head = self._git(memory_root, "rev-parse", "HEAD")
            before_status = self._git(memory_root, "status", "--short")
            before_memory = self._snapshot(memory_root)
            before_skills = self._snapshot(skills_target)
            before_runtime = self._snapshot(runtime_home)

            result = self._install(memory_root, skills_target, self._env_with_fake_uv(root))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("existing RightMemory root is incomplete", result.stderr)
            self.assertIn("missing required files: PURSUITS.md, PURSUIT_RULES.md", result.stderr)
            self.assertIn("installation made no changes", result.stderr)
            self.assertEqual(self._git(memory_root, "rev-parse", "HEAD"), before_head)
            self.assertEqual(self._git(memory_root, "status", "--short"), before_status)
            self.assertEqual(self._snapshot(memory_root), before_memory)
            self.assertEqual(self._snapshot(skills_target), before_skills)
            self.assertEqual(self._snapshot(runtime_home), before_runtime)

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

    def _snapshot(self, root: Path) -> tuple[tuple[str, str, bytes], ...]:
        entries: list[tuple[str, str, bytes]] = []
        for path in sorted((root, *root.rglob("*")), key=lambda item: str(item.relative_to(root))):
            relative = "." if path == root else path.relative_to(root).as_posix()
            if path.is_dir():
                entries.append((relative, "directory", b""))
            else:
                entries.append((relative, "file", path.read_bytes()))
        return tuple(entries)


if __name__ == "__main__":
    unittest.main()
