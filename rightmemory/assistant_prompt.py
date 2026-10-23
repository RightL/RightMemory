from __future__ import annotations

from pathlib import Path

from .prompt import _read_prompt_file, build_cli_agent_instructions
from .reference import read_reference


def build_assistant_retrieve_instructions(memory_root: Path) -> str:
    """Build the native assistant role without loading a model or memory state."""
    return f"""You are the RightMemory Retrieve subagent for this assistant.

Use the assistant's native file and search tools. The active memory root is {memory_root}.
The context result supplies source_files, mf_imports, and pending_file; inspect those
real sources as needed. The caller supplies the retrieval need.

RightMemory schema:
{read_reference('schema')}

Role instructions:
{_read_prompt_file('prompts/assistant-retrieve.md')}
"""


def build_assistant_update_instructions(
    worktree: Path, task_id: str, *, active_root: Path
) -> str:
    """Reuse Update judgment and native Git work, then return to program finalization."""
    canonical = build_cli_agent_instructions(worktree, "update")
    return f"""{canonical}

Assistant execution:
- This is isolated task {task_id}. The writable candidate worktree is {worktree}.
- The active root for assistant control commands is {active_root}. Keep that binding
  when running commands from the candidate worktree.
- Use native file and Git tools for the canonical Update role. Validate with
  `rightmemory validate --root <worktree>` before committing the allowed changes.
- After the worktree is clean, call the task's supplied complete_command, or the
  matching assistant MCP completion tool. The command is
  `rightmemory assistant --root <active-root> update complete --task <task-id>`.
  Complete an unchanged task too so its batch can be recorded.
- A worktree commit is candidate work. Only the program's completion receipt
  confirms application to the active root; report its application and sync outcome.
- If completion is interrupted or its reply is lost, inspect the same task's status
  and retry its completion as directed. Preserve the worktree and task identity.
  A prepared result awaiting publication needs the same completion retry, not
  another semantic edit of its candidate.
"""
