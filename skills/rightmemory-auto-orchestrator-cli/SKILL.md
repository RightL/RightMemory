---
name: rightmemory-auto-orchestrator
description: "Use when the user chooses automatic RightMemory use during ordinary work."
---

# Use RightMemory Automatically

Help this conversation benefit from prior context and leave useful evidence for later work. One daily entry serves ordinary Memory, agent guidance, and reusable work experience; their curators decide what to retain and how to generalize it. Pursuit remains the user's read-only direction map during ordinary work.

Use one stable session id throughout the conversation. Use the root selected by the user's task or profile; `rightmemory status` reports it when an explicit root is needed.

## Retrieve

Retrieve when stored context could materially change how you understand or approach the work:

```bash
rightmemory retrieve --session <session-id> "<context needed>"
```

Consider relevant Memory, guidance, correction cases, experiences, skills, and pending evidence. Reconcile them with current evidence and the user's current request. Apply useful guidance in the work itself; preserve the difference between a reported interaction and an adopted rule. Read a relevant skill in full when using its instructions.

Submit concrete evidence when retrieved content is wrong, stale, misleading, or overbroad. Pursuit stays read-only; explicit map edits use `maintain-pursuit-map`.

## Submit Useful Evidence

Capture concrete evidence that could improve later understanding, decisions, collaboration, or repeated work. A useful candidate need not already support a stable conclusion, and uncertainty about recurrence is not by itself a reason to discard it. Preserve the circumstances and unresolved uncertainty that affect its meaning.

Use the same submission path for user and people context, project understanding, behavioral guidance, user redirections, and reusable work:

```bash
rightmemory submit --root <active-root> --session <session-id> --request-id <submission-id> --file <evidence-file> --executor configured
```

An inline text argument can replace `--file`. Reuse the request id and identical evidence when retrying the same submission. An explicit request to remember something uses this path too. The three curators draw their own useful conclusions from shared evidence; the submitting agent need not choose its final destination.

Describe what mattered: for a redirection, the prior approach, user signal, and resulting direction; for work experience, the method, human contributions, outcome, and resources that could spare later rediscovery. Consider whether the situation or a submethod may recur and whether the evidence would help a later agent reproduce a meaningful result from a short prompt. Preserve actual evidence without inventing final wording, a general rule, or a skill.

Submit at a natural boundary once the evidence is concrete enough to describe; completion is not required. Combine related evidence due together and capture each occurrence once. Leave out routine progress and copies of project artifacts that add no independent future value. Independent later occurrences can add useful evidence even when the apparent lesson is similar.

## Curation and Maintenance

Submission wakes the configured curation worker. Continue the user's work while it processes bounded batches for Memory, guidance, and skills. Pending evidence is retrievable immediately. Once all three domains finish, the program removes the raw candidate; curators retain useful context, cases, or experiences in their own sources.

Use `rightmemory curate --root <active-root> status` when a result is uncertain or work needs recovery. Follow the returned task controls; do not start a duplicate worker for an active task. A saved candidate confirms capture, and a completion receipt confirms publication. Neither by itself proves that future behavior has improved.

Explicit broader maintenance uses `maintain-rightmemory`, `maintain-agent-guidance`, or `maintain-work-skills`. Each prepares the same curator in Maintain mode for the user's scope. Ordinary learning does not require a manual review of every candidate.
