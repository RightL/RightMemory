# DESIGN_NOTES

## Project

### Multi-file memory tree

`MEMORY.md` remains normal memory instead of becoming a routing-only index, because the root file should still carry useful high-level graph nodes and readable context. `#`, `##`, and `###` are normal tree layers; `####` is reserved as a title-only external child pointer so deeper detail can move out without pretending that a broad section is a detail file.

### Addressable headings

`#`, `##`, and `###` headings may carry `{#slug}` anchors and graph edges because some relations apply to an entire subtree, not one fact node. Heading slugs and node ids share one namespace so edges can target either form without fake hub nodes or duplicate identifiers.

### Containment is tree structure

Child nodes should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Structural clarity over node count

Curator edits optimize for a readable heading tree and coherent graph nodes instead of minimizing new nodes. Updating an existing node is appropriate when the same fact is being refined, but adding, splitting, merging, or moving headings and nodes is preferred when it prevents overloaded records or makes the memory structure clearer.

### Detail file naming

Detail files use short explicit slugs from `#### Topic {#slug}` and map to `MEMORY_<slug>.md`. This keeps filenames stable and short while preserving the visible Tree+graph model in the Markdown content; filenames are storage details, not graph nodes.

### Schema ownership

Schema rules live in `skills/rightmemory-schema.md` instead of at the top of every `MEMORY.md`, because memory files should stay focused on user memory while prompt/schema changes remain single-source and installable with the skills.

### Curator baseline commits

The curator makes a baseline commit only before its first write when the memory repo is already dirty, because pre-existing memory edits should not be mixed with curator-created routine changes. Routine curator writes remain uncommitted so users can batch or review them, while dreamer remains the commit-oriented consolidation path.

### Standalone role processes

The independent mode uses explicit `curator` and `dreamer` process roles because those skills carry different authority, commit behavior, and safety rules. The caller chooses the role at startup so the runtime never mixes both prompts in one model context; the configured memory root remains the only runtime root because that is the memory store and the intended ownership boundary.

### Standalone install mode

The installer keeps subagent and standalone agent wiring separate because the host agent should see one memory workflow at a time. Subagent mode installs orchestrator, curator, and dreamer skills; standalone mode installs only an orchestrator skill that calls the CLI, leaving curator and dreamer behavior inside the standalone runtime so duplicate skill triggers do not compete with command-based memory access.

### Standalone model config

Standalone mode keeps the existing role-local model config shape while mapping it onto Pydantic AI because curator and dreamer may need different providers, but callers should not have to rewrite `rightmemory.toml` when switching runtime libraries. `anthropic/...` remains the explicit Anthropic selector; other model ids are treated as OpenAI-compatible so local gateways and hosted vLLM endpoints stay simple.

### Standalone tool boundary

Standalone mode exposes narrow filesystem and git tools instead of arbitrary Python execution because the memory agent should behave like a coding assistant while keeping the configured memory root as the only ownership boundary. Search, outline, context reads, Codex-style patches, and validation reduce line-number guesswork without opening the door to arbitrary shell control or memory-specific CRUD lock-in.

### Standalone commit boundary

Standalone commit tools may stage and commit only `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md` because curator and dreamer roles need to preserve memory edits and dream reports without gaining arbitrary repository-write authority. Unrelated untracked files remain visible through status but outside the stage/commit allowlist so model-driven commits do not sweep up local config, backups, or test artifacts.

### Standalone tool retry behavior

Recoverable tool mistakes, such as stale patch context or invalid read ranges, are returned to the model as retry prompts because the model can usually fix them by searching or re-reading current file context. Hard daemon errors are reserved for problems the model cannot reasonably repair inside the same turn, which keeps caller-visible failures focused on runtime or infrastructure issues.

### Optional MCP adapter

MCP is kept outside the MVP because the primary interface should be plain CLI and JSON-over-stdio for easy use by any agent or shell workflow. A future MCP server can wrap the same daemon protocol without changing the standalone runtime's core behavior.

### File-backed standalone sessions

One-shot standalone calls use an explicit `--session` id and persist native Pydantic AI message history under `.runtime/sessions/` because normal agent callers often start a fresh process per request but still need true multi-turn continuity. RightMemory owns load/save with locking and atomic replacement so callers do not need a background broker, the stored state remains exact model/tool history instead of a lossy chat transcript, and `.runtime/` self-ignores its contents so ephemeral session files do not pollute memory commits.

### Queued standalone updates

Standalone async update submissions queue per role/session instead of rejecting while a worker is active because orchestrators may discover multiple durable updates before the previous curator turn completes. The queue is FIFO and the worker drains it through normal session turns so the existing session-history lock remains the ordering authority: retrievals and updates for the same role/session wait on one another, while queue metadata stays cheap to update and visible through `pull`.
