# DESIGN_NOTES

## Project

### Multi-file memory tree

`MEMORY.md` remains normal memory instead of becoming a routing-only index, because the root file should still carry useful high-level graph nodes and readable context. `#`, `##`, and `###` are normal tree layers; `####` is reserved for file-backed detail pointers under a `###` topic, so deeper detail can move out without pretending that a broad section is a detail file. A `####` pointer may keep short body text that summarizes or explains the detail file, but nodes and child headings belong in the detail file.

### Addressable headings

`#`, `##`, and `###` headings may carry `{#slug}` anchors and graph edges because some relations apply to an entire subtree, not one fact node. Heading slugs and node ids share one namespace so edges can target either form without fake hub nodes or duplicate identifiers.

### Containment is tree structure

Child nodes should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Structural clarity over node count

Memory update edits optimize for a readable heading tree and coherent graph nodes instead of minimizing new nodes. Updating an existing node is appropriate when the same fact is being refined, but adding, splitting, merging, or moving headings and nodes is preferred when it prevents overloaded records or makes the memory structure clearer.

### Detail file naming

Detail files use short explicit slugs from file-backed headings such as `#### Topic {F#slug}` and map to `MEMORY_<slug>.md`. Graph edges still target `slug`, not `F#slug`. This keeps filenames stable and short while preserving the visible tree + graph model in the Markdown content; filenames are storage details, not graph nodes.

### Schema ownership

Schema rules live in `skills/rightmemory-schema.md` instead of at the top of every `MEMORY.md`, because memory files should stay focused on user memory while prompt/schema changes remain single-source and installable with the skills.

### User context and agent behavior domains

`# User Context` and `# Cross-Session Agent Behavior` separate subject matter rather than storage mechanics. User context models the user's durable context profile: who they are in relation to ongoing life and work, what they are pursuing, and why. Agent behavior models how coding agents should act with that user. This keeps profile facts and behavior guidance from blurring while allowing both to live in the same tree + graph memory model.

### Automatic write isolation

Automatic `update`, `reviewer`, `dreamer`, and `pruner` turns run in temporary Git worktrees when they operate on the main state root. The role still behaves like an ordinary memory writer: it reads, edits, validates, and commits allowed memory files. Runtime validates those temporary commits, keeps `MEMORY.md` as a regular file, lands successful commits in the main memory repo, and promotes temporary session/provider state after the isolated turn succeeds. CLI-agent isolation uses a fresh provider session for speculative work so failed attempts do not advance the prior durable provider session.

This keeps the user-facing memory repo focused on completed memory commits instead of partial agent editing state. Failed or interrupted temporary work is discarded because the durable retry source is the original update batch, transcript batch, or dreamer trigger balance. Dirty main memory files block automatic semantic writes independently of remote sync so local edits remain visible instead of being blended into a model-created change.

### Generation pruning

Forgetting is modeled as pressure on the active surface rather than deletion from history. `pruner` runs after the memory repo accumulates a configured number of commits since the latest `prune:` checkpoint. Commit count is the clock because it tracks work done and naturally ignores vacations or quiet periods.

The active files stay free of lifecycle metadata. Prune commits carry the ledger: boundary commit, removed ids, revived ids under grace, and useful skips. A reappeared item receives a small number of grace checkpoints, then returns to ordinary pruning judgment. This keeps recurring memory possible without turning every memory node into a miniature retention object.

Historical retrieval is explicit. `retrieve` stays focused on current active memory, while `historian` searches `prune:` ledgers and Git snapshots and labels results as historical context. Reactivation goes through normal update so old memory has to earn its way back into the active surface.

### Change-triggered dreamer

Dreamer watch is driven by accumulated successful memory work rather than elapsed time. Successful update and review batches add points under `.runtime/dreamer/trigger-state.json`; the watcher checks that balance on its configured cadence and runs when the threshold is reached. A successful automatic dream consumes the threshold after the cycle lands or completes as a valid no-op, while failure preserves the balance. This makes dreamer a consolidation pressure valve instead of a clock-driven maintenance task.

### Command-backed roles

RightMemory exposes explicit command roles because each memory operation has a different authority boundary. `retrieve` is fast and read-oriented. `history` is read-only archaeology over pruned memory. `update` writes candidate memory. `dreamer` consolidates and restructures. `pruner` removes stale active memory after a generation threshold. The caller chooses the role at startup so the runtime can load the right prompt, authority boundary, session history, and executor config without asking one model context to infer behavior from dispatch tags; the configured memory root remains the runtime root because that is the memory store and the intended ownership boundary.

### Command-backed install modes

Both install modes give the host agent the same workflow: a `memory-orchestrator` skill that calls the `rightmemory` command. The difference is the executor behind that command. Standalone mode runs RightMemory's local Pydantic AI agent and bounded memory tools. CLI-agent mode delegates the role turn to Codex CLI or Claude Code CLI while preserving RightMemory's role prompts, session records, memory root, and command surface.

### Executor config

Standalone mode uses role-local model tables because retrieve, history, update, dreamer, reviewer, pruner, and sync repair may need different providers or model sizes. CLI-agent mode uses global `[agent_cli].provider` plus role-local `[<role>.agent_cli]` tables so one memory root can use Codex for some roles and Claude for others. `anthropic/...` remains the explicit Anthropic selector for standalone models; other model ids are treated as OpenAI-compatible so local gateways and hosted vLLM endpoints stay simple.

### Standalone tool boundary

Standalone mode exposes narrow filesystem and git tools instead of arbitrary Python execution because the memory agent should behave like a coding assistant while keeping the configured memory root as the ownership boundary. Search, outline, context reads, Codex-style patches, and validation reduce line-number guesswork without opening the door to arbitrary shell control or memory-specific CRUD lock-in.

### Standalone commit boundary

Standalone commit tools are scoped to `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md` because write-capable roles need to preserve memory edits and dream reports without gaining arbitrary repository-write authority. Empty commits are reserved for `prune:` checkpoints so generation boundaries can advance even when the pruner removes nothing. Retrieve and historian do not receive write tools, so active and historical retrieval remain lower-authority paths. Unrelated untracked files remain visible through status but outside the stage/commit allowlist so model-driven commits do not sweep up local config, backups, or test artifacts.

### Global memory sync

Global memory sync remains local-first: every device keeps a complete memory root, and Git provides distributed transport between those roots. The runtime depends on the ordinary upstream branch contract rather than a hosted-provider API, so a private GitHub repository is convenient but not structurally special.

Runtime code owns deterministic sync mechanics at the point where each one belongs in the workflow. For `update`, `reviewer`, `dreamer`, and `pruner`, sync preflight handles upstream freshness before semantic model work when sync is enabled, and push handling publishes committed memory changes after successful automatic writes land. Sync-detected dirty or conflicted memory state routes to `sync-reconciler`; the isolated-write dirty-main guard remains local and runs even when sync is disabled, but it also gives `sync-reconciler` one bounded repair attempt before failing the automatic write. Retrieval and historical retrieval keep the fast local path by default.

Memory-aware repair stays in `sync-reconciler` because Markdown memory conflicts and dirty memory state require durability and schema judgment, not just Git mechanics. Runtime dirty-main checks and scheduled sync flows call that role with bounded repair context, and the role validates the file set, commits the repaired state, and calls `sync_push` when sync is enabled. `sync-reconciler` stays separate from dreamer because repair is a narrow maintenance responsibility, while dreamer owns broader consolidation and restructuring.

### Standalone tool retry behavior

Recoverable tool mistakes, such as stale patch context or invalid read ranges, are returned to the model as retry prompts because the model can usually fix them by searching or re-reading current file context. Hard daemon errors are reserved for problems the model cannot reasonably repair inside the same turn, which keeps caller-visible failures focused on runtime or infrastructure issues.

### Optional MCP adapter

MCP is kept outside the MVP because the primary interface should be plain CLI and JSON-over-stdio for easy use by any agent or shell workflow. A future MCP server can wrap the same daemon protocol without changing the standalone runtime's core behavior.

### File-backed standalone sessions

One-shot standalone calls use an explicit `--session` id and persist native Pydantic AI message history under `.runtime/sessions/` because normal agent callers often start a fresh process per request but still need true multi-turn continuity. RightMemory owns load/save with locking and atomic replacement so callers do not need a background broker, the stored state remains exact model/tool history instead of a lossy chat transcript, and `.runtime/` self-ignores its contents so ephemeral session files do not pollute memory commits.

### Batched command updates

Update submissions accumulate as candidate briefs under their original session id. Each session keeps a one-hour quiet period from its latest submit, but execution is owned by one global async update worker per memory root. The worker batches eligible session queues by candidate count, keeps each included session queue whole, and runs the update role once for the cross-session batch; per-session state still powers `pull`, `undo`, retry, and recent-submitted retrieval. Async state files keep their own `session_id` and `role` fields instead of inferring them from the read path because submitted candidates are operational state and malformed state should fail visibly.

### Command role prompts

Role prompts live as role-specific Markdown files under the runtime package because the command runtime is the source of role behavior in both install modes. `prompt.py` stays a small composer for schema, workspace, tool, and role prompt fragments, while role-specific judgment such as update candidate triage, historical retrieval, and pruning policy stays in the role prompt where prompt policy is easier to review and revise.

### Semantic upgrade notes

Semantic upgrade notes let maintainers flag conceptual changes that existing memory may need to absorb after reinstall. The notes live with the runtime package and are tracked per memory root under `.runtime/semantic-upgrades.json`, because they are operational maintenance state rather than user memory. Install reports pending notes but leaves memory untouched; dreamer receives pending notes during consolidation and marks them absorbed after a successful run. Missed notes are supplied chronologically so skipped versions keep their semantic trail, while dreamer follows the later note when newer guidance refines or contradicts earlier guidance.
