from __future__ import annotations

from pathlib import Path
from typing import Any


def ok_response(
    message: str,
    data: dict[str, Any] | None = None,
    *,
    warnings: list[str] | None = None,
    paths: list[str | Path] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "message": message,
        "data": data or {},
        "warnings": warnings or [],
        "paths": [str(path) for path in (paths or [])],
    }


def error_detail(message: str, *, technical: str | None = None, next_step: str | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {"message": message}
    if technical:
        detail["technical"] = technical
    if next_step:
        detail["next_step"] = next_step
    return detail
