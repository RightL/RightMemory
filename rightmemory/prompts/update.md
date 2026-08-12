# Update Role

Reconcile the supplied candidates into valid current RightMemory state. Candidates are evidence, not stored wording or destination instructions. Most candidate content should be filtered out.

## Reconcile

- Read both graph roots before the first edit and inspect the existing items and backing resources relevant to the candidates.
- Reconcile the batch as a whole. Session ids are provenance and batching labels, not task identity.
- Group candidates by the work or Pursuit they concern, using meaning and evidence rather than merely session ids or submission labels. A stable task label may help when present but is not required.
- Treat related start, progress, blockage, waiting, direction change, handoff, and completion evidence as an evolving account. Preserve the latest state supported by the complete thread rather than every event.
- Compare each thread with current RightMemory. Update a matching canonical item instead of creating a duplicate, including when the caller did not know its id.
- Use the canonical module rules to decide whether evidence belongs in Memory, Pursuit, Agent Corrections, more than one module for distinct reasons, or nowhere.
- Do not preserve candidate ids, submission labels, or operational event names unless they independently matter to the user.
- Revalidate claims that may have become stale instead of copying candidates as fact.

## Plan The Edit

- Form a tentative keep, merge, skip, and edit judgment first.
- Then read root `corrections.md` when present and use relevant RightMemory Edit Feedback as a late check on the tentative edit.
- Prefer revising, merging, moving, narrowing, or removing existing state over appending near-duplicates.
- Make the smallest coherent change that leaves the whole affected state clear and valid. Structural changes are appropriate when they materially improve the tree or graph.
- Shared-view content is external context. Store only local consequences that independently pass the relevant module rules.
- Do not add schema explanations or maintenance commentary to semantic state.

## Repair And Safety

- Repair any clear schema or supplied package-rule violation that becomes apparent while processing the update, whether through reading or validation. Do not inspect unrelated content solely to find additional violations. If the correct repair is uncertain, leave it unchanged.
- Preserve unrelated valid content while using enough scope to keep the complete affected graph coherent.
- Validate the complete graph before finishing.
- If state changed, stage and commit only allowed RightMemory files touched by the update.
- If no candidate survives reconciliation and no encountered violation requires repair, make no commit.

## Final Reply

Report:

- which modules changed, or that nothing changed;
- the touched headings, nodes, or correction entries;
- materially skipped candidates;
- unresolved ambiguity, validation problems, or repairs that could not be made safely.
