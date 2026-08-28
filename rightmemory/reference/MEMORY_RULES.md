# Memory Rules

## Purpose

Memory is durable context that helps a future agent act, decide, interpret, retrieve, or avoid costly rediscovery. It is not a record of everything that happened.

Project artifacts remain the primary home of project-specific implementation detail. Memory adds value when it preserves compact context, interpretation, scope, or a lookup rule that would otherwise require substantial search or reasoning.

## Admission

Store an item in Memory only when all of the following are true:

1. It is expected to remain useful beyond the current task or session.
2. It can materially affect future action, judgment, interpretation, or retrieval.
3. Its scope and meaning can be stated clearly from stored context.
4. Its value is more than recording a direction in the user's Pursuit map.
5. It is not merely a duplicate of an existing Memory item, project artifact, instruction asset, or Agent Correction.

Durability alone is insufficient. A stable but trivial fact is not good Memory.

Useful unsettled context may be stored as `Uncertain:` when the uncertainty itself affects future work. Do not convert speculation into declarative fact.

## Good Memory

Good Memory commonly includes:

- durable user context, direction, responsibilities, or constraints;
- stable preferences and collaboration expectations;
- environment constraints that recur across work;
- project or domain interpretations that are expensive to reconstruct;
- stable decisions and the reason or scope needed to apply them;
- compact lookup rules for recurring artifact families;
- reusable operating guidance whose rule is useful independently of one correction case;
- concise unresolved questions whose answer would materially improve future work.

Clearly unsuitable as ordinary Memory:

- raw transcripts or turn-by-turn narration;
- current task progress preserved only because work is incomplete;
- completed-task summaries with no independent future value;
- implementation logs, bug databases, or experiment ledgers;
- raw tables, parameter dumps, generated artifact inventories, or Git history;
- copied project documentation or source text;
- repeated examples that do not change a rule or conclusion;
- facts recoverable through a quick inspection when Memory adds no interpretation or retrieval value.

## Item And Structure Quality

A Memory item should express one reusable fact, preference, rule, decision, conclusion, or unresolved question at the smallest scope that remains independently meaningful.

Prefer revising, merging, narrowing, moving, or removing existing content over appending a near-duplicate.

Good structure makes domain, topic, scope, and local reading context obvious. Avoid:

- duplicate items;
- fake hub nodes;
- overloaded headings that mix unrelated subjects or scopes;
- child nodes that merely restate their parent;
- detail that obscures the reusable point;
- edges that only repeat containment;
- `rel:` when a more specific edge type is accurate.

Put new items in the closest existing group with matching domain and scope. Prefer a meaningful anchored heading over a fake hub node. Move a subtree behind `F#`, or split, merge, or relocate it, when that materially improves navigation. About fifteen direct node lines is a useful prompt to reconsider structure, not a validity threshold.

## User Context

Use `# User Context` for durable context about the user as a person with ongoing work and direction: relevant background, goals, motivations, responsibilities, circumstances, and why they matter.

User Context is a compact context profile grounded in evidence. Do not infer personal characteristics beyond what the stored evidence supports.

A direction's place in the Pursuit map is the user's decision. An unfinished commitment is not automatically Memory or Pursuit; preserve independently durable context here and leave execution continuity in project artifacts.

## Cross-Session Agent Behavior

Use `# Cross-Session Agent Behavior` for future-facing guidance about how agents should reason, communicate, use tools, verify work, or collaborate with this user.

Guidance may be global or project-scoped. Express narrower scope through tree placement or explicit wording.

Behavior Memory should read as an operating rule, not as transcript review notes, frustration history, or a growing incident log.

Use Behavior Memory when the generalized rule is independently useful. Use Agent Corrections when the concrete attempted/redirection/outcome contrast is itself valuable. Keep both only when the two representations add distinct value.

## Memory Skills

A Memory skill is a reusable instruction asset: a workflow, judgment playbook, prompt-shaped instruction, or bounded operating style for a recurring situation.

Use an `S#` heading when a compact heading body can explain when the instruction applies and the full instruction belongs in `MEMORY_SKILL_<id>.md`.

The skill file is free-form instructional Markdown, not graph-bearing content. Keep ids, edges, and graph placement on the `S#` heading or nearby Memory.

Do not create a skill for a one-off project detail or a mechanical rule that should be enforced by code or validation.

## Open Context Questions

Use global `# Open Context Questions` for concise unresolved questions that materially affect the usefulness of stored context.

Question nodes use ordinary node syntax. When a question concerns a specific graph item, connect it with `todo:`. A genuinely global question may stand alone. Questions are not tentative declarative facts.

When an answer becomes clear, place the answer in the appropriate Memory section and remove or revise the question.

## Maintenance

When evidence changes:

- update the canonical item rather than layering contradictory versions;
- narrow overbroad guidance;
- preserve provenance only when it helps interpretation or verification;
- remove obsolete content whose continued presence would mislead;
- keep settled content unchanged when ambiguity cannot be resolved safely;
- use an Open Context Question when unresolved ambiguity is itself useful to preserve.
