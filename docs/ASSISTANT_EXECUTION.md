# Assistant Execution

RightMemory can use the current assistant's native agents for retrieval and curation. The main agent submits evidence once and delegates each ready batch to one curation parent. Retrieve inspects real sources. The parent and its Memory, guidance, and skills agents share one isolated Git worktree. The assistant supplies the agents, while RightMemory controls evidence, claims, validation, publication, and recovery.

This path uses the shared [assistant skill](../skills/rightmemory-assistant/SKILL.md), a [native Retrieve role](../rightmemory/prompts/assistant-retrieve.md), and the canonical [curation parent](../rightmemory/prompts/curation-parent.md), [Memory](../rightmemory/prompts/update.md), [guidance](../rightmemory/prompts/guidance-curator.md), and [skills](../rightmemory/prompts/skills-curator.md) roles. Configured standalone and CLI-agent execution use the same curation lifecycle.

## Setup

Run the installer with explicit memory and skill directories:

```bash
./install.sh --mode assistant /absolute/memory-root /absolute/assistant-skills
```

On Windows, use `./install.ps1 --mode assistant C:\path\memory C:\path\assistant-skills` in PowerShell. The installer uses Git and uv, provisions Python 3.11+ and the runtime, initializes a new root, and installs `rightmemory-assistant`, `maintain-rightmemory-system`, and all three domain maintenance skills. It preserves valid existing roots and refuses invalid current state or unfinished tasks before replacing their runtime. Enable the installed skill through the assistant's supported mechanism.

Assistant execution needs no RightMemory model credentials, Codex, or Claude Code. The native agents need filesystem access to the returned sources and worktrees. A remote MCP connection alone does not provide that access; use a common environment or an authorized shared filesystem. Each assistant may use its own synchronized Git checkout.

A worktree isolates candidate edits from the active root. The assistant governs its agents' broader file permissions, while RightMemory validates the result it admits. Before changing execution modes, finish current tasks with their existing executor. Consult [UPGRADING](UPGRADING.md) when replacing an older installation with different commands or state.

## Retrieve and Submit

Delegate the retrieval need, bound root, session id, and preparation command to a native Retrieve subagent:

```bash
rightmemory assistant --root /absolute/memory-root context --session example-session
```

The subagent reads `instructions_file` and relevant real sources. The context includes graph and fixed correction paths, imported-view mappings, a compact `learning_index_file`, and `learning_root` for further native search. `learning_source_count` reports the collection size. Sources remain live, as indicated by `memory_commit` and `live_files: true`. The subagent returns useful context with attribution; it does not need the internal retriever's terminal selector.

Use one shared path for user and people context, project understanding, behavioral guidance, corrections, and work experience. Save UTF-8 evidence outside the memory root, then submit:

```bash
rightmemory submit --root /absolute/memory-root --session example-session --request-id evidence-1 --file /absolute/evidence.txt --executor native
```

The request id makes an identical retry idempotent; a distinct occurrence gets a new id. Capture useful evidence before demanding a settled generalization, and preserve enough circumstances and uncertainty to interpret it. Submission records the candidate without starting a configured model worker. Each domain agent judges its value under its shared core, and the parent reviews those judgments together.

Pending evidence is immediately available to relevant retrieval. At publication, the program records processing for all three domains and removes the fully processed raw sources. Useful retained context, cases, and experiences remain in curated sources; processing alone does not turn evidence into a rule.

## Prepare and Complete Curation

Normal tool responses carry readiness and active-task notices. Update selects one batch for all three domains using the shared [batching settings](../README.md#curation-execution-and-recovery). By default it waits for 15 pending candidates or the maximum waiting time. Waiting notices include `next_ready_at`; native execution rechecks readiness on subsequent tool calls. Subagents relay notices to the main agent, which prepares a ready parent task.

```bash
rightmemory curate --root /absolute/memory-root begin --domain all --mode update
```

Delegate a newly claimed task with its `instructions_file`, `candidates_file`, and `worktree` to one parent agent. The parent prepares each domain contribution with `worker-begin` and delegates its returned instructions and worktree to a native agent. Each domain agent edits its assigned files and reports its judgment. Use the returned `worker-complete` command to record that contribution.

```bash
rightmemory curate --root /absolute/memory-root worker-begin --task TASK --domain memory
rightmemory curate --root /absolute/memory-root worker-complete --task TASK --domain memory --attempt ATTEMPT --output "domain summary"
```

For a configured domain model, `worker-run` handles preparation, execution, and recording the result. Different domains can run concurrently in the same worktree, whether using configured models or the host's subagents.

```bash
rightmemory curate --root /absolute/memory-root worker-run --task TASK --domain memory
```

After all domain agents return, the parent reviews the combined diff, makes any needed corrections itself, validates the result, and owns staging and committing the reviewed changes. It then calls the returned parent completion command, even when the worktree is unchanged. Keep the active-root binding for task commands run from the worktree.

```bash
rightmemory curate --root /absolute/memory-root complete --task TASK
rightmemory curate --root /absolute/memory-root status --task TASK
```

`--output-file FILE` can attach the parent's summary. The parent completion receipt establishes publication of the combined result. Remaining candidates are available to subsequent Update batches.

An active task is existing work, not another delegation request. After interruption, inspect status, verify the previous worker ended, and prefer resuming the same task and worktree. Native work has no automatic expiry. A synchronization failure returns an error and blocks preparation; resolve it before retrying.

After an interrupted completion or lost response, retry the same completion. A prepared result can publish without repeating semantic work. `effects_pending` also calls for `complete` recovery; the semantic result has already landed and needs no new curator.

When a stale base or deliberately abandoned edits require new reconciliation, retry with the task's domain. For Update, use the all-domain scope.

```bash
rightmemory curate --root /absolute/memory-root begin --domain all --retry TASK
```

After confirming the previous worker has stopped, this can also explicitly take over its claim from another checkout. It preserves the selected evidence and fences the old publication, without carrying over unpublished edits. It is not the recovery action for a merely uncertain completion response.

## Requested Maintenance

`maintain-rightmemory-system` uses the parent flow across all three domains. Use `maintain-rightmemory`, `maintain-agent-guidance`, or `maintain-work-skills` for a single domain. A maintenance task starts from scoped existing material and uses the same preparation and completion flow.

```bash
rightmemory curate --root /absolute/memory-root begin --domain skills --mode maintain --scope "improve the report workflow"
rightmemory curate --root /absolute/memory-root count --domain skills --task TASK
```

Maintain leaves pending evidence for Update and uses the same domain cores and publication boundary. Pursuit changes use the separate map workflow.

## Sync and MCP

Configured sync refreshes sources before retrieval and synchronizes before acquiring a curation claim. Completion validates and publishes through the protected shared claim. Explicit assistant sync controls remain available:

```bash
rightmemory assistant --root /absolute/memory-root sync refresh
rightmemory assistant --root /absolute/memory-root sync pull
rightmemory assistant --root /absolute/memory-root sync push
```

An incoming semantic conflict or invalid merged state returns a `sync_repair` notice and blocks new curation work. A Retrieve subagent relays the notice to the main agent, which prepares and delegates one native repair:

```bash
rightmemory assistant --root /absolute/memory-root sync-repair begin
rightmemory assistant --root /absolute/memory-root sync-repair status --task TASK
rightmemory assistant --root /absolute/memory-root sync-repair validate --task TASK
rightmemory assistant --root /absolute/memory-root sync-repair complete --task TASK
```

`begin` returns `claimed`, `task_id`, `instructions_file`, and a candidate `worktree` containing the staged merge. Repeated preparation returns the active task without a new claim. The native subagent reads the canonical Sync Reconciler instructions, uses its own file/Git tools, validates and commits, then calls the returned completion command bound to the active root. `validate` applies sync validation, including retaining Edit Feedback overflow for later maintenance.

The program owns validation and exact fast-forward admission. The active root remains unchanged until completion; program-owned coordination records are not semantic edit input. Ordinary sync and configured repair do not take over an active native task. No process, model credential, or long-held lock is required while the host edits.

After interruption, inspect and resume the same task. A prepared result retries completion without another semantic turn, including when publication succeeded but its reply was lost. When the local base became stale or the host deliberately abandons the edit, stop the previous worker and use `sync-repair begin --retry TASK`. Replacement fences the old task and reports its retained unpublished worktree. Remote advancement alone does not discard a completed local merge: completion continues normal synchronization and reports `applied`, `landed_commit`, and `sync` separately. Further semantic conflicts produce another notice.

This handoff repairs staged incoming semantic state. Pre-existing dirty active files, invalid coordination records, inaccessible roots, and transport failures remain explicit blocking diagnostics rather than instructions for an agent to rewrite program-owned state.

The optional native MCP transport wraps the same controls:

```bash
rightmemory assistant --root /absolute/memory-root mcp --transport stdio
```

Keep the shared assistant skill active with this transport. It wraps the parent and domain worker controls and retains readiness and active-task notices for native delegation. The count tool takes a domain and an optional task ID. Maintenance completion checks the [retained-text limit](../README.md#curation-execution-and-recovery) for each selected domain. Ordinary `rightmemory mcp` exposes retrieval and submission. Manual configured execution and the maintenance skills use the CLI. See [MCP](MCP.md).

Native execution covers retrieval and all three curation domains. Internal model history, incremental retrieval caching, Insight, and Pruner remain capabilities of the configured runtime; native task preparation does not claim those behaviors.
