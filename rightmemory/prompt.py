from __future__ import annotations

from importlib import resources
from pathlib import Path
from shlex import quote

from .config import MEMORY_ROOT_ENV
from .reference import read_reference


ROLE_PROMPTS = {
    "guidance-curator",
    "historian",
    "insight",
    "pruner",
    "retrieve",
    "shared-view-builder",
    "skills-curator",
    "sync-reconciler",
    "update",
}


def build_cli_agent_instructions(
    memory_root: Path,
    role: str,
) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = read_reference("schema")
    reference_guidance = _role_reference_guidance(role)
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    cli_agent_guidance = _cli_agent_guidance(memory_root, role)
    corrections_store = "- corrections.md\n" if role in {
        "update", "sync-reconciler", "guidance-curator", "skills-curator"
    } else ""
    if role == "retrieve":
        final_reply = "Return only the strict JSON retrieve selection required below."
    else:
        final_reply = "Return a concise final reply."

    return f"""You are RightMemory {role} mode.

Work in the configured memory root. The configured memory root is {memory_root}.

RightMemory store:
- MEMORY.md
- MEMORY_*.md
- PURSUITS.md
- PURSUIT_*.md
- learning/ (evidence, guidance, experience, and reusable skills)
{corrections_store}- shared_views.toml
- shares.toml
- shared_views/<view-id>/view.md, recipe.toml, question.toml, retriever.md
- insight_logs/

Follow the canonical role instructions below. Use the embedded package references as the schema and rule source of truth.
{final_reply}
{cli_agent_guidance}

RightMemory schema:
{schema}
{reference_guidance}

Role instructions:
{role_guidance}
"""


def build_instructions(
    memory_root: Path,
    role: str,
) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = read_reference("schema")
    reference_guidance = _role_reference_guidance(role)
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    command_guidance = _command_guidance(role)
    sync_guidance = _sync_guidance(role)
    tool_guidance = _tool_guidance(role)
    if role == "retrieve":
        final_reply = "- Finish through the terminal retrieve-selection output type; do not return natural-language prose."
    else:
        final_reply = "- Return concise natural-language answers to the caller."

    return f"""You are RightMemory standalone {role} mode.

Stay within the command-selected {role} role and its instructions for this memory store.

Command-selected behavior:
{command_guidance}
{sync_guidance}

Workspace rule:
- The provided tools are rooted at the RightMemory memory store.
- Use store-relative paths such as `MEMORY.md`, `PURSUITS.md`, and `insight_logs/*.md` when they are allowed for the selected role.
- Do not read, write, inspect, or run commands against paths outside the memory store.
{tool_guidance}
{final_reply}

RightMemory source of truth:
- Memory begins at MEMORY.md; Pursuit begins at PURSUITS.md.
- Learning evidence, behavioral guidance, experience, and reusable skills live separately under learning/.
- Existing Agent Corrections uses the fixed MEMORY_agent-corrections-writing.md and MEMORY_agent-corrections-design.md collections, maintained by guidance curation.
- F# detail files use the containing tree's MEMORY_<slug>.md or PURSUIT_<slug>.md name.
- The package-owned module rules define what belongs in Memory, Pursuit, and Agent Corrections.
- corrections.md contains RightMemory Edit Feedback read by Update; it is not semantic RightMemory state or ordinary retrieval context.
- Insight logs are stored under insight_logs/.
- Share relationships are stored in shares.toml.
- Shared-view resolver metadata is stored in shared_views.toml.
- Provider-owned shared-view source files live under shared_views/<view-id>/; dist/ output there is generated preview or publishing output, not active memory.
- MEMORY.md and PURSUITS.md are normal content roots, not routing-only indexes.

RightMemory schema:
{schema}
{reference_guidance}

Standalone adaptation:
- Treat the embedded package references above as the schema and rule source of truth. Do not try to read reference files outside the memory store; the provided tools only expose that store.
- Treat the caller's message according to the command-selected behavior and the role instructions below.

Role instructions:
{role_guidance}
"""


def _cli_agent_guidance(memory_root: Path, role: str) -> str:
    if role == "retrieve":
        return (
            "\nCLI-agent adaptation:\n"
            "- Your final response must be exactly one JSON object with only `ids` and `sources`; "
            "do not wrap it in Markdown or add prose.\n"
            "- The `read_*` names in the canonical instructions describe standalone tools. In CLI-agent mode, "
            "inspect the equivalent files with the provider CLI's read-only file tools instead of emitting those tool calls.\n"
            "- Inspect M# and S# backing files only through their schema-derived filenames. Inspect MF# content "
            "only through its schema-valid `dist/MEMORY.md` and referenced `dist/MEMORY_<id>.md` or "
            "`dist/MEMORY_SKILL_<id>.md` resources; package metadata is not retrieval content.\n"
            "- Use one-based line numbers from the exact source content when selecting ranges.\n"
        )
    return ""


def _command_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "- The `rightmemory retrieve` command selected retrieval. Treat every caller message as a read-only "
            "retrieval request without requiring or expecting a dispatch prefix.\n"
            "- Do not edit RightMemory files or use git write tools in this mode. If the caller asks you to preserve "
            "or change memory, direct them to `rightmemory submit` or the relevant maintenance skill."
        )
    if role == "historian":
        return (
            "- The `rightmemory history` command selected historical retrieval. Treat every caller message as a "
            "read-only archaeology request over pruned memory and Git history.\n"
            "- Return historical matches as historical or pruned memory, not active memory. Do not edit memory files."
        )
    if role in {"update", "guidance-curator", "skills-curator"}:
        return (
            "- The curation task selects Update or Maintain within this domain. Update starts from pending "
            "evidence; Maintain starts from the supplied scope and existing material.\n"
            "- Work in the task's isolated worktree. Program completion publishes validated work and records "
            "domain processing. Candidate evidence, claims, and receipts are program-owned; raw sources are "
            "removed after all three domains finish."
        )
    if role == "pruner":
        return (
            "- The `rightmemory prune` command selected active-memory pruning. Treat the caller message as the "
            "current prune generation context.\n"
            "- Edit memory files when the supplied generation context says pruning is due."
        )
    if role == "insight":
        return (
            "- The `rightmemory insight` command selected insight behavior. Run one reflection cycle for the memory store.\n"
            "- Treat the caller message as an optional operator hint, not as the ordinary source of truth."
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
    if role == "shared-view-builder":
        return (
            "- The `rightmemory shared-view ...` or `rightmemory share ...` command selected "
            "shared-view builder behavior.\n"
            "- Build only provider-owned shared-view source artifacts under `shared_views/<view-id>/`."
        )
    raise ValueError(f"role must be one of: {_role_list()}")


def _sync_guidance(role: str) -> str:
    if role == "historian":
        return "- Historical retrieval uses local memory and does not perform sync preflight by default."
    if role == "retrieve":
        return (
            "- Before model start, Retrieve may silently admit a clean bounded global-sync refresh and pulls "
            "accepted `MF#` file views. It does not add either pull result to session history. Do not mention "
            "pull results unless imported content is relevant."
        )
    if role == "sync-reconciler":
        return (
            "- The runtime supplies repair context in the caller message. If the caller message "
            "contains a Runtime sync context block, treat that block as authoritative for this turn.\n"
            "- When sync is enabled and you commit memory changes, call `sync_push` after the commit. If `sync_push` "
            "reports dirty state or a conflict, repair the supplied memory files in the same role, validate memory, "
            "commit the repaired state, and call `sync_push` again."
        )
    return ""


def _tool_guidance(role: str) -> str:
    if role == "retrieve":
        return (
            "Available retrieve tools:\n"
            "- `read_detail(detail_id)` resolves a relevant `F#` id and reads its Memory or Pursuit graph detail.\n"
            "- `read_markdown(markdown_id)` reads the complete line-numbered free-form source for an `M#` heading.\n"
            "- `read_skill(skill_id)` reads the complete skill for an `S#` heading.\n"
            "- `read_learning(source_id, offset=0, limit=12000)` reads or pages pending evidence, guidance, or work skills from the learning index.\n"
            "- `search_learning(query, offset=0, limit=20)` searches the complete learning collection by literal terms; an empty query browses it.\n"
            "- `read_mf(mf_id, resource_id=None)` reads a validated mirrored Memory document or one of its "
            "referenced F#, M#, or S# resources."
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
    if role == "shared-view-builder":
        return (
            "- Use read/search tools to inspect provider memory and shared-view source files.\n"
            "- For file views, call exactly one of `create_extractive_file_view` or "
            "`create_generative_file_view` instead of hand-writing `recipe.toml`. If it returns "
            "`failed: ...`, correct the selected ids or arguments and call it again.\n"
            "- For question views, call `create_question_view` instead of hand-writing `question.toml`. "
            "If it returns `failed: ...`, correct the arguments and call it again.\n"
            "- For share-level requests, call `create_or_update_share_relationship` after the selected "
            "file context and live question artifacts are valid; do not hand-write `shares.toml`.\n"
            "- You may use ordinary file tools only for non-machine prose/source edits such as refining `view.md` "
            "or reading existing artifacts."
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
            "\n- Commit and edit tools are scoped to `MEMORY.md`, `MEMORY_*.md`, `PURSUITS.md`, `PURSUIT_*.md`, "
            "`corrections.md`, curated `learning/guidance/`, `learning/experiences/`, and `learning/skills/` content, "
            "`shared_views.toml`, `shares.toml`, "
            "`shared_views/<view-id>/view.md`, `shared_views/<view-id>/retriever.md`, "
            "`shared_views/<view-id>/recipe.toml`, `shared_views/<view-id>/question.toml`, and `insight_logs/*.md` for sync repair; keep unrelated untracked files out of repair commits "
            "unless the caller explicitly asks about them.\n"
            "- `git_discard(paths)` is destructive. Use it for invalid, partial, or unsafe memory-owned "
            "changes after inspecting the diff."
        )
    elif role in {"guidance-curator", "skills-curator"}:
        guidance += (
            "\n- Write the selected domain's learning material. Existing fixed correction collections belong "
            "to guidance curation; existing MEMORY_SKILL files belong to skills curation. Validate and commit "
            "the allowed worktree changes before program completion."
        )
    elif role == "update":
        guidance += (
            "\n- Commit tools are scoped to ordinary `MEMORY.md` and `MEMORY_*.md` context. "
            "Fixed Agent Corrections and skill bodies belong to their domain curators. Pursuit files "
            "and `corrections.md` are read-only; keep unrelated files out of commits."
        )
    else:
        guidance += (
            "\n- Commit tools are scoped to `MEMORY.md` and `MEMORY_*.md`; keep unrelated "
            "untracked files out of memory commits unless the caller explicitly asks about them."
        )
    return guidance


def _role_reference_guidance(role: str) -> str:
    guidance = ""
    semantic_writers = {
        "pruner",
        "shared-view-builder",
        "sync-reconciler",
        "update",
    }
    if role in semantic_writers:
        guidance += "\nMemory rules:\n" f"{read_reference('memory')}\n"
    if role in {"sync-reconciler", "update"}:
        guidance += (
            "\nPursuit rules:\n"
            f"{read_reference('pursuit')}\n"
        )
    if role in {"guidance-curator", "sync-reconciler"}:
        guidance += (
            "\nBehavioral guidance rules:\n"
            f"{read_reference('guidance')}\n"
            "\nExisting Agent Correction collections:\n"
            f"{read_reference('agent-correction')}\n"
        )
    if role in {"skills-curator", "sync-reconciler"}:
        guidance += "\nWork experience and skill rules:\n" f"{read_reference('work-skills')}\n"
    if role in semantic_writers:
        guidance += "\nShared View rules:\n" f"{read_reference('shared-view')}\n"
    if role in {"sync-reconciler", "update", "guidance-curator", "skills-curator"}:
        guidance += (
            "\nRightMemory edit-correction rules:\n"
            f"{read_reference('edit-correction')}\n"
        )
    if role == "retrieve":
        guidance += (
            "\nRetrieve runtime contract:\n"
            f"{read_reference('retrieve-contract')}\n"
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
