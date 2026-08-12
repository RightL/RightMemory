# Agent Corrections Rules

## Purpose

Agent Corrections is RightMemory's bounded library of reusable cases in which a user redirected prior agent work. It preserves concrete contrast that would be weakened or lost if reduced to a generalized preference or rule.

A stored Agent Correction is not the same thing as a user redirection in conversation. A redirection is evidence; Update or an explicit review workflow decides whether that evidence deserves a reusable entry.

## User Redirection

A user redirection occurs when the user's response, in context, materially changes the course of identifiable prior agent work—something the agent reasoned, proposed, wrote, did, or omitted.

The redirection may be explicit or implicit. The user need not reject the prior work or state the desired replacement. An expression of unease, a request to reconsider, a guiding question, or added information may qualify when it causes the prior work to be reconsidered and leads toward a materially different direction.

Judge the redirection by the settled contrast between what the agent was on course to produce or do and the resulting direction. The difference may concern the conclusion, scope, assumptions, reasoning, process, behavior, omitted considerations, or presentation. The final conclusion need not change when the response instead corrects how it is reached, checked, scoped, or explained.

A different result alone is insufficient. Ordinary continuation, selection among intentionally open options, a genuinely new task, or a correction the agent makes independently is not a user redirection. Added information is useful Agent Correction evidence only when the resulting contrast reveals a reusable difference in how similar prior work should be handled.

## Admission

Store an Agent Correction only when:

1. the prior agent attempt, action, reasoning, or omission is identifiable;
2. the user's response clearly redirects that prior work;
3. the resulting direction or outcome is clear enough to preserve accurately;
4. the pattern is reasonably likely to recur or would be costly to repeat;
5. its concrete contrast adds value beyond existing Memory, Memory skills, project artifacts, and Agent Corrections;
6. its applicability can be scoped clearly.

A strong or frustrating correction is not automatically reusable. A vague interaction with no settled outcome is not ready for storage.

## Collections

The module has two fixed semantic categories:

- `MEMORY_agent-corrections-writing.md` stores **Expression Corrections**.
- `MEMORY_agent-corrections-design.md` stores **Substance Corrections**.

Use this test:

> Would changing expression or presentation alone fully resolve the user's objection?

- **Yes:** Expression Correction.
- **No:** Substance Correction.

Expression includes wording, organization, formatting, tone, and presentation.

Substance includes reasoning, assumptions, decisions, proposed actions, omissions, workflow, and behavior. A prompt-related correction is Substance when the prompt's underlying policy or system design must change.

The physical filenames and retrieval identifiers retain `writing` and `design`; the Expression/Substance semantic test is authoritative.

## Entry Form

Each entry is one `###` section in its fixed collection. Its title states the reusable pattern concisely, and its body preserves enough context to understand the scope, prior attempt or omission, user redirection, and resulting outcome.

No field labels or rigid body template are required. Prefer concrete contrast, prevent overgeneralization through clear scope, preserve meaning faithfully, and omit unresolved intermediate proposals from the outcome. Verbatim transcript text is unnecessary unless exact wording is essential.

## Retention And Maintenance

- Each collection contains at most 10 entries and 180 non-empty lines.
- Each entry contains at most 16 non-empty lines.
- No line exceeds 200 characters.
- The collections are bounded priority sets, not logs, quotas, or FIFO windows.
- Merge new evidence into an existing entry when it represents the same reusable pattern.
- Replace or remove weaker entries when a full collection receives more valuable evidence.
- Judge value by likely recurrence, cost when repeated, breadth of future applicability, strength and clarity of evidence, and coverage by existing guidance.
- Narrow or remove an entry whose scope or outcome later proves misleading.

## Relationship To Other State

Use Cross-Session Agent Behavior when a generalized operating rule is useful independently of the correction case. Use Agent Corrections when the concrete contrast itself helps a future agent recognize or avoid the failure.

Keep both only when each representation adds distinct value. Do not duplicate a correction merely because it can be paraphrased as a rule.

Feedback on proposed RightMemory edits belongs to root `corrections.md` under `RIGHTMEMORY_EDIT_CORRECTION_RULES.md`, not in Agent Corrections.
