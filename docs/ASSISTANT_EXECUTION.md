# Assistant Execution

RightMemory can use the current assistant's native agents for retrieval and curation. The main agent submits evidence once and delegates ready domains. Retrieve inspects real sources; Memory, guidance, and skills curators edit isolated Git worktrees. RightMemory controls evidence, claims, validation, publication, and recovery without creating a model client.

This path uses the shared [assistant skill](../skills/rightmemory-assistant/SKILL.md), a [native Retrieve role](../rightmemory/prompts/assistant-retrieve.md), and the canonical [Memory](../rightmemory/prompts/update.md), [guidance](../rightmemory/prompts/guidance-curator.md), and [skills](../rightmemory/prompts/skills-curator.md) roles. Configured standalone and CLI-agent execution use the same curation lifecycle.

## Setup

Run the installer with explicit memory and skill directories:

```bash
./install.sh --mode assistant /absolute/memory-root /absolute/assistant-skills
```

On Windows, use `./install.ps1 --mode assistant C:\path\memory C:\path\assistant-skills` in PowerShell. The installer uses Git and uv, provisions Python 3.11+ and the runtime, initializes a new root, and installs `rightmemory-assistant` plus all three domain maintenance skills. It preserves valid existing roots and refuses invalid current state or unfinished tasks before replacing their runtime. Enable the installed skill through the assistant's supported mechanism.

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

The request id makes an identical retry idempotent; a distinct occurrence gets a new id. Capture useful evidence before demanding a settled generalization, and preserve enough circumstances and uncertainty to interpret it. Submission records the candidate without starting a configured model worker. All three curators independently judge its value under their shared domain cores.

Pending evidence is immediately available to relevant retrieval. The program removes its raw source after all three domains finish. Useful retained context, cases, and experiences remain in curated sources; processing alone does not turn evidence into a rule.

## Prepare and Complete Curation

Normal tool responses carry readiness and active-task notices. Each domain uses the shared [batching settings](../README.md#curation-execution-and-recovery): by default it waits for 15 pending candidates or the maximum waiting time. Waiting notices include `next_ready_at`; native execution rechecks readiness on subsequent tool calls. Subagents relay notices to the main agent, which owns native delegation. Prepare a new ready domain with:

```bash
rightmemory curate --root /absolute/memory-root begin --domain memory --mode update
rightmemory curate --root /absolute/memory-root begin --domain guidance --mode update
rightmemory curate --root /absolute/memory-root begin --domain skills --mode update
```

Delegate a newly claimed task with its `instructions_file`, `candidates_file`, and `worktree`. The curator reads the supplied task mode and allowed paths, uses native file tools, validates and commits the result, then calls its returned completion command. Keep the active-root binding even when the command runs from the worktree.

```bash
rightmemory curate --root /absolute/memory-root complete --task TASK
rightmemory curate --root /absolute/memory-root status --task TASK
```

`--output-file FILE` can attach the agent's summary. A worktree commit is candidate work; the completion receipt establishes publication. An unchanged reconciliation still completes processing. Updates use bounded batches, with remaining candidates available to subsequent tasks.

An active task is existing work, not another delegation request. After interruption, inspect status, verify the previous worker ended, and prefer resuming the same task and worktree. Native work has no automatic expiry. A synchronization failure returns an error and blocks preparation; resolve it before retrying.

After an interrupted completion or lost response, retry the same completion. A prepared result can publish without repeating semantic work. `effects_pending` also calls for `complete` recovery; the semantic result has already landed and needs no new curator.

When a stale base or deliberately abandoned edits require new reconciliation, use:

```bash
rightmemory curate --root /absolute/memory-root begin --domain memory --retry TASK
```

After confirming the previous worker has stopped, this can also explicitly take over its claim from another checkout. It preserves the selected evidence and fences the old publication, without carrying over unpublished edits. It is not the recovery action for a merely uncertain completion response.

## Requested Maintenance

`maintain-rightmemory`, `maintain-agent-guidance`, and `maintain-work-skills` use the same preparation and completion flow. A maintenance task starts from scoped existing material:

```bash
rightmemory curate --root /absolute/memory-root begin --domain skills --mode maintain --scope "improve the report workflow"
```

Maintain does not consume pending evidence or ask the agent to edit processing records. The domain core and publication boundary remain the same as Update. Pursuit changes use the separate map workflow.

## Sync and MCP

Configured sync refreshes sources before retrieval and synchronizes before acquiring a curation claim. Completion validates and publishes through the protected shared claim. Explicit assistant sync controls remain available:

```bash
rightmemory assistant --root /absolute/memory-root sync refresh
rightmemory assistant --root /absolute/memory-root sync pull
rightmemory assistant --root /absolute/memory-root sync push
```

An incoming conflict or invalid merged state reports the blocking files or operation. Resolve it through the sync repair workflow before preparing new work. This interface does not automatically delegate a native sync-repair agent.

The optional native MCP transport wraps the same controls:

```bash
rightmemory assistant --root /absolute/memory-root mcp --transport stdio
```

Keep the shared assistant skill active with this transport. Its curation tools are `rightmemory_submit`, `rightmemory_begin_curation`, `rightmemory_complete_curation`, `rightmemory_curation_status`, and `rightmemory_withdraw_candidate`. Responses retain readiness and active-task notices because the assistant coordinates native workers. Ordinary `rightmemory mcp` exposes only retrieval and submission. Manual configured execution uses `rightmemory curate run`; neither MCP transport exposes that control. The maintenance skills continue to use the CLI. See [MCP](MCP.md).

Native execution covers retrieval and all three curation domains. Internal model history, incremental retrieval caching, Insight, and Pruner remain capabilities of the configured runtime; native task preparation does not claim those behaviors.
