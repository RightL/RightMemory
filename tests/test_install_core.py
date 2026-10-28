import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rightmemory import install_core
from rightmemory.install_core import InstallError, Installer, _posix_data_home


REPO_ROOT = Path(install_core.__file__).resolve().parents[1]


def _snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: str(item.relative_to(root))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative, "directory", b""))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


class InstallCoreTests(unittest.TestCase):
    def _existing_installer(self, root: Path, mode: str = "standalone") -> Installer:
        memory = root / "memory"
        memory.mkdir()
        (memory / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (memory / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        installer = Installer(REPO_ROOT, mode, memory, [root / "skills"])
        installer.runtime_home = root / "runtime"
        installer.runtime_venv = installer.runtime_home / "venv"
        installer.runtime_bin_dir = installer.runtime_home / "bin"
        installer.runtime_command = installer.runtime_bin_dir / "rightmemory"
        return installer

    def test_source_import_and_clear_error_without_codex_sdk(self):
        script = """
import importlib.abc
import sys

class BlockCodexSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "openai_codex" or fullname.startswith("openai_codex."):
            raise ModuleNotFoundError("masked optional dependency", name=fullname)
        return None

for name in tuple(sys.modules):
    if name == "openai_codex" or name.startswith("openai_codex."):
        sys.modules.pop(name)
sys.meta_path.insert(0, BlockCodexSdk())

from pathlib import Path
from rightmemory.runtime import RightMemoryRuntime
from rightmemory.codex_sdk import CodexSdkRunner

runner = CodexSdkRunner()
try:
    runner.run_turn(
        prompt="test",
        provider_session_id=None,
        cwd=Path.cwd(),
        model=None,
        reasoning_effort=None,
        sandbox="read-only",
    )
except RuntimeError as exc:
    assert "rightmemory[codex-sdk]" in str(exc), str(exc)
else:
    raise AssertionError("Codex mode unexpectedly ran without its optional SDK")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_install_adds_codex_extra_only_for_cli_agent_mode(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            for mode, suffix in (("standalone", ""), ("assistant", ""), ("cli-agent", "[codex-sdk]")):
                with self.subTest(mode=mode):
                    installer = Installer(REPO_ROOT, mode, root / "memory", [root / "skills"])
                    installer.runtime_home = root / f"runtime-{mode}"
                    installer.runtime_venv = installer.runtime_home / "venv"
                    installer.runtime_bin_dir = installer.runtime_home / "bin"
                    installer.runtime_command = installer.runtime_bin_dir / "rightmemory"
                    python = (
                        installer.runtime_venv / "Scripts" / "python.exe"
                        if os.name == "nt"
                        else installer.runtime_venv / "bin" / "python"
                    )
                    python.parent.mkdir(parents=True)
                    python.touch()

                    with (
                        patch.object(install_core, "_run", return_value=subprocess.CompletedProcess([], 0)) as run,
                        patch.object(installer, "_write_runtime_wrapper"),
                        patch("builtins.print"),
                    ):
                        installer._install_runtime()

                    run.assert_called_once_with(
                        [
                            "uv",
                            "pip",
                            "install",
                            "--python",
                            str(python),
                            f"{REPO_ROOT}{suffix}",
                        ]
                    )

    def test_assistant_mode_requires_explicit_paths(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            install_core._parse_args(["--mode", "assistant"])
        args = install_core._parse_args(["--mode", "assistant", "/memory", "/skills"])
        self.assertEqual(args.mode, "assistant")
        self.assertEqual(args.paths, ["/memory", "/skills"])


    def test_assistant_mode_selects_native_skill_instead_of_internal_orchestrator(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            installer = Installer(REPO_ROOT, "assistant", root / "memory", [target])
            with (
                patch.object(installer, "_install_skill") as install,
                patch.object(installer, "_remove_managed_skill") as remove,
                patch("builtins.print"),
            ):
                installer._install_skills()
            self.assertEqual(
                [call.args[1] for call in install.call_args_list],
                ["maintain-rightmemory", "maintain-agent-guidance", "maintain-work-skills", "rightmemory-assistant"],
            )
            remove.assert_called_once_with(
                REPO_ROOT / "skills" / "rightmemory-auto-orchestrator-cli" / "SKILL.md",
                "rightmemory-auto-orchestrator", target,
            )

    def test_internal_mode_removes_managed_native_skill(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            installer = Installer(REPO_ROOT, "standalone", root / "memory", [target])
            with (
                patch.object(installer, "_install_skill"),
                patch.object(installer, "_remove_managed_skill") as remove,
                patch("builtins.print"),
            ):
                installer._install_skills()
            remove.assert_any_call(
                REPO_ROOT / "skills" / "rightmemory-assistant" / "SKILL.md",
                "rightmemory-assistant", target,
            )

    def test_mode_switch_replaces_the_current_daily_entry(self):
        for first, second in (("assistant", "standalone"), ("cli-agent", "assistant")):
            with self.subTest(first=first, second=second), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                target = root / "skills"
                with patch("builtins.print"):
                    Installer(REPO_ROOT, first, root / "memory", [target])._install_skills()
                    Installer(REPO_ROOT, second, root / "memory", [target])._install_skills()
                selected = "rightmemory-assistant" if second == "assistant" else "rightmemory-auto-orchestrator"
                previous = "rightmemory-auto-orchestrator" if second == "assistant" else "rightmemory-assistant"
                self.assertTrue((target / selected / "SKILL.md").is_file())
                self.assertFalse((target / previous).exists())

    def test_mode_switch_preserves_a_daily_skill_with_user_resources(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            native = Installer(REPO_ROOT, "assistant", root / "memory", [target])
            internal = Installer(REPO_ROOT, "standalone", root / "memory", [target])
            with patch("builtins.print"):
                native._install_skills()
                notes = target / "rightmemory-assistant" / "notes.txt"
                notes.write_bytes(b"local supporting resource\n")
                internal._install_skills()
            self.assertTrue((target / "rightmemory-assistant" / "SKILL.md").is_file())
            self.assertEqual(notes.read_bytes(), b"local supporting resource\n")

    def test_unfinished_tasks_block_installation_before_mutations(self):
        for mode in ("standalone", "cli-agent", "assistant"):
            for executor, status in (("native", "active"), ("native", "resume"), ("native", "effects_pending"), ("configured", "resume")):
                with self.subTest(mode=mode, executor=executor, status=status), tempfile.TemporaryDirectory() as tempdir:
                    root = Path(tempdir)
                    installer = self._existing_installer(root, mode)
                    before = _snapshot(root)
                    notice = {"domain": "memory", "task_id": "memory-test", "executor": executor, "status": status}
                    with (
                        patch("rightmemory.learning_service.LearningService.notices", return_value=[notice]),
                        patch.object(installer, "_install_runtime") as runtime,
                        patch.object(installer, "_curation_install_boundary") as boundary,
                        patch("builtins.print"),
                    ):
                        with self.assertRaisesRegex(InstallError, "unfinished curation tasks.*memory-test"):
                            installer.run()
                    runtime.assert_not_called()
                    boundary.assert_not_called()
                    self.assertEqual(_snapshot(root), before)

    def test_install_waits_for_configured_task_before_runtime_and_restarts_after_stamp(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            installer = self._existing_installer(root)
            events = []
            active = {"domain": "skills", "task_id": "skills-test", "executor": "configured", "status": "active"}

            @contextmanager
            def boundary():
                events.append("stop-worker")
                yield True
                events.append("release-boundary")

            with (
                patch("rightmemory.learning_service.LearningService.notices", side_effect=[[active], []]),
                patch.object(installer, "_curation_install_boundary", side_effect=boundary),
                patch.object(installer, "_ensure_memory_git"),
                patch.object(installer, "_install_runtime", side_effect=lambda: events.append("install-runtime")),
                patch.object(installer, "_install_skills"),
                patch.object(installer, "_warn_if_command_not_on_path"),
                patch.object(installer, "_write_install_stamp", side_effect=lambda: events.append("stamp")),
                patch.object(installer, "_restart_curation_worker", side_effect=lambda stopped: events.append(("restart", stopped))),
                patch.object(installer, "_print_next_steps"),
                patch("builtins.print"),
            ):
                installer.run()
            self.assertEqual(events, ["stop-worker", "install-runtime", "stamp", "release-boundary", ("restart", True)])

    def test_unfinished_task_after_worker_boundary_blocks_runtime_replacement(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            installer = self._existing_installer(root)
            active = {"domain": "guidance", "task_id": "guidance-test", "executor": "configured", "status": "effects_pending"}

            @contextmanager
            def boundary():
                yield True

            before = _snapshot(root)
            with (
                patch("rightmemory.learning_service.LearningService.notices", return_value=[active]),
                patch.object(installer, "_curation_install_boundary", side_effect=boundary),
                patch.object(installer, "_install_runtime") as runtime,
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(InstallError, "unfinished curation tasks"):
                    installer.run()
            runtime.assert_not_called()
            self.assertEqual(_snapshot(root), before)

    def test_worker_stop_timeout_prevents_runtime_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            installer = self._existing_installer(root)
            before = _snapshot(root)

            @contextmanager
            def timeout():
                raise TimeoutError("worker busy")
                yield

            with (
                patch("rightmemory.learning_service.LearningService.notices", return_value=[]),
                patch("rightmemory.curation_worker.CurationWorker.install_boundary", side_effect=timeout),
                patch.object(installer, "_install_runtime") as runtime,
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(InstallError, "safe stop boundary"):
                    installer.run()
            runtime.assert_not_called()
            self.assertEqual(_snapshot(root), before)

    def test_restart_uses_installed_python_for_stopped_worker_or_ready_candidates(self):
        for was_running, ready in ((True, False), (False, True), (False, False)):
            with self.subTest(was_running=was_running, ready=ready), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                installer = self._existing_installer(root)
                installer.runtime_python = root / "installed-python"
                with (
                    patch("rightmemory.learning_service.LearningService.notices", return_value=[{"domain": "memory", "ready": ready}]),
                    patch("rightmemory.curation_worker.CurationWorker.wake") as wake,
                    patch("builtins.print"),
                ):
                    installer._restart_curation_worker(was_running)
                if was_running or ready:
                    wake.assert_called_once_with(python_executable=installer.runtime_python)
                else:
                    wake.assert_not_called()

    def test_assistant_install_never_starts_configured_worker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            installer = self._existing_installer(Path(tempdir), "assistant")
            with patch("rightmemory.curation_worker.CurationWorker.wake") as wake:
                installer._restart_curation_worker(True)
            wake.assert_not_called()

    def test_invalid_current_graph_blocks_installation_before_mutations(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            installer = self._existing_installer(root)
            (installer.memory_root / "MEMORY.md").write_text("# Missing backing {F#missing}\n", encoding="utf-8")
            before = _snapshot(root)
            with patch.object(installer, "_install_runtime") as runtime, patch("builtins.print"):
                with self.assertRaisesRegex(InstallError, "existing RightMemory root is invalid"):
                    installer.run()
            runtime.assert_not_called()
            self.assertEqual(_snapshot(root), before)


    def test_empty_xdg_data_home_uses_standard_default(self):
        home = Path("test-home")

        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": ""}, clear=False),
            patch("rightmemory.install_core.Path.home", return_value=home),
        ):
            data_home = _posix_data_home()

        self.assertEqual(data_home, home / ".local" / "share")

    def test_nonempty_xdg_data_home_is_respected(self):
        configured = Path("custom-data-home")

        with patch.dict(os.environ, {"XDG_DATA_HOME": str(configured)}, clear=False):
            data_home = _posix_data_home()

        self.assertEqual(data_home, configured)

    def test_default_install_selects_standalone_mode_and_both_skill_targets(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "home"
            with (
                patch.object(install_core, "_verify_required_commands"),
                patch.object(install_core.Path, "home", return_value=home),
                patch.object(install_core, "Installer") as installer_type,
            ):
                result = install_core.main([])

        self.assertEqual(result, 0)
        installer_type.assert_called_once_with(
            REPO_ROOT,
            "standalone",
            home / ".rightmemory",
            [home / ".codex" / "skills", home / ".claude" / "skills"],
        )
        installer_type.return_value.run.assert_called_once_with()

    def test_install_skills_populates_every_selected_target(self):
        for mode in ("standalone", "cli-agent", "assistant"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                targets = [root / "codex-skills", root / "claude-skills"]
                installer = Installer(REPO_ROOT, mode, root / "memory", targets)
                with patch("builtins.print"):
                    installer._install_skills()

                for target in targets:
                    self.assertFalse((target / "rightmemory-schema.md").exists())
                    self.assertFalse((target / "rightmemory-edit-correction-rules.md").exists())
                    expected = {"maintain-rightmemory", "maintain-agent-guidance", "maintain-work-skills"}
                    if mode == "assistant":
                        expected.add("rightmemory-assistant")
                    else:
                        expected.update({"rightmemory-auto-orchestrator", "maintain-pursuit-map", "rightmemory-manager"})
                    self.assertEqual({path.name for path in target.iterdir()}, expected)
                    for skill_name in expected:
                        self.assertTrue((target / skill_name / "SKILL.md").is_file())
                    self.assertFalse((target / "memory-retriever").exists())
                    self.assertFalse((target / "rightmemory-orchestrator").exists())


    def test_install_refuses_learning_only_root_without_mutation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory = root / "memory"
            evidence = memory / "learning" / "experiences" / "case.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_bytes(b"retained work evidence\n")
            before = _snapshot(memory)
            installer = Installer(REPO_ROOT, "assistant", memory, [root / "skills"])
            with patch("builtins.print"), patch.object(installer, "_install_runtime") as install:
                with self.assertRaises(InstallError):
                    installer.run()
            install.assert_not_called()
            self.assertEqual(_snapshot(memory), before)
            self.assertFalse((root / "skills").exists())

    def test_install_refuses_invalid_learning_before_changes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory = root / "memory"
            invalid = memory / "learning" / "candidates" / "invalid.json"
            invalid.parent.mkdir(parents=True)
            invalid.write_bytes(b"unreadable evidence")
            before = _snapshot(memory)
            installer = Installer(REPO_ROOT, "standalone", memory, [root / "skills"])
            with patch("builtins.print"), patch.object(installer, "_install_runtime") as install:
                with self.assertRaisesRegex(InstallError, "learning state is invalid"):
                    installer.run()
            install.assert_not_called()
            self.assertEqual(_snapshot(memory), before)
            self.assertFalse((root / "skills").exists())


    def test_install_skills_preserves_unowned_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            target.mkdir()
            notes = target / "notes.txt"
            notes.write_bytes(b"User-owned notes\n")
            installer = Installer(REPO_ROOT, "standalone", root / "memory", [target])

            with patch("builtins.print"):
                installer._install_skills()

            self.assertEqual(notes.read_bytes(), b"User-owned notes\n")

    def test_existing_install_preserves_semantic_state_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            memory_root.mkdir()
            files = {
                "MEMORY.md": (
                    "# Real Memory {#real-memory}\n\n"
                    "- `user-language` \u7528\u6237\u504f\u597d\u4e2d\u6587\u3002 -> []\n\n"
                    "# Stale Example Application\n"
                ),
                "PURSUITS.md": (
                    "# User Pursuits\n\n## Continue {#continue}\n\nKeep this.\n\n"
                    "# Stale Release Readiness\n"
                ),
            }
            for name, text in files.items():
                (memory_root / name).write_text(text, encoding="utf-8")
            expected = {name: (memory_root / name).read_bytes() for name in files}
            installer = Installer(REPO_ROOT, "cli-agent", memory_root, [root / "skills"])

            with (
                patch.object(installer, "_inspect_target_git", return_value=(False, None)),
                patch.object(installer, "_print_layout"),
                patch.object(installer, "_bootstrap_state") as bootstrap_state,
                patch.object(installer, "_ensure_memory_git"),
                patch.object(installer, "_install_runtime"),
                patch.object(installer, "_install_skills"),
                patch.object(installer, "_warn_if_command_not_on_path"),
                patch.object(installer, "_write_install_stamp"),
                patch.object(installer, "_print_next_steps"),
                patch("builtins.print"),
            ):
                installer.run()

            bootstrap_state.assert_not_called()
            self.assertEqual(
                {name: (memory_root / name).read_bytes() for name in files},
                expected,
            )

    def test_existing_git_root_preserves_gitignore_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            memory_root.mkdir()
            for name in ("MEMORY.md", "PURSUITS.md"):
                (memory_root / name).write_text(f"# {name}\n", encoding="utf-8")
            gitignore = b"*\r\n!MEMORY.md\r\n!custom-control-plane-entry\r\n"
            (memory_root / ".gitignore").write_bytes(gitignore)
            subprocess.run(["git", "init", "-q"], cwd=memory_root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=memory_root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=memory_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "add",
                    "-f",
                    ".gitignore",
                    "MEMORY.md",
                    "PURSUITS.md",
                ],
                cwd=memory_root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "initial memory"],
                cwd=memory_root,
                check=True,
            )
            installer = Installer(REPO_ROOT, "cli-agent", memory_root, [root / "skills"])

            with patch("builtins.print"):
                installer._ensure_memory_git()

            self.assertEqual((memory_root / ".gitignore").read_bytes(), gitignore)
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--short"],
                    cwd=memory_root,
                    check=True,
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                ).stdout,
                "",
            )

    def test_incomplete_existing_root_is_rejected_before_any_install_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            runtime_home = (
                root / "local" / "RightMemory"
                if os.name == "nt"
                else root / "data" / "rightmemory"
            )
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Existing Memory\n", encoding="utf-8")
            (memory_root / ".runtime").mkdir()
            (memory_root / ".runtime" / "install.stamp").write_text("old stamp\n", encoding="utf-8")
            skills_target.mkdir()
            (skills_target / "keep.txt").write_text("skills stay\n", encoding="utf-8")
            runtime_home.mkdir(parents=True)
            (runtime_home / "keep.txt").write_text("runtime stays\n", encoding="utf-8")
            before_memory = _snapshot(memory_root)
            before_skills = _snapshot(skills_target)
            before_runtime = _snapshot(runtime_home)

            with patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(root / "local"),
                    "XDG_DATA_HOME": str(root / "data"),
                },
                clear=False,
            ):
                installer = Installer(REPO_ROOT, "cli-agent", memory_root, [skills_target])
            with (
                patch.object(installer, "_inspect_target_git", return_value=(True, None)),
                patch.object(installer, "_print_layout"),
            ):
                with self.assertRaisesRegex(
                    InstallError,
                    "existing RightMemory root is incomplete: "
                    "missing required files: PURSUITS.md",
                ) as raised:
                    installer.run()

            self.assertIn("installation made no changes", str(raised.exception))
            self.assertEqual(_snapshot(memory_root), before_memory)
            self.assertEqual(_snapshot(skills_target), before_skills)
            self.assertEqual(_snapshot(runtime_home), before_runtime)


if __name__ == "__main__":
    unittest.main()
