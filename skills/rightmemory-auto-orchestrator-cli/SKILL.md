---
name: rightmemory-auto-orchestrator
description: "Use when the user explicitly chooses automatic RightMemory orchestration for ordinary work."
---

# Use RightMemory Automatically

RightMemory contains durable Memory, the user's Pursuit map, and Agent Corrections. Use it as a client during ordinary work. Pursuit is read-only context here: do not submit map changes through Update. Explicit map requests belong to the dedicated `maintain-pursuit-map` workflow.

## Session

Choose one stable session id for the conversation and reuse it for every RightMemory call.

## Retrieve

- Retrieve when RightMemory plausibly contains context that could materially affect how you understand or approach the current work. Skip clearly self-contained requests for which stored context is unlikely to matter.
- Describe the context needed rather than forwarding the user's message verbatim:

  `rightmemory retrieve --session <session-id> "<need>"`

- Reconcile retrieved content with the current conversation and current evidence. Current user instructions take precedence over stale or conflicting stored state.
- Apply relevant guidance in the work itself; do not merely quote or acknowledge it.
- If retrieved Memory or Agent Corrections are stale, wrong, misleading, or overbroad, do not follow them. Submit the problem and current evidence immediately. Treat stale Pursuit as read-only context too; task activity does not authorize correcting the map.
- Ordinary retrieval already considers relevant Agent Corrections. Use `rightmemory agent-corrections writing` or `rightmemory agent-corrections design` only when the user explicitly requests a whole-collection review. Use `writing` when changing expression or presentation alone could resolve the issue; use `design` when reasoning, decisions, actions, omissions, workflow, or behavior must change. Run both commands only when the requested review spans both collections.

## Submit Updates

- Submit automatic Memory evidence when omitting it would likely cause poorer future decisions or substantial rediscovery.
- Do not submit transient progress, routine task results, unfinished work by itself, or implementation detail already adequately preserved in project-local artifacts.
- Submit once the durable evidence is clear and the work reaches a natural boundary. Completion is not required. If nothing passes the bar, submit nothing; task progress and new ideas do not trigger a map-change suggestion.
- Combine related evidence due at the same boundary into one candidate. State what happened, what is true now, and why it may matter; do not prescribe final stored wording, ids, classification, placement, or edits.
- Submit with:

  `rightmemory update submit --session <session-id> "<candidate>"`

  Continue the user's task without waiting for Update.

## Capture Agent Guidance

Capture plausible evidence about how an agent should handle similar future work. Bias toward capture rather than filtering: uncertainty about whether the pattern will recur is not a reason to skip it, and similar captures from distinct occurrences are useful.

Capture both direct guidance and explicit or implicit user redirections. A redirection is a user response that changes or reveals how identifiable prior work should proceed. Infer an implicit redirection from the contrast between the approach you were taking and the direction the user now indicates.

The signal may be a correction, rejection, unease, guiding question, added constraint or information, or a change in conclusion, scope, reasoning, process, omissions, behavior, or presentation. It does not need to be phrased as a general rule.

Do not require a fully settled general principle or task completion. Capture once the signal is concrete enough to describe the prior direction and what should change.

Skip only mere continuation, selection among intentionally open options, an unrelated new task, or a detail clearly confined to the current artifact with no plausible agent-behavior lesson. Do not skip merely because the guidance may be one-off.

Capture each distinct occurrence once. Similar guidance may be captured again when a later interaction independently provides the same pattern.

If the user explicitly asks RightMemory to remember or follow guidance in future, submit it through Update. Otherwise use:

  `rightmemory guidance submit --session <session-id> "<candidate>"`

For a redirection, record the prior approach or omission, the user's signal, and the resulting direction. For direct guidance, include enough context to judge its scope. Record the interaction evidence; do not invent a broader rule, final stored wording, or destination. Apply the resulting direction to the current work regardless of capture.

One interaction may produce both an Update candidate and a guidance candidate when they preserve distinct evidence. Continue the user's task without waiting.
