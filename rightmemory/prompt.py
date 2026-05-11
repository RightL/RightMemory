from __future__ import annotations

from pathlib import Path


def build_instructions(memory_root: Path, role: str) -> str:
    repo_root = _repo_root()
    skills_root = repo_root / "skills"
    schema = _read_repo_file("skills/rightmemory-schema.md")
    if role == "curator":
        skill_path = "skills/memory-curator/SKILL.md"
    elif role == "dreamer":
        skill_path = "skills/memory-dreamer/SKILL.md"
    else:
        raise ValueError("role must be one of: curator, dreamer")
    role_guidance = _standalone_role_guidance(
        _read_repo_file(skill_path),
        memory_root=memory_root,
        skills_root=skills_root,
    )

    return f"""You are RightMemory standalone {role} mode.

Operate only as the {role} role for the user's memory store. Do not blend curator and dreamer responsibilities.

Workspace rule:
- The only allowed root directory is {memory_root}.
- Treat the current working directory as {memory_root}.
- Do not read, write, inspect, or run commands against paths outside {memory_root}.
- Use the provided tools for file search, outline, context reads, Codex-style patches, git inspection, and validation.
- Patch syntax starts with `*** Begin Patch`, uses `*** Update File: path`, `*** Add File: path`, or `*** Delete File: path`, and ends with `*** End Patch`.
- Commit tools may stage and commit only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; ignore unrelated untracked files unless the caller explicitly asks about them.
- Prefer small, reviewable patches over broad rewrites.
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
- Treat the caller's message as the parent dispatch described by the role skill.

Role skill:
{role_guidance}
"""


def _standalone_role_guidance(text: str, memory_root: Path, skills_root: Path) -> str:
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


def _read_repo_file(relative_path: str) -> str:
    path = _repo_root() / relative_path
    if not path.exists():
        raise FileNotFoundError(f"required prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
