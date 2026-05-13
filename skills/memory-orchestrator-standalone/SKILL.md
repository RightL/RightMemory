---
name: memory-orchestrator
description: "Use when the user's request may depend on long-term context from earlier sessions, or when the current turn may create long-term context worth preserving, such as user preferences, project facts, workflow constraints, decisions, blockers, or repeated failure patterns."
---

- This is standalone mode. Do not spawn memory-curator or memory-dreamer subagents, and do not invoke the memory-curator or memory-dreamer skills directly.
- The main agent must not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by any means — no reading it, no editing it, no writing to it, no running commands that view or modify it. All access to the memory file set goes through the installed standalone `rightmemory` command; do not replace it with repo-local Python or environment-specific launchers.
- Pick one stable session id for this agent conversation and reuse it for every curator call. Use a separate stable dreamer session id only when the user explicitly asks for consolidation.
- For retrieval, call `rightmemory curator --session <stable-session-id> "[RETRIEVE] <memory need>"`. Describe the memory needed based on the user's intent instead of blindly forwarding the user's message verbatim.
- Do not retrieve on every turn. Only retrieve when the user message clearly depends on prior shared context that is not available in the current conversation itself. If in doubt, skip retrieval for ordinary task facts.
- For user/workflow/behavior context, use a lower bar: if prior memory could reasonably change how the agent acts now, do one targeted retrieval. Preferences, communication expectations, tool/environment constraints, process rules, and repeated failure patterns are recognition cues only; they are neither required nor sufficient.
- When retrieval is needed, wait at least 3 minutes for the curator command before acting on that memory. The curator skips items already returned in this session; ask explicitly if you need something again. Use returned addressable lines as context; quote them verbatim when relying on them — do not paraphrase heading ids, node ids, descriptions, or edges.
- If the curator reports "no strong match", proceed without memory; do not retry the same query.
- Skip retrieval when the message is clearly self-contained and answerable from the conversation alone.
- After completing work, judge whether this turn produced durable context that should change how a future agent acts, decides, retrieves context, or avoids repeating a mistake. If not, skip the update.
- If a user/workflow/behavior update may be durable but is uncertain, propose one concise candidate update to the user instead of silently skipping it.
- If the possible update comes from a small iterative adjustment that may still be revised, wait until the next user response. If that response continues to a new topic, accepts the adjustment, or does not reject or materially change it, submit the settled memory update before handling the new request.
- Submit an update when previous work involved a significant amount of effort or reasoning, and reproducing that work later would take substantial effort.
- Memory-worthy context is not limited to completed work. It may include user preferences, workflow preferences, environment/tooling constraints, repeated agent failure patterns and their fixes, project facts, decisions, or blockers. These categories are reminders only: they are neither required nor sufficient; apply the future-use test above in each case.
- For updates, call `rightmemory curator submit --session <stable-session-id> "[UPDATE] <concrete change brief>"` and proceed without waiting or pulling for the update result. Include enough context for the curator to place the memory well: what changed, why it matters, and any known scope or related memory. Do not invent unknown structure, and do not pre-write the diff — let the standalone curator choose structure.
- When the user asks for a memory-update result or status, call `rightmemory curator pull --session <stable-session-id>`.
