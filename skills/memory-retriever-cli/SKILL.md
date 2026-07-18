---
name: memory-retriever
description: "Use when the user explicitly chooses read-only RightMemory retrieval for relevant context from earlier work, preferences, project or domain knowledge, live Pursuits, or reusable agent guidance. This skill never submits updates or modifies RightMemory."
---

# Memory Retriever CLI

## Access Rules

- The RightMemory root is `{{MEMORY_ROOT}}`; do not read or edit files there directly unless the user explicitly permits direct access.
- Pick one stable session id for this agent conversation and reuse it for every retrieve call.

## Retrieval

- Call `rightmemory retrieve --session <stable-session-id> "<memory need>"`.
- Describe the context needed from the user's intent instead of blindly forwarding the user's message.
- Give the retrieve command up to 3 minutes to return. Await or poll that command; do not run a separate blocking wait, explore files, or advance the task while it is pending.
- The retriever skips items already returned in this session. Ask explicitly when the same item is needed again.
- Treat Memory as durable context and Pursuit as live intent or continuity. Do not blur their lifecycle meanings in the answer.
- A returned `F#...` heading points to parsed graph detail. A returned `M#...` heading points to free-form Markdown evidence. A returned `S#...` heading points to reusable instruction.
- Broad retrieval usually returns only a linked-resource heading and its brief body. Retrieve a specific M# or S# item when its full content is actually needed.
- Writing and Design correction M# collections are second-pass evidence. Unless the user asks for them directly, retrieve their full contents only after an initial draft, design, or implementation direction exists.
- Apply retrieved preferences, workflow guidance, and memory skills when the fit is clear. If retrieved state appears stale, wrong, or misleading, report the discrepancy without submitting a correction.
- Treat an `Open context questions` block as agent-facing questions, not settled facts, and do not investigate merely because a question was surfaced.
- Treat `Provider question context` for an `MQ#` heading as an optional external ask opportunity, not a known answer.
- When provider context would materially help, call `rightmemory shared-view ask <mq-id> "<question>"` after retrieve returns. Phrase the question from the actual task; do not forward a question invented by retrieve.
- If the provider question endpoint is unavailable, continue with local context and tell the user.
