---
name: rightmemory-auto-orchestrator
description: "Use when the user chooses automatic RightMemory use during ordinary work."
---

# Use RightMemory Automatically

Help this conversation benefit from prior context and leave useful evidence for later work. One daily entry serves ordinary Memory, agent guidance, and reusable work experience; their curators decide what to retain and how to generalize it. Pursuit remains the user's read-only direction map during ordinary work.

Use one stable session id throughout the conversation. Use the root selected by the user's task or profile; `rightmemory status` reports it when a learning command needs an explicit root.

## Retrieve

Retrieve when stored context could materially change how you understand or approach the work:

```bash
rightmemory retrieve --session <session-id> "<context needed>"
```

Consider relevant Memory, guidance, correction cases, experiences, skills, and raw learning evidence. Reconcile them with current evidence and the user's current request. Apply useful guidance in the work itself; preserve the difference between a reported interaction and an adopted rule. Read a relevant skill in full when using its instructions.

When retrieved content is wrong, stale, misleading, or overbroad, submit the problem and current evidence through its domain's path. Pursuit stays read-only; explicit map edits use `maintain-pursuit-map`.

## Submit Useful Evidence

Capture concrete evidence that could improve later understanding, decisions, collaboration, or repeated work. A possible lesson need not already be a stable rule, and uncertainty about recurrence is not by itself a reason to discard it. Preserve the circumstances and unresolved uncertainty that affect its meaning.

For ordinary context about the user, other people, projects, or decisions, use:

```bash
rightmemory update submit --session <session-id> "<Memory evidence>"
```

For behavioral guidance, user redirections, and work experience that might become reusable, use the shared learning path:

```bash
rightmemory learning --root <active-root> submit --session <session-id> --request-id <submission-id> "<learning evidence>"
```

`--file <evidence-file>` can supply UTF-8 evidence instead of an inline argument. Reuse the request id and evidence when retrying the same learning submission. An explicit request to remember guidance or a procedure uses this same learning path, not ordinary Update.

Describe what mattered: for a redirection, the prior approach, user signal, and resulting direction; for repeated work, the method, human contributions, outcome, and resources that could spare later rediscovery. Preserve actual evidence without inventing final wording, a general rule, or a skill. One learning candidate can help both curators.

Submit at a natural boundary once the evidence is concrete enough to describe; completion is not required. Combine related evidence due together and capture each occurrence once. Routine progress and copies of project artifacts rarely add value. Independent later occurrences can add useful evidence even when the apparent lesson is similar.

## Process Ready Learning

Act on returned learning-ready notices through their `run_argv`. This preserves the mode and scope of interrupted work. For a new update, the command is:

```bash
rightmemory learning --root <active-root> run --domain <guidance|skills> --mode update
```

Use the host's background execution when available while continuing the user's work. Updates process bounded batches; use returned ready notices to continue remaining evidence in subsequent batches. Existing active tasks should be inspected with `learning status`, not started twice. Saved candidates are retrievable evidence immediately; they are not proof that a rule or skill was adopted. Report a curation outcome only when its completion receipt confirms it.

Explicit broader maintenance uses `maintain-rightmemory`, `maintain-agent-guidance`, or `maintain-work-skills` for the corresponding domain. Ordinary learning does not require the user to review every candidate.
