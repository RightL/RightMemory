import tempfile
import unittest
from pathlib import Path

from rightmemory.profiles import (
    Profile,
    ProfileError,
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


if __name__ == "__main__":
    unittest.main()
