---
name: rightmemory-assistant
description: "Use when the user chooses RightMemory assistant execution: retrieve context and reconcile submitted evidence through the current assistant's own agents and file tools."
---

# Use RightMemory with Native Agents

The installed memory root is `{{MEMORY_ROOT}}`. Use that root and one stable session id throughout the conversation. Use its absolute path as `<active-root>` in control commands. The main agent owns submissions and delegates ready curation to a parent. Retrieve, the curation parent, and its domain agents run as native agents with access to the supplied directories; RightMemory manages evidence, isolation, validation, and publication.

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

## Reconcile Ready Evidence

Handle ready notices and interrupted active tasks returned by normal RightMemory calls. A subagent passes these notices to the main agent. Prepare a ready update batch without starting a configured model client:

```bash
rightmemory curate --root <active-root> begin --mode update
```

Delegate the returned `instructions_file`, `candidates_file`, and shared `worktree` to one native parent. It prepares domain contributions with `worker-begin`, delegates their instructions to native agents, and records their returned results with `worker-complete`. Domain agents edit their assigned files; the parent reviews the combined changes, owns Git staging and commits, and uses the supplied completion command. All controls remain bound to the supplied active root.

An active task is existing work. After interruption, inspect its status and confirm prior workers ended before resuming or replacing them. `effects_pending` means semantic work has landed; retry its supplied completion command without starting new learning. Use the returned `retry_argv` when a task must be reconsidered from a new base, preserving unpublished work for inspection.

Continue the user's task while curation runs. Each parent update processes one bounded evidence batch across the three domains; returned ready notices identify remaining batches. Publication records their judgments together and removes fully processed raw evidence. A domain may contribute no changes.

Requested maintenance uses `maintain-rightmemory-system` across domains, or the corresponding domain maintenance skill for a focused scope. All use the same parent review and publication flow.

## Report the Actual Outcome

Only the program's completion receipt confirms that candidate work reached the active root. An unchanged reconciliation still needs completion. On interruption or a lost response, inspect task status and retry the supplied completion command with the same task id; a prepared result retries publication without repeating semantic work.

Use `begin --domain <domain> --retry <task-id>` when a task must be reconciled anew. After confirming the prior worker has stopped, this can also explicitly take over its claim from another checkout. Replacement fences the old publication and does not carry over unpublished edits. It is not the recovery action for an uncertain completion response.

Report an inaccessible directory or unsupported native delegation as an assistant limitation. Do not substitute another agent product or claim adaptation from MCP tool discovery alone.

## Recover Synchronization

Normal responses may include a `sync_repair` notice. A child agent relays it to the main agent alongside its result. The main agent uses `begin_argv` and delegates a newly claimed repair with its `instructions_file` and `worktree` to one native subagent. The repair uses the canonical Sync Reconciler role and returns through the supplied completion command; it needs no configured model client.

An active repair is existing work, including after its preparation command exits. Resume its worker or inspect `status_argv` after interruption instead of starting a duplicate. A prepared result uses `complete_argv` without repeating semantic edits. Explicit `retry_argv` replaces stale or abandoned work after the prior worker has stopped, fences the old task, and retains its unpublished worktree for inspection.

Repair completion reports local application separately from remote synchronization. A failed push does not undo the local repair. Continue the blocked operation after recovery; retrieval can use the existing valid local sources while incoming state remains blocked.
