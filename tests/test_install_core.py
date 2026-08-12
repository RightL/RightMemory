import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory import install_core
from rightmemory.install_core import InstallError, InstallTarget, Installer, _posix_data_home


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
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            targets = [root / "codex-skills", root / "claude-skills"]
            installer = Installer(REPO_ROOT, "standalone", root / "memory", targets)
            for index, target in enumerate(targets):
                target.mkdir()
                current_schema = (
                    REPO_ROOT / "rightmemory" / "reference" / "rightmemory-schema.md"
                ).read_text(encoding="utf-8")
                loose_schema = current_schema
                if index == 1:
                    loose_schema = loose_schema.replace("\n", "\r\n")
                (target / "rightmemory-schema.md").write_bytes(loose_schema.encode("utf-8"))
                (target / "rightmemory-edit-correction-rules.md").write_bytes(
                    (
                        REPO_ROOT
                        / "rightmemory"
                        / "reference"
                        / "RIGHTMEMORY_EDIT_CORRECTION_RULES.md"
                    ).read_bytes()
                )

            current_schema_hash = install_core.sha256(current_schema.encode("utf-8")).hexdigest()
            with (
                patch("builtins.print"),
                patch.dict(
                    install_core.LEGACY_LOOSE_REFERENCE_SHA256,
                    {"rightmemory-schema.md": current_schema_hash},
                ),
            ):
                installer._install_skills()

            for target in targets:
                self.assertFalse((target / "rightmemory-schema.md").exists())
                self.assertFalse((target / "rightmemory-edit-correction-rules.md").exists())
                self.assertTrue((target / "memory-retriever" / "SKILL.md").is_file())
                self.assertTrue((target / "rightmemory-orchestrator" / "SKILL.md").is_file())
                self.assertTrue((target / "rightmemory-auto-orchestrator" / "SKILL.md").is_file())
                maintainer = (target / "maintain-rightmemory" / "SKILL.md")
                self.assertTrue(maintainer.is_file())

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


if __name__ == "__main__":
    unittest.main()
