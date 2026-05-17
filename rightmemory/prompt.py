from __future__ import annotations

from importlib import resources
from pathlib import Path


ROLE_PROMPTS = {"dreamer", "retrieve", "reviewer", "sync-reconciler", "update"}


def build_instructions(memory_root: Path, role: str) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    command_guidance = _command_guidance(role)
    sync_guidance = _sync_guidance(role)
    tool_guidance = _tool_guidance(role)

    return f"""You are RightMemory standalone {role} mode.

Stay within the command-selected {role} role and its instructions for this memory store.

Command-selected behavior:
{command_guidance}
{sync_guidance}

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
- Treat the caller's message according to the command-selected behavior and the role instructions below.

Role instructions:
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
            "- A caller message may contain one update candidate or a batch of submitted candidates. Treat them "
            "as candidate memory, not final memory text."
        )
    if role == "dreamer":
        return "- The `rightmemory dreamer` command selected dreamer consolidation behavior. Run one consolidation cycle for the memory store."
    if role == "reviewer":
        return (
            "- The automatic transcript review scanner selected reviewer behavior. Treat the normalized session "
            "JSON in the caller message as the review input.\n"
            "- Review the whole normalized session for durable memory."
        )
    if role == "sync-reconciler":
        return (
            "- The sync watcher selected sync reconciliation behavior. Treat the caller message as the current "
            "sync-conflict context for this turn.\n"
            "- Resolve RightMemory sync conflicts by reconciling the memory file set, validating it, committing "
            "the resolved memory state, and calling `sync_push`.\n"
            "- Resolve conflicts by preserving coherent durable memory from both sides when the evidence supports "
            "it, narrowing or marking uncertainty rather than dropping durable information."
        )
    raise ValueError(f"role must be one of: {_role_list()}")


def _sync_guidance(role: str) -> str:
    if role == "retrieve":
        return "- Retrieval uses local memory and does not perform sync preflight by default."
    if role == "sync-reconciler":
        return (
            "- The scheduled sync workflow supplies conflict context in the caller message. If the caller message "
            "contains a Runtime sync context block, treat that block as authoritative for this turn.\n"
            "- When sync is enabled and you commit memory changes, call `sync_push` after the commit. If `sync_push` "
            "reports a conflict, resolve the conflicted memory files in the same role, validate memory, commit the "
            "resolved state, and call `sync_push` again."
        )
    if role in {"dreamer", "reviewer", "update"}:
        return (
            "- If the caller message contains a Runtime sync context block, the runtime already performed sync "
            "preflight for this turn. Treat that block as current at turn start and avoid repeating preflight "
            "discovery. Use sync tools again after your own edits or after a later tool result changes sync state.\n"
            "- When sync is enabled and you commit memory changes, call `sync_push` after the commit. If `sync_push` "
            "reports a conflict, resolve the conflicted memory files in the same role, validate memory, commit the "
            "resolved state, and call `sync_push` again."
        )
    return ""


def _tool_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "- Use the provided read-only tools for `read`, `grep`, `glob`, restricted `read_command`, outline, "
            "and validation.\n"
            "- `read_command` accepts common read-only shell forms such as `cat path`, `sed -n 'X,Yp' path`, "
            "`rg pattern`, `rg --files`, `git status --short`, and `git diff`. It does not run a general shell."
        )
    return (
        "- Use the provided tools for `read`, `grep`, `glob`, restricted `read_command`, outline, exact file "
        "edits, file creation, file deletion, file renames, git inspection, and validation.\n"
        "- `read_command` accepts common read-only shell forms such as `cat path`, `sed -n 'X,Yp' path`, "
        "`rg pattern`, `rg --files`, `git status --short`, and `git diff`. It does not run a general shell.\n"
        "- For edits to existing file content, use `edit_file(path, old_string, new_string, replace_all=false)`. "
        "Read the target file first with `read`, `cat`, or `sed -n`, copy `old_string` from the current file text, "
        "and make it large enough to identify the intended occurrence.\n"
        "- Use `create_file`, `delete_file`, and `rename_file` for file lifecycle changes instead of encoding "
        "those operations as textual replacements.\n"
        "- Commit tools may stage and commit only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; ignore "
        "unrelated untracked files unless the caller explicitly asks about them.\n"
        "- Choose the edit shape that makes memory clearer; create, move, split, merge, or rewrite structure "
        "when that improves the tree or graph."
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


def _role_list() -> str:
    return ", ".join(sorted(ROLE_PROMPTS))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
