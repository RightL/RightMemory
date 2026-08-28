# RightMemory Demo Script

This is a short recording plan for a first public demo. It is written for a 90-second terminal video or GIF that can be linked from the README, social posts, and launch threads.

## Story

Show the problem first: a fresh coding-agent session should know relevant durable context, live intent, and reusable user-redirection cases without the user pasting a long summary. Then show RightMemory retrieving a small cross-tree graph slice plus a relevant Agent Correction, submitting only qualifying evidence at a natural boundary, and leaving all three semantic modules as ordinary Git-syncable state that another agent client or device can reuse.

## Setup

Use a small demo memory root so the video stays legible:

```bash
./install.sh --mode cli-agent /tmp/rightmemory-demo ~/.codex/skills
```

Choose the approval-gated `rightmemory-orchestrator` for this recording so the evidence threshold and user control are visible. A separate recording could choose `rightmemory-auto-orchestrator` to submit the same kind of qualifying evidence automatically; do not invoke both in one conversation. Replace
`/tmp/rightmemory-demo/MEMORY.md` with compact durable project context:

```md
# Project Context {#project-context}

## Sync Design {#sync-design}

- `sync-preflight` Runtime checks clean upstream state before automatic semantic work. → []
- `sync-reconciler` Dirty or conflicted memory state is repaired by a bounded sync-reconciler role. → [dep:sync-preflight]
- `sync-conflict-policy` Sync repair preserves non-identical updater-correction entries without ranking them. → [rel:sync-reconciler]
```

Create this small direction map in the disposable demo root:

```md
# Pursuits

## Focus

- `sync-continuity`

## Sync Continuity {#sync-continuity} → [dep:sync-design]

Help the same context remain useful across devices and agent sessions.
Start from the repository's sync documentation.

### Clear Conflict Recovery {#sync-conflict-recovery}
```

Add one small Substance Correction so ordinary Retrieve can demonstrate the third module without a separate collection command:

```md
# /tmp/rightmemory-demo/MEMORY_agent-corrections-design.md

### Do not invent a submission cadence

When describing orchestration, the agent turned contextual evidence judgment into mandatory start and completion updates. The user redirected it toward a high evidence bar and natural-boundary submission. Preserve that distinction when explaining or applying orchestration modes.
```

## Recording Beats

1. Open a fresh Codex or Claude Code session in the RightMemory repo.
2. Ask: `Continue the sync design from last time without requiring me to paste prior context.`
3. Show the user-selected `rightmemory-orchestrator` calling `rightmemory retrieve`.
4. Show a small retrieved result containing the `Sync Design` Memory context, the linked `Sync Continuity` Pursuit, and the complete Substance Correction selected through `AC#design`.
5. Ask the agent to make a tiny doc edit or explain the next implementation step.
6. Let an independently durable decision become clear. At a natural boundary, show the agent proposing why the evidence qualifies, then obtain approval and call `rightmemory update submit`. A routine start or completion should not generate its own proposal.
7. Explain that Update may change Memory or Agent Corrections after reconciling evidence; the direction map stays unchanged. Open `rightmemory pursuit` to show that a human can edit the map directly.
8. End on `git diff`, the relevant semantic files, or `rightmemory status` so viewers see ordinary Git-managed state rather than opaque vendor storage.

## Narration

> RightMemory has three semantic modules: durable Memory, the user's Pursuit map, and reusable Agent Corrections. Memory and Pursuit share one addressable graph, while retrieval can also select relevant correction cases. At a natural boundary, the selected orchestrator proposes qualifying durable evidence for approval or submits it automatically. Update maintains Memory and Agent Corrections. The user owns the map, and Git sync keeps the state portable.

## README GIF Placeholder

Until a real screen recording is captured, the README uses `docs/assets/rightmemory-demo.svg` as a visual explanation. Replace it with a GIF or MP4 thumbnail after recording the flow above.
