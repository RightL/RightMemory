# RightMemory

A tree + graph hybrid memory system for AI coding agents.

Memory starts in a root markdown file (`MEMORY.md`) and may expand into sibling detail files (`MEMORY_<slug>.md`). Each memory file is organized as:

- A **tree** of headings (`#` → `##` → `###`) for human-readable navigation by domain, project, theme.
- Optional `{#slug}` anchors on `#`, `##`, and `###` headings when a whole subtree should be a graph target.
- Title-only `#### Topic {#slug}` pointers for deeper children stored in `MEMORY_<slug>.md`.
- A **graph** of typed edges between anchored headings and nodes (`dep:`, `emb:`, `agg:`, `ver:`, `ext:`, `rel:`, ...) for agents traversing across siblings, parents, and unrelated branches.

Addressable headings and nodes have these shapes:

```
### Human Title {#heading-id} → [edge1, edge2, ...]
- `<node-id>` <description> → [edge1, edge2, ...]
```

The headings tell you where to **read**; `####` pointers tell you where deeper detail lives; the edges tell you where to **walk**. Tree nesting already expresses containment, so child nodes should not point back to their containing heading just to say they belong there.

## Why

Agents that share long-running context with a user accumulate facts about projects, libraries, paths, decisions, and references. Plain markdown notes get long and lossy; pure knowledge graphs are unreadable to humans. This system is a deliberate compromise — humans see a tidy outline, agents follow typed edges, and the same tracked file set serves both.

## Architecture

Three skills, three roles. The main agent never touches `MEMORY*.md` directly — every read and write goes through a subagent.

```
            ┌────────────────────────┐
            │       main agent       │
            │  (memory-orchestrator) │
            └──────────┬─────────────┘
                       │ dispatch
       ┌───────────────┴────────────────┐
       │                                │
┌──────▼──────────┐             ┌───────▼─────────┐
│ memory-curator  │             │ memory-dreamer  │
│ (long-lived,    │             │ (per-cycle,     │
│  one per session)│            │  user-triggered)│
│                 │             │                 │
│ • read & extract│             │ • consolidate   │
│ • schema-correct│             │ • surface       │
│   edits         │             │   conflicts     │
│                 │             │ • git commit    │
└──────┬──────────┘             └───────┬─────────┘
       │                                │
       └──────────────┬─────────────────┘
                      ▼
                MEMORY*.md
          (tracked memory file set)
```

- **memory-orchestrator** — runs in the main agent. Decides whether each user message should trigger a retrieval, dispatches to the curator, never touches `MEMORY*.md`.
- **memory-curator** — long-lived subagent that owns reads and incremental edits. Spawned exactly once per session and reused for every retrieval / update in that session.
- **memory-dreamer** — separate subagent invoked manually when the user wants consolidation. Performs tree/edge cleanup, surfaces contradictions, ages stale nodes, and commits changes via git.

The orchestrator and curator together implement the everyday loop. The dreamer is the periodic "sleep cycle" — explicitly user-triggered, not automatic, because it makes opinionated structural changes and you want to know when they happen.

## Install

```bash
git clone https://github.com/RightL/RightMemory.git
cd RightMemory
./install.sh [--mode subagent|standalone] <memory-root> <skills-target>
```

- `<memory-root>` — where `MEMORY.md`, optional `MEMORY_*.md` files, and `dream_logs/` will live (e.g. `~/.rightmemory`).
- `<skills-target>` — where RightMemory skill folders are installed. Common locations:
  - Claude Code (user): `~/.claude/skills`
  - Claude Code (project): `<project>/.claude/skills`
  - Codex: `~/.codex/skills`
  - Other agents: see your agent's skill loading docs.
- `--mode subagent` — default. Installs `memory-orchestrator`, `memory-curator`, and `memory-dreamer` skills for agents with subagent support.
- `--mode standalone` — installs a `memory-orchestrator` skill that calls the standalone `rightmemory` CLI instead of spawning curator or dreamer subagents. Use `--rightmemory-cmd <command>` when the executable is not simply `rightmemory`.

The script:

1. Creates `<memory-root>/MEMORY.md` from `MEMORY.example.md` (skipped if a `MEMORY.md` already exists).
2. Creates `<memory-root>/dream_logs/`.
3. Initializes a git repo in `<memory-root>` (the dreamer needs git for revertability).
4. Installs the shared schema file to `<skills-target>/rightmemory-schema.md`.
5. Substitutes template placeholders and writes mode-appropriate skills to `<skills-target>`.

Re-run the script any time you want to refresh the skills (e.g. after pulling updates from this repo). Your existing `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/` are preserved.

> **Why the install script?** The skill files reference absolute paths and commands. Hand-editing those after copy-paste is error-prone, so we ship the skills as templates with placeholders and the script substitutes them at install time.

## Usage

After install:

1. **Edit your memory file.** Open `<memory-root>/MEMORY.md`. The example domain (`# Sample Project Graph`) shows the format. Replace it with your own real domains.
2. **The schema file is the spec.** The authoritative source of truth is `<skills-target>/rightmemory-schema.md`, installed from `skills/rightmemory-schema.md`. `MEMORY.md` should contain memory content only, not a schema preamble.
3. **Use heading anchors for subtree-level graph targets.** `#`, `##`, and `###` headings may have `{#slug}` anchors and `→ [...]` edges. Heading slugs and node ids share one namespace.
4. **Do not use edges for containment.** If a node is under `### Web Server Config {#web-server-cfg}`, it already belongs to that topic. Add edges only for cross-links, dependencies, verification, documents, backups, inputs/outputs, or other relations not implied by the tree.
5. **Use `####` only for external child pointers.** Write normal memory under `#`, `##`, and `###`. Use `#### Topic {#slug}` only below a `###` topic when detail should move into `<memory-root>/MEMORY_<slug>.md`; do not write body content under the `####` heading.
6. **Daily loop runs automatically.** In agents that load skills by description match, the orchestrator skill triggers per user message; the curator handles retrievals and edits in the background.
7. **Trigger a dream cycle when you want consolidation.** Ask your agent to invoke the `memory-dreamer` skill. Each cycle:
   - Applies unbounded mechanical fixes (dead pointers, duplicate edges, self-edges, containment-only edges, obvious edge-type upgrades).
   - Applies judgment-driven restructures (merges, splits, `####` detail-file promotions, graveyard moves) when they make the tree and graph clearer.
   - Writes a dream report to `<memory-root>/dream_logs/YYYY-MM-DD.md`.
   - Commits touched `MEMORY*.md` files and the report. Bad dream? `git revert` in `<memory-root>`.

## Standalone mode

RightMemory also ships an independent chat runtime that does not depend on Codex or Claude Code subagents:

```bash
./install.sh --mode standalone --rightmemory-cmd rightmemory ~/.rightmemory ~/.codex/skills
```

Standalone install mode writes a `memory-orchestrator` skill that calls `rightmemory curator` and `rightmemory dreamer` with `RIGHTMEMORY_ROOT=<memory-root>` instead of spawning subagents. If you previously installed subagent mode into the same skill target, disable or remove the old `memory-curator` and `memory-dreamer` folders so your host agent does not load both workflows.

```bash
rightmemory curator --session <agent-session-id> "find memory about the standalone mode"
rightmemory dreamer --session <agent-session-id> "run a dream cycle"
rightmemory curator chat
rightmemory dreamer chat
```

For machine callers:

```bash
rightmemory curator daemon --stdio-json
rightmemory dreamer daemon --stdio-json
```

The daemon reads JSON lines from stdin and writes JSON lines to stdout:

```json
{"message":"find memory about the standalone mode"}
{"message":"remember that MCP should stay optional"}
```

Standalone mode is intentionally small:

- It uses `pydantic_ai.Agent` as a chat-like agent loop.
- The model works through sandboxed tools for file listing, search, outline, context reads, Codex-style patches, git inspection, and memory validation.
- `~/.rightmemory` is the default memory root, and all tool paths must stay inside the configured memory root. Set `RIGHTMEMORY_ROOT` to use a different location.
- Curator and dreamer are separate runtime roles selected by the command line.
- Role-specific model settings are read from `<memory-root>/rightmemory.toml`.
- One-shot calls with `--session` persist exact Pydantic AI message history under `<memory-root>/.runtime/sessions/<role>/`, so normal agent callers can make separate process calls without losing multi-turn context; `.runtime/` is self-ignored so session state does not dirty memory commits.
- Multi-turn daemon context is preserved with Pydantic AI message history.
- MCP support is not part of the MVP; it can be added later as an adapter over the same daemon.

OpenAI-compatible curator config:

```toml
[curator.model]
model_id = "hosted_vllm//models/example-chat-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "<token>"

[curator.model.kwargs]
extra_body = { chat_template_kwargs = { thinking = true, preserve_thinking = true } }
```

Anthropic-compatible dreamer config:

```toml
[dreamer.model]
model_id = "anthropic/example-dreamer-model"
api_base = "https://api.example.com/anthropic"
api_key = "<token>"
```

`model_id` is required for the role being started. `anthropic/...` model ids use `AnthropicModel`; other model ids use `OpenAIChatModel` with `OpenAIProvider`, so OpenAI-compatible local gateways can keep using `api_base` and `api_key`. `[<role>.model.kwargs]` is forwarded as Pydantic AI model settings; unsupported keys fail fast instead of being silently ignored. RightMemory does not inject environment variables from config and does not set generation parameters such as `temperature` or `max_tokens` by default.

Run it from this repository during development:

```bash
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install -e . --python .venv/bin/python
rightmemory curator chat
```

The MVP keeps the boundary simple: it exposes only sandboxed tools rooted at the configured memory root and instructs the agent not to access paths outside that directory. It does not yet provide an OS-level jail.

## Customization

- **Edge types.** Add new types to `skills/rightmemory-schema.md`. Keep the semantics concrete because the curator and dreamer choose edge types from that schema.
- **Domains and headings.** Add new top-level `# domain` sections in `MEMORY.md`. Add `{#short-slug}` to `#`, `##`, or `###` headings when another edge should point to the whole subtree, or when the heading itself needs outgoing edges.
- **Detail files.** Add `#### Topic {#short-slug}` under a `###` topic when detail grows too large for the current file. The pointed file is `<memory-root>/MEMORY_<short-slug>.md`.
- **Aging.** There are no inline timestamps; the dreamer reads git history to identify long-untouched nodes. If you want different aging behavior (per-class TTLs, last-verified tags, archival policies), edit the `memory-dreamer` skill — the simple history-based scheme is intended as a starting point you iterate on.
- **What the orchestrator considers "memory-worthy".** Edit the trigger bullet in `memory-orchestrator/SKILL.md` to suit your workflow.

## File layout

This repo:

```
RightMemory/
├── README.md
├── install.sh
├── MEMORY.example.md
├── rightmemory/
└── skills/
    ├── rightmemory-schema.md
    ├── memory-orchestrator/SKILL.md
    ├── memory-orchestrator-standalone/SKILL.md
    ├── memory-curator/SKILL.md
    └── memory-dreamer/SKILL.md
```

After install (with `MEMORY_ROOT=~/.rightmemory`, `SKILLS_TARGET=~/.claude/skills`):

```
~/.rightmemory/
├── .git/
├── MEMORY.md
├── MEMORY_<slug>.md
└── dream_logs/

~/.claude/skills/
├── rightmemory-schema.md
├── memory-orchestrator/SKILL.md
├── memory-curator/SKILL.md
└── memory-dreamer/SKILL.md
```

## Design notes

- **Why three skills, not one?** Separation of trust. The orchestrator decides *whether* memory matters; the curator owns the *file*; the dreamer owns *consolidation*. Mixing roles makes each role harder to reason about and tune.
- **Why the main agent can't touch memory files.** Single owner = no concurrent writes, no half-edits, easy to reason about state. The curator caches memory files across the session; the orchestrator just sends queries and updates.
- **Why no automatic dreaming.** Deep restructures are opinionated. Auto-rewriting your mental index without consent is hostile. Manual trigger + git-revert safety net = trust.
- **Why git instead of inline timestamps.** Git already records when each line was last changed. Adding `[last:YYYY-MM-DD]` tags to every node duplicates that information and creates a maintenance burden. `git log -L` answers "when was this node last touched?" exactly.

## License

MIT
