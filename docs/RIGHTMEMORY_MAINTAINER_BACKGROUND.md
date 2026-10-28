# RightMemory Maintainer Background

This document gives fresh agents the context needed to review or modify RightMemory. It explains intent, reader boundaries, and settled rationale. It is non-normative: the shared domain cores, schema, focused rules, runtime contracts, and role prompts own the concerns they define.

## Purpose

RightMemory should help later agents understand the user and other people, collaborate with fewer repeated corrections, and repeat meaningful work with less human direction. One daily entry uses three domain curators:

| Domain | Shared core | Curated state |
|---|---|---|
| Ordinary Memory | `MEMORY_RULES.md` | The Memory graph, including user and people context |
| Agent guidance | `AGENT_GUIDANCE_RULES.md` | `learning/guidance/` and the existing Agent Correction collections |
| Work experience and skills | `WORK_SKILLS_RULES.md` | `learning/experiences/` and `learning/skills/` with supporting assets |

Each domain uses the same core for candidate updates and requested maintenance. The mode changes the task scope, not the purpose of the learning. Cores express durable intent; role prompts supply execution procedure; supporting details can evolve without rewriting that intent.

Pursuit remains the user's hierarchical direction map. Ordinary learning does not change it. Project artifacts remain the primary home for implementation history and operational detail; retained learning adds the context, judgment, or resources that help later work.

## Evidence and Curation

All evidence uses one submission path. Memory, guidance, and skills can draw different useful conclusions from the same interaction and record processing independently. The main agent preserves evidence; it need not predict the final domain or wording.

Capture concrete potentially useful evidence before demanding a settled general rule. Preserve the circumstances, user's contribution, observed outcome, and uncertainty needed to interpret it. Formal curation makes a separate judgment about usefulness, recurrence, scope, and strength of evidence. Pending candidates are retrievable before curation. After all three domains finish, the program removes the raw source. Each curator preserves independently useful context, cases, or experience in its owned state before completing the task.

Requested maintenance starts from existing scoped material and uses the same task preparation, domain core, and publication controls as Update. It leaves pending evidence and processing records to the program. No per-candidate human approval queue is required. The user's current direction remains authoritative over an old or overbroad interpretation.

## Readers and Responsibilities

| Component | Reader | Owns |
|---|---|---|
| Shared domain cores | The corresponding update and maintenance agents | Purpose, learning judgment, domain ownership |
| `rightmemory-schema.md` | Retrieval, curators, maintainers | Graph and learning representation and validity |
| `PURSUIT_RULES.md`, `AGENT_CORRECTION_MEMORY_RULES.md` | Relevant curators and maintainers | Focused map and bounded-case semantics |
| `RIGHTMEMORY_EDIT_CORRECTION_RULES.md` | Curators and maintainers | Operational feedback on RightMemory edits |
| `retrieve.md`, `assistant-retrieve.md` | Retrieval agents | Relevance judgment and source inspection |
| `RETRIEVE_CONTRACT.md` | Runtime and internal retrieval agents | Selection and delivery mechanics |
| `update.md`, `guidance-curator.md`, `skills-curator.md` | Domain agents | Bounded execution procedure |
| Daily entry skills and MCP instructions | The main working agent | Retrieval, shared evidence submission, and native task delegation |
| `maintain-rightmemory`, `maintain-agent-guidance`, `maintain-work-skills` | Requested domain maintainer | Broader maintenance within the user's scope |
| `learning.py` and execution adapters | Runtime | Durable evidence, per-domain receipts, isolation, validation, and publication |

One concept should have one normative owner. Other files refer to it rather than restating the full definition. Program-managed evidence and state transitions are enforced by code, not recreated as instructions for every agent.

## Curated State

Agent Corrections remains the existing bounded Expression/Substance case library, now owned by the guidance curator. It is not duplicated in a second case library. A useful generalized rule and a concrete case can coexist when they add distinct value.

Graph-linked S# instructions remain supported, including in shared views. Their representation is separate from who may maintain their bodies. Historical data and retired interfaces do not require runtime compatibility; [UPGRADING](UPGRADING.md) gives an agent the context for requested cleanup of an actual installation.

Root `corrections.md` remains RightMemory Edit Feedback. It is operational curation feedback, not ordinary user memory, behavioral guidance, or a graph item. Curators form a tentative judgment before consulting relevant feedback.

## Retrieval and Visible Behavior

Relevant pending evidence, curated guidance, correction cases, experiences, and complete skills participate in ordinary retrieval. Claims, processing receipts, and withdrawals do not. Retrieval remains driven by the current need; program completion does not create a second raw-history pool.

Applicable guidance should affect the work itself. Ordinary learning does not require a visible acknowledgement, a fixed phrase, or a per-item review ceremony. A candidate receipt confirms durable capture; a completion receipt confirms publication. Neither establishes that the learned behavior has been empirically validated on future work.

## How to Modify RightMemory

Read the current canonical files before proposing changes. Identify which reader owns the decision and which evidence it will receive. Keep the shared intent stable and change the smallest layer that addresses the actual problem.

Separate source evidence, interpreted guidance, reusable method, and execution authority in judgment without imposing a fixed body schema on every document. Prefer meaningful boundaries and useful natural-language context to new taxonomies, duplicate libraries, or protocols for hypothetical scale.
