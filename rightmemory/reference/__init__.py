from __future__ import annotations

from importlib import resources
from typing import Literal


ReferenceName = Literal[
    "schema",
    "pursuit",
    "agent-correction",
    "edit-correction",
]

REFERENCE_FILES: dict[ReferenceName, str] = {
    "schema": "rightmemory-schema.md",
    "pursuit": "PURSUIT_RULES.md",
    "agent-correction": "AGENT_CORRECTION_MEMORY_RULES.md",
    "edit-correction": "RIGHTMEMORY_EDIT_CORRECTION_RULES.md",
}


def read_reference(name: ReferenceName) -> str:
    filename = REFERENCE_FILES[name]
    return resources.files(__package__).joinpath(filename).read_text(encoding="utf-8")
