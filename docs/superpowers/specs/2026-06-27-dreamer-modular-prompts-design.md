# Dreamer Modular Prompt Draft

Status: draft for user review.

This document sketches how the current Dreamer prompt can be split into small
modular prompt paragraphs. The focus is not runtime implementation yet. The
focus is deciding what each prompt block should do, what it must not do, and
where its wording comes from.

## Goal

Turn the current broad Dreamer role into a staged set of compact prompt modules.
Each module should be understandable as one short instruction paragraph.

The target shape is:

1. Shared setup
2. Deterministic mechanical preflight
3. Structure organizer
4. Active memory quality cleanup
5. Behavior memory scope and compaction
6. Skillifier
7. Commit and report

This order is a design draft. Some modules may later merge, split, or move.

## Progressive Disclosure Model

Source: `New boundary`.

The modular prompt design uses progressive disclosure, not one monolithic
Dreamer prompt. Dreamer should not receive the whole future module list as one
large prompt. Instead, the runtime or orchestrating loop should reveal one
compact prompt block, let the model or deterministic tool act, inspect the
result, and only then reveal the next prompt block.

The intended cycle is:

1. Supply shared setup.
2. Run deterministic mechanical cleanup or show its preflight result.
3. Supply the structure organizer prompt and let the model act.
4. After that step finishes, supply the next module prompt with the relevant
   current state or prior-step summary.
5. Reveal and run later modules only when the earlier module has finished.

Each module should see the common setup, its own instruction paragraph, and only
the prior context it needs. Future module instructions should not be included
early, because that would encourage the model to collapse separate steps back
into one broad Dreamer pass.

## Source Labels

The notes below use these source labels instead of line numbers:

- `Dreamer: Sources And Scope`: the current Dreamer prompt section that defines
  memory files, schema use, local/global scope, and judgment-based reasoning.
- `Dreamer: Cleanup And Restructure`: the current section that covers light
  graph fixes, deep restructures, durable value judgment, open questions,
  compaction/removal, shared-view boundaries, and graveyard behavior.
- `Dreamer: Memory Skills`: the current section about turning recurring
  instruction-like memory into `S#` memory skills.
- `Dreamer: Conflicts And Boundaries`: the current section about contradictions,
  schema edge types, and graph edge discipline.
- `Dreamer: Commit / Final Reply`: the current commit and final-report rules.
- `Decision: 2026-06-23 Role Split`: the accepted decision that organizer,
  compactor, and pruner responsibilities should stay visible and separate.
- `Decision: 2026-06-16 Staged Cleanup`: the earlier favored but deferred idea
  that cleanup should happen progressively through organize, triage, and cleanup
  passes.
- `New boundary`: a proposed clarification that is not directly stated in the
  current Dreamer prompt, but follows from the role split discussion.

## Wording Preservation Policy

Source: `New boundary`.

The modular prompt paragraphs should not be a fresh rewrite of Dreamer in new
language. They should preserve the current Dreamer prompt's policy-bearing
phrases wherever those phrases still fit the module. The design should only
rewrite when needed to expose a module's primary attention, support progressive
disclosure, or remove wording that makes a prompt act like a cross-module
router.

In practice:

- Preserve wording that carries behavior, such as "source of truth is the memory
  file set", "reason about similarity, duplication, contradiction, and
  consolidation with judgment, not thresholds", "do not add reverse edges
  mechanically", "deep restructures are encouraged", "compact and current",
  "local meaning", and "Dreaming must be idempotent".
- Add boundary sentences explicitly, and treat them as `New boundary` rather
  than as inherited Dreamer wording.
- Do not preserve old wording mechanically when it would hide the new boundary.
  For example, a sentence that mixes organize, compact, and remove should be
  split across the relevant modules.

## Model-Specific Addendum Marking

Source: `New boundary`.

Some prompt details are model-specific guardrails rather than the stable meaning
of the module. They may exist because a current target model tends to overdo,
miss, or misunderstand something. Do not split those details into a separate
prompt section by default; keep them near the instruction they qualify.

Use the exact marker `[[...]]` for these model-specific addenda.

- Text without this key is general prompt text.
- The key may mark a whole bullet, a sentence, or one clause inside a sentence.
- Model-specific addenda may be removed, narrowed, or replaced when the target
  model changes.
- Do not use model-specific addenda to hide core module behavior.

## Module Overlap Principle

Source: `New boundary`.

Modules have primary attention, not hard ownership walls. Natural overlap is
allowed when it makes the current edit coherent. A module prompt should describe
the work for the current pass rather than route work to named later modules;
progressive disclosure handles sequencing outside the prompt.

## Existing Dreamer Content That Can Become Steps

The current Dreamer prompt already contains several hidden steps:

- Scope setup: use `MEMORY.md` as the overview of the memory root, inspect the
  memory files needed to understand and improve the whole memory root, and use
  the supplied schema.
- Deterministic mechanical cleanup: use Python scripts, validation, or existing
  tooling for issues that tools can detect reliably, such as duplicate graph
  junk, self-edges, dangling edges, invalid ids, and edges from a child node to
  its own parent heading when the edge only repeats the tree placement.
- Structure organizer: use model judgment for structure issues that require
  meaning, such as near-duplicates, unclear heading placement, reciprocal edges,
  stale open questions, edge-type choice that depends on meaning, and
  contradictions.
- Active memory quality cleanup: judge durable value, preserve
  hard-to-reproduce conclusions, compress noisy traces, mark locality issues,
  and move stale low-value material to a graveyard when appropriate.
- Behavior memory scope and compaction: keep behavior entries as scoped
  operating rules instead of ever-growing incident histories.
- Skill extraction: turn reusable instruction-like memory into `S#` skills.
- Commit and final report: commit touched memory files and report the outcome.

The ambiguous parts are mostly where one sentence asks Dreamer to judge,
compress, move, or delete at the same time.

## Boundary Problems To Resolve

### Deterministic Cleanup vs Structure Organizer

Some current "light fixes" are not really AI work. Duplicate edges, self-edges,
dangling edges, invalid ids, validation failures, and other schema-mechanical
problems can be detected by Python scripts or existing tools. AI should not
spend reasoning on stable mechanical detection or repair when deterministic
tooling can do it reliably. Structure organizing should start after those
mechanical problems are either fixed or reported.

### Structure Organizer vs Quality Cleanup

Moving a fact to a clearer heading is structure-shaped work. Rewriting a noisy
trace into a shorter durable conclusion is quality-shaped work. Duplicate
merging is a natural overlap point: exact duplicates are mechanical, and
near-duplicates need judgment about what makes the memory clearer.

### Quality Cleanup vs Pruner

Dreamer quality cleanup may compact, mark locality issues, or move low-value
stale material into a graveyard. Budget enforcement is a separate lifecycle
concern from quality cleanup.

### Behavior Memory Scope

Behavior, preference, and workflow memory has its own failure mode: it can grow
by appending every correction, frustration, and incident. That makes active
memory too long and too global. Behavior cleanup should preserve the rule, keep
only the shortest useful incident evidence, and make scope explicit.

### Archive Gap

The role split decision says compaction should write archive records for details
that leave active memory. The current Dreamer prompt does not define that
archive mechanism. Until the archive exists, quality cleanup should say what
detail would be lost and avoid compression that would permanently drop details
when preservation is unclear.

### Skillifier vs Behavior Cleanup

Skill extraction is not ordinary tree cleanup. It creates or updates reusable
agent instructions. Some behavior entries naturally overlap with skill
extraction, while ordinary user preferences, scoped project behavior, and
one-off corrections remain ordinary memory.

## Severe Active-Memory Problems To Address In Prompts

These problems should be called out directly because the current Dreamer prompt
only covers them indirectly or not at all:

- Behavior memory grows into long incident histories instead of scoped operating
  rules.
- Project-local facts, duplicated docs, generated artifact inventories, and Git
  history notes enter global active memory instead of becoming pointers,
  locality issues, or no durable memory.
- Raw experiment rows and parameter sweeps remain as tactical detail instead of
  compressing into durable conclusion, current best setting, rejected direction,
  reason, and report pointer.
- Insight-style or reflective prose can behave like active operational memory
  instead of being distilled into a durable rule, risk, or decision.

## Module Drafts

### Shared Setup

Source: `Dreamer: Sources And Scope`.

This module is reusable prelude. It is not organizer, triage, or compactor
logic by itself.

Prompt:

> - The source of truth is the memory root: `MEMORY.md` plus the sibling
>   `MEMORY_*.md` files.
> - Start with `MEMORY.md` as the overview of the memory root, then inspect the
>   memory files needed to understand and improve the whole memory root.
> - Use the provided memory schema as the source for memory structure and
>   validation. [[Do not expand this into a schema checklist unless a concrete
>   edit needs that detail.]]
> - [[Do not add schema text to memory files; memory files should contain memory
>   content only.]]
> - [[Do not treat work on the whole memory root as blindly reading every detail
>   file in full before acting. Build the core picture first, then inspect files
>   when they matter.]]
> - [[Reason about similarity, duplication, contradiction, and consolidation
>   with judgment, not thresholds; do not turn this into numeric scores or fixed
>   quotas.]]

### Deterministic Mechanical Preflight
Source: `Dreamer: Cleanup And Restructure` and `New boundary`.

This module is tool-first cleanup. Existing validation can report some graph and
schema problems; future deterministic scripts may fix more of them before model
work starts. This module should still be able to run after Shared Setup by using
the available preflight or validation result.

Prompt:

> - Use the available preflight or validation result for the memory root.
> - Repair reported problems appropriately.
> - Apply the safe mechanical repairs found in this module. [[If the report does
>   not show a mechanical issue, make no changes in this module.]]
> - [[Typical mechanical repairs include duplicate edges, self-edges, dangling
>   edges, invalid ids, schema validation failures, empty headings created by
>   edits, and edges from a child node to its own parent heading when the edge
>   only repeats the tree placement.]]

Boundary notes:

- Exact graph duplicate cleanup is a natural preflight repair.
- Exact text duplicate cleanup is preflight-suitable when duplicate identity is
  mechanically proven.
- This module may run as a deterministic pre-model step, or as a model-visible
  repair step over a preflight report, depending on implementation.

### Structure Organizer

Source: `Dreamer: Cleanup And Restructure`, `Dreamer: Conflicts And Boundaries`,
and `Decision: 2026-06-23 Role Split`.

The structure organizer's primary attention is memory content structure that
requires judgment about meaning. It should improve the memory tree and graph.

Prompt:

> - Improve memory content structure. [[Use this module for placement,
>   grouping, and graph choices that depend on meaning.]]
> - Deep restructures are encouraged when they make the memory tree or graph
>   clearer and better structured, even when they require broad edits.
> - Add reciprocal edges when they improve future retrieval or understanding.
>   [[Do not add reverse edges mechanically, for symmetry alone, or in a way
>   that makes the relationship misleading.]]
> - Keep `# Open Context Questions` compact and current: merge duplicate
>   questions, remove stale questions, revise questions whose linked memory
>   changed, and add a short question when consolidation exposes a loose end in
>   memory.
> - [[When consolidating, use heading bodies for text about the heading itself.
>   Keep child nodes for facts that should stand independently.]]
> - Shared-view relationships use schema-defined `MF#` and `MQ#` headings; keep
>   heading bodies focused on local meaning. [[Do not absorb provider content
>   unless it became a local decision, task, or consequence.]]
> - Clean up clear contradictions: update, merge, narrow, or remove memory as
>   appropriate.
> - When a remaining problem needs an answer or user attention, add or refine a
>   compact `# Open Context Questions` item.
> - Schema rules apply unchanged; pick the most specific useful edge type.
>   [[Use `rel:` only when nothing else fits.]]
> - Keep or add edges for useful graph relationships. [[Do not add an edge just
>   to repeat what the tree placement already says.]]

Boundary notes:

- Near-duplicate cleanup is a natural overlap point; use the edit shape that
  makes memory structure clearer.
- Open question cleanup naturally fits here when it improves structure or keeps
  the question surface current.
- Reciprocal edges require judgment and should be added only when they improve
  future retrieval or understanding without misleading the graph.

### Active Memory Quality Cleanup

Source: `Dreamer: Cleanup And Restructure`,
`Decision: 2026-06-16 Staged Cleanup`, `Decision: 2026-06-23 Role Split`, and
`New boundary`.

This module's primary attention is active memory quality. It combines
durable-value judgment, compaction, locality marking, and graveyard movement
because separating those too rigidly can make the model behave unnaturally.

Prompt:

> - During consolidation, judge each item by durable value: whether it helps a
>   future agent act, decide, retrieve context, or avoid repeating work.
> - Compress or remove memory with low durable value appropriately. [[Common
>   examples include transient progress, over-detailed traces, stale state,
>   low-value repetition, duplicated project documentation, generated artifact
>   inventories, Git history notes, and raw experiment rows.]]
> - Preserve hard-to-reproduce reasoning, conclusions, failed investigations,
>   and decisions when recreating them later would take meaningful effort.
> - If the surrounding record is noisy, keep the durable conclusion and simplify
>   the trace around it.
> - Raw experiment detail should usually compress into the durable conclusion,
>   current best setting, rejected direction, reason, and report pointer.
> - Memory that looks project-local can be compressed to a pointer or marked as
>   a locality issue. [[Dreamer should not try to move it into project files.]]
> - Distill insight-style or reflective prose into durable memory when it
>   matters. [[Do not keep reflective prose as ordinary operational memory unless
>   it has become a durable rule, risk, or decision.]]
> - Move low-value but nontrivial stale memory into a `## Graveyard` heading
>   inside the same `#` memory domain when that is the right cleanup shape.
>   [[Use graveyard movement for material that should leave the main active
>   memory view but should not be deleted yet.]]
> - Preserve important detail when no archive target exists. [[Do not delete,
>   flatten, or permanently drop long memory just because it is long.]]

Boundary notes:

- This module may edit and compact; it is not a read-only triage report.
- Direct deletion should stay narrow: duplicate graph junk, exact duplicate
  memory, or clearly obsolete material.
- Low-value but nontrivial material should usually be compressed or moved to a
  graveyard before deletion.
- Deletion after repeated graveyard cycles is a budget or lifecycle concern,
  not the core quality-cleanup prompt.

### Behavior Memory Scope And Compaction

Source: `Dreamer: Cleanup And Restructure`, `Decision: 2026-06-16 Staged
Cleanup`, `rm-schema-project-agent-behavior`, and `New boundary`.

This module handles preference, workflow, and agent behavior memory. It is
separate from ordinary quality cleanup because the main risk is not just length:
the rule's scope can drift from project-local to global, and incident evidence
can become longer than the rule.

Prompt:

> - For preference, workflow, and agent behavior memory, preserve the rule and
>   its scope.
> - Write agent behavior entries as operating instructions. [[Do not preserve
>   transcript review notes as behavior memory.]]
> - Keep concise incident evidence when it helps explain the rule. [[Remove
>   chat-like chronology, repeated examples, frustration context, and
>   implementation notes unless they change the rule.]]
> - Keep global behavior memory only for cross-project defaults.
> - Keep project-specific agent guidance under explicit project scope. [[Do not
>   promote local rules into global user preferences.]]
> - Merge, narrow, or replace behavior entries that are already covered by a
>   broader principle or an `S#` skill.

Boundary notes:

- Keep useful meaning distinctions while shortening behavior memory.
- Scope labels matter: global preference, cross-session behavior, project agent
  behavior, project-local exception, and reusable skill are different outputs.
- Instruction-like material naturally overlaps with skill extraction; length
  alone is not evidence for skill extraction.

### Skillifier

Source: `Dreamer: Memory Skills`.

This module handles memory that is really reusable agent instruction. It is not
ordinary cleanup.

Prompt:

> - During consolidation, look for memory that describes a recurring way an
>   agent should act but is not yet written as reusable instruction.
> - Strong instruction-like or prompt-like memories may become `S#` memory
>   skills backed by `MEMORY_SKILL_<slug>.md`.
> - Preserve ordinary memory for facts, context, and preferences.
> - Use skills for reusable agent instructions.
> - Create or update a skill when the instruction is reusable, specific enough
>   to apply, and better represented as guidance than as a remembered fact.
>   [[Do not convert ordinary decisions or context into a skill just because the
>   text is long or imperative.]]

Boundary notes:

- Skillifier can run late in the Dreamer cycle after structure and quality
  cleanup.
- Ordinary decisions and context remain ordinary memory.
- It may need its own stricter source-file rules if implementation creates
  `MEMORY_SKILL_<slug>.md` files.

### Commit And Report

Source: `Dreamer: Commit / Final Reply`.

This remains the common ending for any Dreamer-owned edit cycle.

Prompt:

> - Commit changes after editing.
> - Stage touched `MEMORY*.md` files. [[Do not commit unrelated files.]]
> - Use the commit subject as the title.
> - Put the dreamer report in the commit body. [[Cover what matters: what you
>   did, what requires user attention, anything noteworthy you observed, and
>   which modules acted.]]
> - Dreaming must be stable: running it again on the same memory should produce
>   no further changes.
> - If the files are already in good shape, skip the commit and return a concise
>   no-op. [[Do not create an empty commit or rewrite memory just to have a
>   commit.]]
> - Final replies should summarize the result. [[Include the number of light
>   fixes applied, deep restructures applied, durable open questions surfaced or
>   refined, and the resulting commit hash or `no commit`.]]

Boundary notes:

- The role split decision says the division should stay visible in prompts,
  tests, commit subjects, and final reports.
- The report should say whether deterministic preflight, structure organizer,
  active memory quality cleanup, behavior memory scope/compaction, or
  skillifier did meaningful work.

## Possible Progressive Disclosure Flow

This is one possible progressive-disclosure flow if Dreamer remains the
orchestrating role name. These are separately revealed prompt/tool stages, not
one combined prompt:

1. Shared setup
2. Deterministic mechanical preflight
3. Structure organizer
4. Active memory quality cleanup
5. Behavior memory scope and compaction
6. Skillifier when instruction-like memory remains
7. Commit and report

This keeps Dreamer as a staged organizer-orchestrator without making Dreamer do
all meaning-based cleanup in one paragraph.

## Open Design Questions

These are parking-lot questions for later module-by-module discussion. The user
does not need to answer them all at once.

1. Should deterministic mechanical preflight run before the model turn, or
   should it run inside the Dreamer cycle and provide a preflight report to the
   model?
2. How much prior-step summary should be passed to the next module prompt?
3. Until archive support exists, when should active memory quality cleanup
   refuse compression because detail would leave active memory?
4. Should behavior memory scope and compaction run every Dreamer cycle, or only
   when behavior/preference domains changed or became large?
5. Should Skillifier remain a conditional Dreamer module, or should it become a
   separate explicit lifecycle step later?
