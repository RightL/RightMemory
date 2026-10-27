# Assistant Execution

RightMemory can use the current assistant's native agents for memory work. The main agent submits evidence and delegates ready batches. Retrieve inspects real memory and learning sources; Memory Update and the guidance and skills curators edit isolated Git worktrees. RightMemory performs deterministic queue, validation, publication, and recovery operations without creating a model client.

This path uses one shared [assistant skill](../skills/rightmemory-assistant/SKILL.md), a [Retrieve role](../rightmemory/prompts/assistant-retrieve.md), the [Memory Update role](../rightmemory/prompts/update.md), and the [guidance](../rightmemory/prompts/guidance-curator.md) and [skills](../rightmemory/prompts/skills-curator.md) curator roles. The existing standalone and CLI-agent paths remain available.

## Setup

From the repository checkout, run the shared installer with explicit memory and skill directories:

```bash
./install.sh --mode assistant /absolute/memory-root /absolute/assistant-skills
```

On Windows, use `./install.ps1 --mode assistant C:\path\memory C:\path\assistant-skills` in PowerShell. The installer uses Git and uv, provisions Python 3.11+ and the runtime, initializes a new memory root, and installs `rightmemory-assistant` with that root bound plus `maintain-rightmemory`, `maintain-agent-guidance`, and `maintain-work-skills`. Existing complete roots are preserved; incomplete roots are refused before changes. Enable the installed skill using your assistant's supported skill mechanism. The skills directory is an explicit host choice, not automatic dot plugin registration.

Use `rightmemory validate --root /absolute/memory-root` to check the root. Replace the new root's example entries with your own state through the normal memory workflow. Re-run the same installer command after updating the checkout. Stop agents and separately configured watchers that can submit evidence before switching execution modes. Finish or cancel pending updates with their original executor first; the installer refuses a switch while it finds their pending work. Use assistant execution rather than an internal Update worker for this root. If an old orchestrator skill was customized and therefore preserved, disable it before enabling the native assistant skill. Standalone and CLI-agent modes use their command-backed daily entry and install the same domain maintenance skills, plus map maintenance and Manager.

Assistant execution does not require RightMemory model API credentials, Codex, or Claude Code. The assistant uses its own configured agent. Keep the active root explicit:

```bash
rightmemory assistant --root /absolute/memory-root context --session example-session
```

The assistant's native subagents must be able to read the returned memory and pending paths and write the assigned Update worktree. A remote MCP connection does not expose the server's filesystem to native file tools. Place the runtime and native agents in a common accessible environment, or provide an authorized shared filesystem. Each assistant may use its own synchronized Git checkout.

An Update worktree isolates candidate edits from the active root. A working-directory setting alone does not restrict arbitrary native shell access; use the assistant's file permissions where available. RightMemory validates the candidate it admits, while the assistant governs its agents' other file access.

## Read and Submit

The main agent delegates the retrieval need, bound root, session id, and `context` command (or matching MCP tool) to a native Retrieve subagent. That subagent calls `context` and reads its returned instruction file. The result supplies ordinary graph and fixed correction source locations, imported-view mappings, a compact `learning_index_file`, and readable pending evidence. `learning_root` and `legacy_guidance_file` locate the full learning collection for native search; `learning_source_count` reports its size. The observed `memory_commit` and `live_files: true` indicate that sources remain live. The subagent inspects relevant sources with native file tools and returns a useful summary with attribution. This path does not require a `RetrieveSelection` or run the original retrieval renderer.

Save ordinary Memory evidence in a UTF-8 file outside the memory root:

```bash
rightmemory assistant --root /absolute/memory-root submit --session example-session --request-id evidence-1 --file /absolute/evidence.txt
```

Submission records evidence for later reconciliation. It does not start an internal model worker. Retry the same submission with the same request id and content; changed evidence needs a new request id.

Normal assistant tool responses can carry a batch-ready notice. A Retrieve or other subagent passes that notice to its main agent, which arranges Update. The program owns pending and active batch state; the main agent owns native delegation.

## Shared Learning

Behavioral guidance, user redirections, and potentially reusable work use a shared evidence path, including when the user explicitly asks to remember them:

```bash
rightmemory learning --root /absolute/memory-root submit --session example-session --request-id learning-1 --file /absolute/evidence.txt
```

The result and normal tool calls can carry learning-ready notices. The main agent prepares each ready domain without a model client:

```bash
rightmemory learning --root /absolute/memory-root begin --domain guidance --mode update
rightmemory learning --root /absolute/memory-root begin --domain skills --mode update
```

Delegate a newly claimed task. For an active task, do not launch another worker; after interruption, confirm the prior worker stopped and resume the same task and worktree. Use the explicit retry command if its edits must be abandoned or its base is stale. Supply its `instructions_file`, `candidates_file`, and isolated `worktree`; the curator uses native file tools and the shared domain core. It validates and commits allowed changes, then uses the returned completion command:

```bash
rightmemory learning --root /absolute/memory-root complete --task TASK
rightmemory learning --root /absolute/memory-root status --task TASK
```

`--output-file FILE` may attach a summary. A saved candidate is evidence, and only completion confirms publication. After interruption, inspect the same task and retry its completion rather than dispatching another agent. Each domain processes shared evidence in bounded update batches; remaining candidates stay ready for subsequent batches, and useful raw evidence remains retrievable afterward.

Requested broader maintenance uses the corresponding domain skill and prepares a task with `--mode maintain --scope "<request>"`. It starts from scoped existing material and does not automatically mark pending candidates processed. Existing inbox, Behavior, and S# material is preserved unless a concrete scoped maintenance task addresses it.

## Update a Memory Batch

```bash
rightmemory assistant --root /absolute/memory-root update begin
```

Delegate only a newly claimed task (`claimed: true`). Give the native Update subagent the returned `instructions_file`, `candidates_file`, and `worktree`. Repeating ordinary `begin` while a task is active returns that task with `claimed: false`. Do not dispatch a second updater while its worker is running. After interruption, confirm that worker stopped and resume the same task and worktree. `status: not_ready` means there is no eligible batch, and `status: sync_required` means sync needs attention first.

Update follows the canonical role, including native edits, complete validation, and commits restricted to allowed files. `validate` remains independently callable:

```bash
rightmemory validate --root /absolute/candidate-worktree
```

Complete the task using the returned completion command, preserving its active-root binding:

```bash
rightmemory assistant --root /absolute/memory-root update complete --task TASK
rightmemory assistant --root /absolute/memory-root update status --task TASK
```

`complete` admits the exact validated candidate through the program's protected publication path and records the batch outcome. A commit inside the worktree is not yet an applied update. An unchanged reconciliation still needs completion. `--output-file FILE` can attach the agent's UTF-8 summary to completion. The completion receipt includes `landed_commit`, `changed_paths`, and a `sync` outcome of `published` or `disabled`.

After an interrupted call or lost response, inspect the same task and retry its completion. Keep its identity and worktree. When shared Git sync is enabled, completion first durably prepares the candidate, then publishes through the shared queue's compare-and-swap finalizer. If publication fails, the prepared result remains available; repeating `complete` retries publication without repeating semantic Update. With sync disabled, completion uses the local protected write path.

A stale-base conflict requires reconciliation against current state. To abandon the current task's edits and prepare the same batch again, use:

```bash
rightmemory assistant --root /absolute/memory-root update begin --retry TASK
```

The replacement task fences the old task, so its edits cannot later be completed. The program checks for a previously completed or recoverable prepared result before replacing a task. Do not use replacement merely because a completion reply was lost.

## Sync and MCP

Configured ordinary sync refreshes memory before retrieval and pulls before preparing Update. Shared-queue completion publishes through the protected Git finalizer. Explicit ordinary sync operations use the same core:

```bash
rightmemory assistant --root /absolute/memory-root sync refresh
rightmemory assistant --root /absolute/memory-root sync pull
rightmemory assistant --root /absolute/memory-root sync push
```

An incoming conflict or invalid merged state returns sync status and available file/operation details, and blocks a new Update. Resolve the reported conflict using the existing sync repair workflow, then retry ordinary sync before preparing another Update. Sync conflicts are not delegated to a native repair agent by this interface.

The optional MCP transport wraps the same assistant core:

```bash
rightmemory assistant --root /absolute/memory-root mcp --transport stdio
```

Load the shared assistant skill with this MCP transport; it explains how the assistant's own agents use the deterministic tools. The older `rightmemory mcp` command continues to use its configured internal role backend. See [MCP](MCP.md).

Assistant execution covers native retrieval, Memory updates, and learning curation. Internal model-session history, incremental retrieval caching, Insight, and Pruner belong to the separate internal runtime. Requested direct maintenance uses the corresponding domain skill. Use `assistant submit` and its completion receipts for native batches, not the internal `update submit` worker commands.
