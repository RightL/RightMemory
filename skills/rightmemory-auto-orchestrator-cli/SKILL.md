---
name: rightmemory-auto-orchestrator
description: "Use when the user explicitly chooses automatic RightMemory orchestration for ordinary work."
---

# Use RightMemory Automatically

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
- If retrieved content is stale, wrong, misleading, or overbroad, do not follow it. Submit the problem and current evidence immediately.
- Ordinary retrieval already considers relevant Agent Corrections. Use `rightmemory agent-corrections writing` or `rightmemory agent-corrections design` only when the user explicitly requests a whole-collection review. Use `writing` when changing expression or presentation alone could resolve the issue; use `design` when reasoning, decisions, actions, omissions, workflow, or behavior must change. Run both commands only when the requested review spans both collections.

## Submit Updates

- Submit automatically only when omitting the evidence would likely:
  - cause poorer future decisions or substantial rediscovery (**Memory**);
  - lose track of a meaningful pursuit, its place in the hierarchy, or its current direction (**Pursuit**); or
  - make repetition of a settled, reusable failure pattern more likely (**Agent Corrections**).
- Do not submit transient progress, routine task results, unfinished work by itself, or implementation detail already adequately preserved in project-local artifacts.
- Submit once the evidence is clear and the work reaches a natural boundary—for example, when a pursuit becomes established or materially changes, a durable outcome settles, a redirection settles, or the conversation moves away from the work. Completion is not required. If nothing passes the bar, submit nothing.
- Combine related evidence due at the same boundary into one candidate. State what happened, what is true now, and why it may matter; do not prescribe final stored wording, ids, classification, placement, or edits.
- Submit with:

  `rightmemory update submit --session <session-id> "<candidate>"`

  Continue the user's task without waiting for Update.

## User Redirections

A user redirection occurs when the user's response, explicitly or implicitly, materially changes the course of identifiable prior work. Judge it by the settled contrast between what you were on course to produce or do and the resulting direction; the difference may concern the conclusion, scope, reasoning, process, omissions, behavior, or presentation.

Unease, a guiding question, or added information may qualify. Mere continuation, selection among intentionally open options, or a new task does not.

When the outcome is clear and the redirection passes the submission bar, include the original need, prior attempt or omission, user redirection, and resulting direction in the next candidate submitted at a natural boundary.
