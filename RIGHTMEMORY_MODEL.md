# RightMemory Model

This document records the current RightMemory model for agents and maintainers.

## Three semantic modules

RightMemory has three semantic modules:

- `MEMORY.md` contains what remains durable: knowledge, context, preferences, decisions, constraints, and reusable guidance.
- `PURSUITS.md` contains what remains live: intent, Focus, current state, and commitments that should still shape future action.
- Agent Corrections contains bounded, reusable cases in which a user redirected prior agent work.

Memory and Pursuit form one addressable graph organized into two Markdown document trees. Their addressable headings and nodes share one globally unique id namespace, and edges may connect the trees directly. Agent Corrections is a fixed, non-graph case library with its own representation and retention judgment. Root `corrections.md` contains RightMemory Edit Feedback and is not a fourth semantic module.

A Pursuit is not a backlog, work log, or resume transcript. It remains only while its objective belongs in the active or deliberately parked pursuit hierarchy. Duration and incompleteness are insufficient, and detailed execution continuity belongs primarily in project-local artifacts. Completed, abandoned, or superseded intent is removed; only consequences that independently meet Memory's durability standard move to Memory.

## Files and stored-document semantics

The parsed graph begins at `MEMORY.md` and `PURSUITS.md` and recursively follows F# backing files. The root files remain useful documents rather than routing-only indexes.

An `F#id` heading keeps the heading in its containing file and moves its child content into a sibling detail file selected by that document tree:

- Memory F# maps to `MEMORY_<id>.md`.
- Pursuit F# maps to `PURSUIT_<id>.md`.

M# and S# are Memory-only linked-resource forms. Their headings remain addressable graph objects, but their backing files are not parsed as graph content:

- An `M#id` heading points to free-form `MEMORY_<id>.md` content, including curated evidence that is useful to consult but should not become executable instruction.
- An `S#id` heading points to a reusable agent instruction in `MEMORY_SKILL_<id>.md`.

File globs do not determine graph membership. Agent Corrections and RightMemory Edit Feedback are neither M# nor S# content.

Package-owned definitions live under `rightmemory/reference/` and are not Memory-root state. The schema defines representation; Memory, Pursuit, and Agent Corrections rules define module admission and maintenance; RightMemory Edit Feedback and Shared View rules define those focused domains; and the Retrieve contract defines runtime input and terminal-selection mechanics. The Memory and Pursuit examples remain installer seeds rather than authoritative product definitions.

## Agent-facing skills

RightMemory installs four user-selected skills:

- `memory-retriever` provides read-only retrieval from relevant RightMemory context and never submits updates.
- `rightmemory-orchestrator` conditionally retrieves relevant context and normally proposes qualifying evidence at a natural boundary before submitting it with user approval; known-bad retrieved state is the immediate-submission exception described below.
- `rightmemory-auto-orchestrator` uses the same retrieval and evidence judgment but submits qualifying evidence automatically at a natural boundary.
- Explicit-only `maintain-rightmemory` lets the current agent directly maintain Memory, Pursuit, linked content, Agent Corrections, and RightMemory Edit Feedback without submitting candidates or invoking another model role.

The two orchestrators are alternative modes, not sequential stages. Both are installed, and the user selects one for the conversation; the agent does not invoke both. No installer flag, CLI flag, profile value, or persisted RightMemory setting chooses between them.

Once selected, `memory-retriever` calls Retrieve for the user's stated memory need. The two orchestrators call it conditionally when stored context could materially affect how the agent understands or approaches the current work and skip clearly self-contained requests. Ordinary Retrieve receives the fixed Agent Correction collections and may select relevant complete entries through `AC#writing` and `AC#design`. Those are retrieval-only source identifiers, not graph markers. Their ids are one-based entry positions and their ranges are empty. Whole-collection `agent-corrections writing` and `agent-corrections design` calls remain explicit review operations, not a separate routine retrieval pass.

## Orchestration and evidence submission

Both orchestrators use the same high evidence bar. Evidence qualifies when omitting it would likely cause poorer future decisions or substantial rediscovery, lose a meaningful pursuit and its direction, or make repetition of a settled reusable failure pattern more likely. Transient progress, routine task results, unfinished work by itself, and implementation detail already preserved in project-local artifacts do not qualify.

Once qualifying evidence is clear and the conversation reaches a natural boundary, the approval-gated orchestrator names the apparent module and reason, then waits for approval before submitting. The automatic orchestrator submits at that boundary without pausing. Completion is not required, and the beginning or end of work alone does not trigger a submission. Known stale, wrong, misleading, or overbroad retrieved state is an immediate-submission exception because continuing from it would knowingly propagate bad context.

Submission is evidence for Update, not final stored wording, classification, placement, ids, or an instruction to edit a particular module. A settled user redirection can qualify whether it is explicit or implicit; the evidence preserves the original need, identifiable prior attempt or omission, redirection, resulting direction, and scope. Update decides whether that contrast is reusable and where it belongs.

## Unified updating

RightMemory has one updater. It groups related candidates by meaning, reconciles each evidence account as a whole, and prefers the latest supported state. Session ids provide conversation provenance and batching boundaries; they do not imply that every candidate in one session belongs to one task. The updater compares the evidence with current semantic state and asks:

- What remains live?
- What became durable beyond the natural artifacts that already record the work?
- What settled user-redirection contrast deserves a reusable Agent Correction?
- What should be omitted as transient, duplicative, weakly supported, overly granular, or already preserved elsewhere?

Pursuit admission depends on meaningful continued membership in the pursuit hierarchy. Blockage, waiting, or handoff may support that judgment, but none admits an objective automatically, and unfinished operational work alone belongs in project artifacts. A related existing Pursuit is updated instead of duplicated. Completed, abandoned, or superseded Pursuits are removed; a parked Pursuit remains only while future reconsideration is still intended.

Memory remains selective. It is not a bug database, implementation log, experiment ledger, or duplicate of project-local artifacts. A task outcome enters Memory only when it has independent durable value for future agents.

The updater may change Memory, Pursuit, Agent Corrections, any combination of them, or none. Coordinated changes land in one isolated transaction and one commit, so closing live intent, preserving an independently durable consequence, and admitting a reusable redirection case can remain one state transition. Updater-driven changes to all three modules use the same candidate queue, role, and schedule.

Validation covers the complete RightMemory graph. Memory-oriented maintenance roles may keep narrower write authority, but they must preserve ids and edges referenced from Pursuit rather than leaving dangling cross-tree relationships.

## Agent Corrections and edit feedback

RightMemory keeps Agent Corrections separate from RightMemory Edit Feedback because they improve different work.

### Agent Corrections

Agent Corrections preserves concrete, reusable contrast between identifiable prior agent work, the user's redirection, and the resulting direction. A redirection may be explicit or implicit, but the outcome and scope must be clear enough to preserve accurately. Update maintains two fixed standalone collections outside the addressable graph:

- `MEMORY_agent-corrections-writing.md` contains **Expression Corrections**, where changing expression or presentation alone fully resolves the objection.
- `MEMORY_agent-corrections-design.md` contains **Substance Corrections**, where reasoning, assumptions, decisions, actions, omissions, workflow, or behavior must change.

The physical filenames, CLI arguments, and `AC#writing` / `AC#design` retrieval identifiers retain `writing` and `design`; the Expression/Substance test is authoritative. Do not duplicate the same lesson in ordinary Agent Behavior or S# unless that representation adds distinct value. Feedback about a proposed RightMemory edit belongs to the operational edit-feedback surface rather than these semantic collections.

Each collection is a bounded curated set rather than an append-only log or FIFO window. A represented pattern improves its existing item or replaces weaker evidence. A distinct pattern is retained only when sufficiently reusable; when the collection is full, it replaces an existing item only if it is more important. If every existing item is more important, the candidate is discarded.

Importance reflects likely recurrence, cost if repeated, breadth of future applicability, strength and clarity of evidence, and whether existing guidance already covers it. Each collection may contain at most 10 entries and 180 non-empty lines, with at most 16 non-empty lines per entry and 200 characters per line; 10 is a ceiling rather than a target or automatic eviction trigger. Ordinary Retrieve selects a relevant complete entry when its failure pattern could materially affect the current query.

### Update provenance

Every queued updater outcome retains its exact candidate batch in
`update_records/<operation-id>.json`. The immutable record lands in the same
commit as the semantic state change; a no-change outcome lands as a record-only
commit. The record filename and commit operation trailer bind input to outcome,
while Git supplies the authoritative diff without duplicating it in another
artifact. Local and synchronized processing use the same identity. Explicit
Update turns without queued candidates create no provenance artifact.

A correction to an earlier updater result uses the same candidate type, queue,
lease, updater role, and record contract as any other update. There is no
correction-specific queue path or runtime role.

### RightMemory Edit Feedback

Reusable feedback about proposed edits of RightMemory may be curated in `corrections.md` at the RightMemory root. The package-owned RightMemory Edit Feedback rules define the collection. Each entry uses:

```md
## Short correction title

### Candidate

...

### Proposed edit

...

### Accepted edit

...
```

`Candidate` preserves every candidate that materially shaped the edit, using its actual relevant text rather than an id, record path, or paraphrase. `Proposed edit` and `Accepted edit` preserve the smallest exact, self-contained RightMemory fragments needed for comparison, with `[no change]` when a proposed file edit was rejected entirely.

`corrections.md` is tracked and synchronized, but it is not semantic RightMemory state, Agent Corrections, or graph content. Sync machinery transports it; if a conflict requires repair, it preserves non-identical entries without ranking them and does not perform semantic curation.

The collection follows the same bounded priority principle as the Agent Correction collections: represented patterns improve existing examples; distinct examples are admitted only when useful enough; a full file rejects an example unless it is more important than an existing item. Its 10-entry limit is a ceiling, not a reason to evict automatically. Update forms a tentative edit before reading relevant entries as a late check. Explicit session review likewise forms tentative proposals before consulting the file and does not modify it during that review. Ordinary Retrieve excludes it.

## Runtime ownership

Unified updates run in isolated worktrees, validate complete role-owned state, and land only completed role-owned commits. Runtime adds one immutable candidate record to each queued outcome, and Git synchronizes it alongside Memory, Pursuit, Agent Corrections, and `corrections.md`.

Memory-oriented Dreamer, Insight, Historian, and Pruner remain narrower than the unified updater and do not independently maintain Pursuit or Agent Corrections. Automatic idle-session review is a read-only candidate extractor; Update owns any resulting semantic change. Explicit session review is a separate direct-curation workflow for eligible Memory, Cross-Session Agent Behavior, and Agent Corrections, and it consults RightMemory Edit Feedback only after forming tentative proposals. Explicit direct maintenance applies the same package definitions without entering the candidate pipeline.

## Compatibility posture

The redesign favors one coherent current model over compatibility layers. Superseded managed prompts, schema wording, tests, skills, and code paths should be replaced or removed instead of kept through aliases and dual formats. Current user-authored Memory, Pursuit, and correction content remains protected from accidental overwrite; obsolete managed behavior does not receive elaborate migration machinery.
