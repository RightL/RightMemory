---
name: rightmemory-assistant
description: "Use when the user chooses RightMemory assistant execution: retrieve context and reconcile submitted evidence through the current assistant's own agents and file tools."
---

# Use RightMemory with Native Agents

The installed memory root is `{{MEMORY_ROOT}}`. Use that root and one stable session id throughout the conversation. Use its absolute path as `<active-root>` in every assistant command. The main agent owns submissions and Update delegation. Retrieve and Update run as native subagents with access to the supplied directories; RightMemory tools manage the queue, isolation, validation, and publication.

Use the `rightmemory assistant` commands below, or their matching assistant MCP tools. Keep this skill active with either transport.

## Retrieve Useful Context

Retrieve when stored context could materially change how you understand or approach the work. Delegate that need to a native Retrieve subagent, with the bound root, session id, and this preparation command (or matching MCP tool):

```bash
rightmemory assistant --root <active-root> context --session <session-id>
```

The Retrieve subagent calls `context`, reads the returned `instructions_file`, then uses native file tools to inspect the real sources and `pending_file`. It returns useful context with its sources. Supply prior context when it would avoid redundant reading.

Reconcile the handoff with the current conversation and evidence. Apply relevant guidance in the work; current user instructions take precedence over stale or conflicting memory. Submit concrete evidence of wrong, stale, or misleading Memory or Agent Corrections when you find it.

## Submit Evidence

Submit evidence when losing it would impair future decisions, cause substantial rediscovery, or repeat a reusable agent failure. Preserve the circumstances, supporting evidence, and unresolved uncertainty that affect its meaning. For user redirections, describe the prior approach, the user's signal, and the resulting direction. Leave final stored wording and placement to Update.

Combine related evidence ready at the same boundary. Routine progress and details already adequately preserved in project artifacts rarely need submission. Pursuit is read-only context; explicit map edits use the separate maintenance workflow.

Write the evidence to a UTF-8 file outside the memory root, then submit it:

```bash
rightmemory assistant --root <active-root> submit --session <session-id> --request-id <submission-id> --file <evidence-file>
```

Reuse the same request id and evidence when retrying the same submission. Use a new id for new evidence. A saved candidate is pending evidence, not confirmation that it became memory.

## Reconcile Ready Batches

Act on batch-ready notices returned by normal RightMemory tool calls. A subagent that encounters one passes it to the main agent with its result. The main agent obtains the next batch with:

```bash
rightmemory assistant --root <active-root> update begin
```

Delegate a newly claimed task (`claimed: true`) with its instructions, candidates, and isolated worktree to one native Update subagent. It follows the canonical Update role, edits allowed files in that worktree, validates and commits them, and calls `update complete` for the task. Retain the root binding supplied with the task when calling control commands from the worktree. An existing task (`claimed: false`) must not be delegated twice; a sync problem must be resolved before Update begins.

Continue the user's work while Update runs. Treat an already active batch as existing work. Use `rightmemory assistant --root <active-root> update status --task <task-id>` to resolve an uncertain result before dispatching another updater.

## Report the Actual Outcome

Only the program's completion receipt confirms that candidate work reached the active root. A prepared candidate is not a completed update. On interruption or failed publication, inspect task status and retry completion with the same task id and worktree; a prepared result retries publication without repeating semantic Update.

When a stale task must be reconciled anew, `rightmemory assistant --root <active-root> update begin --retry <task-id>` explicitly replaces that task for the same batch and fences its old edits. Use this only after deciding to abandon those edits, rather than for an uncertain completion response.

Report an inaccessible directory or unsupported native delegation as an assistant limitation. Do not substitute another agent product or claim adaptation from MCP tool discovery alone.
