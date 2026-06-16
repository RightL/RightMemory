import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.profiles import (
    Profile,
    ProfileError,
    create_profile,
    default_profile_root,
    discover_project_profile,
    load_profiles,
    profile_registry_path,
    resolve_memory_root,
    save_profiles,
    validate_profile_name,
)


class ProfileTests(unittest.TestCase):
    def test_validate_profile_name_accepts_portable_names(self):
        self.assertEqual(validate_profile_name("my-project_1.dev"), "my-project_1.dev")

    def test_validate_profile_name_rejects_paths(self):
        with self.assertRaises(ProfileError):
            validate_profile_name("../project")

    def test_registry_round_trip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "memory"
            root = Path(tempdir) / "profiles" / "alpha"

            save_profiles(home, {"alpha": Profile(name="alpha", root=root)})
            profiles = load_profiles(home)

        self.assertEqual(profiles["alpha"].root, root)

    def test_registry_relative_root_is_resolved_from_registry_parent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "memory"
            home.mkdir()
            (home / "profiles.toml").write_text('[profiles.alpha]\nroot = "profiles/alpha"\n', encoding="utf-8")

            profiles = load_profiles(home)

        self.assertEqual(profiles["alpha"].root, home / "profiles" / "alpha")

    def test_default_profile_root_is_sibling_area(self):
        home = Path("/tmp/rightmemory-home")

        root = default_profile_root(home, "alpha")

        self.assertEqual(root, Path("/tmp/rightmemory-home-profiles/alpha"))

    def test_profile_registry_path_lives_in_default_root(self):
        home = Path("/tmp/rightmemory-home")

        self.assertEqual(profile_registry_path(home), home / "profiles.toml")

    def test_discover_project_profile_walks_upward(self):
        with tempfile.TemporaryDirectory() as tempdir:
            project = Path(tempdir) / "project"
            nested = project / "src" / "pkg"
            nested.mkdir(parents=True)
            binding = project / ".rightmemory-profile"
            binding.write_text("alpha\n", encoding="utf-8")

            result = discover_project_profile(nested)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "alpha")
        self.assertEqual(result.path, binding)

    def test_resolve_memory_root_uses_explicit_profile_first(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "memory"
            project_root = Path(tempdir) / "project-memory"
            save_profiles(home, {"alpha": Profile(name="alpha", root=project_root)})

            resolved = resolve_memory_root(profile_name="alpha", cwd=Path(tempdir), default_root=home, environ={})

        self.assertEqual(resolved.memory_root, project_root)
        self.assertEqual(resolved.profile_name, "alpha")

    def test_resolve_memory_root_uses_project_binding(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "memory"
            profile_root = Path(tempdir) / "profile-root"
            project = Path(tempdir) / "project"
            project.mkdir()
            (project / ".rightmemory-profile").write_text("alpha\n", encoding="utf-8")
            save_profiles(home, {"alpha": Profile(name="alpha", root=profile_root)})

            resolved = resolve_memory_root(profile_name=None, cwd=project, default_root=home, environ={})

        self.assertEqual(resolved.memory_root, profile_root)
        self.assertEqual(resolved.binding_path, project / ".rightmemory-profile")

    def test_resolve_memory_root_uses_environment_without_profile(self):
        with tempfile.TemporaryDirectory() as tempdir:
            env_root = Path(tempdir) / "env-root"

            resolved = resolve_memory_root(
                profile_name=None,
                cwd=Path(tempdir),
                default_root=Path(tempdir) / "default",
                environ={"RIGHTMEMORY_ROOT": str(env_root)},
            )

        self.assertEqual(resolved.memory_root, env_root)
        self.assertIsNone(resolved.profile_name)

    def test_missing_explicit_profile_mentions_create_command(self):
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir) / "memory"

            with self.assertRaises(ProfileError) as caught:
                resolve_memory_root(profile_name="typo", cwd=Path(tempdir), default_root=home, environ={})

        self.assertIn("profile not found: typo", str(caught.exception))
        self.assertIn("rightmemory profile create typo", str(caught.exception))

    def test_create_profile_initializes_separate_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            default_root.mkdir()
            profile = create_profile(default_root, "alpha")

            memory_exists = (profile.root / "MEMORY.md").exists()
            insight_exists = (profile.root / "insight_logs").is_dir()
            runtime_gitignore = (profile.root / ".runtime" / ".gitignore").read_text(encoding="utf-8")
            gitignore = (profile.root / ".gitignore").read_text(encoding="utf-8")
            git_head = self._git(profile.root, "log", "--oneline", "-1")
            profiles = load_profiles(default_root)

        self.assertEqual(profile.root, Path(tempdir) / "default-profiles" / "alpha")
        self.assertTrue(memory_exists)
        self.assertTrue(insight_exists)
        self.assertEqual(runtime_gitignore, "*\n")
        self.assertEqual(
            gitignore,
            "*\n"
            "!MEMORY.md\n"
            "!MEMORY_*.md\n"
            "!shared_views.toml\n"
            "!shared_views/\n"
            "!shared_views/*/\n"
            "!shared_views/*/view.md\n"
            "!shared_views/*/retriever.md\n"
            "!shared_views/*/recipe.toml\n"
            "!shared_views/*/question.toml\n"
            "!shared_views/*/.gitignore\n"
            "!insight_logs/\n"
            "!insight_logs/*.md\n",
        )
        self.assertIn("memory: initial baseline", git_head)
        self.assertEqual(profiles["alpha"].root, profile.root)

    def test_create_profile_registers_existing_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            existing = Path(tempdir) / "existing"
            existing.mkdir()
            (existing / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            (existing / "insight_logs").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=existing, check=True)

            profile = create_profile(default_root, "existing", root=existing)
            profiles = load_profiles(default_root)

        self.assertEqual(profile.root, existing)
        self.assertEqual(profiles["existing"].root, existing)

    def test_create_profile_normalizes_relative_root(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tempdir:
            try:
                os.chdir(tempdir)
                default_root = Path(tempdir) / "default"
                expected_root = Path.cwd() / "profile-memory"

                profile = create_profile(default_root, "relative", root=Path("profile-memory"))
                profiles = load_profiles(default_root)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(profile.root, expected_root)
        self.assertEqual(profiles["relative"].root, expected_root)

    def test_create_profile_rejects_existing_non_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            existing = Path(tempdir) / "not-memory"
            existing.mkdir()
            (existing / "notes.txt").write_text("hello\n", encoding="utf-8")

            with self.assertRaises(ProfileError) as caught:
                create_profile(default_root, "bad", root=existing)

        self.assertIn("does not look like a RightMemory root", str(caught.exception))

    def test_seed_profile_config_copies_executors_and_disables_broad_review_and_sync(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            default_root.mkdir()
            (default_root / "rightmemory.toml").write_text(
                """
                [agent_cli]
                provider = "codex"

                [retrieve.agent_cli]
                model = "gpt-5"

                [update.agent_cli]
                model = "gpt-5"

                [update.async]
                target_batch_candidates = 7

                [dreamer.watch]
                trigger_points = 25

                [sync]
                enabled = true

                [[review.sources]]
                kind = "codex"
                path = "~/.codex/sessions"
                """,
                encoding="utf-8",
            )

            profile = create_profile(default_root, "alpha")
            config_text = (profile.root / "rightmemory.toml").read_text(encoding="utf-8")

        self.assertIn("[agent_cli]", config_text)
        self.assertIn("[retrieve.agent_cli]", config_text)
        self.assertIn("[update.async]", config_text)
        self.assertIn("[dreamer.watch]", config_text)
        self.assertIn("sources = []", config_text)
        self.assertNotIn("[sync]", config_text)
        self.assertNotIn("[[review.sources]]", config_text)

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
