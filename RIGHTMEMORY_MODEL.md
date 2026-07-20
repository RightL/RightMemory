# RightMemory Model

This document records the settled target model for the current RightMemory redesign. It describes the system agents and maintainers should build toward; exact command names and polling intervals may be chosen during implementation.

## One graph organized into two document trees

RightMemory is one addressable graph organized into two Markdown document trees:

- `MEMORY.md` contains what remains durable: knowledge, context, preferences, decisions, constraints, and reusable guidance.
- `PURSUITS.md` contains what remains live: intent, Focus, current state, and commitments that should still shape future action.

All addressable headings and nodes share one globally unique id namespace. Edges may connect Memory and Pursuit directly. The trees differ by lifecycle and meaning, not by graph membership.

A Pursuit is not a backlog or work log. It records intent that is still live when the updater reconciles the current candidates. Task duration is irrelevant: work completed within the same session normally leaves no Pursuit, while even a short task may belong there when a future agent still needs to continue, wait, or reconsider it. Completed or obsolete intent is removed; only consequences that independently meet Memory's durability standard move to Memory.

## Files and stored-document semantics

The parsed graph begins at `MEMORY.md` and `PURSUITS.md` and recursively follows F# backing files. The root files remain useful documents rather than routing-only indexes.

An `F#id` heading keeps the heading in its containing file and moves its child content into a sibling detail file selected by that document tree:

- Memory F# maps to `MEMORY_<id>.md`.
- Pursuit F# maps to `PURSUIT_<id>.md`.

M# and S# are Memory-only linked-resource forms. Their headings remain addressable graph objects, but their backing files are not parsed as graph content:

- An `M#id` heading points to free-form `MEMORY_<id>.md` content, including curated evidence that is useful to consult but should not become executable instruction.
- An `S#id` heading points to a reusable agent instruction in `MEMORY_SKILL_<id>.md`.

File globs do not determine graph membership. Updater corrections are not M# or S# content.

The shared schema defines graph and file semantics, `PURSUIT_RULES.md` defines Pursuit-specific maintenance judgment, and the Memory and Pursuit examples show valid starting shapes. RightMemory does not add a parallel documentation-first instruction layer.

## Agent-facing skills

RightMemory installs two independent, user-selected skills. Neither has trigger priority over the other.

- `memory-retriever` provides read-only retrieval from relevant RightMemory context and never submits updates.
- `rightmemory-orchestrator` conditionally retrieves relevant RightMemory context and maintains the full Memory + Pursuit state.

The orchestrator does not retrieve everything before every task. It preserves the existing conditional policy: retrieve factual, project, or domain context when the conversation lacks background needed to work well; skip it when the request is self-contained; retrieve preference, workflow, and behavior guidance more proactively when those concerns will shape the work. Correction M# bodies are the exception: consult them only after an initial draft, design, or implementation direction exists.

The previous Memory-only orchestrator is superseded and should be removed during implementation.

## Orchestration and candidate submission

When it actually begins non-trivial work, `rightmemory-orchestrator` submits a concise task-state candidate. If retrieval found a related Pursuit, the candidate identifies that relationship; otherwise the orchestrator makes no claim about whether a Pursuit already exists. It also submits when work completes, becomes blocked, changes direction, or reaches a handoff-worthy state, even if the task was initially too small to receive a start candidate.

Submission is evidence for the updater, not a request to create a Pursuit or Memory entry. The orchestrator applies the existing strict Memory filter to possible durable content: prefer natural project artifacts when they already preserve the useful information, and do not treat ordinary bug causes, implementation steps, experiment runs, or task results as Memory merely because work occurred.

An orchestrator with update authority may also submit the concrete event behind an explicit user correction: what the user requested, what the agent proposed or did, what the user rejected, and what was accepted. It does not decide the final correction category or force persistence.

## Unified updating

RightMemory has one updater. It treats related candidates for the same work or Pursuit as an evolving account, reconciles them as a whole, and prefers the latest supported state. Session ids provide conversation provenance and batching boundaries; they do not imply that every candidate in one session belongs to one task. The updater compares each account with the current graph and asks:

- What remains live?
- What became durable beyond the natural artifacts that already record the work?
- What should be omitted as transient, duplicative, weakly supported, overly granular, or already preserved elsewhere?

Work that began and completed within the candidate batch does not leave a Pursuit. Work that remains active, blocked, waiting, or ready for later continuation may create or update one. A related existing Pursuit is updated instead of duplicated. Completed or obsolete Pursuits are removed; a parked Pursuit remains only while future reconsideration is still intended.

Memory remains selective. It is not a bug database, implementation log, experiment ledger, or duplicate of project-local artifacts. A task outcome enters Memory only when it has independent durable value for future agents.

The updater may change Memory, Pursuit, both, or neither. Coordinated changes land in one isolated transaction and one commit, so closing live intent and preserving an independently durable consequence remain one state transition. Pursuit uses the same candidate queue, updater, model role, and schedule as Memory.

Validation covers the complete RightMemory graph. Memory-oriented maintenance roles may keep narrower write authority, but they must preserve ids and edges referenced from Pursuit rather than leaving dangling cross-tree relationships.

## Correction feedback

RightMemory keeps two correction channels because they improve different work.

### General agent corrections

Corrections to an agent's ordinary writing, reasoning, decisions, or actions may become Memory when their rejected/accepted contrast is reusable evidence for future second-pass review. The updater maintains two fixed M# collections:

- `MEMORY_agent-corrections-writing.md` contains corrections where changing expression or presentation would resolve the objection.
- `MEMORY_agent-corrections-design.md` contains corrections where the underlying reasoning, decision, behavior, or action must change.

If a concise directly executable instruction captures the correction better, update ordinary Agent Behavior or S# instead of preserving duplicate correction evidence. A correction to a RightMemory state edit belongs only to updater correction feedback, not to these M# collections.

Each collection is a bounded curated set rather than an append-only log or FIFO window. A represented pattern improves its existing item or replaces weaker evidence. A distinct pattern is retained only when sufficiently reusable; when the collection is full, it replaces an existing item only if it is more important. If every existing item is more important, the candidate is discarded.

Importance reflects likely recurrence, cost if repeated, applicability across future tasks, strength of the user's correction, and whether existing guidance already covers it. Each collection may contain at most 15 compact items, but 15 is a ceiling rather than a target or automatic eviction trigger. General agents consult the relevant collection after forming an initial draft, design, or implementation direction.

### Offline update review

Every normal unified-updater commit produces one local Markdown review document; correction and maintenance commits do not. Its generated portion explains the update naturally and may include the relevant diff, but it has one free-form human comment area for the whole update and one visible `Ready for correction` checkbox. There are no per-group comment fields or required web UI.

Review files remain under runtime state and are checked periodically. A non-empty comment is inert until Ready is checked; no file-age or modification-time heuristic infers submission. The Markdown owns the human text and submission intent, while the single process lock carries only the bounded fairness cursor needed to attempt one Ready revision per scan. A review id plus normalized-comment hash identifies the correction operation, so retries replay one semantic transaction instead of running duplicate corrections.

The separate internal update-corrector receives the submitted comment plus original-update context re-derived from the durable operation receipt and Git; it does not trust the editable displayed diff. It preserves unrelated later work and commits nothing when the requested result is ambiguous or already satisfied. A clarification question is written back only if the exact submitted document is unchanged, and Ready is cleared so the human can revise and explicitly resubmit. Resolved documents are removed with the same exact-document comparison, failures leave Ready checked for retry, and untouched blank documents expire under bounded retention.

### Updater correction feedback

A successful state correction produces zero or more feedback candidates for `corrections.md` at the RightMemory root. The file is human-readable Markdown using this natural entry shape:

```md
## Short correction title

### Background

...

### Proposed edit

...

### Accepted edit

...
```

`corrections.md` is tracked and synchronized, but it is not Memory and is not part of the graph. Only the updater consumes its semantic content, after forming a tentative update. Sync machinery transports it; if a conflict requires repair, it preserves non-identical entries without ranking them and leaves semantic merging or replacement to the updater.

The requested state correction lands whenever it succeeds. Its feedback candidates may be merged, admitted, or discarded independently. Any resulting `corrections.md` change lands in the same commit, but rejecting a feedback candidate never blocks the state correction. Failed or ambiguous corrections add no feedback.

`corrections.md` follows the same bounded priority principle as the M# collections: represented patterns improve existing examples; distinct candidates are admitted only when useful enough; a full file rejects a candidate unless it is more important than an existing item. Its 15-entry limit is a ceiling, not a reason to evict automatically. There is no JSON duplicate, visible scope label, or generalized lesson-generation pass.

Existing updater-edit corrections stored in the old Memory-backed form may be imported into `corrections.md`; after import they are no longer Memory. The same correction is not stored in both channels.

## Runtime ownership

Unified updates run in isolated worktrees, validate the complete graph, and land only completed role-owned commits. Review documents remain local runtime artifacts; Memory, Pursuit, and `corrections.md` are ordinary synchronized RightMemory state.

Memory-oriented Dreamer, Insight, Historian, and Pruner remain narrower than the unified updater and do not independently maintain Pursuit or the curated correction collections. Transcript review extracts candidates from idle sessions and submits them through unified update rather than editing the graph independently. The unified updater owns lifecycle transitions between live Pursuit and durable Memory as well as admission to correction feedback.

## Compatibility posture

The redesign favors one coherent current model over compatibility layers. Superseded managed prompts, schema wording, tests, skills, and code paths should be replaced or removed instead of kept through aliases and dual formats. Current user-authored Memory, Pursuit, and correction content remains protected from accidental overwrite; obsolete managed behavior does not receive elaborate migration machinery.
