---
name: memory-orchestrator
description: "Use on every user message that might rely on {{MEMORY_ROOT}}/MEMORY.md or sibling MEMORY_*.md files to decide whether to retrieve from or schedule an update through the standalone RightMemory CLI — the main agent must never read or write memory files itself."
---

Understand the core intent of this skill; do not follow it rigidly, and stay flexible based on the actual context.

- This is standalone mode. Do not spawn memory-curator or memory-dreamer subagents, and do not invoke the memory-curator or memory-dreamer skills directly.
- The main agent must not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by any means — no reading it, no editing it, no writing to it, no running commands that view or modify it. All access to the memory file set goes through the standalone `env RIGHTMEMORY_ROOT="{{MEMORY_ROOT}}" {{RIGHTMEMORY_CMD}}` command.
- Pick one stable session id for this agent conversation and reuse it for every curator call. Use a separate stable dreamer session id only when the user explicitly asks for consolidation.
- For retrieval, call `env RIGHTMEMORY_ROOT="{{MEMORY_ROOT}}" {{RIGHTMEMORY_CMD}} curator --session <stable-session-id> "<memory need>"`. Describe the memory needed based on the user's intent instead of blindly forwarding the user's message verbatim.
- Do not retrieve on every turn. Only retrieve when the user message clearly depends on prior shared context that is not available in the current conversation itself. If in doubt, skip retrieval.
- When retrieval is needed, wait for the curator command before acting on that memory. Use returned addressable lines as context; quote them verbatim when relying on them — do not paraphrase heading ids, node ids, descriptions, or edges.
- If the curator reports "no strong match", proceed without memory; do not retry the same query.
- Skip retrieval when the message is clearly self-contained and answerable from the conversation alone.
- After completing work, judge whether this turn produced durable context that should change how a future agent acts, decides, retrieves context, or avoids repeating a mistake. If not, skip the update.
- Memory-worthy context is not limited to completed work. It may include user preferences, workflow preferences, environment/tooling constraints, repeated agent failure patterns and their fixes, project facts, decisions, or blockers. These categories are reminders only: they are neither required nor sufficient; apply the future-use test above in each case.
- For updates, call `env RIGHTMEMORY_ROOT="{{MEMORY_ROOT}}" {{RIGHTMEMORY_CMD}} curator --session <stable-session-id> "<concrete change brief>"`: information type, affected heading / node / section if known, what changed, relations, and reason. Do not pre-write the diff — let the standalone curator choose structure.
- When an update has multiple related facts, ask the curator to decide structure before editing. Remind it that tree containment does not need child-to-containing-heading edges.
- If the user explicitly says to ignore, not use, or not touch memory for this turn, skip both retrieval and update.
- If the user asks for a dream cycle or memory consolidation, call `env RIGHTMEMORY_ROOT="{{MEMORY_ROOT}}" {{RIGHTMEMORY_CMD}} dreamer --session <stable-dreamer-session-id> "<dream request>"` and report the dreamer's substantive result.
- If the standalone command is unavailable or misconfigured, proceed without memory unless the user's task specifically requires memory access. Report the blocker briefly.
- Memory is an aid, not authority. Verify any concrete claim from memory (paths, function names, repo state) against the live working tree before acting on it; if memory and reality disagree, send the curator an update brief.
- Do not surface the command-dispatch mechanics in the final answer; surface only the substantive content the memory contributed.
