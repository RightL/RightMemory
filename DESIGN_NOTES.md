
# DESIGN_NOTES

## Project

### One graph in two document trees

Memory and Pursuit use different lifecycles but belong to one graph. Global ids and cross-tree edges let live work depend on durable context without copying it, while a unified update can close live intent and preserve an independently durable consequence atomically. Treating the trees as separate graphs would require duplicate concepts, prose-only references, or another resolver without creating a useful authority boundary.

### Parsed graph and linked resources

The parsed graph starts from `MEMORY.md` and `PURSUITS.md` and follows F# backing files rather than a filename glob. F# resolves according to its containing tree because the filename is storage detail: Memory uses `MEMORY_<id>.md`, while Pursuit uses `PURSUIT_<id>.md`.

M# and S# headings remain addressable Memory objects, but their backing files are linked resources rather than graph content. M# preserves free-form evidence; S# preserves directly executable instruction. This boundary prevents graph-looking Markdown inside a correction example or skill body from silently becoming graph structure.

### Addressable headings

Addressable headings and fact nodes share the global RightMemory id namespace because relations may apply to an entire subtree rather than one leaf fact. Edges can therefore connect headings or nodes in either document root without fake hub nodes, duplicated identifiers, or root-qualified reference syntax.

### Containment is tree structure

Child nodes in either document root should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Structural clarity over node count

Updates prefer the smallest coherent change that preserves the useful durable or live meaning. Refining an existing node is preferable when it avoids repetition; splitting, merging, moving, or adding structure is justified only when the resulting tree is materially clearer. This guards against both overloaded records and the opposite failure of expanding a small candidate into unnecessary headings, derived facts, or snapshot detail.

### Pursuit is live intent

Pursuit is neither a backlog nor a work log. It records intent that remains relevant after the updater reconciles the latest task state. Duration is not the criterion: work completed in one session normally leaves no Pursuit, while a short interrupted or waiting task may need one. Completed or obsolete intent is removed, and its consequences enter Memory only when they independently satisfy Memory's durability standard.

The full orchestrator submits lightweight task-state candidates when non-trivial work begins and when it completes, blocks, changes direction, or reaches a handoff. This makes interrupted work recoverable without asking the orchestrator to predict whether a Pursuit should exist. Batching lets the updater see a start and completion as one evolving account and avoid preserving short-lived work.

### Schema, rules, and examples

`skills/rightmemory-schema.md`, `PURSUIT_RULES.md`, and the Memory/Pursuit examples are sufficient for stored-document semantics. Skills separately define host-agent workflow, while role prompts define runtime judgment. Adding another documentation-first instruction layer would duplicate those sources without improving the model.

### User context and agent behavior domains

`# User Context` and `# Cross-Session Agent Behavior` separate durable subject matter rather than storage mechanics. User context models stable facts and context about the user; Agent Behavior models how coding agents should act with that user. Intent that is still being pursued belongs under the Pursuit root instead of being mixed into a durable profile.

### Agent-facing skill surfaces

RightMemory installs two user-selected surfaces without implicit priority between them. `memory-retriever` is read-only; `rightmemory-orchestrator` combines conditional retrieval with full Memory + Pursuit maintenance. The orchestrator inherits the existing selective retrieval policy rather than retrieving everything: factual or project context is fetched when the conversation lacks it, while preference and behavior guidance is fetched more proactively when it will shape a decision. Correction M# evidence remains a late second-pass resource rather than part of that proactive retrieval.

The Memory-only orchestrator is superseded because it no longer represents a useful target authority choice. Keeping it alongside the full orchestrator would preserve another prompt and install surface without giving Pursuit a coherent owner.

### Unified RightMemory update

Updating RightMemory is one judgment over two lifecycles. The updater treats related candidates for the same work or Pursuit as an evolving account, reconciles those submissions together, and decides what remains live, what became independently durable, and what should be omitted. Session ids remain transport and batching provenance rather than task identity. The updater may change Memory, Pursuit, both, or neither in one isolated transaction and one commit.

This separation keeps orchestration simple: the skill reports task state, while the updater owns storage judgment and matching against existing Pursuits. Memory retains its strict durable filter and does not become a bug database, implementation log, experiment ledger, or duplicate of project-local artifacts. Pursuit shares the existing update queue, model role, and schedule because a second pipeline would make one task settle at two different times.

### Two correction channels

Corrections to ordinary agent writing, reasoning, or behavior may become Writing or Design M# evidence when the rejected/accepted contrast is useful during a future second pass. A directly executable rule belongs in ordinary Agent Behavior or S# instead, because storing the same lesson as both instruction and correction evidence would increase prompt weight without increasing guidance.

Corrections to RightMemory edits belong in root-level `corrections.md`, which only the updater consumes semantically. Keeping update-edit feedback outside Memory prevents ordinary retrieval and Memory maintenance from treating updater examples as user knowledge. The same event is not stored in both channels.

### Offline review and correction feedback

Each normal unified-updater commit produces one local Markdown review document with a natural account of the change and one free-form human comment area; correction and maintenance commits do not. A periodic checker handles each stable comment revision once. Semantic correction uses the review, original diff, and current state so it can preserve unrelated later work. The review area is a bounded disposable inbox: resolved files are removed, untouched blank files expire, and ambiguous requests remain until the comment changes.

A successful state correction is independent from feedback admission. The state change lands whenever it succeeds; its `corrections.md` candidates may be merged, admitted, or discarded, with any accepted feedback included in the same commit. This preserves the user's requested state without forcing a weak example into the bounded feedback set.

### Bounded agent correction memory

Explicit user corrections can be durable evidence about how future agents should write, reason, decide, or act, so orchestrators with update authority may submit the raw correction event as a candidate. The updater, rather than the orchestrator, decides whether it is reusable and maintains fixed Writing and Design M# collections, classified by whether expression alone or the underlying reasoning and action must change. Fixed categories prevent a correction-file hierarchy from growing around projects and tools.

The M# collections and updater-only `corrections.md` are bounded priority sets rather than append-only logs or FIFO windows. A related correction improves or replaces weaker evidence in an existing item. A distinct correction is admitted only when reusable enough; when its collection is full, it replaces an item only if it is more important and is otherwise discarded. The maximum of 15 compact items is therefore a ceiling, not a quota or automatic eviction trigger. Consulting correction evidence after a tentative draft or update avoids anchoring every task on a large correction prompt.

### Automatic write isolation

Automatic semantic turns run in temporary Git worktrees when they operate on the main state root. Each role keeps an explicit write boundary: unified update may maintain Memory and Pursuit together, correction may also update `corrections.md`, Memory-oriented maintenance roles preserve cross-tree references, and Insight commits only reflective logs. Runtime validates the role-owned result before landing it and promotes temporary session/provider state only after the isolated turn succeeds.

This keeps the user-facing RightMemory repo focused on completed commits instead of partial agent editing state. Failed or interrupted work is discarded because its source input remains retryable. Dirty user-owned state blocks automatic semantic writes independently of remote sync so local edits remain visible instead of being blended into a model-created change.

### Generation pruning

Forgetting is modeled as pressure on active Memory rather than deletion from history. `pruner` remains Memory-only and runs after enough Memory-changing commits accumulate since the latest `prune:` checkpoint. Memory-changing commit count is the clock because it tracks actual pressure on the surface being pruned while ignoring vacations, quiet periods, Pursuit-only activity, and correction-only maintenance. A Memory id referenced from Pursuit is still active graph structure, so Pruner preserves it rather than creating a dangling cross-tree edge.

The active files stay free of lifecycle metadata. Prune commits carry the ledger: boundary commit, removed ids, revived ids under grace, and useful skips. A reappeared item receives a small number of grace checkpoints, then returns to ordinary pruning judgment. This keeps recurring memory possible without turning every memory node into a miniature retention object.

Historical retrieval is explicit. Ordinary retrieval stays focused on the current RightMemory graph, while `historian` searches Memory's `prune:` ledgers and Git snapshots and labels results as historical context. Reactivation goes through unified update so old Memory has to earn its way back into the active surface.

### Change-triggered dreamer

Dreamer watch is driven by accumulated successful Memory work rather than elapsed time. Unified updates and review corrections add consolidation pressure only when they change Memory; Pursuit-only activity does not make Dreamer responsible for live-state maintenance. The watcher consumes its threshold only after a successful cycle or valid no-op, which keeps Dreamer a Memory consolidation pressure valve rather than a second Pursuit updater.

### Insight logs

Insight logs are a reflective artifact stream inside the RightMemory repo. They are useful when durable Memory activity reveals broader patterns, strategy, risks, recommendations, or next-step ideas that should be preserved without turning them into active Memory facts. Insight remains Memory-oriented, writes timestamped essays under `insight_logs/`, and does not treat Pursuit or updater corrections as its own source material.

### Command-backed roles

RightMemory exposes explicit command roles because operations still have different authority boundaries even though Memory and Pursuit share one graph. `retrieve` is read-only over current RightMemory. Transcript review extracts candidates from idle sessions and routes them through `update` rather than writing the graph itself. `history`, `dreamer`, `insight`, and `pruner` remain Memory-oriented, while `update` performs the unified durable-versus-live reconciliation across both document trees. Offline update correction is a mode of that semantic update authority rather than a second updater role. Command-selected roles keep prompts, tools, sessions, and write boundaries explicit without asking one model context to infer authority from dispatch tags.

### Command-backed install modes

Both install modes expose the same two independently selected skills: read-only `memory-retriever` and full `rightmemory-orchestrator`. The difference between install modes remains the executor behind their commands. Standalone mode runs RightMemory's local Pydantic AI agent and bounded tools; CLI-agent mode delegates the same canonical role behavior while preserving RightMemory's prompts, session records, root, and command surface.

### CLI-agent conversation lifecycle

Retrieve keeps a provider conversation across independent commands because follow-up questions benefit from conversational continuity and provider prefix caching. Other roles are semantic transactions rather than chats, so resuming them would mix unrelated evidence and maintenance cycles; they use fresh provider conversations instead. Explicit interactive chat may retain context only for that process, which gives the operator continuity without turning a temporary chat into a later command's hidden input.

Provider conversations created by RightMemory are tracked separately from active retrieve mappings. Exact ownership is necessary both to exclude internal work from transcript review and to delete only conversations RightMemory can prove it created. Registered Codex conversations expire after 24 hours of inactivity through Codex's supported deletion interface. Pre-registry history is intentionally left alone because incomplete cleanup is safer than guessing ownership from prompts, paths, or timestamps.

### Executor config

Standalone mode uses role-local model tables because retrieval, unified update, history, Dreamer, Insight, transcript review, pruning, and sync repair may need different providers or model sizes. Pursuit is part of unified update and therefore does not introduce another updater model table. CLI-agent mode keeps the corresponding global provider plus role-local overrides so one RightMemory root can use different executors where the authority genuinely differs.

### Standalone tool boundary

Standalone mode exposes narrow filesystem and Git tools instead of arbitrary Python execution because the agent should behave like a coding assistant while keeping the configured RightMemory root as the ownership boundary. Search, outline, context reads, exact patches, and complete-graph validation reduce line-number guesswork without opening arbitrary shell control or storage-specific CRUD lock-in.

### Standalone commit boundary

Standalone commit tools are role-aware. Unified update may commit Memory and Pursuit files together; semantic correction may additionally commit `corrections.md`; Memory-oriented maintenance roles retain their smaller surfaces; Insight commits `insight_logs/*.md`. Empty commits remain reserved for `prune:` checkpoints. Retrieval roles receive no write tools, and unrelated files remain outside model-driven stage and commit allowlists.

### Global RightMemory sync

Global sync remains local-first: every device keeps a complete RightMemory root, and Git provides distributed transport between those roots. Memory, Pursuit, and `corrections.md` are synchronized state; offline review documents remain local runtime artifacts. The runtime depends on the ordinary upstream branch contract rather than a hosted-provider API, so a private GitHub repository is convenient but not structurally special.

Runtime code owns deterministic sync mechanics where they belong in the workflow. Sync preflight handles upstream freshness before automatic semantic work, and push handling publishes completed role-owned commits. Dirty or conflicted synchronized state routes to `sync-reconciler`; the isolated-write dirty-main guard remains local even when remote sync is disabled. Retrieval and historical retrieval keep the fast local path by default.

Graph-aware repair stays in `sync-reconciler` because conflicts across Memory, Pursuit, and their relationships require schema judgment rather than Git mechanics alone. For `corrections.md`, repair preserves non-identical entries without ranking them; semantic merging and replacement remain updater-owned. `sync-reconciler` stays separate from Dreamer because repair is a narrow integrity responsibility, while Dreamer owns broader Memory consolidation.

### Standalone tool retry behavior

Recoverable tool mistakes, such as stale patch context or invalid read ranges, are returned to the model as retry prompts because the model can usually fix them by searching or re-reading current file context. Hard daemon errors are reserved for problems the model cannot reasonably repair inside the same turn, which keeps caller-visible failures focused on runtime or infrastructure issues.

### Optional MCP adapter

MCP is kept outside the MVP because the primary interface should be plain CLI and JSON-over-stdio for easy use by any agent or shell workflow. A future MCP server can wrap the same daemon protocol without changing the standalone runtime's core behavior.

### File-backed standalone sessions

One-shot standalone calls use an explicit `--session` id and persist native Pydantic AI message history under `.runtime/sessions/` because normal agent callers often start a fresh process per request but still need true multi-turn continuity. RightMemory owns load/save with locking and atomic replacement so callers do not need a background broker, the stored state remains exact model/tool history instead of a lossy chat transcript, and `.runtime/` self-ignores its contents so ephemeral session files do not pollute memory commits.

### Batched command updates

Update submissions accumulate as RightMemory candidate briefs under their original session id. Related task-start, progress, completion, blockage, and handoff submissions form an evolving account, while the session id remains conversation provenance rather than task identity. One global async worker batches whole eligible session queues and invokes the unified updater once for the cross-session batch, letting it discard work that began and ended within an account while preserving live continuity when needed. There is no second Pursuit queue or cadence. Per-session state still powers cancellation, status, retry, and recent-submitted retrieval.

Normal submitted work batches for token efficiency and coherent state judgment, while failed work uses a separate recovery lane. Once a queue has failed, preserving candidate correctness matters more than waiting for another full batch. Repeated failure stops visibly instead of silently looping forever.

### Command role prompts

Role prompts live as role-specific Markdown files under the runtime package because the command runtime is the source of role behavior in both install modes. Prompt composition stays small, while role-specific judgment such as unified durable-versus-live reconciliation, historical retrieval, and pruning policy remains reviewable in the corresponding role prompt.

### Compatibility posture

RightMemory favors one coherent current model over compatibility scaffolding. When a managed prompt, skill, schema interpretation, test, or code path is superseded, it should be replaced or removed rather than preserved through aliases, dual formats, and migration branches. Current user-authored Memory, Pursuit, and correction content remains protected from accidental overwrite, but obsolete managed behavior does not justify another subsystem.
