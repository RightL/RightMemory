# Global Memory Sync Design

## Context

RightMemory currently treats one local memory root as the source of truth. The root contains `MEMORY.md`, optional `MEMORY_*.md` detail files, `dream_logs/`, `rightmemory.toml`, and `.runtime/`. Git already provides history and revertability for memory files, while `.runtime/` stores local operational state such as sessions, locks, review state, watcher state, and debug traces.

The next step is global memory across a person's devices and networks. The design should preserve the local file model, avoid a central RightMemory service, and let memory roles handle semantic conflicts when Git cannot merge Markdown safely.

## Goals

- Give one person a shared RightMemory across multiple devices.
- Support concurrent writes from multiple agents or devices.
- Keep normal use automatic: write-capable roles sync before and after memory edits, while scheduled sync keeps idle devices reasonably fresh.
- Keep retrieval fast and local by default.
- Use Git as the distributed transport and history log, with private GitHub as the guided setup path and any SSH/HTTPS Git remote as the underlying mechanism.
- Keep `.runtime/` local to each device.
- Let existing write-capable roles resolve conflicts they encounter during their own work.
- Add a narrow sync reconciler role for scheduled sync conflicts when no active write role owns the task.

## Out Of Scope

- A central RightMemory service, account system, hosted API, or remote lock server is outside this design.
- Manual sync commands are not part of the normal user experience.
- Broad consolidation stays outside sync conflict repair.
- Retrieval-time pull is outside the default path.

## Storage Model

Each device has a full local memory repo, normally at `~/.rightmemory`.

Synced files:

- `MEMORY.md`
- `MEMORY_*.md`
- `dream_logs/*.md`
- `rightmemory.toml`, if the user chooses to share role and sync config across devices

Local files:

- `.runtime/`
- session histories
- async update queues
- watcher pid/log/state files
- local locks
- debug traces
- sync state such as last successful pull, pending push, and recent failure details

The installer continues to create a memory-root `.gitignore` allowlist so operational state does not enter memory commits. Sync state lives under `.runtime/sync/`.

## Config Shape

The normal config should stay small:

```toml
[sync]
enabled = true
stale_pull_after_hours = 24
```

Remote name, branch, last pull time, pending push state, and conflict details are operational details. They can live in `.runtime/sync/state.json` or diagnostic output instead of becoming the main user-facing model.

Setup can offer two paths:

- Private GitHub repository as the guided default.
- Any SSH/HTTPS Git remote for users who already have a preferred Git host.

Both setup paths configure a normal Git remote in the memory repo. Runtime sync code should depend on Git behavior rather than GitHub-specific APIs unless setup needs optional convenience.

## Role Model

RightMemory keeps memory judgment inside memory roles and keeps sync timing in runtime code.

- `retrieve` reads local memory and does not pull by default.
- `update` writes candidate memory. Runtime pre-syncs before the model turn; if the role hits a sync conflict during its own write, `update` resolves it.
- `reviewer` writes transcript-derived memory with the same sync behavior as `update`.
- `dreamer` consolidates memory with the same sync behavior as other writers.
- `sync-reconciler` resolves scheduled/background sync conflicts when no active write role owns the conflict.

The sync reconciler prompt should describe its small universe positively:

- It resolves RightMemory sync conflicts.
- The runtime has already detected conflicted memory files.
- It reads conflicted files, resolves markers, and preserves coherent durable memory from both sides.
- It keeps the RightMemory schema valid.
- It commits the resolved memory state and pushes through the provided sync/git tools.
- Its final reply reports files resolved, validation result, commit hash, and push result.

## Runtime-Assisted Sync

Deterministic Git work should happen in code before the model spends tokens on the memory task.

For write-capable roles:

1. Runtime acquires the local memory write lock.
2. Runtime runs sync preflight when sync is enabled.
3. Runtime fetches and fast-forwards or rebases when the local state is suitable.
4. Runtime detects offline state, dirty local state, remote divergence, push-pending state, or conflicts.
5. Runtime injects compact `sync_context` into the role input.
6. The active role performs the memory task.
7. The role validates memory, commits allowed memory files, and pushes.
8. If push is rejected or a rebase conflict appears during this write, the same active role resolves the conflict, validates again, commits the resolution if needed, and pushes again.

The injected `sync_context` is authoritative for the start of that model turn. Role prompts should tell the model to trust it rather than repeating preflight discovery. The role should use tools when its own edits or later sync tool results change the state, or when it needs to inspect specific conflict files.

For scheduled freshness:

1. The existing `rightmemory watch` manager gains a `sync` target.
2. The sync watcher checks `.runtime/sync/state.json`.
3. If the last successful pull is older than `stale_pull_after_hours`, runtime syncs.
4. A clean pull exits without a model call.
5. A scheduled pull conflict invokes `sync-reconciler`.

## Error Handling

- Remote unavailable: the active write may still commit locally, record pending push state, and let a later sync watcher push it.
- Clean pull or push: model intervention is unnecessary.
- Pull/rebase conflict during scheduled sync: call `sync-reconciler`.
- Pull/rebase conflict during an active write: the active write role resolves it.
- Push rejected after commit: runtime fetches/rebases; the active role resolves conflicts if needed; push retries.
- Schema validation failure after conflict resolution: the same role fixes the memory until valid or reports a hard blocker.
- Repeated sync failures: record the latest state under `.runtime/sync/state.json` for watcher/status visibility.

## Testing

Tests should use local bare Git repositories instead of GitHub so they are deterministic and do not need network credentials.

Coverage should include:

- `[sync]` config parsing.
- Runtime preflight for clean, stale, dirty, offline, pending-push, and conflict states.
- Sync context injection for write roles.
- Prompt guidance that treats sync context as already current at turn start.
- Push rejection and rebase conflict paths.
- Scheduled sync watcher invoking `sync-reconciler` for background conflicts.
- Sync reconciler resolving fixture conflict markers in `MEMORY*.md`.
- Schema validation after reconciliation.
- `.runtime/` remaining local and uncommitted.

## Open Decisions For Implementation Planning

- Whether `rightmemory.toml` should be synced by default or treated as per-device config with a separate shared config file.
- Whether setup commands should create the GitHub repo directly through `gh` when available or print exact guidance for the user to run.
- How much diagnostic CLI surface to expose for support, such as `rightmemory sync status`, without making manual sync part of the normal workflow.
