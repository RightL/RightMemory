from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


MEMORY_ROOT_ENV = "RIGHTMEMORY_ROOT"
MEMORY_ROOT = Path(os.environ.get(MEMORY_ROOT_ENV, "~/.rightmemory")).expanduser()
CONFIG_PATH = MEMORY_ROOT / "rightmemory.toml"
ROLES = {"curator", "dreamer"}
DEFAULT_MAX_TOOL_RETRIES = 10


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    model_id: str
    api_base: str | None = None
    api_key: str | None = None
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    memory_root: Path = MEMORY_ROOT
    max_tool_retries: int = DEFAULT_MAX_TOOL_RETRIES


def load_config(role: str) -> RuntimeConfig:
    role = _role(role)
    data: dict[str, object] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as handle:
            data = tomllib.load(handle)

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, {"curator", "dreamer"}, "top-level")
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
    )


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


def _role(value: str) -> str:
    role = value.strip().lower()
    if role not in ROLES:
        joined = ", ".join(sorted(ROLES))
        raise ValueError(f"role must be one of: {joined}")
    return role
