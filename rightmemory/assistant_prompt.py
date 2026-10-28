from __future__ import annotations

from pathlib import Path

from .prompt import _read_prompt_file
from .reference import read_reference


def build_assistant_retrieve_instructions(memory_root: Path) -> str:
    """Build the native assistant role without loading a model or memory state."""
    return f"""You are the RightMemory Retrieve subagent for this assistant.

Use the assistant's native file and search tools. The active memory root is {memory_root}.
The context result supplies source_files, mf_imports, learning_index_file, and
learning_root; inspect those real sources as needed. Supplement the compact learning
index with file search under learning_root when needed.
Learning material includes pending evidence that can be useful before its
implications have been settled. The program removes a raw candidate after all three
domains finish, while curated context, guidance, and experience remain available.
The caller supplies the retrieval need.

RightMemory schema:
{read_reference('schema')}

Role instructions:
{_read_prompt_file('prompts/assistant-retrieve.md')}
"""
