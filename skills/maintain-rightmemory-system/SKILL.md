---
name: maintain-rightmemory-system
description: "Use when the user asks to inspect or maintain RightMemory across Memory, agent guidance, and work experience or skills."
---

# Maintain RightMemory Together

Act as the parent for the requested maintenance. Review the domains together so useful material is neither repeated without added value nor lost between their separate judgments. The prepared task supplies the shared parent core, domain rules, and maintenance requirements.

Use the user's exact root or profile. Otherwise run `rightmemory status` from the task's working directory and use its reported root. For a read-only request, inspect the relevant sources without preparing a write task. Pursuit changes use `maintain-pursuit-map`.

For requested changes, prepare one isolated parent task:

```bash
rightmemory curate --root <active-root> begin --mode maintain --scope "<requested scope>"
```

Read the returned `instructions_file`. Check the returned mode and scope; an existing task remains existing work. The parent and domain agents share the returned `worktree`. Use the task's `worker-begin` commands to prepare native domain contributions, then give each available subagent its instructions and shared worktree. Record its returned result with the supplied `worker-complete` command. Configured domain execution is also available through the supplied `worker-run` commands.

Domain agents edit their assigned files and report their judgments. After they return, review the actual combined changes against the evidence and shared cores, including material omitted or repeated across domains. Make any needed corrections yourself. You own final validation, staging, commits, and the task's completion command; its receipt confirms publication to the active root. Maintenance leaves pending evidence for Update.

If interrupted, inspect the same task's status and resume its existing work. Confirm workers have ended before replacing an attempt or using the parent task's `retry_argv`; replacement leaves unpublished work available for inspection. Retry an uncertain completion with the same task id instead of repeating the maintenance.

Report the meaningful changes, remaining uncertainty, and actual publication result.
