---
name: maintain-rightmemory
description: "Use when the user explicitly asks to inspect or maintain ordinary RightMemory context, including memory about the user and other people."
---

# Maintain Memory

Use the shared Memory core to improve the requested context. This is the maintenance mode of the same curator that learns from ordinary submissions; it does not require another per-item approval round.

Use the user's exact root or profile. Otherwise run `rightmemory status` from the task's working directory and use its reported root. If the root remains unknown, ask rather than guessing.

For a read-only request, inspect relevant Memory and its references, using `rightmemory reference schema` and `rightmemory reference memory`, and report your findings without editing. For requested changes, prepare an isolated task:

```bash
rightmemory curate --root <active-root> begin --domain memory --mode maintain --scope "<requested scope>"
```

Read the returned `instructions_file` and work in its isolated `worktree` with native file tools. You may delegate that task to an available native agent. Check the returned mode and scope before editing. If another task is active, resolve it separately before preparing the requested maintenance. After interruption, confirm its worker has ended, inspect status, and resume the same task and worktree. Use the returned `retry_argv` when its edits must be abandoned or the base is stale.

Improve future understanding of the user, other people, decisions, and circumstances within the requested scope. Remove trivial or misleading context rather than merely compacting it, and preserve consequential uncertainty or missing context. Agent guidance and correction cases belong to `maintain-agent-guidance`; work experiences and instruction bodies belong to `maintain-work-skills`. Preserve relevant graph references. Pursuit remains read-only; explicit map changes use `maintain-pursuit-map`.

Validate and commit as the task directs, then use its returned completion command. The program's completion receipt confirms publication to the active root. If completion is interrupted, inspect the supplied status command and retry the same task instead of repeating the curation. Maintenance leaves pending candidates and their processing state to Update.

Report the meaningful changes, remaining uncertainty, and actual publication result.
