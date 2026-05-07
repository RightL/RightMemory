---
name: memory-orchestrator
description: "Use on every user message that might rely on {{MEMORY_ROOT}}/MEMORY.md or sibling MEMORY_*.md files to decide whether to retrieve from or schedule an update via one dedicated curator subagent — the main agent must never read or write memory files itself."
---

Understand the core intent of this skill; do not follow it rigidly, and stay flexible based on the actual context.

- The main agent must not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by any means — no reading it, no editing it, no writing to it, no running commands that view or modify it. All access to the memory file set goes through one curator subagent.
- Spawn the curator subagent **exactly once per session**, the first time memory work is needed. For every subsequent retrieval or update in the same session, send a new message to that same long-lived subagent — never spawn a fresh subagent for memory work again in this session.
- The dispatch prompt sent to the curator subagent should (a) point it at `{{SKILLS_ROOT}}/memory-curator/SKILL.md` and tell it to follow that skill, (b) include the user's message verbatim and unedited when the task is retrieval, (c) include a concrete change brief when the task is an update.
- Do not retrieve on every turn. Only retrieve when the user message clearly depends on prior shared context that is not available in the current conversation itself. If in doubt, skip retrieval.
- When retrieval is needed, send a request to the curator subagent and **wait for its reply before acting** on that memory. Let the curator choose which `MEMORY*.md` files are relevant. Use the returned addressable lines as context; quote them verbatim when relying on them — do not paraphrase heading ids, node ids, descriptions, or edges.
- If the curator reports "no strong match", proceed without memory; do not retry the same query.
- Skip retrieval when the message is clearly self-contained and answerable from the conversation alone.
- After completing work, judge whether any durable fact or preference changed. If nothing durable changed, skip the update.
- For updates, send a brief to the same curator subagent and **proceed without waiting** for its reply (fire-and-forget): information type, affected heading / node / section, what changed, relations, and reason. Do not pre-write the diff — let the curator choose structure.
- When an update has multiple related facts, explicitly ask the curator to decide structure before editing. Remind it that tree containment does not need child-to-containing-heading edges.
- If the user explicitly says to ignore, not use, or not touch memory for this turn, skip both retrieval and update.
- Memory is an aid, not authority. Verify any concrete claim from memory (paths, function names, repo state) against the live working tree before acting on it; if memory and reality disagree, send the curator an update brief.
- Do not surface the dispatch mechanics to the user in the final answer; surface only the substantive content the memory contributed.
