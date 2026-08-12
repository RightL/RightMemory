
# DESIGN_NOTES

## Project

### Three semantic modules, one graph in two document trees

Memory and Pursuit use different lifecycles but belong to one graph. Global ids and cross-tree edges let live intent depend on durable context without copying it, while a unified update can close live intent and preserve an independently durable consequence atomically. Agent Corrections is the third semantic module: a bounded, non-graph library whose concrete user-redirection cases use a different representation and retention judgment. Root `corrections.md` is operational RightMemory Edit Feedback, not a fourth semantic module. Treating the two document trees as separate graphs would require duplicate concepts, prose-only references, or another resolver without creating a useful authority boundary; forcing Agent Corrections into that graph would obscure the contrasts the module exists to preserve.

### Parsed graph and linked resources

The parsed graph starts from `MEMORY.md` and `PURSUITS.md` and follows F# backing files rather than a filename glob. F# resolves according to its containing tree because the filename is storage detail: Memory uses `MEMORY_<id>.md`, while Pursuit uses `PURSUIT_<id>.md`.

M# and S# headings remain addressable Memory objects, but their backing files are linked resources rather than graph content. M# preserves free-form evidence; S# preserves directly executable instruction. This boundary prevents graph-looking Markdown inside a correction example or skill body from silently becoming graph structure.

One canonical parser owns that grammar and produces an ordinary in-memory document index with physical and logical hierarchy, source spans, F# ownership, backing references, hashes, and diagnostics. Validation, retrieval, graph-aware tools, sync, and shared-view extraction consume the same interpretation. Rebuilding the index from authoritative Markdown keeps the design simpler than a persistent parser cache while preventing independent consumers from quietly disagreeing about an id or subtree.

### Schema-valid MF graphs

An MF# heading is a local relationship object, while its imported version-two package is a separate read-only Memory graph. Each view gets its own id namespace: providers can write natural ids without colliding with the consumer or another view, edges cannot escape the package, and only the outer local MF# heading relates imported context to the consumer graph.

The package's direct `dist/MEMORY.md` follows the same canonical Memory grammar and may use ordinary, F#, M#, and S# content with package-local backing files. Nested MF# and MQ# connections are rejected because a mirrored package carries no authority for transitive live resolution. Requiring all direct semantic prose to be addressable removes ambiguous whole-file ranges; qualified nested sources preserve progressive M# range reads and complete S# reads without merging linked resources into the MF graph.

Provider rendering and publication validate an exact temporary package before promotion. HTTP and Git consumers validate the exact downloaded candidate before replacing their last valid import. This validate-before-replace boundary lets a failed refresh remain stale instead of turning a previously usable connection into invalid runtime state, while deliberately treating old free-form version-one packages as derived cache that must be regenerated.

### Addressable headings

Addressable headings and fact nodes share the global RightMemory id namespace because relations may apply to an entire subtree rather than one leaf fact. Edges can therefore connect headings or nodes in either document root without fake hub nodes, duplicated identifiers, or root-qualified reference syntax.

### Containment is tree structure

Child nodes in either document root should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Structural clarity over node count

Updates prefer the smallest coherent change that preserves the useful durable or live meaning. Refining an existing node is preferable when it avoids repetition; splitting, merging, moving, or adding structure is justified only when the resulting tree is materially clearer. This guards against both overloaded records and the opposite failure of expanding a small candidate into unnecessary headings, derived facts, or snapshot detail.

### Pursuit is live intent

Pursuit is neither a backlog nor a work log. It records an objective only while that intent remains part of the active or deliberately parked pursuit structure. Duration and incompleteness are not admission criteria, and detailed execution continuity belongs primarily in project-local artifacts. Completed, abandoned, or superseded intent is removed, and its consequences enter Memory only when they independently satisfy Memory's durability standard.

Both orchestrators use a high evidence bar rather than reporting routine operational events. In the normal path, evidence is due once it is clear and the conversation reaches a natural boundary: approval-gated orchestration proposes it, while automatic orchestration submits it. Known stale, wrong, misleading, or overbroad retrieved state is submitted immediately rather than awaiting a boundary or approval. Work need not be complete, but transient progress, ordinary outcomes, unfinished work by itself, and implementation detail already preserved in project artifacts do not qualify. This keeps Pursuit focused on meaningful live intent rather than treating every interrupted task as semantic state.

### Schema, rules, and examples

The model-facing schema and focused definitions live together under `rightmemory/reference/`, at the same package-relative paths in the source tree and installed runtime. The schema owns representation; the Memory, Pursuit, and Agent Corrections rules own module judgment; the RightMemory Edit Feedback and Shared View rules own their narrower domains; and the Retrieve contract owns runtime input and terminal-selection mechanics. These are product definitions owned by the software version that implements them, not editable or synchronized root state. Runtime prompt assembly reads those package resources directly, while host skills obtain them through `rightmemory reference`; skills define workflow rather than owning shared definitions. The Memory/Pursuit examples remain installer seeds, while the fixed Agent Correction collections and optional `corrections.md` remain root-local evidence governed by the package rules.

### User context and agent behavior domains

`# User Context` and `# Cross-Session Agent Behavior` separate durable subject matter rather than storage mechanics. User context models stable facts and context about the user; Agent Behavior models how coding agents should act with that user. Intent that is still being pursued belongs under the Pursuit root instead of being mixed into a durable profile.

### Agent-facing skill surfaces

RightMemory installs four user-selected surfaces. `memory-retriever` is read-only; `rightmemory-orchestrator` normally proposes qualifying evidence and waits for approval; `rightmemory-auto-orchestrator` submits the same kind of evidence automatically; and explicit-only `maintain-rightmemory` lets the current agent maintain Memory, Pursuit, linked content, Agent Corrections, and RightMemory Edit Feedback directly under their definitions. The approval-gated mode submits known stale, wrong, misleading, or overbroad retrieved state immediately so it does not propagate known-bad context. The two orchestrators are alternative modes for ordinary work, not layers to run together. Both are installed, and the user selects one for the conversation rather than configuring a persisted mode.

Once selected, `memory-retriever` calls Retrieve for the user's stated memory need. The two orchestrators retrieve conditionally when stored context could materially affect how the agent understands or approaches the work and skip clearly self-contained requests. Every ordinary Retrieve call may select relevant Agent Corrections through the fixed `AC#writing` and `AC#design` sources; the whole-collection commands are reserved for explicitly requested review.

### Unified RightMemory update

Updating RightMemory is one reconciliation judgment across three semantic modules. The updater groups related evidence by meaning, compares it with current state, and decides what remains live, what became independently durable, what deserves a reusable Agent Correction, and what should be omitted. Session ids remain transport and batching provenance rather than task identity. The updater may change any combination of the modules, or none of them, in one isolated transaction and one commit.

The orchestrators submit evidence rather than final stored wording, ids, classification, placement, or edit instructions. Evidence clears their shared admission bar only when omission would likely cause poorer future decisions or substantial rediscovery, lose meaningful pursuit context, or allow a settled reusable failure pattern to recur. The updater owns storage judgment and matching against current state. Memory retains its strict durable filter, and updater-driven changes to all three modules share one queue, role, and schedule rather than settling through competing pipelines.

The exact reconciled candidate batch is durable input provenance rather than
queue history. Runtime writes one immutable
`update_records/<operation-id>.json` in the same commit as the semantic outcome;
a no-change outcome creates a record-only commit. The record contains source
evidence only. Its filename and the commit's `RightMemory-Operation` trailer bind
input to outcome, while Git supplies the diff, so the record does not duplicate
derived paths, diffs, or model-authored candidate-to-hunk claims. Local and
synchronized batching use the same canonical operation identity.

### Agent Corrections and edit feedback

Agent Corrections preserves reusable, settled cases in which a user redirected identifiable prior agent work. A redirection may be explicit or implicit, but its resulting direction must be clear, reusable, and accurately scoped before admission. The two fixed files retain their `writing` and `design` filenames and retrieval identifiers, while their semantic categories are Expression and Substance. The deciding question is whether changing expression or presentation alone would fully resolve the objection. If it would, the case is Expression; otherwise a change to reasoning, assumptions, decisions, action, omission, workflow, or behavior makes it Substance. These entries are not M# resources, and ordinary Retrieve can select relevant entries directly.

Reusable feedback about proposed RightMemory edits belongs in root-level `corrections.md`. Update and explicit session review consume it only after forming a tentative curation judgment, while direct maintenance may curate it under its own rules. Keeping this operational feedback outside all three semantic modules prevents ordinary retrieval and Memory maintenance from treating those examples as user knowledge.

A correction to an updater result follows the ordinary candidate path rather than becoming a distinct work type. Update reconciles that evidence against current state, and the outcome receives the same immutable candidate record as any other queued update. This keeps correction processing, provenance, and multi-device fencing in one model.

### Bounded Agent Corrections

A settled user redirection can be evidence about how future agents should express or substantively approach similar work. The approval-gated orchestrator proposes qualifying evidence; the automatic orchestrator submits it at a natural boundary. In either flow, the updater rather than the orchestrator decides whether the contrast is reusable, whether it is Expression or Substance, and how it belongs in the fixed standalone collections outside the addressable graph. Fixed collections prevent a correction-file hierarchy from growing around projects and tools.

The Agent Correction collections and RightMemory edit `corrections.md` are bounded priority sets rather than append-only logs or FIFO windows. A related correction improves or replaces weaker evidence in an existing item. A distinct correction is admitted only when reusable enough; when its collection is full, it replaces an item only if it is more important and is otherwise discarded. The maximum of 15 compact items is therefore a ceiling, not a quota or automatic eviction trigger. Retrieve selects only Agent Correction entries relevant to the current query, while curation roles use `corrections.md` as a late check on tentative RightMemory edits.

### Automatic write isolation

Automatic semantic turns run in temporary Git worktrees when they operate on the main state root. Each role keeps an explicit write boundary: unified Update may maintain Memory, Pursuit, and Agent Corrections together, Memory-oriented maintenance roles preserve cross-tree references, and Insight commits only reflective logs. Runtime validates the role-owned result before landing it and promotes temporary session/provider state only after the isolated turn succeeds.

This keeps the user-facing RightMemory repo focused on completed commits instead of partial agent editing state. Failed or interrupted work is discarded because its source input remains retryable. Dirty user-owned state blocks automatic semantic writes independently of remote sync so local edits remain visible instead of being blended into a model-created change.

### Generation pruning

Forgetting is modeled as pressure on active Memory rather than deletion from history. `pruner` remains Memory-only and runs after enough Memory-changing commits accumulate since the latest `prune:` checkpoint. Memory-changing commit count is the clock because it tracks actual pressure on the surface being pruned while ignoring vacations, quiet periods, Pursuit-only activity, and correction-only maintenance. A Memory id referenced from Pursuit is still active graph structure, so Pruner preserves it rather than creating a dangling cross-tree edge.

The active files stay free of lifecycle metadata. Prune commits carry the ledger: boundary commit, removed ids, revived ids under grace, and useful skips. A reappeared item receives a small number of grace checkpoints, then returns to ordinary pruning judgment. This keeps recurring memory possible without turning every memory node into a miniature retention object.

Historical retrieval is explicit. Ordinary retrieval stays focused on the current Memory/Pursuit graph and relevant current Agent Corrections, while `historian` searches Memory's `prune:` ledgers and Git snapshots and labels results as historical context. Reactivation goes through unified update so old Memory has to earn its way back into the active surface.

### Change-triggered dreamer

Dreamer watch is driven by accumulated successful Memory work rather than elapsed time. Unified updates add consolidation pressure only when they change Memory; Pursuit-only activity does not make Dreamer responsible for live-state maintenance. The watcher consumes its threshold only after a successful cycle or valid no-op, which keeps Dreamer a Memory consolidation pressure valve rather than a second Pursuit updater.

### Insight logs

Insight logs are a reflective artifact stream inside the RightMemory repo. They are useful when durable Memory activity reveals broader patterns, strategy, risks, recommendations, or next-step ideas that should be preserved without turning them into active Memory facts. Insight remains Memory-oriented, writes timestamped essays under `insight_logs/`, and does not treat Pursuit or updater corrections as its own source material.

### Command-backed roles

RightMemory exposes explicit command roles because operations still have different authority boundaries even though Memory and Pursuit share one graph. `retrieve` is read-only over current Memory, Pursuit, and Agent Corrections. Transcript review extracts candidates from idle sessions and routes them through `update` rather than writing semantic state itself; explicit session review forms tentative proposals before consulting relevant edit feedback. `history`, `dreamer`, `insight`, and `pruner` remain Memory-oriented, while `update` performs unified reconciliation across all three semantic modules, including ordinary candidates that correct an earlier result.

### Command-backed install modes

Both install modes expose the same four skills: read-only `memory-retriever`, approval-gated `rightmemory-orchestrator`, automatic `rightmemory-auto-orchestrator`, and direct `maintain-rightmemory`. The user selects one orchestrator mode at a time; install mode affects only the executor behind command-backed role calls. Standalone mode runs RightMemory's local Pydantic AI agent and bounded tools; CLI-agent mode delegates the same canonical role behavior while preserving RightMemory's prompts, session records, root, and command surface.

Installation has a strict bootstrap boundary. A genuinely new root receives the semantic seed and tracked root `.gitignore` control-plane allowlist, while a complete existing root is preserved byte-for-byte and an incomplete existing root is refused before the installer changes either the root or external runtime and skill targets. Runtime refresh and state migration are separate responsibilities; letting reinstall synthesize missing documents, refresh examples, or rewrite the allowlist would turn a package update into an unreviewed synchronized-state edit.

### CLI-agent conversation lifecycle

Retrieve keeps a provider conversation across independent commands because follow-up questions benefit from conversational continuity and provider prefix caching. Other roles are semantic transactions rather than chats, so resuming them would mix unrelated evidence and maintenance cycles; they use fresh provider conversations instead. Explicit interactive chat may retain context only for that process, which gives the operator continuity without turning a temporary chat into a later command's hidden input.

Provider conversations created by RightMemory are tracked separately from active retrieve mappings. Exact ownership is necessary both to exclude internal work from transcript review and to delete only conversations RightMemory can prove it created. Registered Codex conversations expire after 24 hours of inactivity through Codex's supported deletion interface. Pre-registry history is intentionally left alone because incomplete cleanup is safer than guessing ownership from prompts, paths, or timestamps.

### Executor config

Standalone mode uses role-local model tables because retrieval, unified update, history, Dreamer, Insight, transcript review, pruning, and sync repair may need different providers or model sizes. Pursuit is part of unified update and therefore does not introduce another updater model table. CLI-agent mode keeps the corresponding global provider plus role-local overrides so one RightMemory root can use different executors where the authority genuinely differs.

### Standalone tool boundary

Standalone mode exposes narrow filesystem and Git tools instead of arbitrary Python execution because the agent should behave like a coding assistant while keeping the configured RightMemory root as the ownership boundary. Search, outline, context reads, exact patches, and complete-graph validation reduce line-number guesswork without opening arbitrary shell control or storage-specific CRUD lock-in.

### Standalone commit boundary

Standalone commit tools are role-aware. Unified Update may commit Memory, Pursuit, and Agent Correction files together, and runtime adds an immutable candidate record to that same commit for queued work. Memory-oriented maintenance roles retain their smaller surfaces; Insight commits `insight_logs/*.md`. Empty commits remain reserved for `prune:` checkpoints. Retrieval roles receive no write tools, and unrelated files remain outside model-driven stage and commit allowlists.

### Global RightMemory sync

Global sync remains local-first: every device keeps a complete RightMemory root, and Git provides distributed transport between those roots. Memory, Pursuit, Agent Corrections, `corrections.md`, immutable update records, and the package-owned root `.gitignore` are synchronized state; the allowlist is control plane rather than semantic Memory. Schema and rule references travel with the installed package so runtime behavior and its definitions remain version-coherent. The runtime depends on the ordinary upstream branch contract rather than a hosted-provider API, so a private GitHub repository is convenient but not structurally special.

Runtime code owns deterministic sync mechanics where they belong in the workflow. It fetches and captures exact commits, then merges incoming history in a leased candidate worktree while the active root remains unchanged. Only a complete candidate whose paths and canonical graph validate may be published, and publication is one guarded fast-forward from the captured active commit. A concurrent active change or failed merge, repair, or validation refuses publication instead of requiring rollback.

Retrieve participates without becoming a writer. A five-minute attempt gate and two-second fetch bound let active retrieval admit clean remote state before reading memory without paying normal sync latency on every request. Foreground retrieve sync is pull-only and cannot invoke model repair. An incomplete check falls back to the last valid local state and, only after retrieval completes, launches a detached one-shot full sync cycle. Watcher, foreground, and deferred cycles share a nonblocking lease, while state timestamps use a separate short lock. This makes the persistent watcher an accelerator rather than the only recovery path and keeps synchronized update processing out of retrieve latency.

The synchronized update queue is a protocol surface rather than model-owned
content. Only canonical candidate, recovery, and singleton-lease JSON paths are
admitted, and their complete machine schema validates before incoming state can
publish. Malformed queue data and queue conflicts fail closed; they never enter
`sync-reconciler`. Immutable operation records are validated and synchronized by
the same fail-closed boundary but remain outside the queue lifecycle. A normal candidate remains self-contained semantic evidence
without a commit dependency. Before publishing it, the originating device fully
synchronizes local state; failure leaves the candidate in its local outbox, while
success makes the queue commit descend naturally from its state context. A
claimant fully synchronizes again before leasing work. The existing lease and
fenced Git finalization atomically settle each candidate batch. The six-hour
lease is an availability window measured by device clocks, so its approximate
takeover delay assumes reasonable clock synchronization; fencing correctness
does not.

Graph-aware repair stays in `sync-reconciler` because conflicts across Memory, Pursuit, and their relationships require schema judgment rather than Git mechanics alone, but that role edits and commits only the candidate. Durable prepared-operation records make a completed repair recoverable after a crash without another model turn. For `corrections.md`, repair preserves non-identical entries without ranking them; explicit direct maintenance handles later semantic curation. Push transport remains a retryable follow-up after local publication, and `sync-reconciler` stays separate from Dreamer because repair is a narrow integrity responsibility while Dreamer owns broader Memory consolidation.

### Standalone tool retry behavior

Recoverable tool mistakes, such as stale patch context or invalid read ranges, are returned to the model as retry prompts because the model can usually fix them by searching or re-reading current file context. Hard daemon errors are reserved for problems the model cannot reasonably repair inside the same turn, which keeps caller-visible failures focused on runtime or infrastructure issues.

### Optional MCP adapter

MCP is kept outside the MVP because the primary interface should be plain CLI and JSON-over-stdio for easy use by any agent or shell workflow. A future MCP server can wrap the same daemon protocol without changing the standalone runtime's core behavior.

### File-backed standalone sessions

One-shot standalone calls use an explicit `--session` id and persist native Pydantic AI message history under `.runtime/sessions/` because normal agent callers often start a fresh process per request but still need true multi-turn continuity. RightMemory owns load/save with locking and atomic replacement so callers do not need a background broker, the stored state remains exact model/tool history instead of a lossy chat transcript, and `.runtime/` self-ignores its contents so ephemeral session files do not pollute memory commits.

### Batched command updates

Update submissions accumulate as RightMemory candidate briefs under their original session id. Related natural-boundary submissions form an evolving evidence account, while the session id remains conversation provenance rather than task identity. One global async worker batches whole eligible session queues and invokes the unified updater once for the cross-session batch, letting it reconcile live intent, durable context, and reusable redirection cases together. There is no module-specific queue or cadence. Per-session state still powers cancellation, status, retry, and recent-submitted retrieval.

Normal submitted work batches for token efficiency and coherent state judgment, while failed work uses a separate recovery lane. Once a queue has failed, preserving candidate correctness matters more than waiting for another full batch. Repeated failure stops visibly instead of silently looping forever.

### Command role prompts

Role prompts live as role-specific Markdown files under the runtime package because the command runtime is the source of role behavior in both install modes. Shared representation and semantic judgment remain in the package schema and focused rules, while the Retrieve contract owns its input and terminal-selection protocol. This keeps prompt composition small and role-specific authority reviewable without copying product definitions into every skill or prompt.

### Compatibility posture

RightMemory favors one coherent current model over compatibility scaffolding. When a managed prompt, skill, schema interpretation, test, or code path is superseded, it should be replaced or removed rather than preserved through aliases, dual formats, and migration branches. Current user-authored Memory, Pursuit, and correction content remains protected from accidental overwrite, but obsolete managed behavior does not justify another subsystem.
