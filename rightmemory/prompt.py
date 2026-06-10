from __future__ import annotations

from importlib import resources
from pathlib import Path
from shlex import quote

from .config import MEMORY_ROOT_ENV
from .semantic_upgrades import SemanticUpgradeContext, render_prompt_context


ROLE_PROMPTS = {"dreamer", "historian", "insight", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"}


def build_cli_agent_instructions(
    memory_root: Path,
    role: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    cli_agent_guidance = _cli_agent_guidance(memory_root, role)
    semantic_guidance = _semantic_upgrade_guidance(role, semantic_upgrades)

    return f"""You are RightMemory {role} mode.

Work in the configured memory root. The configured memory root is {memory_root}.

Memory store:
- MEMORY.md
- MEMORY_*.md
- insight_logs/

Follow the canonical role instructions below. Use the embedded schema as the schema source of truth.
Return a concise final reply.
{cli_agent_guidance}

RightMemory schema:
{schema}
{semantic_guidance}

Role instructions:
{role_guidance}
"""


def build_instructions(
    memory_root: Path,
    role: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    command_guidance = _command_guidance(role)
    sync_guidance = _sync_guidance(role)
    tool_guidance = _tool_guidance(role)
    semantic_guidance = _semantic_upgrade_guidance(role, semantic_upgrades)

    return f"""You are RightMemory standalone {role} mode.

Stay within the command-selected {role} role and its instructions for this memory store.

Command-selected behavior:
{command_guidance}
{sync_guidance}

Workspace rule:
- The provided tools are rooted at the RightMemory memory store.
- Use memory-store-relative paths such as `MEMORY.md`, `MEMORY_*.md`, and `insight_logs/*.md` when they are allowed for the selected role.
- Do not read, write, inspect, or run commands against paths outside the memory store.
{tool_guidance}
- Return concise natural-language answers to the caller.

Memory source of truth:
- The root file is MEMORY.md.
- Optional detail files are named MEMORY_<slug>.md.
- Insight logs are stored under insight_logs/.
- MEMORY.md is normal memory, not a routing-only index.

RightMemory schema:
{schema}

Standalone adaptation:
- Treat the embedded schema above as the schema source of truth. Do not try to read skill or schema files outside the memory store; the provided tools only expose that store.
- Treat the caller's message according to the command-selected behavior and the role instructions below.
{semantic_guidance}

Role instructions:
{role_guidance}
"""


def _semantic_upgrade_guidance(role: str, semantic_upgrades: SemanticUpgradeContext | None) -> str:
    if role != "dreamer" or semantic_upgrades is None:
        return ""
    rendered = render_prompt_context(semantic_upgrades)
    if not rendered:
        return ""
    return f"\nSemantic upgrade context:\n{rendered}\n"


def _cli_agent_guidance(memory_root: Path, role: str) -> str:
    if role == "retrieve":
        root_env = f"{MEMORY_ROOT_ENV}={quote(str(memory_root))}"
        return (
            "\nCLI-agent adaptation:\n"
            "- For a strongly relevant `M#` heading, retrieve external shared context with "
            f"`{root_env} rightmemory shared-view retrieve <heading-id> <query>` using the local heading id and caller query.\n"
        )
    return ""


def _command_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "- The `rightmemory retrieve` command selected retrieval. Treat every caller message as a read-only "
            "retrieval request without requiring or expecting a dispatch prefix.\n"
            "- Do not edit memory files or use git write tools in this mode. If the caller asks you to remember "
            "or change memory, ask them to use `rightmemory update`."
        )
    if role == "historian":
        return (
            "- The `rightmemory history` command selected historical retrieval. Treat every caller message as a "
            "read-only archaeology request over pruned memory and Git history.\n"
            "- Return historical matches as historical or pruned memory, not active memory. Do not edit memory files."
        )
    if role == "update":
        return (
            "- The `rightmemory update` command selected updating. Treat every caller message as a read-write "
            "memory update request without requiring or expecting a dispatch prefix.\n"
            "- A caller message may contain one update candidate or a batch of submitted candidates. Treat them "
            "as candidate memory, not final memory text."
        )
    if role == "pruner":
        return (
            "- The `rightmemory prune` command selected active-memory pruning. Treat the caller message as the "
            "current prune generation context.\n"
            "- Edit memory files when the supplied generation context says pruning is due."
        )
    if role == "dreamer":
        return (
            "- The `rightmemory dreamer` command selected dreamer consolidation behavior. Run one consolidation cycle for the memory store.\n"
            "- Treat the caller message as an optional operator hint, not as the ordinary source of truth."
        )
    if role == "insight":
        return (
            "- The `rightmemory insight` command selected insight behavior. Run one reflection cycle for the memory store.\n"
            "- Treat the caller message as an optional operator hint, not as the ordinary source of truth."
        )
    if role == "reviewer":
        return (
            "- The automatic transcript review scanner selected reviewer behavior. Treat the normalized transcript "
            "batch JSON in the caller message as the review input.\n"
            "- Review the ordered batch for durable memory."
        )
    if role == "sync-reconciler":
        return (
            "- Runtime selected memory reconciliation behavior. Treat the caller message as the current repair "
            "context for this turn.\n"
            "- Repair RightMemory dirty or conflicted memory state by reconciling the memory file set, validating "
            "it, committing the repaired memory state, and calling `sync_push` when sync is enabled.\n"
            "- Preserve coherent durable memory when the evidence supports it, narrowing or marking uncertainty "
            "rather than dropping durable information."
        )
    raise ValueError(f"role must be one of: {_role_list()}")


def _sync_guidance(role: str) -> str:
    if role in {"historian", "retrieve"}:
        return "- Retrieval uses local memory and does not perform sync preflight by default."
    if role == "sync-reconciler":
        return (
            "- The runtime supplies repair context in the caller message. If the caller message "
            "contains a Runtime sync context block, treat that block as authoritative for this turn.\n"
            "- When sync is enabled and you commit memory changes, call `sync_push` after the commit. If `sync_push` "
            "reports dirty state or a conflict, repair the supplied memory files in the same role, validate memory, "
            "commit the repaired state, and call `sync_push` again."
        )
    if role in {"dreamer", "insight", "pruner", "reviewer", "update"}:
        return ""
    return ""


def _tool_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "- Use the provided read-only tools for `read`, `grep`, `glob`, restricted `read_command`, outline, "
            "validation, and `retrieve_shared_view`.\n"
            "- Use `retrieve_shared_view(heading_id, query)` when a relevant `M#` heading points to an external "
            "shared view endpoint. Pass the local heading id and the caller's query.\n"
            "- Treat shared-view results as external context with their own provenance and freshness. Do not read "
            "external shared-view Markdown as local memory.\n"
            "- `read_command` accepts common read-only shell forms such as `cat path`, `sed -n 'X,Yp' path`, "
            "`rg pattern`, `rg --files`, `git status --short`, and `git diff`. It does not run a general shell."
        )
    if role == "historian":
        return (
            "- Use the provided read-only tools for `read`, `grep`, `glob`, restricted `read_command`, outline, "
            "validation, `git_log`, and `git_show_file`.\n"
            "- Use `git_log` to inspect `prune:` commit ledgers and `git_show_file` to recover memory snapshots."
        )
    if role == "insight":
        return (
            "- Use the provided tools for memory-root reads, Insight log creation or refinement, git inspection, and committing Insight logs.\n"
            "- Commit tools are scoped to `insight_logs/*.md`; keep active memory and unrelated files out of Insight commits.\n"
            "- Do not run memory validation; Insight does not edit the memory graph."
        )
    guidance = (
        "- Use the provided tools for `read`, `grep`, `glob`, restricted `read_command`, outline, exact file "
        "edits, file creation, file deletion, file renames, git inspection, and validation.\n"
        "- Use `validate_memory` to run a graph and schema sanity pass before finishing memory edits.\n"
        "- `read_command` accepts common read-only shell forms such as `cat path`, `sed -n 'X,Yp' path`, "
        "`rg pattern`, `rg --files`, `git status --short`, and `git diff`. It does not run a general shell.\n"
        "- For edits to existing file content, use `edit_file(path, old_string, new_string, replace_all=false)`. "
        "Read the target file first with `read`, `cat`, or `sed -n`, copy `old_string` from the current file text, "
        "and make it large enough to identify the intended occurrence.\n"
        "- Use `create_file`, `delete_file`, and `rename_file` for file lifecycle changes instead of encoding "
        "those operations as textual replacements.\n"
        "- Choose the edit shape that makes memory clearer; create, move, split, merge, or rewrite structure "
        "when that improves the tree or graph."
    )
    if role == "pruner":
        guidance += (
            "\n- Use `git_log` to inspect previous `prune:` ledgers and `git_show_file` to compare boundary snapshots.\n"
            "- Use `git_commit(..., allow_empty=true)` for `prune: checkpoint` commits that advance the prune ledger."
        )
    if role == "sync-reconciler":
        guidance += (
            "\n- Commit and edit tools are scoped to `MEMORY.md`, `MEMORY_*.md`, `shared_views.toml`, "
            "and `insight_logs/*.md` for sync repair; keep unrelated untracked files out of repair commits "
            "unless the caller explicitly asks about them.\n"
            "- `git_discard(paths)` is destructive. Use it for invalid, partial, or unsafe memory-owned "
            "changes after inspecting the diff."
        )
    else:
        guidance += (
            "\n- Commit tools are scoped to `MEMORY.md` and `MEMORY_*.md`; keep unrelated "
            "untracked files out of memory commits unless the caller explicitly asks about them."
        )
    return guidance


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
