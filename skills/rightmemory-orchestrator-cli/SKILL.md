---
name: rightmemory-orchestrator
description: "Use when the user explicitly chooses approval-gated RightMemory orchestration for ordinary work."
---

# Use RightMemory

RightMemory contains ordinary Memory, guidance and correction cases, reusable work experience and skills, and the user's Pursuit map. Use it as a client during ordinary work. Pursuit is read-only context here: do not submit map changes through Update. Explicit map requests belong to the dedicated `maintain-pursuit-map` workflow.

Submissions are evidence for the corresponding domain curator; the curator decides whether and how stored understanding changes.

## Session

Choose one stable session id for the conversation and reuse it for every RightMemory call.

## Retrieve

- Retrieve when RightMemory plausibly contains context that could materially affect how you understand or approach the current work. Skip clearly self-contained requests for which stored context is unlikely to matter.
- Describe the context needed rather than forwarding the user's message verbatim:

  `rightmemory retrieve --session <session-id> "<need>"`

- Reconcile retrieved content with the current conversation and current evidence. Current user instructions take precedence over stale or conflicting stored state.
- Apply relevant guidance in the work itself; do not merely quote or acknowledge it.
- If retrieved content is stale, wrong, misleading, or overbroad, do not follow it. Submit the problem and current evidence through the shared submission path immediately without waiting for approval. Treat stale Pursuit as read-only context too; task activity does not authorize correcting the map.
- Ordinary retrieval already considers relevant Agent Corrections. Use `rightmemory agent-corrections writing` or `rightmemory agent-corrections design` only when the user explicitly requests a whole-collection review. Use `writing` when changing expression or presentation alone could resolve the issue; use `design` when reasoning, decisions, actions, omissions, workflow, or behavior must change. Run both commands only when the requested review spans both collections.

## Propose Updates

- Apart from stale, wrong, misleading, or overbroad retrieved state, propose possible updates instead of submitting them automatically.
- Propose concrete evidence that could improve later understanding, decisions, collaboration, or repeated work. A useful candidate need not already support a stable conclusion; preserve context and uncertainty, and leave adoption and generalization to the corresponding domain's Update core. Uncertainty about recurrence alone is not a reason to omit useful evidence.
- For work experience, consider whether the situation or a submethod may recur and whether the evidence would help a later agent reproduce a meaningful result from a short prompt. Preserve the method, human contributions, outcome, and relevant resources.
- Leave out routine progress and copies of project artifacts that add no independent future value, unless needed to correct existing state.
- Briefly explain the possible future value. Do not propose final stored wording, ids, or edits.
- Present the proposal once the evidence is concrete enough to describe and the conversation reaches a natural boundary. Completion is not required. If nothing passes the bar, say nothing about updating RightMemory.
- After approval, combine related approved evidence for the same submission path. An explicit request to submit, save, or remember the evidence, or to follow it in future, counts as approval.
- Submit approved context, behavioral guidance, user redirections, and potentially reusable work through one shared path:

  `rightmemory submit --root <active-root> --session <session-id> --request-id <submission-id> --executor configured "<evidence>"`

  Use the root reported by `rightmemory status` when it is not already bound. Reuse the request id and identical content for the same submission retry. All three curators can draw useful conclusions from the same evidence.

- Submission wakes the configured worker. Continue the user's task while it processes ready domains. Inspect `rightmemory curate --root <active-root> status` when recovery is needed. A saved candidate confirms capture; the completion receipt confirms publication. The program removes raw evidence after all three domains finish, with useful context or cases retained by their curators.

## User Redirections

A user redirection occurs when the user's response, explicitly or implicitly, materially changes the course of identifiable prior work. Judge it by the contrast between what you were on course to produce or do and the resulting direction; the difference may concern the conclusion, scope, reasoning, process, omissions, behavior, or presentation.

Unease, a guiding question, or added information may qualify. Mere continuation, selection among intentionally open options, or a new task does not.

When the redirection is concrete enough to describe, propose the original need, prior attempt or omission, user signal, and resulting direction as learning evidence. Leave generalization and case admission to the guidance curator.
