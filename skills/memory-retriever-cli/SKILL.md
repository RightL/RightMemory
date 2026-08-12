---
name: memory-retriever
description: "Use when the user explicitly chooses read-only RightMemory retrieval for relevant durable Memory, live Pursuit, Agent Corrections, or reusable guidance. This skill never submits updates or modifies RightMemory."
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
- Distinguish durable Memory, live Pursuit, and Agent Correction cases in the returned context.
- Retrieval output is authoritative source Markdown selected by the model and rendered by RightMemory, not a model-written summary.
- A selected F# heading includes its parsed detail subtree. A local M#, S#, or MF# heading does not by itself expand linked content; local M# evidence uses source ranges and local S# expands only as a complete instruction. Imported MF# graph content uses ids scoped to `MF#<view-id>`, including F# detail items; direct MF ranges are invalid. Imported M# ranges and complete S# instructions use qualified sources such as `MF#<view-id>/M#<id>` and `MF#<view-id>/S#<id>`.
- Ordinary retrieval already considers relevant Agent Corrections. Use `rightmemory agent-corrections writing` or `rightmemory agent-corrections design` only when the user explicitly requests a whole-collection review. Use the writing collection when expression or presentation alone could resolve the issue, and the design collection when reasoning, decisions, actions, omissions, workflow, or behavior must change. Run both commands only when the requested review spans both collections.
- For retrieved preferences, workflow or behavior guidance, reusable instructions, and Agent Corrections—but not ordinary facts, knowledge, or descriptive context—use this fixed user-visible format when such guidance is active or may apply later:

  ```text
  [RightMemory] Retrieved guidance
  - Active: <guidance being followed now>
  - Deferred: <guidance that may apply later, with its applicability condition stated in the sentence>
  ```

- Omit empty lines and weak or rejected matches. Show the block before active guidance first affects the work. Reassess deferred guidance as context changes, and repeat the block only when its status changes.
- If retrieved state appears stale, wrong, or misleading, report the discrepancy without submitting a correction.
- Treat retrieved open-context questions as agent-facing questions, not settled facts, and do not investigate merely because one was surfaced.
- Treat `Provider question context` for an `MQ#` heading as an optional external ask opportunity, not a known answer.
- When provider context would materially help, call `rightmemory shared-view ask <mq-id> "<question>"` after retrieve returns. Phrase the question from the actual task; do not forward a question invented by retrieve.
- If the provider question endpoint is unavailable, continue with local context and tell the user.
