from __future__ import annotations

from importlib import resources
from pathlib import Path


def build_instructions(memory_root: Path, role: str) -> str:
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    if role not in {"dreamer", "retrieve", "update"}:
        raise ValueError("role must be one of: dreamer, retrieve, update")
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    tool_guidance = _tool_guidance(role)

    return f"""You are RightMemory standalone {role} mode.

Operate only as the {role} role for the user's memory store. Do not blend curator and dreamer responsibilities.

Workspace rule:
- The only allowed root directory is {memory_root}.
- Treat the current working directory as {memory_root}.
- Do not read, write, inspect, or run commands against paths outside {memory_root}.
{tool_guidance}
- Return concise natural-language answers to the caller.

Memory source of truth:
- The root file is MEMORY.md.
- Optional detail files are named MEMORY_<slug>.md.
- The dream report directory is dream_logs/.
- MEMORY.md is normal memory, not a routing-only index.
- Never touch the "# User Pending Task and Thoughts" section.

RightMemory schema:
{schema}

Standalone adaptation:
- Treat the embedded schema above as the schema source of truth. Do not try to read skill or schema files outside {memory_root}; the provided tools only expose the memory root.
- Treat the caller's message as the parent dispatch described by the role instructions below.

Role instructions:
{role_guidance}
"""


def _tool_guidance(role: str) -> str:
    if role == "retrieve":
        return "- Use the provided read-only tools for file listing, search, outline, context reads, and validation."
    return (
        "- Use the provided tools for file search, outline, context reads, Codex-style patches, git inspection, "
        "and validation.\n"
        "- Patch syntax starts with `*** Begin Patch`, uses `*** Update File: path`, `*** Add File: path`, or "
        "`*** Delete File: path`, and ends with `*** End Patch`.\n"
        "- Commit tools may stage and commit only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; ignore "
        "unrelated untracked files unless the caller explicitly asks about them.\n"
        "- Prefer small, reviewable patches over broad rewrites."
    )


def _read_prompt_file(relative_path: str) -> str:
    packaged = resources.files("rightmemory").joinpath(relative_path)
    try:
        return packaged.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass

    source_tree = _repo_root() / relative_path
    if source_tree.exists():
        return source_tree.read_text(encoding="utf-8")
    raise FileNotFoundError(f"required prompt file not found: {packaged} or {source_tree}")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
