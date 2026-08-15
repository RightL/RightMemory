---
name: rightmemory-orchestrator
description: "Use when the user explicitly chooses approval-gated RightMemory orchestration for ordinary work."
---

# Use RightMemory

RightMemory contains durable Memory, live Pursuit, and Agent Corrections. Use it as a client during ordinary work; do not curate its files directly unless the user explicitly requests maintenance.

Submissions are evidence for Update; Update decides whether and how RightMemory changes.

## Session

Choose one stable session id for the conversation and reuse it for every RightMemory call.

## Retrieve

- Retrieve when RightMemory plausibly contains context that could materially affect how you understand or approach the current work. Skip clearly self-contained requests for which stored context is unlikely to matter.
- Describe the context needed rather than forwarding the user's message verbatim:

  `rightmemory retrieve --session <session-id> "<need>"`

- Reconcile retrieved content with the current conversation and current evidence. Current user instructions take precedence over stale or conflicting stored state.
- Apply relevant guidance in the work itself; do not merely quote or acknowledge it.
- If retrieved content is stale, wrong, misleading, or overbroad, do not follow it. Submit the problem and current evidence immediately without waiting for approval.
- Ordinary retrieval already considers relevant Agent Corrections. Use `rightmemory agent-corrections writing` or `rightmemory agent-corrections design` only when the user explicitly requests a whole-collection review. Use `writing` when changing expression or presentation alone could resolve the issue; use `design` when reasoning, decisions, actions, omissions, workflow, or behavior must change. Run both commands only when the requested review spans both collections.

## Propose Updates

- Apart from stale, wrong, misleading, or overbroad retrieved state, propose possible updates instead of submitting them automatically.
- Propose only when omitting the evidence would likely:
  - cause poorer future decisions or substantial rediscovery (**Memory**);
  - lose track of a meaningful pursuit, its place in the hierarchy, or its current direction (**Pursuit**); or
  - make repetition of a settled, reusable failure pattern more likely (**Agent Corrections**).
- Do not propose transient progress, routine task results, unfinished work by itself, or implementation detail already adequately preserved in project-local artifacts.
- Name the apparent module and briefly explain why the evidence passes this bar. Do not propose final stored wording, ids, or edits.
- Present the proposal once the evidence is clear and the conversation reaches a natural boundary. Completion is not required. If nothing passes the bar, say nothing about updating RightMemory.
- After approval, combine related approved evidence into one candidate. An explicit request to submit, save, or remember the evidence, or to follow it in future, counts as approval.
- Submit with:

  `rightmemory update submit --session <session-id> "<candidate>"`

  Continue the user's task without waiting for Update.

## User Redirections

A user redirection occurs when the user's response, explicitly or implicitly, materially changes the course of identifiable prior work. Judge it by the settled contrast between what you were on course to produce or do and the resulting direction; the difference may concern the conclusion, scope, reasoning, process, omissions, behavior, or presentation.

Unease, a guiding question, or added information may qualify. Mere continuation, selection among intentionally open options, or a new task does not.

When the outcome is clear and the redirection passes the proposal bar, propose the original need, prior attempt or omission, user redirection, and resulting direction as Agent Correction evidence.
