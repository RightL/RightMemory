---
name: rightmemory-assistant
description: "Use when the user chooses RightMemory assistant execution: retrieve context and reconcile submitted evidence through the current assistant's own agents and file tools."
---

# Use RightMemory with Native Agents

The installed memory root is `{{MEMORY_ROOT}}`. Use that root and one stable session id throughout the conversation. Use its absolute path as `<active-root>` in control commands. The main agent owns submissions and curation delegation. Retrieve and the three domain curators run as native agents with access to the supplied directories; RightMemory manages evidence, isolation, validation, and publication.

Use the commands below or their matching assistant MCP tools. Keep this skill active with either transport.

## Retrieve Useful Context

Retrieve when stored context could materially change how you understand or approach the work. Delegate that need to a native Retrieve subagent, with the bound root, session id, and this preparation command:

```bash
rightmemory assistant --root <active-root> context --session <session-id>
```

The Retrieve subagent calls `context`, reads the returned `instructions_file` and `learning_index_file`, then uses native file tools to inspect relevant real sources. The index is compact; search `learning_root` when it is insufficient. `learning_source_count` reports the full collection size. It returns useful context with its sources. Supply prior context when it would avoid redundant reading.

Reconcile the handoff with the current conversation and evidence. Apply relevant guidance in the work; current user instructions take precedence over stale or conflicting memory. Consider guidance, correction cases, experiences, skills, and pending evidence along with Memory. Preserve the distinction between evidence and an adopted rule, and read a relevant skill in full when using it. Submit concrete evidence of wrong, stale, or misleading content.

## Submit Evidence

Capture concrete evidence that could improve future understanding, decisions, collaboration, or repeated work. A useful candidate need not already support a stable conclusion, and uncertainty about recurrence is not by itself a reason to discard it. Preserve the circumstances, supporting evidence, and unresolved uncertainty that affect its meaning.

Write UTF-8 evidence outside the memory root and use one shared submission path for all three domains:

```bash
rightmemory submit --root <active-root> --session <session-id> --request-id <submission-id> --file <evidence-file> --executor native
```

For a redirection, preserve the prior approach, user signal, and resulting direction. For work experience, consider whether the situation or a submethod may recur and whether the evidence would help a later agent reproduce a meaningful result from a short prompt. Preserve the method, human contributions, outcome, and resources that could spare later rediscovery. The curators decide what belongs in Memory, guidance, or reusable work; submission need not invent a general rule, a finished skill, or a final destination. Explicit requests to remember material use this same path.

Combine related evidence ready at a natural boundary and capture each occurrence once; completion is not required. Independent later occurrences can add evidence. Leave out routine progress and copies of project artifacts that add no independent future value. Pursuit is read-only context; explicit map edits use its separate maintenance workflow.

Reuse the same request id and evidence when retrying a submission. A saved candidate confirms capture, not adoption as formal memory, guidance, or a skill.

## Reconcile Ready Domains

Handle ready notices and interrupted or uncertain active tasks returned by normal RightMemory calls. A subagent passes these notices to the main agent with its result. For a new ready domain, the main agent prepares its task without starting a model client:

```bash
rightmemory curate --root <active-root> begin --domain <memory|guidance|skills> --mode update
```

When a notice reports `effects_pending`, use its `complete_argv` to retry program finalization; the semantic work has already landed and needs no new curator.

Delegate a newly claimed task with its `instructions_file`, `candidates_file`, and isolated `worktree` to one native curator. It follows the shared domain core, edits allowed files, validates and commits the result, then uses the supplied completion command. Keep the supplied active-root binding when calling control commands from the worktree.

An active task is existing work. Do not dispatch another worker while its worker is running. After interruption, inspect its status, confirm the prior worker ended, and prefer resuming the same task and worktree through the returned controls. An active native task can report `ready: false`; do not overlook it merely because it is not a new batch. If its edits must be abandoned or its base is stale, use the returned `retry_argv`.

Continue the user's task while curation runs. Each domain processes bounded update batches; use returned ready notices to continue remaining evidence. The program removes a raw candidate after all three domains finish. Curators preserve useful context, cases, or experiences in their own sources before completion.

Requested broader maintenance uses the corresponding domain maintenance skill. It starts from scoped existing material and uses the same preparation and completion flow.

## Report the Actual Outcome

Only the program's completion receipt confirms that candidate work reached the active root. An unchanged reconciliation still needs completion. On interruption or a lost response, inspect task status and retry the supplied completion command with the same task id; a prepared result retries publication without repeating semantic work.

Use `begin --domain <domain> --retry <task-id>` when a task must be reconciled anew. After confirming the prior worker has stopped, this can also explicitly take over its claim from another checkout. Replacement fences the old publication and does not carry over unpublished edits. It is not the recovery action for an uncertain completion response.

Report an inaccessible directory or unsupported native delegation as an assistant limitation. Do not substitute another agent product or claim adaptation from MCP tool discovery alone.
