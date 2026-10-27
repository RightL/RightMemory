from __future__ import annotations

import shlex
from pathlib import Path

from .prompt import _read_prompt_file, build_cli_agent_instructions
from .reference import read_reference


def build_assistant_retrieve_instructions(memory_root: Path) -> str:
    """Build the native assistant role without loading a model or memory state."""
    return f"""You are the RightMemory Retrieve subagent for this assistant.

Use the assistant's native file and search tools. The active memory root is {memory_root}.
The context result supplies source_files, mf_imports, pending_file, and
learning_index_file; inspect those real sources as needed. The compact learning index
can be supplemented by file search under learning_root and in legacy_guidance_file.
Learning material includes concrete evidence that can be useful before its
implications have been settled.
The caller supplies the retrieval need.

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
    validate_command = shlex.join(["rightmemory", "validate", "--root", str(worktree)])
    control = ["rightmemory", "assistant", "--root", str(active_root), "update"]
    complete_command = shlex.join([*control, "complete", "--task", task_id])
    status_command = shlex.join([*control, "status", "--task", task_id])
    return f"""{canonical}

Assistant execution:
- This is isolated task {task_id}. The writable candidate worktree is {worktree}.
- The active root for assistant control commands is {active_root}. Keep that binding
  when running commands from the candidate worktree.
- Use native file and Git tools for the canonical Update role. Validate with
  `{validate_command}` before committing the allowed changes.
- After the worktree is clean, run `{complete_command}`, or the matching
  assistant MCP completion tool.
  Complete an unchanged task too so its batch can be recorded.
- A worktree commit is candidate work. Only the program's completion receipt
  confirms application to the active root; report its application and sync outcome.
  Successful completion removes the candidate worktree; run later checks from
  the active root instead.
- If completion is interrupted or its reply is lost, run `{status_command}`
  and retry its completion as directed. Preserve the worktree and task identity.
  A prepared result awaiting publication needs the same completion retry, not
  another semantic edit of its candidate.
"""
