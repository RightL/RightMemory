---
name: maintain-agent-guidance
description: "Use when the user asks to inspect or maintain RightMemory guidance, agent-behavior preferences, or reusable user-redirection cases."
---

# Maintain Agent Guidance

Use the shared guidance core for the requested scope. This is the maintenance mode of the same curator that learns from ordinary submissions; it does not require another per-item approval round.

Use the user's exact root or profile. Otherwise run `rightmemory status` from the task's working directory and use its reported root. If the root remains unknown, ask rather than guessing. Read `rightmemory reference guidance` and the Agent Corrections rules when cases are involved.

For a read-only request, inspect relevant guidance, cases, and evidence and report your findings without editing. For requested changes, prepare a bounded task:

```bash
rightmemory curate --root <active-root> begin --domain guidance --mode maintain --scope "<requested scope>"
```

Read the returned `instructions_file` and act as the parent for this guidance-scoped task. Prepare its domain contribution with the supplied `worker-begin` command and delegate the returned instructions and shared `worktree` to an available native agent, then record its result with `worker-complete`. The supplied `worker-run` command can execute that contribution with the configured model. Domain agents edit their assigned files; you review the actual result before validation, Git commits, and publication. Check the returned mode and scope before proceeding. If another task is active, inspect and resolve that existing work. After interruption, confirm workers have ended and resume the same task; use its `retry_argv` for abandoned or stale work.

Inspect existing guidance and supporting evidence within the requested scope. Preserve unrelated material and references while working within the supplied writable scope. Apply the shared core's distinction between evidence, scoped guidance, and a reusable correction case.

Validate and commit as the task directs, then use its returned completion command. The program's completion receipt confirms publication to the active root. If completion is interrupted, inspect the supplied status command and retry the same task instead of repeating the curation. Maintenance leaves pending candidates and their processing state to Update.

Report the meaningful changes, remaining uncertainty, and actual publication result.
