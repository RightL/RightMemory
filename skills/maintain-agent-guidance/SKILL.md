---
name: maintain-agent-guidance
description: "Use when the user asks to inspect or maintain RightMemory guidance, agent-behavior preferences, or reusable user-redirection cases."
---

# Maintain Agent Guidance

Use the shared guidance core for the requested scope. This is the maintenance mode of the same curator that learns from ordinary submissions; it does not require another per-item approval round.

Use the user's exact root or profile. Otherwise run `rightmemory status` from the task's working directory and use its reported root. If the root remains unknown, ask rather than guessing. Read `rightmemory reference guidance` and the Agent Corrections rules when cases are involved.

For a read-only request, inspect relevant guidance, cases, and evidence and report your findings without editing. For requested changes, prepare a bounded task:

```bash
rightmemory learning --root <active-root> begin --domain guidance --mode maintain --scope "<requested scope>"
```

Read the returned `instructions_file` and work in its isolated `worktree` with native file tools. You may delegate that task to an available native agent. Check the returned mode and scope before editing. If another task is active, resolve that task separately before preparing the requested maintenance. Do not dispatch a second worker for an active task. After interruption, confirm the prior worker is no longer running, inspect status, and resume the same task and worktree; use the returned `retry_argv` when its edits must be abandoned or the base is stale.

Inspect existing guidance and supporting evidence within the requested scope. Legacy inbox or Behavior content may inform the maintenance; preserve unrelated material and references, and change ownership only within the supplied writable scope. Apply the shared core's distinction between evidence, scoped guidance, and a reusable correction case.

Validate and commit as the task directs, then use its returned completion command. The program's completion receipt confirms publication to the active root. If completion is interrupted, inspect the supplied status command and retry the same task instead of repeating the curation.

Report the meaningful changes, remaining uncertainty, and actual publication result.
