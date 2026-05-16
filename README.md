# RightMemory

A tree + graph hybrid memory system for AI coding agents.

RightMemory keeps durable project and workflow context in Markdown files structured for agent retrieval. The tree gives agents local reading context; the graph gives agents cross-links between related headings and facts. Humans can still inspect and edit the files directly, but the format is primarily an agent memory substrate, not a notes app.

## What It Gives You

- A root memory file, `MEMORY.md`, plus optional sibling detail files named `MEMORY_<slug>.md`.
- A tree of `#`, `##`, and `###` headings for hierarchical retrieval context.
- Addressable heading anchors and node ids for agent retrieval.
- Typed edges such as `dep:`, `cfg:`, `ver:`, `doc:`, and `todo:` for graph traversal across the tree.
- Agent skills for retrieval, updates, and periodic consolidation.
- A standalone CLI runtime for agents that cannot spawn subagents.
- Optional automatic transcript review for idle Codex and Claude sessions.

## Quick Start

```bash
git clone https://github.com/RightL/RightMemory.git
cd RightMemory
./install.sh ~/.rightmemory ~/.claude/skills
```

For Codex standalone mode:

```bash
./install.sh --mode standalone ~/.rightmemory ~/.codex/skills
```

After install, open `~/.rightmemory/MEMORY.md` and add your own memory before the managed example block. Re-run the installer after pulling updates; existing real memory is preserved, and the managed example block refreshes when present.

## Memory Model

Each memory file is ordinary Markdown with a small schema.

```md
# Work Context {#work-context}

## Project Alpha {#project-alpha}

### Runtime {F#alpha-runtime} -> [cfg:alpha-config]

Runtime facts that apply to the whole project.

- `alpha-python-env` Uses Python 3.11 in `.venv` for local development. -> [cfg:alpha-runtime]
- `alpha-test-command` Run the backend tests with `pytest tests/backend`. -> [ver:alpha-python-env]
```

The tree tells agents where to read in local context. Anchors and node ids tell agents what can be referenced. Edges tell agents where to walk across otherwise separate branches.

### Headings

`#`, `##`, and `###` are normal tree layers and may contain memory content. They can have `{#slug}` anchors and heading-level edges when the whole subtree is useful as a graph target.

Addressable `#`, `##`, and `###` headings may also have body paragraphs directly under the heading. Those paragraphs describe the heading itself. Use a heading body when the text explains the whole concept; use child nodes when the fact should stand on its own.

Use `{F#slug}` instead of `{#slug}` when a heading is backed by a sibling detail file. The file is `MEMORY_<slug>.md`; graph edges still target `slug`, not `F#slug`.

`####` is the deepest heading level allowed in a memory file. A `#### Topic {F#slug}` heading points to `MEMORY_<slug>.md`; do not put body paragraphs, nodes, or child headings underneath it in the current file.

### Nodes

Nodes are durable facts under a heading:

```md
- `<node-id>` <description> -> [edge1, edge2, ...]
```

Heading ids and node ids share one namespace. A node with no edges still writes `-> []`; a heading with no edges may omit the edge list.

### Detail Files

Any `#`, `##`, or `###` heading can use its slug as a detail-file target by writing `{F#slug}`. For example, `{F#alpha-runtime}` maps to `MEMORY_alpha-runtime.md`.

Move child content into a detail file when a heading becomes too dense, especially past about 15 direct node lines. Count only direct node lines, not child headings or `####` pointers. After moving content out, keep only the `F#` heading line and any heading body paragraphs in the parent file.

### Edges

Use the most specific edge type that fits:

- `dep:` A depends on B.
- `emb:` A embeds a copy of B.
- `bak:` A is a backup or snapshot of B.
- `agg:` A aggregates B's outputs.
- `ver:` A verifies or tests B.
- `ext:` A extends or enhances B.
- `up:` A is upstream of B.
- `loc:` A is located inside B.
- `run:` A runs or launches through B.
- `cfg:` A uses B as configuration.
- `out:` A outputs B.
- `in:` A consumes B as input.
- `doc:` A documents B.
- `todo:` A is a todo or blocker for B.
- `rel:` A has a general relation to B; use only when no specific type fits.

Tree nesting already expresses containment. Do not add edges from a child node to its containing heading just to say where it belongs.

## Agent Roles

RightMemory separates ordinary work from memory ownership.

```text
main agent
  |
  | dispatches memory requests
  v
memory-orchestrator
  |
  +--> memory-curator   reads, retrieves, and edits MEMORY*.md
  |
  +--> memory-dreamer   consolidates, restructures, writes dream logs, commits
```

- `memory-orchestrator` decides when memory is relevant and routes requests.
- `memory-curator` owns retrieval and incremental edits.
- `memory-dreamer` runs explicit consolidation cycles and commits the result.

The main agent should not read or edit `MEMORY*.md` directly. This keeps one clear owner for memory writes and prevents half-edits or competing updates.

## Prompt Sources

RightMemory keeps role behavior in one canonical prompt set under `rightmemory/prompts/`:

```text
rightmemory/prompts/retrieve.md
rightmemory/prompts/update.md
rightmemory/prompts/dreamer.md
rightmemory/prompts/reviewer.md
```

Standalone mode reads these files at runtime. Subagent mode installs thin skill wrappers and renders the same role prompts into those wrappers during `install.sh`. Runtime-specific wrappers define access boundaries and dispatch style; role behavior should be edited in the canonical prompt files.

## Install Modes

RightMemory has two install modes.

| Mode | Use When | What Gets Installed |
| --- | --- | --- |
| `subagent` | Your agent can spawn subagents, such as Claude Code-style workflows. | `memory-orchestrator`, generated `memory-curator`, and generated `memory-dreamer` skills. |
| `standalone` | Your agent needs a command-line runtime instead of subagents. | A `memory-orchestrator` skill plus the `rightmemory` CLI. |

The installer arguments are:

```bash
./install.sh [--mode subagent|standalone] <memory-root> <skills-target>
```

- `<memory-root>` is where `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/` live.
- `<skills-target>` is where your agent loads skills from, such as `~/.claude/skills` or `~/.codex/skills`.

Standalone mode requires `uv` and installs the runtime under `${XDG_DATA_HOME:-$HOME/.local/share}/rightmemory/venv`. It also writes a `~/.local/bin/rightmemory` wrapper bound to your chosen memory root.

## Everyday Use

1. Edit `MEMORY.md` after install and add your own memory before the managed example block.
2. Let the installed orchestrator decide when a user request needs memory retrieval.
3. Let the curator or update role write durable updates after work that should affect future sessions.
4. Ask for a dream cycle when you want cleanup, consolidation, or stale-memory review.

Dream cycles write reports to `dream_logs/YYYY-MM-DD.md` and commit touched memory files. If a consolidation is wrong, use normal git tools in the memory root to inspect or revert it.

## Standalone Runtime

Standalone install mode uses `uv` to install the runtime into a user-local venv, writes a `memory-orchestrator` skill that calls `rightmemory retrieve`, `rightmemory update`, and `rightmemory dreamer` instead of spawning subagents, and removes old `memory-curator` / `memory-dreamer` skill folders from the same skill target. The installed `rightmemory` wrapper is bound to `<memory-root>`.

```bash
rightmemory retrieve --session <agent-session-id> "find memory about the standalone mode"
rightmemory update submit --session <agent-session-id> "remember that MCP should stay optional"
rightmemory update pull --session <agent-session-id>
rightmemory dreamer --session <agent-session-id> "run a dream cycle"
rightmemory retrieve chat
rightmemory update chat
rightmemory dreamer chat
```

For machine callers:

```bash
rightmemory retrieve daemon --stdio-json
rightmemory update daemon --stdio-json
rightmemory dreamer daemon --stdio-json
```

The daemon reads JSON lines from stdin and writes JSON lines to stdout:

```json
{"message":"find memory about the standalone mode"}
{"message":"remember that MCP should stay optional"}
```

Standalone mode is intentionally small:

- It uses `pydantic_ai.Agent` as a chat-like agent loop.
- Retrieve uses Claude-shaped read-only tools (`read`, `grep`, `glob`) plus a restricted `read_command` for familiar forms such as `cat`, `sed -n`, `rg`, and read-only `git`; update, dreamer, and reviewer also get exact `edit_file` replacements, file lifecycle tools, and narrow git tools.
- `~/.rightmemory` is the default memory root, and all tool paths must stay inside the configured memory root. Set `RIGHTMEMORY_ROOT` to use a different location.
- Retrieve, update, dreamer, and reviewer are separate runtime roles selected by command line or scanner.
- Role-specific model settings are read from `<memory-root>/rightmemory.toml`.
- One-shot calls with `--session` persist exact Pydantic AI message history under `<memory-root>/.runtime/sessions/<role>/`, so normal agent callers can make separate process calls without losing multi-turn context; `.runtime/` is self-ignored so session state does not dirty memory commits.
- Optional debug tracing appends live JSONL events under `<memory-root>/.runtime/debug/<role>/<session>.jsonl` without changing the canonical session history.
- The installer creates a root `.gitignore` allowlist so git status only surfaces `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; existing user `.gitignore` files are preserved.
- Async `update submit` calls for the same `--session` accumulate as pending candidates. The worker waits one hour from the latest submit, then sends the pending candidates to the update role as one batch; `pull` reports phase, pending candidates, current batch, and timing.
- Multi-turn daemon context is preserved with Pydantic AI message history.
- MCP support is not part of the MVP; it can be added later as an adapter over the same daemon.

OpenAI-compatible retrieve/update config:

```toml
[retrieve.model]
model_id = "hosted_vllm//models/example-fast-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "<token>"

[update.model]
model_id = "hosted_vllm//models/example-accurate-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "<token>"

[update.model.kwargs]
extra_body = { chat_template_kwargs = { thinking = true, preserve_thinking = true } }
```

Anthropic-compatible dreamer/reviewer config:

```toml
[dreamer.model]
model_id = "anthropic/example-dreamer-model"
api_base = "https://api.example.com/anthropic"
api_key = "<token>"

[reviewer.model]
model_id = "anthropic/example-reviewer-model"
api_base = "https://api.example.com/anthropic"
api_key = "<token>"
```

`model_id` is required for the role being started. `anthropic/...` model ids use `AnthropicModel`; other model ids use `OpenAIChatModel` with `OpenAIProvider`, so OpenAI-compatible local gateways can use `api_base` and `api_key`. `[<role>.model.kwargs]` is forwarded as Pydantic AI model settings and unsupported keys fail fast.

Configs must use `[retrieve.model]`, `[update.model]`, `[dreamer.model]`, and optionally `[reviewer.model]`; old `[curator.model]` configs are rejected so stale read-write settings are migrated deliberately.

To debug in-flight standalone calls, enable append-only trace logs:

```toml
[debug]
trace = true
```

Trace files include run, history-save, and tool events. They may include prompts, model outputs, and tool results, so leave tracing off unless you need live debugging.

### Automatic Transcript Review

RightMemory can scan idle provider chat sessions and run the standalone `reviewer` role. For a long-running local process, use `watch`:

```bash
rightmemory review watch
```

`watch` starts immediately and runs one-session scans until no eligible work remains, then sleeps before checking again. A reviewed or failed session triggers another immediate scan, so backlog and recovery attempts are not delayed by the interval. The default interval is two hours; override it with `--interval <seconds>`.

For cron, launchd, or other supervisors, call one bounded scan at a time:

```bash
rightmemory review scan --once
```

Each `scan --once` command reviews at most one eligible session and then exits.

For debugging an adapter without calling a model:

```bash
rightmemory review normalize --source claude --path ~/.claude/projects/<project>/<session>.jsonl
```

Add source presets to `<memory-root>/rightmemory.toml`:

```toml
[review]
idle_seconds = 3600
since_days = 30

[[review.sources]]
kind = "claude"
path = "~/.claude/projects"

[[review.sources]]
kind = "codex"
path = "~/.codex/sessions"
```

If `[[review.sources]]` is omitted, RightMemory checks the default Codex and Claude locations. By default it only considers transcript files modified in the last 30 days. Review state is stored under `<memory-root>/.runtime/review/state.json` and records the last reviewed turn count and a hash of the reviewed prefix for each transcript. When a session resumes later, the reviewer receives the whole normalized session for context but extracts only from the new suffix. If a transcript prefix changes, RightMemory resets that session cursor and reviews it from the beginning.

Run it from this repository during development:

```bash
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install -e . --python .venv/bin/python
rightmemory retrieve chat
```

The standalone runtime exposes sandboxed tools rooted at the configured memory root. It does not provide an OS-level jail.

## File Layout

Repository:

```text
RightMemory/
├── README.md
├── install.sh
├── MEMORY.example.md
├── rightmemory/
│   └── prompts/
└── skills/
    ├── rightmemory-schema.md
    ├── memory-orchestrator/SKILL.md
    ├── memory-orchestrator-standalone/SKILL.md
    ├── memory-curator/SKILL.md
    └── memory-dreamer/SKILL.md
```

After install:

```text
~/.rightmemory/
├── .git/
├── MEMORY.md
├── MEMORY_<slug>.md
└── dream_logs/

~/.codex/skills/
├── rightmemory-schema.md
└── memory-orchestrator/SKILL.md
```

Subagent installs also include `memory-curator/` and `memory-dreamer/` in the skills target.

## Design Notes

- The tree gives agents hierarchical context; the graph gives agents cross-branch traversal.
- Human readability is useful, but agent retrieval is the primary design center.
- `MEMORY.md` remains real memory, not a routing-only index.
- The curator owns memory edits so the main agent does not race itself or leave partial writes.
- Dreaming is explicit because structural cleanup is opinionated.
- Git provides history and revertability without adding inline timestamps to every node.

## License

Copyright 2026 RightL.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
