# Assistant Execution

RightMemory can use the current assistant's native agents for memory work. The main agent submits evidence and delegates ready batches. Retrieve inspects the real memory files and pending evidence; Update edits an isolated Git worktree. RightMemory performs deterministic queue, validation, publication, and recovery operations without creating a model client.

This path uses one shared [assistant skill](../skills/rightmemory-assistant/SKILL.md), a [Retrieve role](../rightmemory/prompts/assistant-retrieve.md), and the existing [Update role](../rightmemory/prompts/update.md). The existing standalone and CLI-agent paths remain available.

## Setup

Install the package in a Python 3.11+ environment on the computer that will hold the memory root:

```bash
python -m pip install .
```

Use a complete RightMemory Git root with a configured Git author. A disposable root is appropriate for initial verification. Copy `skills/rightmemory-assistant/` from the same checkout into the assistant's supported skill location and enable it in the conversation. The existing `install.sh` and `install.ps1` modes do not select assistant execution or install this skill automatically.

Assistant execution does not require RightMemory model API credentials, Codex, or Claude Code. The assistant uses its own configured agent. Keep the active root explicit:

```bash
rightmemory assistant --root /absolute/memory-root context --session example-session
```

The assistant's native subagents must be able to read the returned memory and pending paths and write the assigned Update worktree. A remote MCP connection does not expose the server's filesystem to native file tools. Place the runtime and native agents in a common accessible environment, or provide an authorized shared filesystem. Each assistant may use its own synchronized Git checkout.

An Update worktree isolates candidate edits from the active root. A working-directory setting alone does not restrict arbitrary native shell access; use the assistant's file permissions where available. RightMemory validates the candidate it admits, while the assistant governs its agents' other file access.

## Read and Submit

The main agent delegates the retrieval need, bound root, session id, and `context` command (or matching MCP tool) to a native Retrieve subagent. That subagent calls `context` and reads its returned instruction file. The result supplies real source locations, imported-view mappings, and readable pending evidence, plus the observed `memory_commit` and `live_files: true`: memory sources remain live, rather than becoming a frozen snapshot. The subagent inspects them with native file tools and returns a useful summary with sources. This path does not require a `RetrieveSelection` or run the original retrieval renderer.

Save submission evidence in a UTF-8 file outside the memory root:

```bash
rightmemory assistant --root /absolute/memory-root submit --session example-session --request-id evidence-1 --file /absolute/evidence.txt
```

Submission records evidence for later reconciliation. It does not start an internal model worker. Retry the same submission with the same request id and content; changed evidence needs a new request id.

Normal assistant tool responses can carry a batch-ready notice. A Retrieve or other subagent passes that notice to its main agent, which arranges Update. The program owns pending and active batch state; the main agent owns native delegation.

## Update a Batch

```bash
rightmemory assistant --root /absolute/memory-root update begin
```

Delegate only a newly claimed task (`claimed: true`). Give the native Update subagent the returned `instructions_file`, `candidates_file`, and `worktree`. Repeating ordinary `begin` while a task is active returns that task with `claimed: false`; resume the existing task instead of dispatching a second updater. `status: not_ready` means there is no eligible batch, and `status: sync_required` means sync needs attention first.

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

An incoming conflict or invalid merged state returns sync status and available file/operation details, and blocks a new Update. This assistant interface does not yet provide a native-agent sync-repair workspace protocol. The existing separate repair paths remain available; resolve the conflict before retrying ordinary sync.

The optional MCP transport wraps the same assistant core:

```bash
rightmemory assistant --root /absolute/memory-root mcp --transport stdio
```

Load the shared assistant skill with this MCP transport; it explains how the assistant's own agents use the deterministic tools. The older `rightmemory mcp` command continues to use its configured internal role backend. See [MCP](MCP.md).

The assistant path is not a replacement for all internal-runtime behavior. It does not promise the old model-session history, incremental retrieval cache, delivery suppression, transcript watchers, Dreamer, or scheduled maintenance. Use the existing paths for those mechanisms unless separately supported. The public assistant queue and completion receipts cover native batch execution rather than pretending that the old `update submit` worker queue dispatches native subagents.

## Dot Verification

Run the following in an explicitly authorized disposable memory root. Do not connect real memory, create credentials, or enable a persistent connection merely to run this checklist. In that root's `rightmemory.toml`, use a small test batch so one submitted candidate becomes ready immediately:

```toml
[update.async]
trigger_candidates = 1
target_batch_candidates = 1
max_wait_seconds = 1
```

The normal defaults are 15 candidates to trigger, 30 as the preferred batch size, and a 24-hour fallback after the quiet period. The small values above are for this disposable exercise.

1. **Check the actual execution environment.** Let a native Retrieve subagent read a fictional marker stored in a valid memory entry using its own file tools. During Update, let its native subagent modify an allowed entry in the assigned worktree, then have RightMemory inspect that candidate. This establishes shared filesystem access; MCP discovery alone does not.
2. **Exercise the shared skill.** Submit a fictional durable fact. Before reconciliation, have a fresh Retrieve subagent find it as pending evidence. Trigger a normal tool call that returns a ready-batch notice to the subagent and verify that the main agent receives it and delegates Update.
3. **Finish a real Update.** Let the native Update subagent read the candidate, decide and make the edit in the assigned worktree, run standalone validation, commit, and complete the task. Inspect the program's outcome and the active root's Git change.
4. **Read it independently.** Start a fresh Retrieve subagent with a question that requires the new fact, without giving it the answer or the Update transcript. Check the returned source against the committed memory.
5. **Exercise failure cases.** Run the cases below against disposable state and inspect both operation receipts and Git state.

| Case | Required result |
| --- | --- |
| Same submission retried | The original submission is returned; evidence is not duplicated. Reusing its id for different content fails. |
| Active root changed after `begin` | Completion reports a conflict; it does not overwrite the newer state. |
| Invalid path or non-regular file | Candidate admission rejects unsafe Git paths or file types; task and session ids cannot escape their state directory. No outside path is changed through the program. |
| Invalid candidate graph | Independent validation and completion reject it; active memory stays unchanged. |
| Interrupted before application | The task remains inspectable and recoverable; no partial active update appears. |
| Applied, reply lost | Status or repeated completion reports the same outcome; no second memory commit is created. |
| Shared-queue publication failed | The durable prepared result remains; repeating completion retries publication without repeating semantic Update. |

Record the assistant/version, RightMemory commit, root and worktree locations, native agent identities, task id, validation result, applied commit, and sync outcome. A check passes only when its observed output supports that result.

## Assistant Verification Status

| Assistant | What the existing evidence establishes | Still to verify for this path |
| --- | --- | --- |
| dot | An earlier isolated stdio handshake, three-tool discovery/error handling, and a limited synthetic write/read exercise. | The native-agent batch flow and failure/recovery cases above; native plugin and filesystem access in the intended deployment. |
| DeepSeek Harness | No completed test of this path is recorded. | This shared skill and tool flow in DSH, using its native provider and filesystem. |
| WorkBuddy | No completed test of this path in WorkBuddy itself is recorded. | The full flow in WorkBuddy itself, including native delegation and worktree access. CodeBuddy results do not establish this. |

These are verification boundaries, not claims that a product has been adapted. Private dot MCP access may use [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), including its direct stdio bridge; tunnel authentication and a persistent client require separate setup and authorization.
