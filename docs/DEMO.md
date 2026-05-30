# RightMemory Demo Script

This is a short recording plan for a first public demo. It is written for a 90-second terminal video or GIF that can be linked from the README, social posts, and launch threads.

## Story

Show the problem first: a fresh coding-agent session should know relevant prior project context without the user pasting a long summary. Then show RightMemory retrieving a small tree + graph slice, submitting a durable update after the task, and leaving the memory as ordinary Git-syncable state that another agent client or device can reuse.

## Setup

Use a small demo memory root so the video stays legible:

```bash
./install.sh --mode cli-agent /tmp/rightmemory-demo ~/.codex/skills
```

Seed `/tmp/rightmemory-demo/MEMORY.md` with a compact project memory entry:

```md
# Project Context {#project-context}

## Sync Design {#sync-design}

- `sync-preflight` Runtime pulls clean upstream memory before update, reviewer, and dreamer work. → [cfg:sync-runtime]
- `sync-reconciler` Dirty or conflicted memory state is repaired by a bounded sync-reconciler role. → [dep:sync-preflight]
- `sync-git-scope` Runtime commits are limited to MEMORY.md, MEMORY_*.md, and dream_logs/*.md. → [ver:sync-reconciler]
```

## Recording Beats

1. Open a fresh Codex or Claude Code session in the RightMemory repo.
2. Ask: `Continue the sync design from last time without requiring me to paste prior context.`
3. Show the agent calling `rightmemory retrieve` through the installed `memory-orchestrator`.
4. Show a small retrieved result: the `Sync Design` heading and its three linked facts.
5. Ask the agent to make a tiny doc edit or explain the next implementation step.
6. Show the agent submitting a memory update with `rightmemory update submit`.
7. End on `git diff`, `MEMORY.md`, or `rightmemory status` so viewers see this is structured memory in normal Git-managed files, not opaque vendor storage.

## Narration

> RightMemory gives coding agents a tree + graph memory they can retrieve, update, and consolidate across sessions, agent clients, and devices. The tree gives local context, graph edges connect related facts, Git sync keeps the memory portable, and separate memory roles keep retrieval and edits from becoming tangled with ordinary coding work.

## README GIF Placeholder

Until a real screen recording is captured, the README uses `docs/assets/rightmemory-demo.svg` as a visual explanation. Replace it with a GIF or MP4 thumbnail after recording the flow above.
