import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rightmemory import install_core
from rightmemory.install_core import InstallError, InstallTarget, Installer, _posix_data_home
from rightmemory.update_queue import UpdateCandidate, UpdateQueueStore


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
            for mode, suffix in (("standalone", ""), ("cli-agent", "[codex-sdk]")):
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
        for mode in ("standalone", "cli-agent"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                targets = [root / "codex-skills", root / "claude-skills"]
                installer = Installer(REPO_ROOT, mode, root / "memory", targets)
                with patch("builtins.print"):
                    installer._install_skills()

                for target in targets:
                    self.assertFalse((target / "rightmemory-schema.md").exists())
                    self.assertFalse((target / "rightmemory-edit-correction-rules.md").exists())
                    for skill_name in (
                        "rightmemory-auto-orchestrator",
                        "maintain-rightmemory",
                        "review-agent-guidance-inbox",
                        "maintain-pursuit-map",
                    ):
                        self.assertTrue((target / skill_name / "SKILL.md").is_file())
                    self.assertFalse((target / "memory-retriever").exists())
                    self.assertFalse((target / "rightmemory-orchestrator").exists())

    def test_install_skills_replaces_managed_variants_and_keeps_independent_skills(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            target.mkdir()
            installer = Installer(REPO_ROOT, "standalone", root / "memory", [target])
            previous_defaults = (
                ("memory-retriever-cli", "memory-retriever"),
                ("rightmemory-orchestrator-cli", "rightmemory-orchestrator"),
                ("maintain-rightmemory", "maintain-rightmemory"),
                ("review-agent-guidance-inbox", "review-agent-guidance-inbox"),
            )
            with patch("builtins.print"):
                for source_directory, skill_name in previous_defaults:
                    installer._install_skill(
                        REPO_ROOT / "skills" / source_directory / "SKILL.md",
                        skill_name,
                        target,
                    )
            modified = target / "memory-retriever" / "SKILL.md"
            modified.write_text(
                "User-owned file.\n",
                encoding="utf-8",
            )

            with patch("builtins.print"):
                installer._install_skills()

            self.assertTrue(modified.is_file())
            self.assertFalse((target / "rightmemory-orchestrator").exists())
            self.assertTrue((target / "maintain-rightmemory" / "SKILL.md").is_file())
            self.assertTrue((target / "review-agent-guidance-inbox" / "SKILL.md").is_file())
            self.assertTrue((target / "maintain-pursuit-map" / "SKILL.md").is_file())
            self.assertTrue((target / "rightmemory-auto-orchestrator" / "SKILL.md").is_file())

    def test_remove_old_loose_reference_accepts_managed_bytes_and_known_line_endings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            canonical = root / "canonical.bin"
            canonical.write_bytes(b"managed-data\n")
            installer = Installer(REPO_ROOT, "standalone", root / "memory", [root / "skills"])
            for index, content in enumerate((b"managed-data\n", b"managed-data\r\n")):
                target = root / f"target-{index}"
                target.mkdir()
                legacy = target / "rightmemory-schema.md"
                legacy.write_bytes(content)
                with (
                    patch("builtins.print"),
                    patch.dict(
                        install_core.LEGACY_LOOSE_REFERENCE_SHA256,
                        {"rightmemory-schema.md": install_core.sha256(b"managed-data\n").hexdigest()},
                    ),
                ):
                    installer._remove_old_loose_reference(target, legacy.name, canonical)
                self.assertFalse(legacy.exists())

    def test_install_skills_preserves_modified_loose_reference(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            target = root / "skills"
            target.mkdir()
            legacy = target / "rightmemory-schema.md"
            legacy.write_text("# User-owned reference\n", encoding="utf-8")
            installer = Installer(REPO_ROOT, "standalone", root / "memory", [target])

            with patch("builtins.print"):
                installer._install_skills()

            self.assertEqual(legacy.read_text(encoding="utf-8"), "# User-owned reference\n")

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
                patch.object(installer, "_run_semantic_upgrades") as semantic_upgrades,
                patch.object(installer, "_install_skills"),
                patch.object(installer, "_warn_if_command_not_on_path"),
                patch.object(installer, "_write_install_stamp"),
                patch.object(installer, "_print_next_steps"),
                patch("builtins.print"),
            ):
                installer.run()

            bootstrap_state.assert_not_called()
            target = semantic_upgrades.call_args.args[0]
            self.assertEqual(target.kind, "existing")
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

    def test_existing_root_with_legacy_references_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"
            memory_root.mkdir()
            (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (memory_root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
            for name in ("PURSUIT_RULES.md", "AGENT_CORRECTION_MEMORY_RULES.md"):
                (memory_root / name).write_text(f"# Custom {name}\n", encoding="utf-8")
            before = _snapshot(memory_root)
            installer = Installer(REPO_ROOT, "cli-agent", memory_root, [skills_target])

            with (
                patch.object(installer, "_inspect_target_git", return_value=(True, None)),
                patch.object(installer, "_print_layout"),
            ):
                with self.assertRaisesRegex(
                    InstallError,
                    "legacy package-reference files: "
                    "PURSUIT_RULES.md, AGENT_CORRECTION_MEMORY_RULES.md",
                ) as raised:
                    installer.run()

            self.assertIn("remove them explicitly", str(raised.exception))
            self.assertEqual(_snapshot(memory_root), before)
            self.assertFalse(skills_target.exists())

    def test_semantic_upgrade_failure_prevents_success_stamp_and_remaining_install(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            installer = Installer(REPO_ROOT, "cli-agent", memory_root, [root / "skills"])
            installer.runtime_python = Path("fake-python")
            target = InstallTarget("new", False, (), ())
            failure = subprocess.CompletedProcess(["fake-python"], 7)

            with (
                patch.object(installer, "_inspect_target", return_value=target),
                patch.object(installer, "_print_layout"),
                patch.object(installer, "_bootstrap_state"),
                patch.object(installer, "_ensure_memory_git"),
                patch.object(installer, "_install_runtime"),
                patch.object(installer, "_install_skills") as install_skills,
                patch.object(installer, "_write_install_stamp") as write_install_stamp,
                patch.object(install_core, "_run", return_value=failure),
            ):
                with self.assertRaisesRegex(
                    InstallError,
                    "semantic upgrade baseline failed with status 7",
                ):
                    installer.run()

            install_skills.assert_not_called()
            write_install_stamp.assert_not_called()
            self.assertFalse((memory_root / ".runtime" / "install.stamp").exists())

    def test_install_quiesces_worker_before_runtime_and_restarts_after_stamp(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            installer = Installer(REPO_ROOT, "cli-agent", root / "memory", [root / "skills"])
            target = InstallTarget("new", False, (), ())
            events = []

            @contextmanager
            def install_boundary():
                events.append("quiesce")
                yield True

            with (
                patch.object(installer, "_inspect_target", return_value=target),
                patch.object(installer, "_print_layout"),
                patch.object(
                    installer,
                    "_async_update_install_boundary",
                    side_effect=install_boundary,
                ),
                patch.object(installer, "_bootstrap_state"),
                patch.object(installer, "_ensure_memory_git"),
                patch.object(
                    installer,
                    "_install_runtime",
                    side_effect=lambda: events.append("install-runtime"),
                ),
                patch.object(installer, "_run_semantic_upgrades"),
                patch.object(installer, "_install_skills"),
                patch.object(installer, "_warn_if_command_not_on_path"),
                patch.object(
                    installer,
                    "_write_install_stamp",
                    side_effect=lambda: events.append("stamp"),
                ),
                patch.object(
                    installer,
                    "_restart_async_update_worker",
                    side_effect=lambda was_running: events.append(f"restart:{was_running}"),
                ),
                patch.object(installer, "_print_next_steps"),
            ):
                installer.run()

        self.assertEqual(
            events,
            ["quiesce", "install-runtime", "stamp", "restart:True"],
        )

    def test_install_refuses_before_runtime_change_when_worker_cannot_stop(self):
        with tempfile.TemporaryDirectory() as tempdir:
            installer = Installer(
                REPO_ROOT,
                "cli-agent",
                Path(tempdir) / "memory",
                [Path(tempdir) / "skills"],
            )

            with patch(
                "rightmemory.install_core.AsyncUpdateStore.runtime_install_locked",
                side_effect=TimeoutError("busy"),
            ):
                with self.assertRaisesRegex(InstallError, "did not reach a safe stop boundary"):
                    with installer._async_update_install_boundary():
                        pass

    def test_pending_synchronized_queue_starts_newly_installed_worker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            candidate = UpdateCandidate(
                uid="a" * 32,
                session_id="agent-session",
                display_id=1,
                message="pending synchronized evidence",
                submitted_at="2026-08-18T09:00:00+00:00",
            )
            UpdateQueueStore(memory_root).write_candidate(candidate)
            installer = Installer(REPO_ROOT, "cli-agent", memory_root, [root / "skills"])
            installer.runtime_python = root / "runtime" / "python"

            with patch("rightmemory.install_core.AsyncUpdateStore.wake_worker") as wake_worker:
                installer._restart_async_update_worker(False)

        wake_worker.assert_called_once_with(python_executable=installer.runtime_python)


if __name__ == "__main__":
    unittest.main()
