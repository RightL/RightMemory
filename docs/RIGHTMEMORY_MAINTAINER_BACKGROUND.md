# RightMemory Maintainer Background

This document gives fresh agents the context needed to review or modify RightMemory. It explains intent, reader boundaries, and settled rationale. It is **non-normative**: the schema, module rules, runtime contracts, and role prompts remain authoritative for the concerns they own.

## Purpose

RightMemory is curated cross-session context. It should preserve compact state that materially improves future work without becoming a transcript archive, task log, or duplicate project database.

Its three semantic modules are:

- **Memory**: durable context that improves future judgment, action, interpretation, or retrieval.
- **Pursuit**: the user's hierarchical map of ongoing directions and useful entry context.
- **Agent Corrections**: bounded, reusable cases in which user feedback redirected prior agent work.

Project-local artifacts remain the primary home for implementation detail, experiment history, operational state, and detailed continuation instructions. Pursuit may support continuity, but it is not primarily a resume log.

Root `corrections.md` contains **RightMemory Edit Feedback** used to avoid repeated curation mistakes. It is operational feedback, not semantic RightMemory state.

Root `AGENT_GUIDANCE_INBOX.md` contains pending agent-guidance evidence awaiting explicit review. It is Git-tracked and synchronized, but it is not a fourth semantic module and is not exposed to Retrieve, shared views, or ordinary RightMemory model roles.

The governing bias for semantic RightMemory is **high-signal retention rather than maximum capture**: save substantial rediscovery, preserve meaningful intent, improve future decisions, or prevent a reusable failure—not ordinary progress merely because work occurred. The guidance inbox may capture evidence at a lower bar because pending entries cannot affect future agents until they are reviewed and promoted.

## Readers and Responsibilities

| Component | Reader | Owns |
|---|---|---|
| `rightmemory-schema.md` | Retrieve, Update, maintainers | Representation, graph structure, backing forms, validity |
| `MEMORY_RULES.md`, `PURSUIT_RULES.md`, `AGENT_CORRECTION_MEMORY_RULES.md` | Update, maintenance and review workflows | Meaning, admission, scope, lifecycle, and retention of each semantic module |
| `RIGHTMEMORY_EDIT_CORRECTION_RULES.md` | Update, direct maintenance | RightMemory Edit Feedback |
| `retrieve-role.md` | Internal Retrieve agent | Retrieval judgment and selection procedure |
| `RETRIEVE_CONTRACT.md` | Runtime and integration code | Input envelope, selectors, delivery, terminal output |
| `update-role.md` | Internal Update agent | Candidate reconciliation and edit procedure |
| `rightmemory-orchestrator.SKILL.md`, `rightmemory-auto-orchestrator.SKILL.md` | Ordinary main agent | Approval-gated or automatic use of RightMemory |
| `review-agent-guidance-inbox.SKILL.md` | Explicitly invoked general agent | User-reviewed admission or rejection of pending agent guidance |
| `maintain-pursuit-map/SKILL.md` | Explicitly invoked general agent | Requested map edits, separate from Update and ordinary maintenance |
| `rightmemory/guidance.py`, sync and tool boundaries | Runtime | Inbox format, atomic capture, deterministic merge, validation, and model-role isolation |

The orchestrator files are **client skills read by the main working agent**, not internal orchestrator roles. Update is an internal RightMemory agent. The guidance-review skill is a separate direct-curation workflow used through agents such as Codex or Claude Code.

Automatic Reviewer is currently deprecated and outside the active workflow. `review-rightmemory-session` is also not part of the expected ordinary flow; automatic orchestration should capture qualifying evidence during the session, while pending guidance is handled by the dedicated inbox review workflow.

## Ownership Boundaries

A concept should have one normative owner:

- The **schema** defines stored objects, relationships, and validity.
- **Module rules** define meaning, admission, scope, and retention.
- **Role prompts** define invocation-specific procedure.
- **Client skills** define only what the ordinary main agent must decide.
- **Runtime contracts and code** own transport, selectors, caching, suppression, and output mechanics.
- **Validators** enforce mechanically checkable constraints.

Other files may rely on a definition without restating it. A compact operational test is appropriate only when that reader must make the decision itself. The orchestrator therefore contains omission tests for proposal, submission, or guidance capture, but not the complete module rules.

## Settled Design Choices

### Pursuit

Pursuit is a human-owned map of ongoing directions. An item has a title, stable id, position in the tree, and optional Markdown body. Detailed execution continuity belongs in project artifacts. Incompleteness alone does not justify a Pursuit. The human editor and explicitly invoked `maintain-pursuit-map` are the normal semantic write entrances; Update and ordinary maintenance read the map without changing it. See [Pursuit Map](PURSUIT_MAP.md) for use and [design rationale](../DESIGN_NOTES.md) for the ownership boundary.

### Orchestration Modes

Both orchestrator skills share the same retrieval policy and candidate discipline, but guidance capture now differs from formal semantic admission.

- The approval-gated skill proposes qualifying Memory or Agent Correction evidence and submits it to Update only after user approval. An explicit request to submit, save, remember, or follow the evidence in future counts as approval.
- The automatic skill sends qualifying Memory evidence directly to Update at a natural boundary. Neither mode submits or proposes Pursuit changes from ordinary task activity.
- In automatic mode, settled agent guidance or user redirection goes to `AGENT_GUIDANCE_INBOX.md` when the resulting direction is clear and may be useful in similar future work. Unresolved discussion and obviously one-off local adjustments are excluded.
- When the user explicitly asks RightMemory to remember guidance or follow it in future, automatic mode sends it through Update rather than the inbox.
- One interaction may produce both an Update candidate and a guidance-inbox entry when they preserve distinct evidence.

The inbox deliberately lowers the **capture** bar without lowering the formal semantic **admission** bar. Pending guidance does not affect acting agents. A later user-reviewed workflow decides whether it should become Cross-Session Agent Behavior, Agent Corrections, both for distinct reasons, or neither.

This separation allows more reliable capture of implicit and explicit redirections without letting guidance accumulate uncontrollably in formal Memory or consume the bounded Agent Correction collections.

### Agent Guidance Inbox

`AGENT_GUIDANCE_INBOX.md` contains pending entries only. Each entry has a stable generated id, session provenance, a submission timestamp, and free-form Markdown evidence.

The inbox is structurally excluded from the semantic graph, Retrieve, shared views, and all ordinary RightMemory model-role file, search, and Git views. Sync transports it and resolves independent additions mechanically by entry id; it does not ask a model to interpret pending guidance.

`review-agent-guidance-inbox` considers related entries together, compares them with current Cross-Session Agent Behavior and Agent Corrections, and proposes add, merge, replace, remove, covered, reject, or defer outcomes. Only explicitly approved semantic changes are applied. Accepted, rejected, and already-covered entries are removed; deferred and unreviewed entries remain pending. Git history is the audit trail.

### Agent Corrections

A user redirection may be explicit or highly implicit. The core signal is the **settled contrast between the course of identifiable prior agent work and the resulting direction**. Unease, a guiding question, or added information may expose an omission and redirect the work without explicitly saying it was wrong.

The contrast may concern conclusion, scope, assumptions, reasoning, process, omissions, behavior, or presentation. The final conclusion need not change if the user corrected how it was reached, checked, scoped, or explained. A different result alone is insufficient: ordinary continuation, selection among intentionally open options, a new task, or an independent agent correction is not automatically a user redirection.

An inbox entry is evidence, not a pre-classified Agent Correction. Review may instead promote a reusable operating rule to Cross-Session Agent Behavior, preserve a concrete contrast as an Agent Correction, retain both for distinct value, merge with existing guidance, or admit nothing. Formal Agent Corrections remain bounded and high-signal.

### Retrieval and Inactive Review Paths

The two complete but bounded Agent Correction collections are supplied in Retrieve's cached stable context and participate in ordinary retrieval. The guidance inbox never participates in retrieval before promotion.

Do not add a separate correction index, correction-specific pass, progressive correction-reading protocol, or inbox retrieval path without evidence that the bounded design fails.

Automatic Reviewer is deprecated for now. Do not modify or depend on it for the guidance-inbox workflow unless the user explicitly revives that role. `review-rightmemory-session` is likewise not an active dependency of this design.

### Visible Behavior and Repair

RightMemory does not require a visible retrieved-guidance block or a fixed phrase such as `Correction noted.` Applicable guidance should affect the work itself; those mechanisms were compensations for weak agent behavior, not product semantics.

Update may repair a clear schema or supplied-rule violation in its writable Memory or Agent Correction state when that violation becomes apparent while processing an update. Pursuit stays read-only even when a map repair seems useful. Update should not inspect unrelated content merely to turn the work into a general audit, and should report rather than guess when the correct repair is uncertain.

## How to Modify RightMemory

Read the current canonical files before proposing changes; do not reconstruct the design from old versions or memory. First identify which file owns the concept. Then stand in each reader's shoes: **what decisions must this agent make, what context will it receive, and what should it not need to know?**

Keep capture, review, and semantic admission distinct. A pending inbox entry is not formal guidance, and runtime isolation should be enforced structurally rather than repeated as instructions in every prompt.

Precision takes priority over brevity. Shorten by removing duplication, moving mechanics to code or contracts, and narrowing each file to its reader—not by replacing necessary judgment with vague abstraction. Restore older wording only when it expresses a missing decision, not when its meaning is already owned elsewhere.

Avoid broad redesigns for local wording problems and new protocols for hypothetical scale issues. Discuss larger changes one conceptual group at a time, use concrete diffs, and check terminology, references, and reader assumptions after merging. Do not create another all-in-one model document that competes with the canonical schema and rules.
