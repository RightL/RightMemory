# RightMemory Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named RightMemory profiles so project work can use separate memory roots without interfering with the default memory root.

**Architecture:** Add a `rightmemory.profiles` module for profile registry, project binding lookup, active-root resolution, and profile-root creation. Refactor config loaders to accept an explicit memory root at call time, then route every CLI command through one resolved active root before loading config or touching runtime state.

**Tech Stack:** Python standard library, `argparse`, `tomllib`, `dataclasses`, Git subprocess calls, existing RightMemory config/watch/session/semantic-upgrade helpers, `unittest`, shell installer tests.

---

## Scope Check

This plan implements one subsystem: named local profiles over separate memory roots. It does not add automatic profile creation from typos, profile-root deletion, hosted sync, team policy for `.rightmemory-profile`, or shared memory across profiles.

## File Structure

- Create `rightmemory/profiles.py`: profile name validation, registry read/write, project binding discovery, root resolution, profile config seeding, and profile-root initialization.
- Modify `rightmemory/config.py`: make every loader accept `memory_root: Path | None = None`; keep existing no-argument behavior compatible with `RIGHTMEMORY_ROOT`.
- Modify `rightmemory/cli.py`: parse global `--profile`, add profile subcommands, pass the resolved active root into config/status/watch/runtime paths.
- Modify `rightmemory/watch.py`: pass the selected root to managed watcher subprocesses through `RIGHTMEMORY_ROOT`.
- Modify `pyproject.toml`: include `MEMORY.example.md` in the installed runtime package so `profile create` can seed roots after install.
- Create `tests/test_profiles.py`: focused registry, resolution, initialization, and config-seeding tests.
- Modify `tests/test_config.py`: explicit-root loader tests plus compatibility coverage for existing patched constants.
- Modify `tests/test_cli.py`: global `--profile`, project binding, profile commands, status, and watch root-selection coverage.
- Modify `tests/test_install.py`: wrapper compatibility coverage.
- Modify `README.md` and `AGENTS.md`: document profiles, root isolation, local project binding, and watch/status behavior.

## Task 1: Config Loaders Accept An Explicit Root

**Files:**
- Modify: `rightmemory/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for explicit-root config loading**

Add these tests to `tests/test_config.py` near the existing config loader tests:

```python
    def test_load_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [retrieve.model]
                model_id = "openai/project"
                """,
                encoding="utf-8",
            )

            config = load_config("retrieve", memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertEqual(config.state_root, root)
        self.assertEqual(config.model_id, "openai/project")

    def test_load_review_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [review]
                sources = []
                """,
                encoding="utf-8",
            )

            config = load_review_config(memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertEqual(config.sources, [])

    def test_load_sync_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [sync]
                enabled = true
                stale_pull_after_hours = 8
                """,
                encoding="utf-8",
            )

            config = load_sync_config(memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertTrue(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 8)
```

- [ ] **Step 2: Run tests and verify the new tests fail**

Run:

```bash
python -m unittest tests.test_config
```

Expected: FAIL with `TypeError` for unexpected `memory_root`.

- [ ] **Step 3: Refactor config root access**

In `rightmemory/config.py`, keep `MEMORY_ROOT_ENV`, `MEMORY_ROOT`, and `CONFIG_PATH` for compatibility, then add helpers:

```python
def default_memory_root() -> Path:
    return Path(os.environ.get(MEMORY_ROOT_ENV, "~/.rightmemory")).expanduser()


def _active_memory_root(memory_root: Path | None) -> Path:
    return MEMORY_ROOT if memory_root is None else Path(memory_root).expanduser()


def _active_config_path(memory_root: Path | None) -> Path:
    if memory_root is None:
        return CONFIG_PATH
    return Path(memory_root).expanduser() / "rightmemory.toml"


def _load_raw_config(memory_root: Path | None = None) -> dict[str, object]:
    config_path = _active_config_path(memory_root)
    if config_path.exists():
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    return {}
```

Change loader signatures and local root variables:

```python
def load_config(role: str, memory_root: Path | None = None) -> RuntimeConfig:
    role = _role(role)
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")
```

Use `root` in every returned config instead of `MEMORY_ROOT`:

```python
    return RuntimeConfig(
        role=role,
        model_id=model_id,
        runtime_mode="standalone",
        api_base=api_base,
        api_key=api_key,
        model_kwargs=model_kwargs,
        memory_root=root,
        state_root=root,
        max_tool_retries=DEFAULT_MAX_TOOL_RETRIES,
        debug_trace=_debug_trace(data.get("debug", {})),
        sync=_sync_config(data.get("sync", {}), memory_root=root),
    )
```

Apply the same pattern to:

```python
def load_review_config(memory_root: Path | None = None) -> ReviewConfig:
def load_async_update_config(memory_root: Path | None = None) -> AsyncUpdateConfig:
def load_dreamer_watch_config(memory_root: Path | None = None) -> DreamerWatchConfig:
def load_insight_watch_config(memory_root: Path | None = None) -> InsightWatchConfig:
def load_pruner_config(memory_root: Path | None = None) -> PrunerConfig:
def load_sync_config(memory_root: Path | None = None) -> SyncConfig:
```

Update `_agent_cli_runtime_config` and `_sync_config` signatures:

```python
def _agent_cli_runtime_config(
    role: str,
    data: dict[str, object],
    role_section: dict[str, object],
    *,
    executor_role: str,
    memory_root: Path,
) -> RuntimeConfig:
```

```python
def _sync_config(section: object, *, memory_root: Path) -> SyncConfig:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[sync] must be a TOML table")
    _reject_unknown_keys(section, {"enabled", "stale_pull_after_hours"}, "[sync]")

    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("[sync].enabled must be a boolean")

    stale_pull_after_hours = section.get("stale_pull_after_hours", DEFAULT_SYNC_STALE_PULL_HOURS)
    if (
        isinstance(stale_pull_after_hours, bool)
        or not isinstance(stale_pull_after_hours, int)
        or stale_pull_after_hours < 1
    ):
        raise ValueError("[sync].stale_pull_after_hours must be a positive integer")

    return SyncConfig(
        memory_root=memory_root,
        enabled=enabled,
        stale_pull_after_hours=stale_pull_after_hours,
    )
```

When formatting the invalid model-section message, compute the active path:

```python
config_path = _active_config_path(memory_root)
raise ValueError(f"{config_path} must contain a [{executor_role}.model] table")
```

- [ ] **Step 4: Run config tests**

Run:

```bash
python -m unittest tests.test_config
```

Expected: PASS.

- [ ] **Step 5: Commit config root refactor**

Run:

```bash
git add rightmemory/config.py tests/test_config.py
git commit -m "refactor: pass memory roots into config loaders"
```

## Task 2: Add Profile Registry And Resolution

**Files:**
- Create: `rightmemory/profiles.py`
- Create: `tests/test_profiles.py`

- [ ] **Step 1: Write failing registry and resolution tests**

Create `tests/test_profiles.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python -m unittest tests.test_profiles
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.profiles'`.

- [ ] **Step 3: Implement `rightmemory/profiles.py` registry and resolution**

Create `rightmemory/profiles.py`:

```python
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import MEMORY_ROOT_ENV, default_memory_root
from .session import _fsync_directory


PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
PROJECT_PROFILE_FILE = ".rightmemory-profile"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    root: Path


@dataclass(frozen=True)
class ProjectProfileBinding:
    name: str
    path: Path


@dataclass(frozen=True)
class ResolvedMemoryRoot:
    memory_root: Path
    default_root: Path
    profile_name: str | None = None
    binding_path: Path | None = None


def validate_profile_name(name: str) -> str:
    value = name.strip()
    if not value:
        raise ProfileError("profile name must not be empty")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ProfileError(f"profile name must be a portable name, got: {name!r}")
    if PROFILE_NAME_PATTERN.fullmatch(value) is None:
        raise ProfileError(f"profile name must contain letters, numbers, '.', '_', or '-': {name!r}")
    return value


def profile_registry_path(default_root: Path) -> Path:
    return Path(default_root).expanduser() / "profiles.toml"


def default_profile_root(default_root: Path, name: str) -> Path:
    profile_name = validate_profile_name(name)
    root = Path(default_root).expanduser()
    return root.with_name(f"{root.name}-profiles") / profile_name


def load_profiles(default_root: Path) -> dict[str, Profile]:
    registry = profile_registry_path(default_root)
    if not registry.exists():
        return {}
    try:
        with registry.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"malformed profile registry: {registry}: {exc}") from exc
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ProfileError(f"{registry} must contain a [profiles] table")
    profiles: dict[str, Profile] = {}
    for raw_name, raw_entry in raw_profiles.items():
        name = validate_profile_name(str(raw_name))
        if not isinstance(raw_entry, dict):
            raise ProfileError(f"[profiles.{name}] must be a TOML table")
        raw_root = raw_entry.get("root")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ProfileError(f"[profiles.{name}].root must be a non-empty string")
        profiles[name] = Profile(name=name, root=Path(raw_root).expanduser())
    return profiles


def save_profiles(default_root: Path, profiles: dict[str, Profile]) -> None:
    root = Path(default_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory profile registry", ""]
    for name in sorted(profiles):
        profile_name = validate_profile_name(name)
        profile = profiles[name]
        lines.append(f"[profiles.{profile_name}]")
        lines.append(f"root = {_toml_string(str(profile.root))}")
        lines.append("")
    _atomic_write_text(profile_registry_path(root), "\n".join(lines).rstrip() + "\n")


def discover_project_profile(cwd: Path) -> ProjectProfileBinding | None:
    current = Path(cwd).resolve()
    for directory in (current, *current.parents):
        binding_path = directory / PROJECT_PROFILE_FILE
        if binding_path.exists():
            name = validate_profile_name(binding_path.read_text(encoding="utf-8").strip())
            return ProjectProfileBinding(name=name, path=binding_path)
    return None


def resolve_memory_root(
    *,
    profile_name: str | None,
    cwd: Path | None = None,
    default_root: Path | None = None,
    environ: dict[str, str] | None = None,
) -> ResolvedMemoryRoot:
    env = os.environ if environ is None else environ
    home = Path(default_root).expanduser() if default_root is not None else default_memory_root()
    workdir = Path.cwd() if cwd is None else Path(cwd)
    selected_name = validate_profile_name(profile_name) if profile_name else None
    binding: ProjectProfileBinding | None = None
    if selected_name is None:
        binding = discover_project_profile(workdir)
        selected_name = binding.name if binding is not None else None
    if selected_name is not None:
        profiles = load_profiles(home)
        profile = profiles.get(selected_name)
        if profile is None:
            location = f" selected by {binding.path}" if binding is not None else ""
            raise ProfileError(
                f"profile not found: {selected_name}{location}. "
                f"Create it with: rightmemory profile create {selected_name}"
            )
        return ResolvedMemoryRoot(
            memory_root=profile.root,
            default_root=home,
            profile_name=selected_name,
            binding_path=binding.path if binding is not None else None,
        )
    env_root = env.get(MEMORY_ROOT_ENV)
    if env_root:
        return ResolvedMemoryRoot(memory_root=Path(env_root).expanduser(), default_root=home)
    return ResolvedMemoryRoot(memory_root=home, default_root=home)


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
```

- [ ] **Step 4: Run profile tests**

Run:

```bash
python -m unittest tests.test_profiles
```

Expected: PASS.

- [ ] **Step 5: Commit profile registry and resolution**

Run:

```bash
git add rightmemory/profiles.py tests/test_profiles.py
git commit -m "feat: add rightmemory profile resolution"
```

## Task 3: Initialize Profile Roots And Seed Safe Config

**Files:**
- Modify: `rightmemory/profiles.py`
- Modify: `tests/test_profiles.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing initialization and config-seeding tests**

Append these tests to `ProfileTests` in `tests/test_profiles.py`:

```python
    def test_create_profile_initializes_separate_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            default_root.mkdir()
            profile = create_profile(default_root, "alpha")

            memory = profile.root / "MEMORY.md"
            runtime_gitignore = profile.root / ".runtime" / ".gitignore"
            gitignore = profile.root / ".gitignore"

        self.assertEqual(profile.root, Path(tempdir) / "default-profiles" / "alpha")
        self.assertTrue(memory.exists())
        self.assertTrue((profile.root / "insight_logs").is_dir())
        self.assertEqual(runtime_gitignore.read_text(encoding="utf-8"), "*\n")
        self.assertIn("!MEMORY.md", gitignore.read_text(encoding="utf-8"))

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
```

Add imports at the top of `tests/test_profiles.py`:

```python
import subprocess

from rightmemory.profiles import create_profile
```

- [ ] **Step 2: Run profile tests and verify failures**

Run:

```bash
python -m unittest tests.test_profiles
```

Expected: FAIL with `ImportError` or `AttributeError` for `create_profile`.

- [ ] **Step 3: Include the memory seed in installed packages**

Modify `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"skills" = "rightmemory/skills"
"MEMORY.example.md" = "rightmemory/MEMORY.example.md"
```

- [ ] **Step 4: Implement root initialization and config seeding**

Extend `rightmemory/profiles.py` with:

```python
import subprocess
from importlib import resources
from typing import Any

from .semantic_upgrades import baseline_packaged_notes
from .session import _ensure_memory_gitignore, _ensure_runtime_gitignore
```

Add creation helpers:

```python
def create_profile(default_root: Path, name: str, root: Path | None = None) -> Profile:
    profile_name = validate_profile_name(name)
    home = Path(default_root).expanduser()
    target_root = default_profile_root(home, profile_name) if root is None else Path(root).expanduser()
    profiles = load_profiles(home)
    if profile_name in profiles:
        raise ProfileError(f"profile already exists: {profile_name}")
    if target_root.exists() and not _looks_like_memory_root(target_root):
        raise ProfileError(f"{target_root} does not look like a RightMemory root")
    if not target_root.exists():
        initialize_memory_root(target_root, source_root=home)
    profiles[profile_name] = Profile(name=profile_name, root=target_root)
    save_profiles(home, profiles)
    return profiles[profile_name]


def initialize_memory_root(memory_root: Path, *, source_root: Path) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "insight_logs").mkdir(exist_ok=True)
    if not (root / "MEMORY.md").exists():
        (root / "MEMORY.md").write_text(_memory_example_text(), encoding="utf-8")
    _ensure_memory_gitignore(root)
    _ensure_runtime_gitignore(root / ".runtime")
    _ensure_git_repo(root)
    _ensure_git_author(root)
    _ensure_initial_memory_commit(root)
    _seed_profile_config(source_root=source_root, target_root=root)
    baseline_packaged_notes(root)


def _looks_like_memory_root(path: Path) -> bool:
    return (path / "MEMORY.md").is_file() and (path / "insight_logs").is_dir()


def _memory_example_text() -> str:
    source_tree_seed = Path(__file__).resolve().parents[1] / "MEMORY.example.md"
    if source_tree_seed.exists():
        return source_tree_seed.read_text(encoding="utf-8")
    return resources.files("rightmemory").joinpath("MEMORY.example.md").read_text(encoding="utf-8")
```

Add Git helpers:

```python
def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ProfileError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result


def _ensure_git_repo(root: Path) -> None:
    if (root / ".git").is_dir():
        return
    _git(root, "init", "-q")


def _ensure_git_author(root: Path) -> None:
    name = _git(root, "config", "--local", "--get", "user.name", check=False).stdout.strip()
    email = _git(root, "config", "--local", "--get", "user.email", check=False).stdout.strip()
    if not name:
        global_name = _git(root, "config", "--global", "--get", "user.name", check=False).stdout.strip()
        _git(root, "config", "--local", "user.name", global_name or "RightMemory")
    if not email:
        global_email = _git(root, "config", "--global", "--get", "user.email", check=False).stdout.strip()
        _git(root, "config", "--local", "user.email", global_email or "rightmemory@localhost")


def _ensure_initial_memory_commit(root: Path) -> None:
    if _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode == 0:
        return
    memory_files = ["MEMORY.md"]
    memory_files.extend(sorted(path.name for path in root.glob("MEMORY_*.md")))
    insight_files = [path.relative_to(root).as_posix() for path in sorted((root / "insight_logs").glob("*.md"))]
    staged = [*memory_files, *insight_files]
    _git(root, "add", "--", *staged)
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    _git(root, "commit", "-q", "-m", "memory: initial baseline", "--", *staged)
```

Add a small TOML writer and config seeding:

```python
def _seed_profile_config(*, source_root: Path, target_root: Path) -> None:
    source_config = source_root / "rightmemory.toml"
    target_config = target_root / "rightmemory.toml"
    if target_config.exists():
        return
    data: dict[str, Any] = {}
    if source_config.exists():
        with source_config.open("rb") as handle:
            raw = tomllib.load(handle)
        data = _profile_seed_config(raw)
    data.setdefault("review", {})["sources"] = []
    target_config.write_text(_dump_toml(data), encoding="utf-8")


def _profile_seed_config(raw: dict[str, Any]) -> dict[str, Any]:
    seeded: dict[str, Any] = {}
    if isinstance(raw.get("agent_cli"), dict):
        seeded["agent_cli"] = dict(raw["agent_cli"])
    for role in ("retrieve", "update", "dreamer", "insight", "reviewer", "pruner", "historian", "sync-reconciler"):
        section = raw.get(role)
        if not isinstance(section, dict):
            continue
        copied: dict[str, Any] = {}
        for key in ("model", "agent_cli", "async", "watch", "generation_commits", "revival_grace_checkpoints"):
            value = section.get(key)
            if value is not None:
                copied[key] = value
        if copied:
            seeded[role] = copied
    review = raw.get("review")
    if isinstance(review, dict):
        seeded["review"] = {key: value for key, value in review.items() if key != "sources"}
    return seeded
```

Implement `_dump_toml` for RightMemory's supported config values:

```python
def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _write_toml_table(lines, [], data)
    return "\n".join(lines).rstrip() + "\n"


def _write_toml_table(lines: list[str], prefix: list[str], data: dict[str, Any]) -> None:
    scalar_items: list[tuple[str, Any]] = []
    table_items: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(data):
        value = data[key]
        if isinstance(value, dict):
            table_items.append((key, value))
        else:
            scalar_items.append((key, value))
    if prefix:
        lines.append(f"[{'.'.join(prefix)}]")
    for key, value in scalar_items:
        lines.append(f"{key} = {_toml_value(value)}")
    if scalar_items:
        lines.append("")
    for key, value in table_items:
        _write_toml_table(lines, [*prefix, key], value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{key} = {_toml_value(value[key])}" for key in sorted(value))
        return "{ " + inner + " }"
    raise ProfileError(f"unsupported config value for profile seed: {value!r}")
```

- [ ] **Step 5: Run profile tests**

Run:

```bash
python -m unittest tests.test_profiles
```

Expected: PASS.

- [ ] **Step 6: Commit profile creation**

Run:

```bash
git add pyproject.toml rightmemory/profiles.py tests/test_profiles.py
git commit -m "feat: create isolated rightmemory profiles"
```

## Task 4: Add CLI Profile Commands And Global Selection

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI selection tests**

Add tests to `tests/test_cli.py` near the JSON request tests:

```python
    def test_main_global_profile_selects_registered_root(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "project-memory"
            profile_root.mkdir(parents=True)
            default_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
                encoding="utf-8",
            )

            def fake_load_config(role, memory_root=None):
                self.assertEqual(memory_root, profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("sys.stdout", stdout),
            ):
                result = main(["--profile", "alpha", "retrieve", "--session", "s1", "hello"])

        self.assertEqual(result, 0)
        self.assertIn("session s1: hello", stdout.getvalue())

    def test_main_project_binding_selects_registered_root(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            project = Path(tempdir) / "project"
            project.mkdir()
            default_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
                encoding="utf-8",
            )
            (project / ".rightmemory-profile").write_text("alpha\n", encoding="utf-8")

            def fake_load_config(role, memory_root=None):
                self.assertEqual(memory_root, profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
                patch("sys.stdout", stdout),
            ):
                result = main(["retrieve", "--session", "s1", "hello"])

        self.assertEqual(result, 0)
        self.assertIn("session s1: hello", stdout.getvalue())

    def test_profile_list_ignores_project_binding(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            project = Path(tempdir) / "project"
            project.mkdir()
            default_root.mkdir()
            (project / ".rightmemory-profile").write_text("missing\n", encoding="utf-8")
            (default_root / "profiles.toml").write_text(
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
                encoding="utf-8",
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("sys.stdout", stdout),
            ):
                result = main(["profile", "list"])

        self.assertEqual(result, 0)
        self.assertIn(f"alpha\t{profile_root}", stdout.getvalue())

    def test_profile_create_calls_create_profile(self):
        stdout = io.StringIO()
        profile = type("Profile", (), {"name": "alpha", "root": Path("/profiles/alpha")})()

        with (
            patch("rightmemory.cli.default_memory_root", return_value=Path("/default")),
            patch("rightmemory.cli.create_profile", return_value=profile) as create_profile,
            patch("sys.stdout", stdout),
        ):
            result = main(["profile", "create", "alpha", "--root", "/profiles/alpha"])

        self.assertEqual(result, 0)
        create_profile.assert_called_once_with(Path("/default"), "alpha", root=Path("/profiles/alpha"))
        self.assertIn("alpha\t/profiles/alpha", stdout.getvalue())

    def test_profile_command_rejects_global_profile_flag(self):
        with self.assertRaises(ValueError) as caught:
            main(["--profile", "alpha", "profile", "list"])

        self.assertIn("--profile is for runtime commands", str(caught.exception))

    def test_top_level_help_does_not_resolve_project_binding(self):
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            project = Path(tempdir) / "project"
            project.mkdir()
            (project / ".rightmemory-profile").write_text("missing\n", encoding="utf-8")

            with (
                patch("rightmemory.cli.Path.cwd", return_value=project),
                patch("rightmemory.cli.resolve_memory_root", side_effect=AssertionError("root should not resolve")),
                patch("sys.stdout", stdout),
            ):
                with self.assertRaises(SystemExit) as caught:
                    main(["--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("RightMemory", stdout.getvalue())
```

- [ ] **Step 2: Run CLI tests and verify failures**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: FAIL because `--profile` and `profile` commands are not wired.

- [ ] **Step 3: Add imports and global parse helper**

In `rightmemory/cli.py`, replace the config import of `MEMORY_ROOT` with explicit root helpers:

```python
from .config import (
    ROLES,
    default_memory_root,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_insight_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
from .profiles import (
    ProfileError,
    create_profile,
    load_profiles,
    resolve_memory_root,
    save_profiles,
    validate_profile_name,
)
```

Add:

```python
def _parse_global_args(argv: list[str]) -> tuple[str | None, list[str]]:
    parser = argparse.ArgumentParser(prog="rightmemory", add_help=False)
    parser.add_argument("--profile")
    namespace, remaining = parser.parse_known_args(argv)
    return namespace.profile, remaining
```

Start `main` with:

```python
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    profile_name, argv = _parse_global_args(argv)
    if argv and argv[0] == "profile":
        if profile_name is not None:
            raise ValueError("--profile is for runtime commands, not profile management")
        return _profile_main(argv[1:])
    if not argv:
        _top_level_parser().print_help()
        return 0
    if argv[0] in {"-h", "--help"}:
        _top_level_parser().parse_args(argv)
        return 0
    active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
    memory_root = active.memory_root
```

Then pass `memory_root` to every dispatcher that needs it:

```python
if argv and argv[0] == "watch":
    return _watch_manager_main(argv[1:], memory_root)
if argv and argv[0] == "review":
    return _review_main(argv[1:], memory_root)
if argv and argv[0] == "sync":
    return _sync_main(argv[1:], memory_root)
if argv and argv[0] == "doctor":
    return _doctor_main(argv[1:], memory_root)
if argv and argv[0] == "status":
    return _status_main(argv[1:], memory_root)
if argv and argv[0] == "prune":
    return _prune_main(argv[1:], memory_root)
if argv and argv[0] == "history":
    return _history_main(argv[1:], memory_root)
config = load_config(args.role, memory_root=memory_root)
```

Add `_top_level_parser` so help stays root-free:

```python
def _top_level_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory", description="RightMemory memory runtime")
    parser.add_argument("--profile", help="named memory profile for runtime commands")
    parser.add_argument("role", nargs="?", choices=tuple(sorted(ROLES)), help="RightMemory runtime role")
    return parser
```

- [ ] **Step 4: Add profile management parser**

Add to `rightmemory/cli.py`:

```python
def _profile_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory profile")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    create = subparsers.add_parser("create")
    create.add_argument("name")
    create.add_argument("--root", type=Path)
    show = subparsers.add_parser("show")
    show.add_argument("name")
    remove = subparsers.add_parser("remove")
    remove.add_argument("name")
    args = parser.parse_args(argv)
    home = default_memory_root()
    if args.command == "list":
        profiles = load_profiles(home)
        for name in sorted(profiles):
            print(f"{name}\t{profiles[name].root}")
        return 0
    if args.command == "create":
        profile = create_profile(home, args.name, root=args.root)
        print(f"{profile.name}\t{profile.root}")
        return 0
    if args.command == "show":
        name = validate_profile_name(args.name)
        profiles = load_profiles(home)
        if name not in profiles:
            raise ProfileError(f"profile not found: {name}. Create it with: rightmemory profile create {name}")
        print(f"{name}\t{profiles[name].root}")
        return 0
    if args.command == "remove":
        name = validate_profile_name(args.name)
        profiles = load_profiles(home)
        profile = profiles.pop(name, None)
        if profile is None:
            raise ProfileError(f"profile not found: {name}")
        save_profiles(home, profiles)
        print(f"removed {name}; memory root remains at {profile.root}")
        return 0
    raise ValueError(f"unknown profile command: {args.command}")
```

- [ ] **Step 5: Update command helpers to accept `memory_root`**

Change signatures and call sites:

```python
def _watch_manager_main(argv: list[str], memory_root: Path) -> int:
def _watch_start(target: str, memory_root: Path) -> int:
def _watch_stop(target: str, timeout: int, memory_root: Path) -> int:
def _watch_status(target: str, memory_root: Path) -> int:
def _review_main(argv: list[str], memory_root: Path) -> int:
def _status_main(argv: list[str], memory_root: Path) -> int:
def _prune_main(argv: list[str], memory_root: Path) -> int:
def _sync_main(argv: list[str], memory_root: Path) -> int:
def _doctor_main(argv: list[str], memory_root: Path) -> int:
def _history_main(argv: list[str], memory_root: Path) -> int:
```

For loaders inside those helpers, pass the root:

```python
reviewer_config = load_config("reviewer", memory_root=memory_root)
review_config = load_review_config(memory_root=memory_root)
sync_config = load_sync_config(memory_root=memory_root)
pruner_config = load_pruner_config(memory_root=memory_root)
```

For status:

```python
def _status_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory status")
    parser.parse_args(argv)
    print(format_status_dashboard(collect_status(memory_root)))
    return 0
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: PASS.

- [ ] **Step 7: Commit CLI profile surface**

Run:

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: add rightmemory profile cli"
```

## Task 5: Make Managed Watchers Inherit The Selected Root

**Files:**
- Modify: `rightmemory/watch.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing watcher env tests**

Add this test near the managed watch tests in `tests/test_cli.py`:

```python
    def test_watch_start_passes_selected_profile_root_to_subprocess_env(self):
        stdout = io.StringIO()
        events = []

        class FakeProcess:
            pid = 501

        def fake_popen(command, **kwargs):
            events.append((command, kwargs["env"]["RIGHTMEMORY_ROOT"], kwargs["cwd"]))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tempdir:
            default_root = Path(tempdir) / "default"
            profile_root = Path(tempdir) / "profile-root"
            default_root.mkdir()
            profile_root.mkdir()
            (default_root / "profiles.toml").write_text(
                f'[profiles.alpha]\nroot = "{profile_root}"\n',
                encoding="utf-8",
            )

            def fake_load_config(role, memory_root=None):
                self.assertEqual(memory_root, profile_root)
                return type("Config", (), {"memory_root": profile_root})()

            def fake_load_sync_config(memory_root=None):
                self.assertEqual(memory_root, profile_root)
                return type("SyncConfig", (), {"memory_root": profile_root, "enabled": False})()

            with (
                patch("rightmemory.cli.default_memory_root", return_value=default_root),
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.IsolatedWriteSupervisor.cleanup_stale", return_value=None),
                patch("rightmemory.watch.subprocess.Popen", side_effect=fake_popen),
                patch("sys.stdout", stdout),
            ):
                result = main(["--profile", "alpha", "watch", "start", "review"])

        self.assertEqual(result, 0)
        self.assertEqual(events[0][1], str(profile_root))
        self.assertEqual(events[0][2], str(profile_root))
        self.assertIn("review: running pid 501", stdout.getvalue())
```

- [ ] **Step 2: Run the watcher test and verify failure**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_watch_start_passes_selected_profile_root_to_subprocess_env
```

Expected: FAIL because `Popen` has no `env` entry.

- [ ] **Step 3: Pass `RIGHTMEMORY_ROOT` to watch subprocesses**

In `rightmemory/watch.py`, import the env name:

```python
from .config import MEMORY_ROOT_ENV
```

Update `start_managed_watch`:

```python
    command = [python_executable or sys.executable, "-m", "rightmemory.cli", *WATCH_COMMANDS[name]]
    env = {**os.environ, MEMORY_ROOT_ENV: str(memory_root)}
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=str(memory_root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: PASS.

- [ ] **Step 5: Commit watcher env inheritance**

Run:

```bash
git add rightmemory/watch.py tests/test_cli.py
git commit -m "fix: run managed watchers in selected memory root"
```

## Task 6: Documentation, Installer Compatibility, And Agent Notes

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `tests/test_install.py`

- [ ] **Step 1: Add installer compatibility test**

Add this assertion to `tests/test_install.py` in `test_cli_agent_installs_command_backed_orchestrator_without_role_skills` after reading the installed wrapper:

```python
            wrapper = (root / "home" / ".local" / "bin" / "rightmemory").read_text(encoding="utf-8")
```

Then assert:

```python
        self.assertIn('export RIGHTMEMORY_ROOT="', wrapper)
        self.assertIn('exec "', wrapper)
        self.assertIn(' -m rightmemory.cli "$@"', wrapper)
```

This verifies the wrapper still forwards arbitrary CLI args, including `--profile`, while seeding the no-profile root.

- [ ] **Step 2: Run installer test and verify behavior**

Run:

```bash
python -m unittest tests.test_install.InstallScriptTests.test_cli_agent_installs_command_backed_orchestrator_without_role_skills
```

Expected: PASS.

- [ ] **Step 3: Update README**

In `README.md`, update the install/runtime sections with concise profile docs:

```markdown
### Profiles

The default memory root is still `~/.rightmemory`, or `RIGHTMEMORY_ROOT` when set.
Named profiles let a project use a separate memory root:

```bash
rightmemory profile create my-project
rightmemory --profile my-project retrieve "what do we know about this repo?"
```

Profile aliases live in `<default-memory-root>/profiles.toml`. New profile roots
default to a sibling profile-root area, such as `~/.rightmemory-profiles/my-project`
for the normal default root. Each profile root has its own `MEMORY.md`,
`rightmemory.toml`, `.runtime/`, Git history, watcher state, async update queues,
sessions, and insight logs.

A project can opt into a local default profile by adding `.rightmemory-profile`
with the profile name:

```text
my-project
```

Tracking or ignoring that file is a user/project decision. Use `--profile` when
you want an explicit override for one command.
```

Update the watcher/status sections to show:

```bash
rightmemory --profile my-project watch start
rightmemory --profile my-project status
```

State that managed watcher pid files and logs live under the selected profile root.

- [ ] **Step 4: Update AGENTS.md**

Add a concise runtime note:

```markdown
- Named profiles are registered in `<default-memory-root>/profiles.toml`.
  `rightmemory profile create <name>` defaults new roots to a sibling profile
  area such as `~/.rightmemory-profiles/<name>` for the normal default root.
- Runtime commands can select a profile with `--profile <name>`, or by a
  user-managed `.rightmemory-profile` file in the project tree. Tracking that
  file is a user/project choice.
- Profile roots are ordinary memory roots with separate `MEMORY.md`,
  `rightmemory.toml`, `.runtime/`, Git history, watcher state, async queues,
  sessions, and insight logs.
```

- [ ] **Step 5: Run docs-related tests**

Run:

```bash
python -m unittest tests.test_install
```

Expected: PASS.

- [ ] **Step 6: Commit docs and installer coverage**

Run:

```bash
git add README.md AGENTS.md tests/test_install.py
git commit -m "docs: document rightmemory profiles"
```

## Task 7: Full Verification And Polish

**Files:**
- Review: all touched files

- [ ] **Step 1: Run focused profile/config/CLI tests**

Run:

```bash
python -m unittest tests.test_profiles tests.test_config tests.test_cli tests.test_install
```

Expected: PASS.

- [ ] **Step 2: Run syntax checks**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 3: Run the full suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD
git diff --check
```

Expected: working tree contains the intended uncommitted changes for the last task, and `git diff --check` reports no whitespace errors.

- [ ] **Step 5: Final commit if needed**

If Task 7 found polish changes, commit them:

```bash
git add rightmemory tests README.md AGENTS.md pyproject.toml
git commit -m "test: verify rightmemory profiles"
```

Expected: commit succeeds, or no commit is needed because prior task commits already cover all changes.

## Self-Review Notes

Spec coverage is represented by tasks:

- Registry under default root: Task 2.
- Sibling profile-root default: Task 2 and Task 3.
- No auto-create on typo: Task 2.
- Explicit `--profile` and project binding: Task 4.
- Profile commands: Task 4.
- Root initialization and safe config seeding: Task 3.
- Watch/status selected-root behavior: Task 4 and Task 5.
- Wrapper compatibility: Task 6.
- Docs and AGENTS updates: Task 6.

The plan avoids moving existing roots, deleting profile roots, copying sync enablement, or enabling broad transcript review in project profiles. It keeps `RIGHTMEMORY_ROOT` behavior compatible for no-profile commands while making selected profiles explicit at CLI resolution time.
