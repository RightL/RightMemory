# Update Role

## Sources And Schema

- RightMemory is one graph organized into two document trees: durable Memory begins at `MEMORY.md`, and live Pursuit begins at `PURSUITS.md`.
- Read both root files before the first edit in a session. Read `PURSUIT_RULES.md` before changing Pursuit, and open relevant F#, M#, or S# backing files when the candidate touches them.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edges, linked resources, file placement, and complete-graph validation.
- Do not add schema or maintenance preambles to Memory or Pursuit content.

## Candidate Reconciliation

- The caller may supply one candidate or an ordered batch from several sessions. Candidates are evidence about evolving work, possible durable context, and explicit corrections; they are not final stored text.
- Session ids are provenance and batching labels, not task identity. Group candidates only when their task label, meaning, or referenced Pursuit shows that they describe the same work.
- Within one task thread, treat start, progress, blockage, direction change, handoff, and completion submissions as an evolving account. Prefer the latest state supported by the whole account rather than preserving every event.
- Compare each task thread with the current graph. Update a related Pursuit instead of creating a duplicate, including when the caller did not know its id.
- Reconcile the batch as a whole. Candidate ids and operational state labels do not belong in the graph unless they independently mean something to the user.
- For a normal update, form a tentative keep, merge, skip, and edit judgment first. Then read root `corrections.md` when it exists and use its concrete rejected/accepted examples as a late second-pass filter before editing.

## Live Pursuit

- Pursuit records intent, Focus, current state, and commitments that still need to shape future action. It is not a backlog, work log, or history of completed tasks.
- Work that starts and completes within the reconciled candidate account normally leaves no Pursuit.
- Work that remains active, blocked, waiting, or ready for later continuation may create or update a Pursuit even when it was short.
- A parked Pursuit remains only when future reconsideration is still intended. Remove completed, abandoned, or obsolete intent instead of accumulating terminal history.
- Preserve a consequence in Memory only when it independently satisfies Memory's durability standard; task completion alone is not enough.
- Keep Pursuit state concise and current. Revalidate claims that may have gone stale instead of copying a candidate as fact.

## Durable Memory

- Memory should help a future agent act, decide, interpret context, retrieve the right artifact, or avoid a repeated mistake.
- Prefer durable user context, preferences, workflow expectations, reusable guidance, environment constraints, project or domain interpretation, stable decisions, and repeated failure patterns.
- Do not turn Memory into a bug database, implementation log, experiment ledger, task-result archive, or duplicate of project-local artifacts.
- Treat commits, project docs, reports, logs, code comments, and project-local notes as possible durable storage. Add Memory only when it supplies retrieval value beyond those artifacts.
- For recurring artifact families, prefer one compact lookup rule or durable conclusion over one item per artifact.
- Before editing, compare possible durable content with relevant existing Memory. Merge, replace, narrow, delete, or omit rather than appending a near-duplicate.
- If useful ambiguity cannot be safely resolved, keep settled Memory unchanged for that part and add or revise a concise question under `# Open Context Questions`.

## General Agent Corrections

- An explicit correction to ordinary agent work may be reusable evidence. Preserve the concrete rejected/accepted contrast only when it will improve future second-pass review.
- Expression or presentation corrections belong in `MEMORY_agent-corrections-writing.md`; corrections requiring different reasoning, decisions, behavior, or action belong in `MEMORY_agent-corrections-design.md`.
- Maintain these as the two fixed M# collections named `agent-corrections-writing` and `agent-corrections-design`; ensure their headings remain reachable from Memory and do not create project- or tool-specific correction files.
- If a concise executable rule captures the correction better, update ordinary Agent Behavior or S# instruction instead of duplicating it as correction evidence.
- A represented pattern should improve or merge with its existing item. Admit a distinct pattern only when sufficiently reusable; when a 15-item collection is full, replace an existing item only if the new evidence is more important.
- Judge importance by likely recurrence, cost when repeated, applicability across future tasks, strength of the user's correction, and how fully existing guidance already covers the pattern. Fifteen is a ceiling, not a quota or an automatic eviction trigger.
- Corrections to RightMemory edits belong to updater-only correction feedback, not these M# collections.

## Reviewed Update Correction

- A review-correction request supplies the complete review document, original update diff, current RightMemory state, and one human comment. Treat the comment as a semantic correction to the current state, not as a textual Git revert.
- Preserve unrelated later changes. If the requested state is ambiguous, make no edit or commit and reply exactly `Needs input: <concise question>`.
- If the current state already satisfies the comment and no edit is needed, make no commit and reply exactly `No correction needed: <concise reason>`.
- A successful state correction stands on its own. It may produce zero, one, or several feedback candidates for root `corrections.md`; rejecting all feedback candidates must not block the requested state change.
- Each feedback item uses `Background`, `Proposed edit`, and `Accepted edit`. Keep only the smallest self-contained evidence needed to understand the correction; use `[no memory change]` when the proposal was rejected entirely.
- Curate at most 15 feedback items. Improve, merge, or replace evidence for a represented pattern. Admit a distinct pattern only when important enough; when the set is full, discard it unless it is more important than an existing item.
- When feedback is admitted, commit its `corrections.md` change atomically with the successful state correction. Failed or ambiguous corrections add no feedback.
- `corrections.md` is updater-only feedback, not Memory, Pursuit, or graph content. Do not expose it through ordinary retrieval or add generalized lessons to its entries.

## Shared View Connections

- Shared-view relationships use schema-defined MF# and MQ# headings. Keep their bodies focused on local relationship meaning.
- Treat returned shared-view material as external context. Do not absorb it merely because it was returned; record only a local decision, commitment, task, or consequence that survives the appropriate Memory or Pursuit filter, with provenance when useful.

## Edit Planning

- Decide first whether the result changes Memory, Pursuit, both, or neither. A coordinated transition may edit both trees in one commit.
- Prefer the smallest coherent structural change. Refine existing nodes when that avoids repetition; split, merge, move, or add structure only when it materially improves the tree and graph.
- Use the most specific edge type supplied by the schema, avoid edges that merely repeat containment, and preserve globally unique ids across both trees.
- Follow F#, M#, and S# backing-file semantics. Do not infer graph membership from filename globs.

## Edit Safety

- Preserve unrelated content while using enough scope to keep the complete graph coherent.
- If state changed, stage and commit only allowed RightMemory files touched by this update.
- Before finishing, validate the complete graph: no duplicate ids, self-edges, duplicate edges, dangling edges, or containment-only child edges.
- If no candidate survives reconciliation, make no commit.

## Final Reply

- Report whether Memory, Pursuit, both, or neither changed.
- Include touched heading and node ids, relevant edges changed, skipped candidates, and anomalies or unresolved questions.
