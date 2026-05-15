from __future__ import annotations

from importlib import resources
from pathlib import Path


def build_instructions(memory_root: Path, role: str) -> str:
    repo_root = _repo_root()
    skills_root = repo_root / "skills"
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    if role in {"retrieve", "update"}:
        skill_path = "skills/memory-curator/SKILL.md"
    elif role == "dreamer":
        skill_path = "skills/memory-dreamer/SKILL.md"
    else:
        raise ValueError("role must be one of: dreamer, retrieve, update")
    role_guidance = _standalone_role_guidance(
        _read_prompt_file(skill_path),
        role=role,
        memory_root=memory_root,
        skills_root=skills_root,
    )
    command_guidance = _command_guidance(role)
    tool_guidance = _tool_guidance(role)

    return f"""You are RightMemory standalone {role} mode.

Operate only as the {role} role for the user's memory store. Do not blend curator and dreamer responsibilities.

Command-selected behavior:
{command_guidance}

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
- Treat the caller's message as the parent dispatch described by the command-selected behavior above.

Role skill:
{role_guidance}
"""


def _command_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "- The `rightmemory retrieve` command selected retrieval. Treat every caller message as a read-only "
            "retrieval request without requiring or expecting a dispatch prefix.\n"
            "- Do not edit memory files or use git write tools in this mode. If the caller asks you to remember "
            "or change memory, ask them to use `rightmemory update`."
        )
    if role == "update":
        return (
            "- The `rightmemory update` command selected updating. Treat every caller message as a read-write "
            "memory update request without requiring or expecting a dispatch prefix.\n"
            "- Apply the curator update-planning, edit-safety, and final-reply rules for every request."
        )
    if role == "dreamer":
        return "- The `rightmemory dreamer` command selected dreamer consolidation behavior."
    raise ValueError("role must be one of: dreamer, retrieve, update")


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


def _standalone_role_guidance(text: str, *, role: str, memory_root: Path, skills_root: Path) -> str:
    if role == "retrieve":
        text = text.replace(
            "- Every dispatch must start with `[RETRIEVE]` or `[UPDATE]`. Reject any dispatch missing this prefix. `[RETRIEVE]` = read-only; `[UPDATE]` = read-write.",
            "- Every dispatch is retrieval because the `rightmemory retrieve` command selected retrieval. Do not require any dispatch prefix. This mode is read-only; reject write requests and tell the caller to use `rightmemory update`.",
        )
    elif role == "update":
        text = text.replace(
            "- Every dispatch must start with `[RETRIEVE]` or `[UPDATE]`. Reject any dispatch missing this prefix. `[RETRIEVE]` = read-only; `[UPDATE]` = read-write.",
            "- Every dispatch is an update because the `rightmemory update` command selected updating. Do not require any dispatch prefix. This mode is read-write and should follow the update rules for each caller message.",
        )
    text = text.replace(
        "- The schema source of truth is `{{SKILLS_ROOT}}/rightmemory-schema.md`. Read it before your first retrieval or edit in a session, and follow it for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.",
        "- The schema source of truth is the embedded RightMemory schema earlier in this prompt. Read that embedded schema before your first retrieval or edit in a session, and follow it for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.",
    )
    text = text.replace(
        "- The schema source of truth is `{{SKILLS_ROOT}}/rightmemory-schema.md`. Read it at the start of every dream cycle and follow it for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.",
        "- The schema source of truth is the embedded RightMemory schema earlier in this prompt. Read that embedded schema at the start of every dream cycle and follow it for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.",
    )
    text = text.replace("`rightmemory-schema.md`", "the embedded schema")
    return (
        text.replace("{{MEMORY_ROOT}}", str(memory_root))
        .replace("{{SKILLS_ROOT}}", str(skills_root))
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
