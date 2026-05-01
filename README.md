# rightmem

A tree + graph hybrid memory system for AI coding agents.

Memory lives in a single markdown file (`MEMORY.md`) organized as:

- A **tree** of headings (`#` → `##` → `###`) for human-readable navigation by domain, project, theme.
- A **graph** of typed edges between nodes (`dep:`, `emb:`, `agg:`, `ver:`, `ext:`, `rel:`, ...) for agents traversing across siblings, parents, and unrelated branches.

Every node has the shape:

```
- `<node-id>` <description> → [edge1, edge2, ...]
```

The headings tell you where to **read**; the edges tell you where to **walk**.

## Why

Agents that share long-running context with a user accumulate facts about projects, libraries, paths, decisions, and references. Plain markdown notes get long and lossy; pure knowledge graphs are unreadable to humans. This system is a deliberate compromise — humans see a tidy outline, agents follow typed edges, and the same file serves both.

## Architecture

Three skills, three roles. The main agent never touches `MEMORY.md` directly — every read and write goes through a subagent.

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
                 MEMORY.md
            (single source of truth)
```

- **memory-orchestrator** — runs in the main agent. Decides whether each user message should trigger a retrieval, dispatches to the curator, never touches `MEMORY.md`.
- **memory-curator** — long-lived subagent that owns reads and incremental edits. Spawned exactly once per session and reused for every retrieval / update in that session.
- **memory-dreamer** — separate subagent invoked manually when the user wants consolidation. Performs tree/edge cleanup, surfaces contradictions, ages stale nodes, and commits changes via git.

The orchestrator and curator together implement the everyday loop. The dreamer is the periodic "sleep cycle" — explicitly user-triggered, not automatic, because it makes opinionated structural changes and you want to know when they happen.

## Install

```bash
git clone https://github.com/<you>/rightmem.git
cd rightmem
./install.sh <memory-root> <skills-target>
```

- `<memory-root>` — where `MEMORY.md` and `dream_logs/` will live (e.g. `~/.rightmem`).
- `<skills-target>` — where the three skill folders are installed. Common locations:
  - Claude Code (user): `~/.claude/skills`
  - Claude Code (project): `<project>/.claude/skills`
  - Codex: `~/.codex/skills`
  - Other agents: see your agent's skill loading docs.

The script:

1. Creates `<memory-root>/MEMORY.md` from `MEMORY.example.md` (skipped if a `MEMORY.md` already exists).
2. Creates `<memory-root>/dream_logs/`.
3. Initializes a git repo in `<memory-root>` (the dreamer needs git for revertability).
4. Substitutes `{{MEMORY_ROOT}}` and `{{SKILLS_ROOT}}` placeholders in the three skill files and writes them to `<skills-target>`.

Re-run the script any time you want to refresh the skills (e.g. after pulling updates from this repo). Your existing `MEMORY.md` and `dream_logs/` are preserved.

> **Why the install script?** The skill files reference absolute paths (the memory file, the curator's own location). Hand-editing those after copy-paste is error-prone, so we ship the skills as templates with `{{MEMORY_ROOT}}` and `{{SKILLS_ROOT}}` placeholders, and the script substitutes them at install time.

## Usage

After install:

1. **Edit your memory file.** Open `<memory-root>/MEMORY.md`. The example domain (`# Sample Project Graph`) shows the format. Replace it with your own real domains.
2. **The schema preamble is the spec.** The section at the top of `MEMORY.md` (`# Memory File — Schema and Maintenance Rules`) is the authoritative source of truth. Both the curator and the dreamer re-read it on every load and treat it as the law if anything in the skills disagrees. Edit it freely if you want different conventions — the agents will follow.
3. **Daily loop runs automatically.** In agents that load skills by description match, the orchestrator skill triggers per user message; the curator handles retrievals and edits in the background.
4. **Trigger a dream cycle when you want consolidation.** Ask your agent to invoke the `memory-dreamer` skill. Each cycle:
   - Applies unbounded mechanical fixes (missing reverse edges, dead pointers, obvious edge-type upgrades).
   - Applies up to ~5 judgment-driven restructures (merges, splits, promotions, graveyard moves).
   - Writes a dream report to `<memory-root>/dream_logs/YYYY-MM-DD.md`.
   - Commits both `MEMORY.md` and the report. Bad dream? `git revert` in `<memory-root>`.

## Customization

- **Edge types.** Add new types to the table in `MEMORY.md`. Always include a real example in the `Example` column — the curator and dreamer pick edge types by precedent, so unused types drift fastest.
- **Domains.** Add new top-level `# domain` sections in `MEMORY.md`. The curator places new nodes inside the closest existing `###` group of the matching `#` domain.
- **Aging.** There are no inline timestamps; the dreamer reads git history to identify long-untouched nodes. If you want different aging behavior (per-class TTLs, last-verified tags, archival policies), edit the `memory-dreamer` skill — the simple history-based scheme is intended as a starting point you iterate on.
- **What the orchestrator considers "memory-worthy".** Edit the trigger bullet in `memory-orchestrator/SKILL.md` to suit your workflow.

## File layout

This repo:

```
rightmem/
├── README.md
├── install.sh
├── MEMORY.example.md
└── skills/
    ├── memory-orchestrator/SKILL.md
    ├── memory-curator/SKILL.md
    └── memory-dreamer/SKILL.md
```

After install (with `MEMORY_ROOT=~/.rightmem`, `SKILLS_TARGET=~/.claude/skills`):

```
~/.rightmem/
├── .git/
├── MEMORY.md
└── dream_logs/

~/.claude/skills/
├── memory-orchestrator/SKILL.md
├── memory-curator/SKILL.md
└── memory-dreamer/SKILL.md
```

## Design notes

- **Why three skills, not one?** Separation of trust. The orchestrator decides *whether* memory matters; the curator owns the *file*; the dreamer owns *consolidation*. Mixing roles makes each role harder to reason about and tune.
- **Why the main agent can't touch the file.** Single owner = no concurrent writes, no half-edits, easy to reason about state. The curator caches the file across the session; the orchestrator just sends queries and updates.
- **Why no automatic dreaming.** Deep restructures are opinionated. Auto-rewriting your mental index without consent is hostile. Manual trigger + git-revert safety net = trust.
- **Why git instead of inline timestamps.** Git already records when each line was last changed. Adding `[last:YYYY-MM-DD]` tags to every node duplicates that information and creates a maintenance burden. `git log -L` answers "when was this node last touched?" exactly.

## License

MIT
