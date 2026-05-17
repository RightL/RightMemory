from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


MEMORY_ROOT_ENV = "RIGHTMEMORY_ROOT"
MEMORY_ROOT = Path(os.environ.get(MEMORY_ROOT_ENV, "~/.rightmemory")).expanduser()
CONFIG_PATH = MEMORY_ROOT / "rightmemory.toml"
ROLES = {"dreamer", "retrieve", "reviewer", "update"}
DEFAULT_MAX_TOOL_RETRIES = 10
DEFAULT_REVIEW_IDLE_SECONDS = 3600
DEFAULT_REVIEW_SINCE_DAYS = 3


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    model_id: str
    api_base: str | None = None
    api_key: str | None = None
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    memory_root: Path = MEMORY_ROOT
    max_tool_retries: int = DEFAULT_MAX_TOOL_RETRIES
    debug_trace: bool = False


@dataclass(frozen=True)
class ReviewSourceConfig:
    kind: str
    path: Path


@dataclass(frozen=True)
class ReviewConfig:
    memory_root: Path = MEMORY_ROOT
    idle_seconds: int = DEFAULT_REVIEW_IDLE_SECONDS
    since_days: int = DEFAULT_REVIEW_SINCE_DAYS
    sources: list[ReviewSourceConfig] = field(default_factory=list)


def load_config(role: str) -> RuntimeConfig:
    role = _role(role)
    data = _load_raw_config()

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, {*ROLES, "review", "debug"}, "top-level")
    role_section = data.get(role)
    if not isinstance(role_section, dict):
        raise ValueError(f"{CONFIG_PATH} must contain a [{role}.model] table")
    _reject_unknown_keys(role_section, {"model"}, f"[{role}]")

    model_section = role_section.get("model")
    if not isinstance(model_section, dict):
        raise ValueError(f"{CONFIG_PATH} must contain a [{role}.model] table")
    _reject_unknown_keys(model_section, {"model_id", "api_base", "api_key", "kwargs"}, f"[{role}.model]")

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
        api_base=api_base,
        api_key=api_key,
        model_kwargs=model_kwargs,
        memory_root=MEMORY_ROOT,
        max_tool_retries=DEFAULT_MAX_TOOL_RETRIES,
        debug_trace=_debug_trace(data.get("debug", {})),
    )


def load_review_config() -> ReviewConfig:
    data = _load_raw_config()

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, {*ROLES, "review", "debug"}, "top-level")
    section = data.get("review", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[review] must be a TOML table")
    _reject_unknown_keys(section, {"idle_seconds", "since_days", "sources"}, "[review]")

    idle_seconds = section.get("idle_seconds", DEFAULT_REVIEW_IDLE_SECONDS)
    if not isinstance(idle_seconds, int) or idle_seconds < 1:
        raise ValueError("[review].idle_seconds must be a positive integer")

    since_days = section.get("since_days", DEFAULT_REVIEW_SINCE_DAYS)
    if not isinstance(since_days, int) or since_days < 1:
        raise ValueError("[review].since_days must be a positive integer")

    raw_sources = section.get("sources")
    if raw_sources is None:
        sources = _default_review_sources()
    else:
        if not isinstance(raw_sources, list):
            raise ValueError("[[review.sources]] must be an array of tables")
        sources = [_review_source(item) for item in raw_sources]

    return ReviewConfig(memory_root=MEMORY_ROOT, idle_seconds=idle_seconds, since_days=since_days, sources=sources)


def _load_raw_config() -> dict[str, object]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as handle:
            return tomllib.load(handle)
    return {}


def _reject_unknown_keys(data: dict[str, object], allowed: set[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported {context} config key(s): {joined}")


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
