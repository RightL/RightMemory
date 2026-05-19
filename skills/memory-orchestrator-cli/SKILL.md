---
name: memory-orchestrator
description: "Use when the user's request may depend on long-term context from earlier sessions, or when the current turn may create long-term context worth preserving, such as user preferences, project facts, workflow constraints, blockers, or repeated failure patterns."
---

# Memory Orchestrator CLI

## Access Rules

- Do not spawn memory-curator or memory-dreamer subagents, and do not invoke memory-curator or memory-dreamer skills directly.
- The main agent must not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by any means — no reading it, no editing it, no writing to it, no running commands that view or modify it. All access to the memory file set goes through the installed `rightmemory` command; do not replace it with repo-local Python or environment-specific launchers.
- Pick one stable session id for this agent conversation and reuse it for every retrieve/update call. Use a separate stable dreamer session id when the user explicitly asks for consolidation.

## Retrieval

- For retrieval, call `rightmemory retrieve --session <stable-session-id> "<memory need>"`. Describe the memory needed based on the user's intent instead of blindly forwarding the user's message verbatim. Do not add a dispatch prefix; the command selects retrieval behavior.
- Do not retrieve on every turn. Retrieve when the user message clearly depends on prior shared context that is not available in the current conversation itself. If in doubt, skip retrieval for ordinary task facts.
- For user/workflow/behavior context, use a lower bar: if prior memory could reasonably change how the agent acts now, do one targeted retrieval. Preferences, communication expectations, tool/environment constraints, process rules, and repeated failure patterns are recognition cues; apply judgment to the current turn.
- When retrieval is needed, wait at least 3 minutes for the retrieve command before acting on that memory. The retriever skips items already returned in this session; ask explicitly if you need something again. Use returned addressable lines as context; quote them verbatim when relying on them — do not paraphrase heading ids, node ids, descriptions, or edges. If current work shows retrieved memory is stale, wrong, too broad, or misleading, preserve the returned address and the correction needed for the next update brief.
- If the retriever reports "no strong match", proceed without memory; do not retry the same query.
- Skip retrieval when the message is clearly self-contained and answerable from the conversation alone.

## Updates

- After completing work, judge whether this turn produced durable context that should change how a future agent acts, decides, retrieves context, or avoids repeating a mistake. If not, skip the update.
- If a user/workflow/behavior update may be durable but is uncertain, submit it as a candidate brief with the uncertainty and surrounding context included. The command-backed update role will triage candidate briefs before editing memory.
- Submit an update when previous work involved a significant amount of effort or reasoning, and reproducing that work later would take substantial effort.
- Memory-worthy context may include user preferences, workflow preferences, emergent reusable workflows discovered through iteration, environment/tooling constraints, repeated agent failure patterns and their fixes, project facts, decisions, or blockers. These categories are reminders; apply the future-use test above in each case.
- For updates, call `rightmemory update submit --session <stable-session-id> "<concrete candidate brief>"` and proceed without waiting or pulling for the update result.
- The runtime accumulates submitted candidates, waits 1 hour from the latest submit, and sends the pending candidates to the update role as one batch.
- The first update for a stable session id should include fuller surrounding context: meaning, relevance, uncertainty, and relationship to existing memory.
- Later updates with the same session id may be shorter when earlier submitted context or queued candidates are enough. Include fresh context when the meaning changed or depends on details not yet submitted.
- For corrections to retrieved memory, describe the stale or wrong memory well enough for the updater to find it, and say whether it should be revised, narrowed, or deleted.
- When the user asks for a memory-update result or status, call `rightmemory update pull --session <stable-session-id>`; the output includes current phase, pending candidates, current batch, and timing information.
