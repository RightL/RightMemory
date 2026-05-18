# DESIGN_NOTES

## Project

### Multi-file memory tree

`MEMORY.md` remains normal memory instead of becoming a routing-only index, because the root file should still carry useful high-level graph nodes and readable context. `#`, `##`, and `###` are normal tree layers; `####` is reserved for file-backed detail pointers under a `###` topic, so deeper detail can move out without pretending that a broad section is a detail file. A `####` pointer may keep short body text that summarizes or explains the detail file, but nodes and child headings belong in the detail file.

### Addressable headings

`#`, `##`, and `###` headings may carry `{#slug}` anchors and graph edges because some relations apply to an entire subtree, not one fact node. Heading slugs and node ids share one namespace so edges can target either form without fake hub nodes or duplicate identifiers.

### Containment is tree structure

Child nodes should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Structural clarity over node count

Curator edits optimize for a readable heading tree and coherent graph nodes instead of minimizing new nodes. Updating an existing node is appropriate when the same fact is being refined, but adding, splitting, merging, or moving headings and nodes is preferred when it prevents overloaded records or makes the memory structure clearer.

### Detail file naming

Detail files use short explicit slugs from file-backed headings such as `#### Topic {F#slug}` and map to `MEMORY_<slug>.md`. Graph edges still target `slug`, not `F#slug`. This keeps filenames stable and short while preserving the visible tree + graph model in the Markdown content; filenames are storage details, not graph nodes.

### Schema ownership

Schema rules live in `skills/rightmemory-schema.md` instead of at the top of every `MEMORY.md`, because memory files should stay focused on user memory while prompt/schema changes remain single-source and installable with the skills.

### Curator baseline commits

The curator makes a baseline commit only before its first write when the memory repo is already dirty, because pre-existing memory edits should not be mixed with curator-created routine changes. Routine curator writes remain uncommitted so users can batch or review them, while dreamer remains the commit-oriented consolidation path.

### Standalone command roles

The independent mode uses explicit `retrieve`, `update`, and `dreamer` command roles because retrieval should be fast and read-only, updates should be more careful and write-capable, and dreamer remains a separate consolidation authority. The caller chooses the role at startup so the runtime can load the right prompt, tools, session history, and model config without asking one model context to infer behavior from `[RETRIEVE]` or `[UPDATE]` tags; the configured memory root remains the only runtime root because that is the memory store and the intended ownership boundary.

### Standalone install mode

The installer keeps subagent and standalone agent wiring separate because the host agent should see one memory workflow at a time. Subagent mode installs orchestrator, curator, and dreamer skills; standalone mode installs only an orchestrator skill that calls the CLI, leaving retrieve, update, and dreamer behavior inside the standalone runtime so duplicate skill triggers do not compete with command-based memory access.

### Standalone model config

Standalone mode uses explicit `[retrieve.model]`, `[update.model]`, and `[dreamer.model]` tables because retrieve, update, and dreamer may need different providers or model sizes. Old `[curator.model]` config is rejected instead of silently mapped because the split changed authority boundaries as well as names, and requiring migration makes stale read-write configuration visible. `anthropic/...` remains the explicit Anthropic selector; other model ids are treated as OpenAI-compatible so local gateways and hosted vLLM endpoints stay simple.

### Standalone tool boundary

Standalone mode exposes narrow filesystem and git tools instead of arbitrary Python execution because the memory agent should behave like a coding assistant while keeping the configured memory root as the only ownership boundary. Search, outline, context reads, Codex-style patches, and validation reduce line-number guesswork without opening the door to arbitrary shell control or memory-specific CRUD lock-in.

### Standalone commit boundary

Standalone commit tools are scoped to `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md` because update and dreamer roles need to preserve memory edits and dream reports without gaining arbitrary repository-write authority. The retrieve role does not receive write or git tools at all, so retrieval remains a lower-authority fast path. Unrelated untracked files remain visible through status but outside the stage/commit allowlist so model-driven commits do not sweep up local config, backups, or test artifacts.

### Global memory sync

Global memory sync remains local-first: every device keeps a complete memory root, and Git provides distributed transport between those roots. The runtime depends on the ordinary upstream branch contract rather than a hosted-provider API, so a private GitHub repository is convenient but not structurally special.

Runtime code owns deterministic sync mechanics at the point where each one belongs in the workflow. For `update`, `reviewer`, and `dreamer`, preflight fetches, merges available upstream changes, checks freshness, and routes dirty or conflicted memory state to `sync-reconciler` before semantic model work. After those roles commit memory changes, runtime push handling publishes the committed state and routes dirty or conflicted push results to `sync-reconciler`. Retrieval keeps the fast local path by default.

Memory-aware sync repair stays in `sync-reconciler` because Markdown memory conflicts and dirty memory state require durability and schema judgment, not just Git mechanics. Runtime and scheduled sync flows call that role with bounded repair context, and the role validates the file set, commits the repaired state, and calls `sync_push`. `sync-reconciler` stays separate from dreamer because scheduled sync repair is a narrow maintenance responsibility, while dreamer owns broader consolidation and restructuring.

### Standalone tool retry behavior

Recoverable tool mistakes, such as stale patch context or invalid read ranges, are returned to the model as retry prompts because the model can usually fix them by searching or re-reading current file context. Hard daemon errors are reserved for problems the model cannot reasonably repair inside the same turn, which keeps caller-visible failures focused on runtime or infrastructure issues.

### Optional MCP adapter

MCP is kept outside the MVP because the primary interface should be plain CLI and JSON-over-stdio for easy use by any agent or shell workflow. A future MCP server can wrap the same daemon protocol without changing the standalone runtime's core behavior.

### File-backed standalone sessions

One-shot standalone calls use an explicit `--session` id and persist native Pydantic AI message history under `.runtime/sessions/` because normal agent callers often start a fresh process per request but still need true multi-turn continuity. RightMemory owns load/save with locking and atomic replacement so callers do not need a background broker, the stored state remains exact model/tool history instead of a lossy chat transcript, and `.runtime/` self-ignores its contents so ephemeral session files do not pollute memory commits.

### Batched standalone updates

Standalone update submissions accumulate as candidate briefs for one hour from the latest submit before the update role is invoked, because memory quality is better when small corrections and follow-up clarifications are reconciled together instead of written as separate turn-by-turn facts. The worker remains automatic so callers do not need an explicit flush step, but update state distinguishes pending candidates from the current batch so status output can explain whether the worker is waiting or actively editing. Async state files keep their own `session_id` and `role` fields instead of inferring them from the read path because submitted candidates are operational state and malformed state should fail visibly.

### Standalone role prompts

Standalone retrieve, update, and dreamer prompts live as role-specific Markdown files under the runtime package instead of being derived from subagent skills in Python, because retrieve and update now have different operating contracts and should not inherit mixed curator behavior through string replacements. `prompt.py` stays a small composer for schema, workspace, tool, and role prompt fragments, while update-specific candidate triage stays in the update role prompt where prompt policy is easier to review and revise.
