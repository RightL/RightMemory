---
name: maintain-work-skills
description: "Use when the user asks to inspect or maintain RightMemory work experiences, reusable procedures, skills, or their supporting resources."
---

# Maintain Work Experience and Skills

Use the shared work-skills core for the requested scope. This is the maintenance mode of the same curator that learns from ordinary submissions; it does not require another per-item approval round.

Use the user's exact root or profile. Otherwise run `rightmemory status` from the task's working directory and use its reported root. If the root remains unknown, ask rather than guessing. Read `rightmemory reference work-skills`.

For a read-only request, inspect relevant experiences, skills, and available resources and report your findings without editing. For requested changes, prepare a bounded task:

```bash
rightmemory curate --root <active-root> begin --domain skills --mode maintain --scope "<requested scope>"
```

Read the returned `instructions_file` and work in its isolated `worktree` with native file tools. You may delegate that task to an available native agent. Check the returned mode and scope before editing. If another task is active, resolve that task separately before preparing the requested maintenance. Do not dispatch a second worker for an active task. After interruption, confirm the prior worker is no longer running, inspect status, and resume the same task and worktree; use the returned `retry_argv` when its edits must be abandoned or the base is stale.

Judge whether retained work helps a later agent reproduce a meaningful outcome with less user direction. Improve relevant experience, methods, checks, or resources without forcing every case into a skill. Graph-linked S# resources may inform the task; preserve working references while improving instruction bodies within the supplied writable scope.

Validate and commit as the task directs, then use its returned completion command. The program's completion receipt confirms publication to the active root. If completion is interrupted, inspect the supplied status command and retry the same task instead of repeating the curation. Maintenance leaves pending candidates and their processing state to Update.

Report what improved, which outcomes were actually demonstrated, remaining uncertainty, and the actual publication result.
