from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
import tomllib


MEMORY_ROOT_ENV = "RIGHTMEMORY_ROOT"
MEMORY_ROOT = Path(os.environ.get(MEMORY_ROOT_ENV, "~/.rightmemory")).expanduser()
_STATE_ROOT_UNSET = cast(Path, object())
CONFIG_PATH = MEMORY_ROOT / "rightmemory.toml"
ROLES = {
    "dreamer",
    "historian",
    "insight",
    "pruner",
    "retrieve",
    "reviewer",
    "shared-view-builder",
    "sync-reconciler",
    "update",
}
MODEL_FALLBACK_ROLES = (
    "update",
    "dreamer",
    "insight",
    "reviewer",
    "pruner",
    "sync-reconciler",
    "historian",
    "shared-view-builder",
)
DEFAULT_MAX_TOOL_RETRIES = 10
DEFAULT_RETRIEVE_MAX_OUTPUT_CHARS = 100_000
DEFAULT_REVIEW_IDLE_SECONDS = 6 * 60 * 60
DEFAULT_REVIEW_SINCE_DAYS = 3
DEFAULT_REVIEW_BATCH_SIZE = 3
DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES = 15
DEFAULT_UPDATE_MAX_WAIT_SECONDS = 24 * 60 * 60
DEFAULT_SYNC_STALE_PULL_HOURS = 24
DEFAULT_PRUNER_GENERATION_COMMITS = 70
DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS = 2
DEFAULT_DREAMER_TRIGGER_POINTS = 50.0
DEFAULT_DREAMER_UPDATE_CANDIDATE_POINTS = 1.0
DEFAULT_DREAMER_CHECK_INTERVAL_SECONDS = 3000
DEFAULT_INSIGHT_TRIGGER_POINTS = 150.0
DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS = 1.0
DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS = 3000


def default_memory_root() -> Path:
    return Path(os.environ.get(MEMORY_ROOT_ENV, "~/.rightmemory")).expanduser()


def _active_memory_root(memory_root: Path | None) -> Path:
    return MEMORY_ROOT if memory_root is None else Path(memory_root).expanduser()


def _active_config_path(memory_root: Path | None) -> Path:
    if memory_root is None:
        return CONFIG_PATH
    return Path(memory_root).expanduser() / "rightmemory.toml"


@dataclass(frozen=True)
class SyncConfig:
    memory_root: Path = MEMORY_ROOT
    enabled: bool = False
    stale_pull_after_hours: int = DEFAULT_SYNC_STALE_PULL_HOURS


@dataclass(frozen=True)
class ReviewSourceConfig:
    kind: str
    path: Path


@dataclass(frozen=True)
class ReviewConfig:
    memory_root: Path = MEMORY_ROOT
    idle_seconds: int = DEFAULT_REVIEW_IDLE_SECONDS
    since_days: int = DEFAULT_REVIEW_SINCE_DAYS
    batch_size: int = DEFAULT_REVIEW_BATCH_SIZE
    sources: list[ReviewSourceConfig] = field(default_factory=list)


@dataclass(frozen=True)
class AsyncUpdateConfig:
    memory_root: Path = MEMORY_ROOT
    target_batch_candidates: int = DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES
    max_wait_seconds: int = DEFAULT_UPDATE_MAX_WAIT_SECONDS


@dataclass(frozen=True)
class DreamerWatchConfig:
    memory_root: Path = MEMORY_ROOT
    trigger_points: float = DEFAULT_DREAMER_TRIGGER_POINTS
    update_candidate_points: float = DEFAULT_DREAMER_UPDATE_CANDIDATE_POINTS
    check_interval_seconds: int = DEFAULT_DREAMER_CHECK_INTERVAL_SECONDS


@dataclass(frozen=True)
class InsightWatchConfig:
    memory_root: Path = MEMORY_ROOT
    trigger_points: float = DEFAULT_INSIGHT_TRIGGER_POINTS
    update_candidate_points: float = DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS
    check_interval_seconds: int = DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS


@dataclass(frozen=True)
class PrunerConfig:
    memory_root: Path = MEMORY_ROOT
    generation_commits: int = DEFAULT_PRUNER_GENERATION_COMMITS
    revival_grace_checkpoints: int = DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS


@dataclass(frozen=True)
class AgentCliConfig:
    provider: str
    model: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    model_id: str | None = None
    runtime_mode: str = "standalone"
    agent_cli: AgentCliConfig | None = None
    api_base: str | None = None
    api_key: str | None = None
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    memory_root: Path = MEMORY_ROOT
    state_root: Path = _STATE_ROOT_UNSET
    max_tool_retries: int = DEFAULT_MAX_TOOL_RETRIES
    retrieve_max_output_chars: int = DEFAULT_RETRIEVE_MAX_OUTPUT_CHARS
    debug_trace: bool = False
    sync: SyncConfig = field(default_factory=SyncConfig)
    fresh_provider_session: bool = False

    def __post_init__(self) -> None:
        if self.state_root is _STATE_ROOT_UNSET:
            object.__setattr__(self, "state_root", self.memory_root)


def load_config(role: str, memory_root: Path | None = None) -> RuntimeConfig:
    role = _role(role)
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    role_section = _role_section(data, role)
    executor_role = role
    if role != "retrieve" and not _has_executor_config(role_section):
        inherited = _fallback_executor_section(data, role)
        if inherited is not None:
            executor_role, role_section = inherited

    has_model = "model" in role_section
    has_agent_cli = "agent_cli" in role_section
    if has_model and has_agent_cli:
        raise ValueError(
            f"[{executor_role}] must not define both [{executor_role}.model] and [{executor_role}.agent_cli]"
        )

    model_section = role_section.get("model") if has_model else None
    if model_section is None:
        return _agent_cli_runtime_config(
            role,
            data,
            role_section,
            executor_role=executor_role,
            memory_root=root,
        )
    if not isinstance(model_section, dict):
        config_path = _active_config_path(memory_root)
        raise ValueError(f"{config_path} must contain a [{executor_role}.model] table")
    _reject_unknown_keys(model_section, {"model_id", "api_base", "api_key", "kwargs"}, f"[{executor_role}.model]")

    model_id = _required_string(model_section, "model_id")
    api_base = _optional_string(model_section, "api_base")
    api_key = _optional_string(model_section, "api_key")
    model_kwargs = _model_kwargs(model_section.get("kwargs", {}))
    reserved_kwargs = {"model_id", "api_base", "api_key"}
    collisions = reserved_kwargs.intersection(model_kwargs)
    if collisions:
        joined = ", ".join(sorted(collisions))
        raise ValueError(f"[model.kwargs] must not redefine reserved model config keys: {joined}")

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
        retrieve_max_output_chars=_retrieve_max_output_chars(role, role_section),
        debug_trace=_debug_trace(data.get("debug", {})),
        sync=_sync_config(data.get("sync", {}), memory_root=root),
    )


def load_review_config(memory_root: Path | None = None) -> ReviewConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("review", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[review] must be a TOML table")
    _reject_unknown_keys(section, {"idle_seconds", "since_days", "batch_size", "sources"}, "[review]")

    idle_seconds = section.get("idle_seconds", DEFAULT_REVIEW_IDLE_SECONDS)
    if not isinstance(idle_seconds, int) or idle_seconds < 1:
        raise ValueError("[review].idle_seconds must be a positive integer")

    since_days = section.get("since_days", DEFAULT_REVIEW_SINCE_DAYS)
    if not isinstance(since_days, int) or since_days < 1:
        raise ValueError("[review].since_days must be a positive integer")

    batch_size = section.get("batch_size", DEFAULT_REVIEW_BATCH_SIZE)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("[review].batch_size must be a positive integer")

    raw_sources = section.get("sources")
    if raw_sources is None:
        sources = _default_review_sources()
    else:
        if not isinstance(raw_sources, list):
            raise ValueError("[[review.sources]] must be an array of tables")
        sources = [_review_source(item) for item in raw_sources]

    return ReviewConfig(
        memory_root=root,
        idle_seconds=idle_seconds,
        since_days=since_days,
        batch_size=batch_size,
        sources=sources,
    )


def load_async_update_config(memory_root: Path | None = None) -> AsyncUpdateConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("update", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[update] must be a TOML table")
    _reject_unknown_keys(section, {"model", "agent_cli", "async"}, "[update]")

    async_section = section.get("async", {})
    if async_section is None:
        async_section = {}
    if not isinstance(async_section, dict):
        raise ValueError("[update.async] must be a TOML table")
    _reject_unknown_keys(async_section, {"target_batch_candidates", "max_wait_seconds"}, "[update.async]")

    return AsyncUpdateConfig(
        memory_root=root,
        target_batch_candidates=_positive_integer(
            async_section,
            "target_batch_candidates",
            DEFAULT_UPDATE_TARGET_BATCH_CANDIDATES,
            "[update.async]",
        ),
        max_wait_seconds=_positive_integer(
            async_section,
            "max_wait_seconds",
            DEFAULT_UPDATE_MAX_WAIT_SECONDS,
            "[update.async]",
        ),
    )


def load_dreamer_watch_config(memory_root: Path | None = None) -> DreamerWatchConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("dreamer", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[dreamer] must be a TOML table")
    _reject_unknown_keys(section, {"model", "agent_cli", "watch"}, "[dreamer]")

    watch = section.get("watch", {})
    if watch is None:
        watch = {}
    if not isinstance(watch, dict):
        raise ValueError("[dreamer.watch] must be a TOML table")
    _reject_unknown_keys(
        watch,
        {"trigger_points", "update_candidate_points", "review_session_points", "check_interval_seconds"},
        "[dreamer.watch]",
    )
    if "review_session_points" in watch:
        # Keep older user config loadable, but transcript extraction no longer creates pressure directly.
        _positive_number(watch, "review_session_points", 1.0, "[dreamer.watch]")

    return DreamerWatchConfig(
        memory_root=root,
        trigger_points=_positive_number(
            watch,
            "trigger_points",
            DEFAULT_DREAMER_TRIGGER_POINTS,
            "[dreamer.watch]",
        ),
        update_candidate_points=_positive_number(
            watch,
            "update_candidate_points",
            DEFAULT_DREAMER_UPDATE_CANDIDATE_POINTS,
            "[dreamer.watch]",
        ),
        check_interval_seconds=_positive_integer(
            watch,
            "check_interval_seconds",
            DEFAULT_DREAMER_CHECK_INTERVAL_SECONDS,
            "[dreamer.watch]",
        ),
    )


def load_insight_watch_config(memory_root: Path | None = None) -> InsightWatchConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("insight", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[insight] must be a TOML table")
    _reject_unknown_keys(section, {"model", "agent_cli", "watch"}, "[insight]")

    watch = section.get("watch", {})
    if watch is None:
        watch = {}
    if not isinstance(watch, dict):
        raise ValueError("[insight.watch] must be a TOML table")
    _reject_unknown_keys(
        watch,
        {"trigger_points", "update_candidate_points", "review_session_points", "check_interval_seconds"},
        "[insight.watch]",
    )
    if "review_session_points" in watch:
        # Keep older user config loadable, but transcript extraction no longer creates pressure directly.
        _positive_number(watch, "review_session_points", 1.0, "[insight.watch]")

    return InsightWatchConfig(
        memory_root=root,
        trigger_points=_positive_number(
            watch,
            "trigger_points",
            DEFAULT_INSIGHT_TRIGGER_POINTS,
            "[insight.watch]",
        ),
        update_candidate_points=_positive_number(
            watch,
            "update_candidate_points",
            DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS,
            "[insight.watch]",
        ),
        check_interval_seconds=_positive_integer(
            watch,
            "check_interval_seconds",
            DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS,
            "[insight.watch]",
        ),
    )


def load_pruner_config(memory_root: Path | None = None) -> PrunerConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)

    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("pruner", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[pruner] must be a TOML table")
    _reject_unknown_keys(
        section,
        {"model", "agent_cli", "generation_commits", "revival_grace_checkpoints"},
        "[pruner]",
    )

    return PrunerConfig(
        memory_root=root,
        generation_commits=_positive_integer(
            section,
            "generation_commits",
            DEFAULT_PRUNER_GENERATION_COMMITS,
            "[pruner]",
        ),
        revival_grace_checkpoints=_positive_integer(
            section,
            "revival_grace_checkpoints",
            DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS,
            "[pruner]",
        ),
    )


def load_sync_config(memory_root: Path | None = None) -> SyncConfig:
    root = _active_memory_root(memory_root)
    data = _load_raw_config(memory_root)
    if not root.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")
    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    return _sync_config(data.get("sync", {}), memory_root=root)


def _load_raw_config(memory_root: Path | None = None) -> dict[str, object]:
    config_path = _active_config_path(memory_root)
    if config_path.exists():
        with config_path.open("rb") as handle:
            return tomllib.load(handle)
    return {}


def _reject_unknown_keys(data: dict[str, object], allowed: set[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported {context} config key(s): {joined}")


def _top_level_keys() -> set[str]:
    return {*ROLES, "agent_cli", "review", "debug", "sync"}


def _role_section(data: dict[str, object], role: str) -> dict[str, object]:
    section = data.get(role, {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError(f"[{role}] must be a TOML table")
    _reject_unknown_keys(section, _allowed_role_keys(role), f"[{role}]")
    return section


def _allowed_role_keys(role: str) -> set[str]:
    allowed = {"model", "agent_cli"}
    if role in {"dreamer", "insight"}:
        allowed.add("watch")
    if role == "pruner":
        allowed.update({"generation_commits", "revival_grace_checkpoints"})
    if role == "update":
        allowed.add("async")
    if role == "retrieve":
        allowed.add("max_output_chars")
    return allowed


def _has_executor_config(section: dict[str, object]) -> bool:
    return "model" in section or "agent_cli" in section


def _fallback_executor_section(data: dict[str, object], role: str) -> tuple[str, dict[str, object]] | None:
    for candidate in MODEL_FALLBACK_ROLES:
        if candidate == role:
            continue
        section = _role_section(data, candidate)
        if _has_executor_config(section):
            return candidate, section
    return None


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[model].{key} must be a non-empty string")
    return value.strip()


def _optional_string(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[model].{key} must be a non-empty string when set")
    return value.strip()


def _model_kwargs(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("[model.kwargs] must be a TOML table")
    return dict(value)


def _positive_number(data: dict[str, object], key: str, default: float, context: str) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{context}.{key} must be a positive number")
    return float(value)


def _positive_integer(data: dict[str, object], key: str, default: int, context: str) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{context}.{key} must be a positive integer")
    return value


def _agent_cli_runtime_config(
    role: str,
    data: dict[str, object],
    role_section: dict[str, object],
    *,
    executor_role: str,
    memory_root: Path,
) -> RuntimeConfig:
    global_section = data.get("agent_cli", {})
    if global_section is None:
        global_section = {}
    if not isinstance(global_section, dict):
        raise ValueError("[agent_cli] must be a TOML table")
    _reject_unknown_keys(global_section, {"provider"}, "[agent_cli]")

    role_cli = role_section.get("agent_cli", {})
    if role_cli is None:
        role_cli = {}
    if not isinstance(role_cli, dict):
        raise ValueError(f"[{executor_role}.agent_cli] must be a TOML table")
    _reject_unknown_keys(role_cli, {"provider", "model"}, f"[{executor_role}.agent_cli]")

    provider = role_cli.get("provider", global_section.get("provider"))
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(f"[agent_cli].provider or [{executor_role}.agent_cli].provider must be a non-empty string")
    provider = provider.strip()
    if provider not in {"codex", "claude"}:
        raise ValueError("agent_cli provider must be one of: claude, codex")

    model = _optional_agent_cli_model(executor_role, role_cli.get("model"))
    return RuntimeConfig(
        role=role,
        model_id=None,
        runtime_mode="cli-agent",
        agent_cli=AgentCliConfig(provider=provider, model=model),
        memory_root=memory_root,
        state_root=memory_root,
        max_tool_retries=DEFAULT_MAX_TOOL_RETRIES,
        retrieve_max_output_chars=_retrieve_max_output_chars(role, role_section),
        debug_trace=_debug_trace(data.get("debug", {})),
        sync=_sync_config(data.get("sync", {}), memory_root=memory_root),
    )


def _optional_agent_cli_model(role: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[{role}.agent_cli].model must be a non-empty string when set")
    return value.strip()


def _retrieve_max_output_chars(role: str, role_section: dict[str, object]) -> int:
    if role != "retrieve":
        return DEFAULT_RETRIEVE_MAX_OUTPUT_CHARS
    return _positive_integer(
        role_section,
        "max_output_chars",
        DEFAULT_RETRIEVE_MAX_OUTPUT_CHARS,
        "[retrieve]",
    )


def _debug_trace(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, dict):
        raise ValueError("[debug] must be a TOML table")
    _reject_unknown_keys(value, {"trace"}, "[debug]")
    trace = value.get("trace", False)
    if not isinstance(trace, bool):
        raise ValueError("[debug].trace must be a boolean")
    return trace


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


def _role(value: str) -> str:
    role = value.strip().lower()
    if role not in ROLES:
        joined = ", ".join(sorted(ROLES))
        raise ValueError(f"role must be one of: {joined}")
    return role


def _review_source(data: object) -> ReviewSourceConfig:
    if not isinstance(data, dict):
        raise ValueError("[[review.sources]] entries must be TOML tables")
    _reject_unknown_keys(data, {"kind", "path"}, "[[review.sources]]")
    kind = _required_string(data, "kind").lower()
    if kind not in {"codex", "claude"}:
        raise ValueError("[[review.sources]].kind must be one of: codex, claude")
    path = _required_string(data, "path")
    return ReviewSourceConfig(kind=kind, path=Path(path).expanduser())


def _default_review_sources() -> list[ReviewSourceConfig]:
    home = Path.home()
    return [
        ReviewSourceConfig(kind="codex", path=home / ".codex" / "sessions"),
        ReviewSourceConfig(kind="claude", path=home / ".claude" / "projects"),
    ]
