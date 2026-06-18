---
name: memory-orchestrator
description: "Use when the user's request may depend on long-term context from earlier sessions, or when the current turn may create long-term context worth preserving, such as durable user context, user preferences, project facts, workflow expectations, blockers, or repeated failure patterns."
---

# Memory Orchestrator CLI

## Access Rules

- The memory root is `{{MEMORY_ROOT}}`; the main agent must not read or edit files there by any means unless the user explicitly permits direct access.
- Pick one stable session id for this agent conversation and reuse it for every retrieve/update call.

## Retrieval

- For retrieval, call `rightmemory retrieve --session <stable-session-id> "<memory need>"`.
- Describe the memory needed based on the user's intent instead of blindly forwarding the user's message verbatim.
- For factual, project, or domain context, do not retrieve on every turn. Retrieve when the current conversation lacks the background needed to answer or work well.
- Skip this factual/context retrieval when the message is clearly self-contained and answerable from the conversation alone.
- For preference-, workflow-, and behavior-related memory, retrieve proactively and very frequently when the agent is about to make choices that affect how it collaborates, implements, verifies, communicates, or finishes work.
- Treat phase and topic changes as strong retrieval triggers for preference, workflow, and behavior memory, especially transitions between discussion, implementation, and finishing work.
- When running retrieve, wait at least 3 minutes for the command before acting on memory. During that wait, do not explore files or advance the task independently.
- The retriever skips items already returned in this session; ask explicitly if you need something again.
- A returned `S#...` heading is a memory skill: reusable instruction backed by a separate skill body, not an ordinary memory fact.
- Broad retrieval usually returns only the skill heading and brief body paragraph.
- Before using a memory skill, retrieve that specific skill again to get its full body.
- Treat retrieved behavior guidance and memory skills seriously: apply them directly when the fit is clear, briefly say how they will guide the work when useful, and ask the user when the fit is unclear.
- If current work shows retrieved memory is stale, wrong, too broad, or misleading, send the correction in the next update brief. This matters because bad memory can keep steering future agents wrong.
- Retrieval may include an `Open context questions` block after ordinary memory matches. Treat those lines as agent-facing questions, not memory facts.
- If the current task or workspace context already answers one, include the question id and answer in the next memory update brief.
- Do not start extra investigation just because a question was surfaced.
- Retrieval may include `Provider question context` lines for relevant `MQ#` headings. Treat these as optional external ask opportunities, not memory facts.
- If provider-question context would materially help the current task, call `rightmemory shared-view ask <mq-id> "<question>"` yourself after retrieve returns.
- Phrase the question from the actual task context; do not forward a question invented by retrieve.
- If the ask reports unavailable, continue with available local context and tell the user the provider question endpoint is currently unavailable.

## Updates

- After completing work, judge whether this turn produced durable context that should change how a future agent acts, decides, retrieves context, or avoids repeating a mistake. If not, skip the update.
- If a user context, preference, workflow, or behavior update may be durable but is uncertain, submit it as a candidate brief with the uncertainty and surrounding context included. The command-backed update role will triage candidate briefs before editing memory.
- Submit an update when previous work involved a significant amount of effort or reasoning, and reproducing that work later would take substantial effort.
- Memory-worthy context may include durable user context, user preferences, workflow expectations, emergent reusable workflows discovered through iteration, environment/tooling constraints, repeated agent failure patterns and their fixes, project facts, decisions, blockers, or domain working knowledge.
- Domain working knowledge is reusable understanding about a project, company, product, data model, terminology, conventions, or local artifact semantics that helps future agents interpret things correctly without rediscovering them.
- Capture domain working knowledge when remembering it would help future agents avoid rediscovering how to interpret the same kind of thing.
- For updates, call `rightmemory update submit --session <stable-session-id> "<concrete candidate brief>"` and proceed without waiting or pulling for the update result.
- The first update for a stable session id should include fuller surrounding context: meaning, relevance, uncertainty, and relationship to existing memory.
- Later updates with the same session id may be shorter when earlier submitted context or queued candidates are enough. Include fresh context when the meaning changed or depends on details not yet submitted.
- For corrections to retrieved memory, describe the stale or wrong memory well enough for the updater to find it, and say whether it should be revised, narrowed, or deleted.
- When the user asks for a memory-update result or status, call `rightmemory update pull --session <stable-session-id>`.
- To cancel a submitted update candidate that is still pending, call `rightmemory update undo --session <stable-session-id> <candidate-id>`.
