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
            auto_orchestrator_exists = (
                skills_target / "rightmemory-auto-orchestrator" / "SKILL.md"
            ).exists()
            maintainer = (skills_target / "maintain-rightmemory" / "SKILL.md")
            maintainer_exists = maintainer.exists()
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
        self.assertTrue(auto_orchestrator_exists)
        self.assertTrue(maintainer_exists)
        self.assertIn("rightmemory-auto-orchestrator", result.stdout)
        self.assertFalse(legacy_orchestrator_exists)
        self.assertIn('set "PYTHONUTF8=1"', wrapper_text)
        self.assertIn('set "RIGHTMEMORY_ROOT=', wrapper_text)
        self.assertIn(str(memory_root), wrapper_text)
        self.assertIn(' -m rightmemory.entrypoint %*', wrapper_text)
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
                for skill_name in (
                    "memory-retriever",
                    "rightmemory-orchestrator",
                    "rightmemory-auto-orchestrator",
                    "maintain-rightmemory",
                    "review-agent-guidance-inbox",
                ):
                    self.assertTrue((skills_target / skill_name / "SKILL.md").is_file())

    def test_windows_installer_reports_missing_uv_before_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            env = os.environ.copy()
            env["PATH"] = str(root)
            env["PATHEXT"] = ".CMD;.EXE;.BAT;.COM"
            fake_git = root / "git.cmd"
            fake_git.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

            result = self._run_installer(
                ["--mode", "standalone", str(memory_root), str(skills_target)],
                root,
                env,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing or unusable required command: uv", result.stderr)
        self.assertFalse(memory_root.exists())
        self.assertFalse(skills_target.exists())

    def test_windows_installer_propagates_install_core_failure(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("memory", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("pursuit", encoding="utf-8")
            self._git(memory_root, "init")
            self._git(memory_root, "config", "user.email", "test@example.com")
            self._git(memory_root, "config", "user.name", "Test")
            self._git(memory_root, "add", "MEMORY.md", "PURSUITS.md")
            self._git(memory_root, "commit", "-m", "seed")
            (memory_root / "PURSUIT_RULES.md").write_text("legacy", encoding="utf-8")
            env = self._env_with_fake_uv(root)

            result = self._install(memory_root, skills_target, env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy package-reference files", result.stderr)

    def _install(self, memory_root: Path, skills_target: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return self._run_installer(
            ["--mode", "cli-agent", str(memory_root), str(skills_target)],
            memory_root.parent,
            env,
        )

    def _run_installer(
        self,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO_ROOT / "install.ps1"), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )

    def _env_with_fake_uv(self, root: Path) -> dict[str, str]:
        env = os.environ.copy()
        local_app_data = root / "local"
        env["LOCALAPPDATA"] = str(local_app_data)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        fake_uv = fake_bin / "uv.cmd"
        python = Path(sys.executable)
        fake_uv.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" (echo uv 0.0.0& exit /b 0)\r\n"
            f"if \"%1 %2 %3 %4\"==\"python find --no-project >=3.11\" (echo {python}& exit /b 0)\r\n"
            "if \"%1\"==\"venv\" (\r\n"
            "  set target=%6\r\n"
            "  mkdir \"%target%\\Scripts\" >nul 2>nul\r\n"
            f"  >\"%target%\\Scripts\\python.cmd\" echo @\"{python}\" %%*\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "if \"%1 %2\"==\"pip install\" exit /b 0\r\n"
            "exit /b 1\r\n",
            encoding="utf-8",
        )
        env["PATH"] = f"{fake_bin};{env.get('PATH', '')}"
        return env

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            [self.git, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
