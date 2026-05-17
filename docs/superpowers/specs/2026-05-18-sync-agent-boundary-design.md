# Sync Agent Boundary Design

## Context

The current reviewer skill distillation branch adds memory-backed skill support on top of `codex/global-memory-sync`. While doing that, it also expanded dirty-state handling in the reviewer prompt and exposed `git_discard` guidance through common write-role instructions.

That makes semantic memory roles think about operational sync problems. The cleaner model is to keep `update`, `dreamer`, and `reviewer` focused on memory content, while sync code and the sync reconciler handle pull, push, dirty state, and Git conflict repair.

## Goal

Keep routine sync automatic and cheap. Code should perform clean pull and push work directly. The sync AI role should run when Git state needs memory-aware reasoning, such as dirty memory files, pull conflicts, or push conflicts that require reconciliation.

Semantic roles should validate and commit their own memory changes. They should not receive prompt instructions to pull, push, inspect dirty sync state, or repair sync conflicts.

## Role Boundary

`update`, `dreamer`, and `reviewer` own semantic memory edits:

- read and edit the relevant memory files;
- validate the RightMemory schema;
- stage and commit the allowed files they changed;
- report semantic anomalies they could not safely resolve.

They do not need sync recovery instructions in their role prompts. They should also not be prompted to call `sync_push`. Normal agents do not spontaneously push or pull when prompts do not ask them to.

`sync-reconciler` owns sync repair:

- dirty memory state that blocks sync;
- pull or merge conflicts;
- push rejection paths that become conflicts after fetch or merge;
- validation and commit of the repaired memory state;
- the final push after repair.

Clean sync does not need an AI role.

## Runtime Flow

Before a semantic writer starts, runtime may run deterministic sync preflight in code:

1. Acquire the memory write lock.
2. Check for conflicted or dirty memory files.
3. If clean and behind, fetch and fast-forward or merge when Git can complete without conflict.
4. If dirty or conflicted, route to sync reconciliation instead of starting the semantic role.
5. If sync is current or cleanly updated, run the semantic role.

After a semantic role commits:

1. Runtime performs the push in code.
2. If the push succeeds, the turn is done.
3. If push can be repaired deterministically, runtime does that without a model call.
4. If the push path creates dirty state or conflicts, route the problem to `sync-reconciler`.

The semantic role does not need to see dirty state. If dirty state exists at turn start, the semantic role should not be launched for that work.

## Prompt Changes

Remove sync-operation instructions from `update`, `dreamer`, and `reviewer` prompts:

- no `sync_push` instruction;
- no pull, push, or dirty-state recovery guidance;
- no reviewer-specific `git_discard` workflow.

Common write-tool guidance should keep the allowed commit boundary, including `skill_artifacts/<slug>/...`, but should not frame `git_discard` as a normal semantic-role task.

The sync reconciler prompt should be the place that describes dirty-state and conflict repair. It can mention `git_discard` when discarding invalid or partial reviewer-owned or memory-owned changes is part of the supplied sync repair context.

## Tool Exposure

The simplest implementation can keep `git_discard` available in the write tool set while removing it from semantic role prompts. If tool exposure becomes noisy or risky, a later pass can narrow `git_discard` to `sync-reconciler`, but that is not required for this design.

`sync_push` should move out of normal semantic-role instructions. Runtime code can call the sync manager directly after a successful commit, so semantic agents do not need to know about push mechanics.

## Tests

Focused tests should cover:

- semantic role prompts no longer mention `sync_push`, dirty-state recovery, or pull/push work;
- sync reconciler prompt still owns conflict and dirty-state repair;
- clean preflight and clean post-commit push complete in code without invoking the sync AI role;
- dirty state before a semantic role prevents that role from starting and routes to sync reconciliation;
- push or pull conflicts route to sync reconciliation;
- clean skill artifact commits still stay within the allowed commit path rules.
