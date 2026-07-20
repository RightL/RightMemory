from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class UpdateCorrectionResult(BaseModel):
    """Terminal decision returned by the internal update-corrector role."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "no_change", "needs_input"]
    message: str

    @field_validator("message")
    @classmethod
    def _nonempty_message(cls, value: str) -> str:
        message = value.strip()
        if not message:
            raise ValueError("message must not be empty")
        return message


def parse_update_correction_result(value: object) -> UpdateCorrectionResult:
    if isinstance(value, UpdateCorrectionResult):
        return value
    if isinstance(value, str):
        try:
            return UpdateCorrectionResult.model_validate_json(value)
        except Exception as exc:
            raise ValueError(f"update-corrector terminal output is not valid JSON: {exc}") from exc
    try:
        return UpdateCorrectionResult.model_validate(value)
    except Exception as exc:
        raise ValueError(f"invalid update-corrector terminal output: {exc}") from exc


def render_update_correction_result(value: object) -> str:
    return parse_update_correction_result(value).model_dump_json()
