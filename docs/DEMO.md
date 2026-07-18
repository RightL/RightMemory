# RightMemory Demo Script

This is a short recording plan for a first public demo. It is written for a 90-second terminal video or GIF that can be linked from the README, social posts, and launch threads.

## Story

Show the problem first: a fresh coding-agent session should know relevant durable context and live intent without the user pasting a long summary. Then show RightMemory retrieving a small cross-tree graph slice, submitting task evidence, and leaving Memory plus Pursuit as ordinary Git-syncable state that another agent client or device can reuse.

## Setup

Use a small demo memory root so the video stays legible:

```bash
./install.sh --mode cli-agent /tmp/rightmemory-demo ~/.codex/skills
```

Choose `rightmemory-orchestrator` for this full-state demo. Replace
`/tmp/rightmemory-demo/MEMORY.md` with compact durable project context:

```md
# Project Context {#project-context}

## Sync Design {#sync-design}

- `sync-preflight` Runtime checks clean upstream state before automatic semantic work. → []
- `sync-reconciler` Dirty or conflicted memory state is repaired by a bounded sync-reconciler role. → [dep:sync-preflight]
- `sync-conflict-policy` Sync repair preserves non-identical updater-correction entries without ranking them. → [rel:sync-reconciler]
```

Replace `/tmp/rightmemory-demo/PURSUITS.md` with the live continuation:

```md
# Pursuits

## Focus

- `sync-continuity` — finish the sync lifecycle demonstration.

## Finish Sync Continuity {#sync-continuity} → [dep:sync-design]

Make the demo show how durable sync context guides work that still needs continuation.

**State:** The durable sync policy is recorded; the demo flow still needs verification.

**Next:**
- `do` Verify the retrieval and update sequence in a fresh agent session.

**Done when:** The recording shows retrieval, candidate submission, and one coherent updater result.
```

## Recording Beats

1. Open a fresh Codex or Claude Code session in the RightMemory repo.
2. Ask: `Continue the sync design from last time without requiring me to paste prior context.`
3. Show the user-selected `rightmemory-orchestrator` calling `rightmemory retrieve`.
4. Show a small retrieved result containing the `Sync Design` Memory context and the linked `Finish Sync Continuity` Pursuit.
5. Ask the agent to make a tiny doc edit or explain the next implementation step.
6. Show the agent submitting start and terminal evidence with `rightmemory update submit`.
7. Explain that the unified updater may change Memory, Pursuit, both, or neither after reconciling the evidence.
8. End on `git diff`, the two root files, or `rightmemory status` so viewers see ordinary Git-managed state rather than opaque vendor storage.

## Narration

> RightMemory keeps durable Memory and live Pursuit in one addressable graph. Agents retrieve only the context they need, submit evidence as work changes, and let one updater reconcile both trees. Git sync keeps the state portable, while explicit role boundaries keep retrieval, updates, and review from becoming tangled with ordinary coding work.

## README GIF Placeholder

Until a real screen recording is captured, the README uses `docs/assets/rightmemory-demo.svg` as a visual explanation. Replace it with a GIF or MP4 thumbnail after recording the flow above.
