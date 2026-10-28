---
name: rightmemory-assistant
description: "Use when the user chooses RightMemory assistant execution: retrieve context and reconcile submitted evidence through the current assistant's own agents and file tools."
---

# Use RightMemory with Native Agents

The installed memory root is `{{MEMORY_ROOT}}`. Use that root and one stable session id throughout the conversation. Use its absolute path as `<active-root>` in every assistant command. The main agent owns submissions and curation delegation. Retrieve, Memory Update, and the guidance and skills curators run as native agents with access to the supplied directories; RightMemory tools manage evidence, isolation, validation, and publication.

Use the `rightmemory assistant` commands below, or their matching assistant MCP tools. Keep this skill active with either transport.

## Retrieve Useful Context

Retrieve when stored context could materially change how you understand or approach the work. Delegate that need to a native Retrieve subagent, with the bound root, session id, and this preparation command (or matching MCP tool):

```bash
rightmemory assistant --root <active-root> context --session <session-id>
```

The Retrieve subagent calls `context`, reads the returned `instructions_file` and `learning_index_file`, then uses native file tools to inspect relevant real sources and `pending_file`. The learning index is compact; search the returned `learning_root` and `legacy_guidance_file` when it is insufficient. `learning_source_count` reports the full collection size. It returns useful context with its sources. Supply prior context when it would avoid redundant reading.

Reconcile the handoff with the current conversation and evidence. Apply relevant guidance in the work; current user instructions take precedence over stale or conflicting memory. Retrieve relevant guidance, correction cases, experiences, skills, and raw learning evidence along with Memory. Preserve the distinction between evidence and an adopted rule, and read a relevant skill in full when using it. Submit concrete evidence of wrong, stale, or misleading content through its domain's path.

## Submit Evidence

Capture concrete evidence that could improve future understanding, decisions, collaboration, or repeated work. A useful candidate need not already support a stable conclusion, and uncertainty about recurrence is not by itself a reason to discard it. Preserve the circumstances, supporting evidence, and unresolved uncertainty that affect its meaning. The corresponding domain curator uses its shared core during Update to judge adoption and generalization.

For ordinary context about the user, other people, projects, or decisions, write UTF-8 evidence outside the memory root and submit it to Memory Update:

```bash
rightmemory assistant --root <active-root> submit --session <session-id> --request-id <submission-id> --file <evidence-file>
```

For behavioral guidance, user redirections, and potentially reusable work experience, use the shared learning path:

```bash
rightmemory learning --root <active-root> submit --session <session-id> --request-id <submission-id> --file <evidence-file>
```

For a redirection, preserve the prior approach, user signal, and resulting direction. For work experience, consider whether the situation or a submethod may recur and whether the evidence would help a later agent reproduce a meaningful result from a short prompt. Preserve the method, human contributions, outcome, and resources that could spare later rediscovery. Do not invent a general rule or finished skill. Both learning curators can use the same evidence. An explicit request to remember guidance or a procedure uses the learning path too.

Combine related evidence ready at a natural boundary and capture each occurrence once; completion is not required. Independent later occurrences can add evidence. Leave out routine progress and copies of project artifacts that add no independent future value. Pursuit is read-only context; explicit map edits use its separate maintenance workflow.

Reuse the same request id and evidence when retrying a submission. A saved candidate is retrievable evidence, not confirmation that it became formal memory, guidance, or a skill.

## Reconcile Ready Batches

Handle ready notices and interrupted or uncertain active tasks returned by normal RightMemory calls. A subagent passes these notices to the main agent with its result. Inspect an active task's status and confirm whether its prior worker is still running; resume the same task after that worker has ended. The main agent obtains a new ready batch with:

```bash
rightmemory assistant --root <active-root> update begin
```

Delegate a newly claimed task (`claimed: true`) with its instructions, candidates, and isolated worktree to one native Update subagent. It follows the canonical Update role, edits allowed files in that worktree, validates and commits them, and calls `update complete` for the task. Retain the root binding supplied with the task when calling control commands from the worktree. Do not dispatch a second worker for an existing task (`claimed: false`). After interruption, confirm the prior worker is no longer running, inspect status, and resume that task and worktree. Resolve a sync problem before Update begins.

Continue the user's work while Update runs. Treat an already active batch as existing work. Use `rightmemory assistant --root <active-root> update status --task <task-id>` to resolve an uncertain result before dispatching another updater.

## Reconcile Ready Learning

Handle ready learning and interrupted or uncertain active learning notices. An active native task can report `ready: false` after interruption; inspect its `status_argv` and confirm the prior worker ended before resuming through its `begin_argv`. Keep the existing task's mode and scope. For a new ready domain, obtain a model-free curation task:

```bash
rightmemory learning --root <active-root> begin --domain <guidance|skills> --mode update
```

Delegate a newly claimed task with its `instructions_file`, `candidates_file`, and isolated `worktree` to a native curator. It follows the shared domain core, edits allowed files, validates and commits the result, then uses the supplied completion command. Keep the supplied active-root binding for control commands from the worktree. Do not dispatch a second worker while that task is running. After interruption, confirm the prior worker stopped, inspect status, and resume the same task in its existing worktree. If its base is stale or its edits are deliberately abandoned, use the returned `retry_argv`.

Continue the user's task while curation runs. The guidance and skills curators process shared evidence independently in bounded update batches; use returned ready notices to continue remaining evidence in subsequent batches. Their completed processing does not by itself remove useful raw evidence from retrieval. Requested broader maintenance uses the corresponding domain maintenance skill.

## Report the Actual Outcome

Only the program's completion receipt confirms that candidate work reached the active root. A prepared candidate is not a completed update. On interruption or failed publication, inspect task status and retry the supplied completion command with the same task id and worktree; a prepared result retries publication without repeating semantic work.

When a stale task must be reconciled anew, `rightmemory assistant --root <active-root> update begin --retry <task-id>` explicitly replaces that task for the same batch and fences its old edits. Use this only after deciding to abandon those edits, rather than for an uncertain completion response.

Report an inaccessible directory or unsupported native delegation as an assistant limitation. Do not substitute another agent product or claim adaptation from MCP tool discovery alone.
