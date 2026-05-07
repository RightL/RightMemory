---
name: memory-orchestrator
description: "Use on every user message that might rely on {{MEMORY_ROOT}}/MEMORY.md or sibling MEMORY_*.md files to decide whether to retrieve from or schedule an update via one dedicated curator subagent — the main agent must never read or write memory files itself."
---

Understand the core intent of this skill; do not follow it rigidly, and stay flexible based on the actual context.

- The main agent must not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by any means — no reading it, no editing it, no writing to it, no running commands that view or modify it. All access to the memory file set goes through one curator subagent.
- Spawn the curator subagent **exactly once per session**, the first time memory work is needed. For every subsequent retrieval or update in the same session, send a new message to that same long-lived subagent — never spawn a fresh subagent for memory work again in this session.
- The dispatch prompt sent to the curator subagent should (a) point it at `{{SKILLS_ROOT}}/memory-curator/SKILL.md` and tell it to follow that skill, (b) include the user's message verbatim and unedited when the task is retrieval, (c) include a concrete change brief when the task is an update.
- After each user message, judge whether the message could benefit from memory. Triggers worth checking (examples only, not a template; do not follow rigidly, stay flexible to context): named projects, libraries, paths, machines, people, prior decisions, "where is…", "what did we say about…", references to past conversations, status / dependency questions.
- If yes, send a retrieval request to the curator subagent and **wait for its reply before taking any other action**. Let the curator choose which `MEMORY*.md` files are relevant. Use the returned addressable lines as context; quote them verbatim when relying on them — do not paraphrase heading ids, node ids, descriptions, or edges.
- If the curator reports "no strong match", proceed without memory; do not retry the same query.
- Skip retrieval when the message is clearly self-contained (generic coding question, environment / config tweak unrelated to project memory, a typo fix, etc.) (examples only, not a template; do not follow rigidly, stay flexible to context).
- After completing work, judge whether memory should change. Triggers worth checking (examples only, not a template; do not follow rigidly, stay flexible to context): a new project / library / file / machine appears, a durable user preference or correction surfaces, status / decision / dependency / external-reference moves, a recorded fact is now wrong.
- For updates, send a message to the same curator subagent with a concrete brief: the information type (project fact, implementation status, verification, user preference, agent policy, design rationale, etc.), which heading id / node id / section is affected, what fact changed, what relations may matter, and the reason. Do not pre-write the diff yourself — let the curator choose whether to update a node, add a compact node, add/reuse a heading anchor, create a `##`/`###` subgroup, or move detail into a `MEMORY_<slug>.md` file.
- When an update has multiple related facts, explicitly ask the curator to decide structure before editing. Remind it that tree containment does not need child-to-containing-heading edges.
- If the user explicitly says to ignore, not use, or not touch memory for this turn, skip both retrieval and update.
- Memory is an aid, not authority. Verify any concrete claim from memory (paths, function names, repo state) against the live working tree before acting on it; if memory and reality disagree, send the curator an update brief.
- Do not surface the dispatch mechanics to the user in the final answer; surface only the substantive content the memory contributed.
