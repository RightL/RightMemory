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
- The retriever skips unchanged content already returned in this session. When the user needs matching content repeated, add `--include-returned` for that call; do not change the session id.
- Treat Memory as durable context and Pursuit as live intent or continuity. Do not blur their lifecycle meanings in the answer.
- Retrieval output is authoritative source Markdown selected by the model and rendered by RightMemory, not a model-written summary.
- A selected F# heading includes its parsed detail subtree. A local M#, S#, or MF# heading does not by itself expand linked content; local M# evidence uses source ranges and local S# expands only as a complete instruction. Imported MF# graph content uses ids scoped to `MF#<view-id>`, including F# detail items; direct MF ranges are invalid. Imported M# ranges and complete S# instructions use qualified sources such as `MF#<view-id>/M#<id>` and `MF#<view-id>/S#<id>`.
- Agent Correction Memory is second-pass evidence. Unless the user asks for it directly, wait until an initial draft, design, or implementation direction exists; then use `rightmemory agent-corrections writing` for expression or presentation review, or `rightmemory agent-corrections design` for reasoning, decisions, actions, or behavior. Run only the relevant command; do not run both by default.
- Apply retrieved preferences, workflow guidance, and memory skills when the fit is clear. If retrieved state appears stale, wrong, or misleading, report the discrepancy without submitting a correction.
- Treat retrieved open-context questions as agent-facing questions, not settled facts, and do not investigate merely because one was surfaced.
- Treat `Provider question context` for an `MQ#` heading as an optional external ask opportunity, not a known answer.
- When provider context would materially help, call `rightmemory shared-view ask <mq-id> "<question>"` after retrieve returns. Phrase the question from the actual task; do not forward a question invented by retrieve.
- If the provider question endpoint is unavailable, continue with local context and tell the user.
