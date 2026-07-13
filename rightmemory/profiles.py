from __future__ import annotations

import os
import re
import subprocess
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .config import MEMORY_ROOT_ENV, default_memory_root
from .semantic_upgrades import baseline_packaged_notes
from .session import _ensure_memory_gitignore, _ensure_runtime_gitignore, _fsync_directory


PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
PROJECT_PROFILE_FILE = ".rightmemory-profile"
PROFILE_REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0
PROFILE_REGISTRY_LOCK_POLL_SECONDS = 0.05


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
        profiles[name] = Profile(name=name, root=_normalize_profile_root(raw_root, base=registry.parent))
    return profiles


def save_profiles(default_root: Path, profiles: dict[str, Profile]) -> None:
    root = Path(default_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory profile registry", ""]
    for name in sorted(profiles):
        profile_name = validate_profile_name(name)
        profile = profiles[name]
        lines.append(f"[profiles.{profile_name}]")
        lines.append(f"root = {_toml_string(str(_normalize_profile_root(profile.root)))}")
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


def create_profile(default_root: Path, name: str, root: Path | None = None) -> Profile:
    profile_name = validate_profile_name(name)
    home = Path(default_root).expanduser()
    target_root = _normalize_profile_root(default_profile_root(home, profile_name) if root is None else root)
    with _profile_registry_lock(home):
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


def remove_profile(default_root: Path, name: str) -> Profile:
    profile_name = validate_profile_name(name)
    home = Path(default_root).expanduser()
    with _profile_registry_lock(home):
        profiles = load_profiles(home)
        profile = profiles.pop(profile_name, None)
        if profile is None:
            raise ProfileError(f"profile not found: {profile_name}")
        save_profiles(home, profiles)
        return profile


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
    _seed_profile_config(source_root=Path(source_root).expanduser(), target_root=root)
    baseline_packaged_notes(root)


def _looks_like_memory_root(path: Path) -> bool:
    return (path / "MEMORY.md").is_file() and (path / "insight_logs").is_dir()


def _normalize_profile_root(path: str | Path, *, base: Path | None = None) -> Path:
    root = Path(path).expanduser()
    if root.is_absolute():
        return root.absolute()
    parent = Path.cwd() if base is None else Path(base).expanduser()
    return (parent / root).absolute()


def _memory_example_text() -> str:
    source_tree_seed = Path(__file__).resolve().parents[1] / "MEMORY.example.md"
    if source_tree_seed.exists():
        return source_tree_seed.read_text(encoding="utf-8")
    return resources.files("rightmemory").joinpath("MEMORY.example.md").read_text(encoding="utf-8")


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


@contextmanager
def _profile_registry_lock(default_root: Path):
    root = Path(default_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = root / "profiles.toml.lock"
    deadline = time.monotonic() + PROFILE_REGISTRY_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ProfileError(f"profile registry is locked: {lock_dir}")
            time.sleep(PROFILE_REGISTRY_LOCK_POLL_SECONDS)
    try:
        (lock_dir / "owner").write_text(str(os.getpid()), encoding="utf-8")
        yield
    finally:
        owner = lock_dir / "owner"
        try:
            owner.unlink()
        except FileNotFoundError:
            pass
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            pass


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


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise ProfileError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result


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
