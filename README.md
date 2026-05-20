# RightMemory

A tree + graph hybrid memory system for AI coding agents.

RightMemory keeps durable project and workflow context in Markdown files structured for agent retrieval. The tree gives agents local reading context; the graph gives agents cross-links between related headings and facts. Humans can still inspect and edit the files directly, but the format is primarily an agent memory substrate, not a notes app.

## What It Gives You

- A root memory file, `MEMORY.md`, plus optional sibling detail files named `MEMORY_<slug>.md`.
- A tree of `#`, `##`, and `###` headings for hierarchical retrieval context.
- Addressable heading anchors and node ids for agent retrieval.
- Typed edges such as `dep:`, `cfg:`, `ver:`, `doc:`, and `todo:` for graph traversal across the tree.
- A command-backed `memory-orchestrator` skill for retrieval, updates, and periodic consolidation.
- Two executor modes behind the same `rightmemory` CLI: local standalone runtime or delegated Codex/Claude CLI role execution.
- Optional automatic transcript review for idle Codex and Claude sessions.

## Quick Start

```bash
git clone https://github.com/RightL/RightMemory.git
cd RightMemory
./install.sh
```

The default install uses standalone mode, creates `~/.rightmemory`, installs the `rightmemory` CLI, and installs the command-backed orchestrator skill into both `~/.codex/skills` and `~/.claude/skills`. For a custom memory root or skill target:

```bash
./install.sh ~/.rightmemory ~/.codex/skills
```

For CLI-agent mode, where RightMemory delegates role execution to Codex CLI or Claude Code CLI:

```bash
./install.sh --mode cli-agent ~/.rightmemory ~/.codex/skills
```

After install, open `~/.rightmemory/MEMORY.md` and add your own memory before the managed example block. Real memory can include project context, durable user context, and cross-session agent behavior guidance. Re-run the installer after pulling updates; existing real memory is preserved, and the managed example block refreshes when present.

## Memory Model

Each memory file is ordinary Markdown with a small schema.

```md
# Work Context {#work-context}

## Project Alpha {#project-alpha}

### Runtime {F#alpha-runtime} → [cfg:alpha-config]

Runtime facts that apply to the whole project.

- `alpha-python-env` Uses Python 3.11 in `.venv` for local development. → [cfg:alpha-runtime]
- `alpha-test-command` Run the backend tests with `pytest tests/backend`. → [ver:alpha-python-env]
```

The tree tells agents where to read in local context. Anchors and node ids tell agents what can be referenced. Edges tell agents where to walk across otherwise separate branches.

Common top-level domains include project or work domains, `# User Context`, and `# Cross-Session Agent Behavior`. User context stores durable facts about the user and their direction when those facts help future collaboration. Agent behavior stores guidance about how agents should communicate, choose tools, and avoid repeated mistakes with the user.

### Headings

`#`, `##`, and `###` are normal tree layers and may contain memory content. They can have `{#slug}` anchors and heading-level edges when the whole subtree is useful as a graph target.

Addressable `#`, `##`, and `###` headings may also have body paragraphs directly under the heading. Those paragraphs describe the heading itself. Use a heading body when the text explains the whole concept; use child nodes when the fact should stand on its own.

Use `{F#slug}` instead of `{#slug}` when a heading is backed by a sibling detail file. The file is `MEMORY_<slug>.md`; graph edges still target `slug`, not `F#slug`.

`####` is the deepest heading level allowed in a memory file. A `#### Topic {F#slug}` heading points to `MEMORY_<slug>.md`; it may have body paragraphs that summarize or explain the detail file, but do not put nodes or child headings underneath it in the current file.

### Nodes

Nodes are durable facts under a heading:

```md
- `<node-id>` <description> → [edge1, edge2, ...]
```

Heading ids and node ids share one namespace. A node with no edges still writes `→ []`; a heading with no edges may omit the edge list.

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

RightMemory separates ordinary work from memory ownership. The host agent talks to one installed skill, and that skill calls the `rightmemory` command for role-specific memory work.

```text
main agent
  |
  | dispatches memory requests
  v
memory-orchestrator
  |
  +--> rightmemory retrieve         read-only memory search
  |
  +--> rightmemory update           durable memory edits
  |
  +--> rightmemory dreamer          consolidation, dream logs, commits
  |
  +--> rightmemory review/sync      transcript review and sync repair
```

- `memory-orchestrator` decides when memory is relevant and routes requests.
- Runtime roles own direct `MEMORY*.md` access.
- `retrieve` is read-oriented; write-capable roles perform memory changes through the selected executor. Standalone mode uses RightMemory's bounded tools, while CLI-agent mode delegates to Codex/Claude CLI with role-specific sandbox or permission defaults.

The main agent should avoid reading or editing `MEMORY*.md` directly. Memory access goes through the installed orchestrator and the command roles, which keeps ownership clear and reduces half-edits or competing updates.

## Prompt Sources

RightMemory keeps role behavior in one canonical prompt set under `rightmemory/prompts/`:

```text
rightmemory/prompts/retrieve.md
rightmemory/prompts/update.md
rightmemory/prompts/dreamer.md
rightmemory/prompts/reviewer.md
rightmemory/prompts/sync-reconciler.md
```

Both install modes use these files through the `rightmemory` runtime. Standalone mode loads them into the local Pydantic AI agent and tool loop. CLI-agent mode wraps the same role instructions into prompts sent to Codex CLI or Claude Code CLI. The installed `memory-orchestrator` remains a thin command dispatcher, so role behavior should be edited in the canonical prompt files.

## Install Modes

RightMemory has two command-backed install modes. The default is `standalone`.

| Mode | Use When | What Gets Installed |
| --- | --- | --- |
| `standalone` | You want RightMemory to run its own local Pydantic AI role agents and memory tools. | The command-backed `memory-orchestrator` skill plus the `rightmemory` CLI. |
| `cli-agent` | You want RightMemory to delegate each role turn to Codex CLI or Claude Code CLI. | The same command-backed `memory-orchestrator` skill plus the `rightmemory` CLI. |

The installer arguments are:

```bash
./install.sh [--mode cli-agent|standalone] [<memory-root> <skills-target>]
```

- `<memory-root>` is where `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/` live.
- `<skills-target>` is where your agent loads skills from, such as `~/.claude/skills` or `~/.codex/skills`.
- With no path arguments, the installer uses `~/.rightmemory` and installs the orchestrator skill into both `~/.codex/skills` and `~/.claude/skills`.

Both modes use `uv` to install the runtime under `${XDG_DATA_HOME:-$HOME/.local/share}/rightmemory/venv`. The installer writes a `~/.local/bin/rightmemory` wrapper bound to your chosen memory root. If `~/.local/bin` is not on `PATH`, the installer prints shell-profile guidance after install.

## Everyday Use

1. Edit `MEMORY.md` after install and add your own durable memory before the managed example block, including user context or project context that should guide future sessions.
2. Let the installed orchestrator decide when a user request needs memory retrieval.
3. Let the update role write durable updates after work that should affect future sessions.
4. Ask for a dream cycle when you want cleanup, consolidation, or stale-memory review.

Dream cycles write reports to `dream_logs/YYYY-MM-DD.md` and commit touched memory files. If a consolidation is wrong, use normal git tools in the memory root to inspect or revert it.

## Command Runtime

Both install modes expose the same command surface. The installed `memory-orchestrator` calls `rightmemory retrieve`, `rightmemory update`, and `rightmemory dreamer`; the selected mode determines who executes the role prompt after the command starts.

```bash
rightmemory retrieve --session <agent-session-id> "find memory about the standalone mode"
rightmemory update submit --session <agent-session-id> "remember that MCP should stay optional"
rightmemory update pull --session <agent-session-id>
rightmemory dreamer --session <agent-session-id> "run a dream cycle"
rightmemory watch start
rightmemory watch status
rightmemory watch stop
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

The runtime is intentionally small:

- Standalone mode uses `pydantic_ai.Agent` as a chat-like agent loop.
- CLI-agent mode delegates the same role turn to Codex CLI or Claude Code CLI and records the provider session under `<memory-root>/.runtime/agent_cli_sessions/`.
- In standalone mode, retrieve uses Claude-shaped read-only tools (`read`, `grep`, `glob`) plus a restricted `read_command` for familiar forms such as `cat`, `sed -n`, `rg`, and read-only `git`; update, dreamer, and reviewer also get exact `edit_file` replacements, file lifecycle tools, and narrow git tools.
- `~/.rightmemory` is the default memory root, and all tool paths must stay inside the configured memory root. Set `RIGHTMEMORY_ROOT` to use a different location.
- Retrieve, update, dreamer, and reviewer are separate runtime roles selected by command line or scanner.
- Role-specific executor settings are read from `<memory-root>/rightmemory.toml`.
- Standalone one-shot calls with `--session` persist exact Pydantic AI message history under `<memory-root>/.runtime/sessions/<role>/`; CLI-agent calls persist provider session mappings under `<memory-root>/.runtime/agent_cli_sessions/<role>/`.
- Optional debug tracing appends live JSONL events under `<memory-root>/.runtime/debug/<role>/<session>.jsonl` without changing the canonical session history.
- The installer creates a root `.gitignore` allowlist so git status only surfaces `MEMORY.md`, `MEMORY_*.md`, and `dream_logs/*.md`; existing user `.gitignore` files are preserved.
- Async `update submit` calls for the same `--session` accumulate as pending candidates. The worker waits one hour from the latest submit, then sends the pending candidates to the update role as one batch; `pull` reports phase, pending candidates, current batch, and timing. While submissions are waiting or being processed, retrieve can see newly submitted unconsolidated memory as `Recent submitted memory` so fresh context is available before the updater writes it.
- Standalone daemon context is preserved with Pydantic AI message history.
- MCP support can be added later as an adapter over the same daemon.

### CLI-Agent Config

CLI-agent mode uses a global provider and per-role model settings. A minimal Codex setup looks like:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5"

[update.agent_cli]
model = "gpt-5"

[dreamer.agent_cli]
model = "gpt-5"

[reviewer.agent_cli]
model = "gpt-5"

[sync-reconciler.agent_cli]
model = "gpt-5"
```

A role can override the provider when a different CLI should execute that role:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5"

[dreamer.agent_cli]
provider = "claude"
model = "sonnet"
```

Use `rightmemory doctor agent-cli` after configuring CLI-agent mode. It checks that role config resolves to CLI-agent execution, required provider commands are available, and read/write role probes can complete.

### Standalone Config

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

Standalone configs use `[retrieve.model]`, `[update.model]`, `[dreamer.model]`, and optionally `[reviewer.model]`.

`[sync-reconciler.model]` is needed when the sync watcher should repair scheduled pull conflicts. Without it, the watcher can report a conflict but cannot run the repair role. In CLI-agent mode, use `[sync-reconciler.agent_cli]` instead.

To debug in-flight standalone calls, enable append-only trace logs:

```toml
[debug]
trace = true
```

Trace files include run, history-save, and tool events. They may include prompts, model outputs, and tool results, so leave tracing off unless you need live debugging.

### Automatic Transcript Review

RightMemory can scan idle provider chat sessions and run the `reviewer` role. The normal background controls are:

```bash
rightmemory watch start
rightmemory watch status
rightmemory watch stop
rightmemory watch restart
```

By default these commands manage review and dreamer watchers, plus sync when `[sync].enabled` is true. Pass a target name when you want one role: `rightmemory watch start review`. Managed watcher pid files and logs live under `<memory-root>/.runtime/watch/`.

The lower-level review loop is still available:

```bash
rightmemory review watch
```

It starts immediately and runs one-session scans until no eligible work remains, then sleeps before checking again. A reviewed or failed session triggers another immediate scan, so backlog and recovery attempts are not delayed by the interval. The default interval is two hours; override it with `--interval <seconds>`.

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
since_days = 3

[[review.sources]]
kind = "claude"
path = "~/.claude/projects"

[[review.sources]]
kind = "codex"
path = "~/.codex/sessions"
```

If `[[review.sources]]` is omitted, RightMemory checks the default Codex and Claude locations. By default it considers transcript files modified in the last 3 days. Review state is stored under `<memory-root>/.runtime/review/state.json` and records reviewed provider sessions by source and session id. A session is reviewed as one whole unit; if the same provider session later changes or resumes, scanner state treats it as already reviewed unless you clear the corresponding review state.

### Scheduled Dream Cycles

Dreamer can run periodic consolidation from the same manager:

```bash
rightmemory watch start dreamer
```

The underlying `rightmemory dreamer watch` process runs a dream cycle when no prior scheduled run is recorded, then records its last attempt under `<memory-root>/.runtime/dreamer/watch-state.json`. After that, the default interval is 2 days; override it with `rightmemory dreamer watch --interval <seconds>` when running the lower-level loop directly.

Review and dreamer watchers hold per-role watch locks under `.runtime/watch/`, so a duplicate watcher exits instead of creating a competing background loop. The write phase still uses the shared memory write lock, so review, update, and dreamer roles do not edit memory files at the same time.

`rightmemory watch stop` sends a graceful terminate signal. A sleeping watcher exits within a few seconds; a watcher doing model work finishes the current cycle first. When `install.sh` finishes, it updates `<memory-root>/.runtime/install.stamp`. Watchers check that stamp between runs and while sleeping; if it changes, they re-exec themselves with the same arguments.

### Automatic Global Sync

RightMemory can keep one memory root shared across devices by using a normal private Git remote. GitHub private repositories are the easiest hosted setup, and any SSH or HTTPS Git remote works once the memory repo has an upstream branch.

Enable sync in `<memory-root>/rightmemory.toml`:

```toml
[sync]
enabled = true
stale_pull_after_hours = 24
```

When sync is enabled, runtime code runs deterministic Git preflight for `update`, `reviewer`, and `dreamer` before model work. Clean sync state stays invisible to the role; dirty or conflicted memory state is routed to `sync-reconciler` for repair. After a semantic role commits durable memory changes, the runtime pushes the committed state and invokes `sync-reconciler` if that push exposes dirty state or a conflict. Retrieval stays local by default for speed.

Managed watch includes a `sync` target. `rightmemory watch start` starts it when sync is enabled, and `rightmemory watch start sync` runs that target by itself. The sync watcher pulls when no successful pull is recorded or when the last successful pull is older than `stale_pull_after_hours`; clean pulls and fresh checks stay deterministic and do not call a model.

If a scheduled pull finds dirty memory files or creates a memory conflict, RightMemory invokes `sync-reconciler` with the affected files and repair context. The reconciler repairs the memory state from that bounded context, validates it, commits the result, and calls `sync_push`.

Run standalone mode from this repository during development:

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
    ├── memory-orchestrator-cli/SKILL.md
    └── provider-transcript-normalizer/SKILL.md
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

## Design Notes

- The tree gives agents hierarchical context; the graph gives agents cross-branch traversal.
- Human readability is useful, but agent retrieval is the primary design center.
- `MEMORY.md` remains real memory, not a routing-only index.
- Dedicated memory roles own memory edits so the main agent does not race itself or leave partial writes.
- Dreaming is explicit because structural cleanup is opinionated.
- Git provides history and revertability without adding inline timestamps to every node.

## License

Copyright 2026 RightL.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
