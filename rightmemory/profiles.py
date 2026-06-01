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
    current = Path(cwd).absolute()
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
