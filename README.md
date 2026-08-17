# RightMemory

**Durable Memory, live Pursuit, and reusable Agent Corrections for AI coding agents.**

RightMemory gives people and coding agents three semantic modules: durable context lives in Memory, live intent lives in Pursuit, and reusable cases of user redirection live in Agent Corrections. Memory and Pursuit are Markdown document trees in one addressable graph; Agent Corrections is a fixed, non-graph case library. Shared views provide controlled collaboration between RightMemory roots. The same context can move across sessions, devices, users, agent clients, and collaborating agent teams instead of living inside one vendor UI.

![How one RightMemory root works](docs/assets/rightmemory-single-root.svg)

![How RightMemory roots collaborate](docs/assets/rightmemory-shared-roots.svg)

## Why RightMemory

Modern coding agents are strong inside a single conversation, then strangely forgetful in the next one. RightMemory treats memory as owned project and collaboration state:

- **Multi-agent collaboration:** memory roots can share selected context, answer constrained questions, and exchange notes without exposing the whole private memory store.
- **Three focused modules:** Memory and Pursuit use separate lifecycles inside one graph, while Agent Corrections preserves reusable concrete contrasts without turning them into graph content.
- **Multi-device continuity:** the same memory can follow agents across laptops, desktops, clients, and project-specific roots.
- **Clear ownership:** retrieval, unified updates, transcript-review extraction, sync repair, consolidation, and reflection run through explicit role boundaries instead of letting the main agent half-edit RightMemory while doing unrelated work.
- **Vendor-neutral command surface:** Codex CLI and Claude Code CLI have built-in delegated execution today; Gemini CLI-style workflows and other command-capable agents can use the same `rightmemory` CLI or JSON-over-stdio daemon surface.

## The RightMemory Semantic Model

RightMemory is not only a place to store and retrieve context. Its semantic model defines what deserves preservation, where it belongs, how each kind of state evolves, and which roles may change it.

| Module | What it preserves | What it is not |
| --- | --- | --- |
| **Memory** | Durable context that should remain useful beyond the current task or session. | A transcript, task log, or record of everything that happened. |
| **Pursuit** | Live or deliberately parked intent, its hierarchy, and the context needed to understand its current direction. | A backlog, work log, or detailed execution record. |
| **Agent Corrections** | Bounded, reusable cases in which the user redirected prior agent work. | A collection of every correction or a substitute for general behavior guidance. |

Memory and Pursuit form one addressable graph organized as two Markdown document trees. Agent Corrections remains a bounded, non-graph case library.

### What Belongs Where?

- Use **Memory** when durable context can materially improve future action, judgment, interpretation, or retrieval.
- Use **Pursuit** when the intent itself should remain active or deliberately parked after the current update.
- Use **Agent Corrections** when a concrete user redirection forms a reusable case whose contrast would lose value if generalized away.
- Store nothing when the evidence is transient, duplicated, insufficiently settled, or better preserved in project artifacts.

The updater may change any combination of the three modules—or decide that the submitted evidence should not be stored at all.

Canonical definitions and rules: [Memory Rules](rightmemory/reference/MEMORY_RULES.md), [Pursuit Rules](rightmemory/reference/PURSUIT_RULES.md), [Agent Corrections Rules](rightmemory/reference/AGENT_CORRECTION_MEMORY_RULES.md), and the [RightMemory Schema](rightmemory/reference/rightmemory-schema.md).

## Who It Is For

RightMemory is aimed at developers who spend serious time with coding agents and want durable context plus live continuity across new sessions, devices, and agent clients. It is especially useful when agents need to remember decisions and preferences, resume active commitments, or inspect the evidence behind past updates.

## Quick Start

Install prerequisites:

```bash
# macOS
brew install uv git
# or install git with Apple's tools:
xcode-select --install

# Ubuntu / Debian / WSL
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt update && sudo apt install -y git

# Linux Fedora
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo dnf install git
```

On native Windows, install Git for Windows and uv, then run the installer from
PowerShell 5.1 or newer. WSL can use the Linux commands above.

More options: [uv install](https://docs.astral.sh/uv/getting-started/installation/),
[git install](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git).

```bash
git clone https://github.com/RightL/RightMemory.git
cd RightMemory
./install.sh
```

```powershell
git clone https://github.com/RightL/RightMemory.git
cd RightMemory
.\install.ps1
```

The default install uses standalone mode, creates `~/.rightmemory`, installs the `rightmemory` CLI, and installs five user-facing skills into both `~/.codex/skills` and `~/.claude/skills`: `memory-retriever` for read-only use, approval-gated `rightmemory-orchestrator`, automatic `rightmemory-auto-orchestrator`, `maintain-rightmemory` for explicitly requested direct maintenance by the current agent, and `review-agent-guidance-inbox` for explicitly reviewing pending agent guidance before formal admission. On Windows, `~` means your PowerShell home directory. Any agent that can run shell commands, including Gemini CLI-style workflows, can call the CLI directly; the packaged skill install currently targets Codex and Claude Code.

If you already use Codex CLI or Claude Code CLI and want RightMemory roles to run through those tools:

```bash
./install.sh --mode cli-agent ~/.rightmemory ~/.codex/skills
rightmemory doctor agent-cli
```

```powershell
.\install.ps1 --mode cli-agent ~\.rightmemory ~\.codex\skills
rightmemory doctor agent-cli
```

After install, add a short instruction to your agent guidance file, such as
`AGENTS.md` for Codex or `CLAUDE.md` for Claude Code:

```markdown
Use memory-retriever when I choose read-only RightMemory retrieval.
Use rightmemory-orchestrator when I choose approval-gated RightMemory orchestration.
Use rightmemory-auto-orchestrator when I choose automatic RightMemory orchestration.
Use maintain-rightmemory only when I explicitly ask the current agent to edit RightMemory directly.
Use review-agent-guidance-inbox when I explicitly ask to review pending agent guidance.
```

The five skills are user-selected. When using orchestration, the user chooses one of the two orchestrator skills for the conversation; the agent does not invoke both. Both are installed, and the choice is not an installer option, CLI flag, profile value, or persisted RightMemory setting. The shared schema and focused rule documents define valid state, while each skill defines how the host agent accesses it.

Then start the background manager. It reviews idle agent sessions, evaluates prune generations, runs Dreamer consolidation, and produces Insight reflections when enough work has accumulated:

```bash
rightmemory watch start
```

## Demo Flow

![RightMemory terminal demo](docs/assets/rightmemory-demo.svg)

A typical RightMemory turn looks like this:

```text
You ask a coding agent:
  "Continue the sync work from last time."

The selected orchestrator conditionally calls:
  rightmemory retrieve --session <id> "project sync decisions and open issues"

RightMemory returns:
  deterministic source Markdown selected from Memory, Pursuit, linked resources,
  shared views, and relevant Agent Corrections

At a natural boundary, once evidence clears the admission bar:
  rightmemory-orchestrator proposes a submission and waits for approval
  or rightmemory-auto-orchestrator submits it automatically

After approval, or automatically in auto mode:
  rightmemory update submit --session <id> "settled evidence and why it may matter"

When automatic orchestration encounters settled, potentially reusable agent guidance without an explicit request to remember it:
  rightmemory guidance submit --session <id> "settled guidance evidence"

The guidance inbox is synchronized but excluded from ordinary retrieval until the user explicitly reviews and promotes selected entries.

Later:
  the unified updater may change any combination of the three modules, or none
  each queued outcome keeps its exact candidate batch beside the corresponding Git change
  rightmemory dreamer consolidates stale, duplicated, or overgrown memory
  rightmemory insight writes durable reflection artifacts when useful
```

For a short recording script, see [docs/DEMO.md](docs/DEMO.md).

## What It Gives You

- Three semantic modules: durable Memory, live Pursuit, and reusable Agent Corrections.
- One global id namespace and typed graph edges such as `dep:`, `cfg:`, `ver:`, `doc:`, and `todo:` across both trees.
- Multi-device memory continuity across laptops, desktops, agent clients, and project-specific roots.
- Five installed skills: read-only retrieval, approval-gated orchestration, automatic orchestration, explicit direct maintenance, and pending agent-guidance review.
- Two executor modes behind the same `rightmemory` CLI: standalone runtime or delegated Codex/Claude CLI role execution.
- Model-selected, runtime-rendered retrieval output without model-authored summaries or commentary.
- One updater for all three semantic modules, automatic transcript-review candidate extraction, and immutable candidate records for input-to-edit provenance.

## Install Options And Updates

For a custom memory root or skill target:

```bash
./install.sh ~/.rightmemory ~/.codex/skills
```

```powershell
.\install.ps1 ~\.rightmemory ~\.codex\skills
```

CLI-agent mode delegates role execution to Codex CLI or Claude Code CLI while preserving the same `rightmemory` command surface:

```bash
./install.sh --mode cli-agent ~/.rightmemory ~/.codex/skills
```

```powershell
.\install.ps1 --mode cli-agent ~\.rightmemory ~\.codex\skills
```

Install creates semantic state only when bootstrapping a new root. A fresh root receives `MEMORY.md`, `PURSUITS.md`, and the tracked root `.gitignore` control-plane allowlist, then Git baselines the complete synchronized state. The schema and semantic rules live with the installed RightMemory package rather than in each Memory or skills root. A complete pre-existing root is preserved byte-for-byte; reinstall may refresh package-owned runtime files and installed skills, but it does not refresh the allowlist or examples, synthesize missing semantic files, or migrate user state. A complete pre-existing Markdown root without Git history is committed exactly as found.

If an existing root has a Git commit or any recognized semantic state but lacks a regular, non-symlink required root document, install refuses it before making any change. It also refuses legacy root copies named `PURSUIT_RULES.md` or `AGENT_CORRECTION_MEMORY_RULES.md`; review any local changes and remove those obsolete package references explicitly before reinstalling. The memory root, runtime installation, installed skills, and install stamp remain untouched so the user can perform an explicit, reviewed transition first. Fresh installs still baseline current semantic-upgrade notes, while a successful reinstall may report pending notes for a later Dreamer cycle without applying them. Legacy `MEMORY_*.md` files remain protected even when they are not currently reachable from `MEMORY.md`.

Installer admission also validates any existing tracked update queue and checks
for live async jobs from the pre-queue runtime. It changes nothing when either is
incompatible. Finish, retry, or undo those live jobs with the currently installed
RightMemory before rerunning the installer; version one does not invent migrated
candidate identities during package installation.

The memory root must be a standalone, non-bare Git working tree. An existing target nested inside another working tree, a bare repository, or an unusable `.git` entry is refused before installation writes anything.

Reinstall replaces the superseded managed Memory-only orchestrator with the current four-skill surface and removes exact managed copies of former loose skills-root references. Modified loose files are left untouched. It does not keep an alias or a second behavior path.

### Profiles

The default memory root is still `~/.rightmemory`, or `RIGHTMEMORY_ROOT` when set.
Named profiles let a project use a separate memory root:

```bash
rightmemory profile create my-project
rightmemory --profile my-project retrieve --session <agent-session-id> "what do we know about this repo?"
```

Profile aliases live in `<default-memory-root>/profiles.toml`. New profile roots
default to a sibling profile-root area, such as
`~/.rightmemory-profiles/my-project` for the normal default root. Each profile
root has its own `MEMORY.md`, `PURSUITS.md`, optional fixed Agent Correction collections and `corrections.md`,
`rightmemory.toml`, `.runtime/`, change history, watcher state, async update
queues, sessions, and insight logs.

Creating a new profile root bootstraps all required root documents. Registering an existing root requires those documents to be complete and regular; profile creation does not synthesize missing state in an existing root.

A project can opt into a local default profile by adding `.rightmemory-profile`
with the profile name:

```text
my-project
```

Tracking or ignoring that file is a user/project decision. Use `--profile` when
you want an explicit override for one command.

## Why Not Raw Notes Or A Vector DB?

RightMemory is not trying to replace notes, search, or embeddings. It focuses on the part those systems often leave underspecified: how agents preserve structured context, who owns memory edits, how related facts stay connected across sessions, and how durable memory remains reviewable over time.

| Approach | Works Well For | RightMemory Adds |
| --- | --- | --- |
| Raw Markdown notes | Human-readable context | Agent-addressable trees, graph edges, and role-owned updates |
| Vector retrieval | Fuzzy recall across large text | Inspectable structure, deterministic files, and explicit consolidation |
| Agent chat history | Recent session continuity | Durable project memory that survives new sessions, devices, and agent clients |
| MCP memory adapters | Tool integration | A file schema and command runtime that can be wrapped by adapters later |

## Document Model And Reference

Memory and Pursuit are represented as ordinary Markdown document trees in one graph:

- `MEMORY.md` and `MEMORY_<id>.md` hold durable knowledge, context, preferences, decisions, constraints, and reusable guidance.
- `PURSUITS.md` and `PURSUIT_<id>.md` hold live intent, Focus, current state, and next movements that should still shape future action.

Agent Corrections uses two fixed, non-graph files for concrete Expression and Substance cases.

All addressable headings and nodes share one globally unique id namespace, and typed edges may cross between Memory and Pursuit. For canonical semantics and validity, use the rules linked in [The RightMemory Semantic Model](#the-rightmemory-semantic-model); this section focuses on the Markdown representation.

RightMemory parses this syntax once per operation into one canonical in-memory document index. Validation, structured retrieval, graph-aware tools, sync validation, and shared-view extraction all use that index for ids, hierarchy, F# expansion, backing references, source spans, and diagnostics. The index is rebuilt from the authoritative Markdown rather than persisted as a second database.

A Memory fragment looks like this:

```md
# Work Context {#work-context}

## Project Alpha {#project-alpha}

### Runtime {#alpha-runtime}

Runtime facts that apply to the whole project.

- `alpha-python-env` Uses Python 3.11 in `.venv` for local development. → []
- `alpha-test-command` Run the backend tests with `pytest tests/backend`. → [ver:alpha-python-env]
```

The tree tells agents where to read in local context. Anchors and node ids tell agents what can be referenced. Edges tell agents where to walk across otherwise separate branches or between durable context and live intent.

A Pursuit is not a backlog or work log. It preserves an objective only while that intent remains part of the active or deliberately parked pursuit structure. Unfinished work alone does not qualify, and project-local artifacts remain the primary home for operational resume detail. Completed, abandoned, or superseded intent is removed, while only independently durable consequences move to Memory.

Common top-level domains include project or work domains, `# User Context`, and `# Cross-Session Agent Behavior`. User context stores the user's durable context profile. Agent behavior stores guidance about how coding agents should collaborate with that user.

### Headings

`#`, `##`, and `###` are normal tree layers and may contain Memory or Pursuit content. They can have `{#slug}` anchors and heading-level edges when the whole subtree is useful as a graph target.

Addressable `#`, `##`, and `###` headings may also have body paragraphs directly under the heading. Those paragraphs describe the heading itself. Use a heading body when the text explains the whole concept; use child nodes when the fact should stand on its own.

Use `{F#slug}` instead of `{#slug}` when a heading is backed by a sibling parsed detail file. F# is root-relative: Memory maps to `MEMORY_<slug>.md`, while Pursuit maps to `PURSUIT_<slug>.md`. Graph edges still target `slug`, not `F#slug`.

`####` is the deepest heading level allowed in a parsed graph file. A `#### Topic {F#slug}` heading points to the root-relative detail file; it may have body paragraphs that summarize or explain that file, but no nodes or child headings in its containing file.

### Nodes

Nodes are addressable statements under a heading:

```md
- `<node-id>` <description> → [edge1, edge2, ...]
```

Heading ids and node ids share one namespace across both trees. A node with no edges still writes `→ []`; a heading with no edges may omit the edge list. Pursuit `Next` control entries and `Focus` references are not graph nodes.

### Detail Files

Any `#`, `##`, or `###` heading can use its id as a parsed detail-file target by writing `{F#slug}`. For example, Memory `{F#alpha-runtime}` maps to `MEMORY_alpha-runtime.md`; the same form in Pursuit maps to `PURSUIT_alpha-runtime.md`.

Move child content into a detail file when a heading becomes too dense, especially past about 15 direct node lines. Count only direct node lines, not child headings or `####` pointers. After moving content out, keep the F# heading and optional summary in the parent file.

Memory also supports linked resources that are not parsed as graph content: `{M#slug}` points to free-form `MEMORY_<slug>.md` evidence, while `{S#slug}` points to reusable `MEMORY_SKILL_<slug>.md` instructions. M#, S#, MF#, and MQ# are Memory-only. File globs never determine graph membership.

### Shared Views

Shared views connect one memory root to collaboration context owned somewhere else: another person, team space, project, or agent memory root. The package-owned [Shared View rules](rightmemory/reference/SHARED_VIEW_RULES.md) define the relationship and package semantics. RightMemory uses two explicit heading types:

- `{MF#slug}` records a mirrored file shared view. Retrieve silently pulls the latest valid HTTP package before the model starts. Its version-two `dist/MEMORY.md` is a schema-valid Memory document with a view-local id namespace; imported graph items are selected by ids scoped to that MF# source, not by direct file ranges.
- `{MQ#slug}` records a provider question shared view. Retrieve may report that provider-question context is relevant, but the main agent, CLI, or Web Studio calls the question endpoint explicitly with `rightmemory shared-view ask`.

An MF document may contain ordinary, F#, M#, and S# content with all referenced backings packaged under `dist/`. F# participates in the imported graph, M# remains range-addressable evidence through a qualified source such as `MF#auth-api/M#incident-evidence`, and S# returns a complete instruction through a qualified source such as `MF#auth-api/S#review-checklist`. Nested MF# and MQ# connections are invalid. Provider builds validate before replacing preview output or publishing; consumer pulls validate the downloaded candidate before atomically replacing the last valid import.

For a practical provider/consumer walkthrough, see [docs/shared-views-usage.md](docs/shared-views-usage.md).

`rightmemory share` is the normal relationship-level workflow. A share groups one optional file part and one optional question part under one relationship and one bundled invitation. The lower-level `rightmemory shared-view` commands remain available for advanced use and debugging.

The provider owns source files under `shared_views/<view-id>/`. File views use `view.md` and `recipe.toml`, then render version-two `dist/` packages containing a schema-valid `MEMORY.md`, `manifest.toml`, and any referenced `MEMORY_<id>.md` or `MEMORY_SKILL_<id>.md` backings for HTTP publication. Question views use `view.md`, `question.toml`, and provider-private `retriever.md`. Share relationships live in `shares.toml`; consumers record low-level resolver metadata in `shared_views.toml`; credentials, imports, and interaction records stay under `.runtime/shared_views/`.

```bash
rightmemory share create auth-api \
  --provider alice \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --request "Share auth API context with the frontend project and allow live questions." \
  --capability both \
  --question-base-url http://127.0.0.1:8766

rightmemory share revise auth-api "Narrow the share to login refresh-token behavior."
rightmemory share approve auth-api
rightmemory share publish auth-api --label frontend
rightmemory share join http://127.0.0.1:8765/i/share/<invite-token> --consumer-label frontend
rightmemory share status auth-api
rightmemory shared-view ask auth-api-ask "How do tokens refresh?"
rightmemory shared-view note auth-api-files --confirm --task "frontend login migration" \
  "Docs are missing token_expires_at."
rightmemory shared-view inbox
```

All normal sharing goes through HTTP, even on one machine. Initialize and serve a separate hub root, create a provider token, and store that token as a local runtime credential in the provider memory root:

```bash
rightmemory hub init --public-base-url http://127.0.0.1:8765
rightmemory hub token create --provider alice --label publish
rightmemory hub serve --host 127.0.0.1 --port 8765
```

After the hub is running, open `http://127.0.0.1:8765/console` and enter the admin token printed by `rightmemory hub init`. The console is for hub administration: providers, tokens, views, invitations, connections, inbox, and audit.

Then store the printed provider token through a hidden prompt, so it does not need to appear in shell history:

```bash
rightmemory shared-view credential set alice-publish \
  --kind http-publish \
  --hub-url http://127.0.0.1:8765 \
  --provider alice \
  --token-prompt
```

Approved `MF#` file views rebuild and publish automatically after successful memory-write roles. `rightmemory retrieve` silently syncs accepted `MF#` packages before it searches, without adding sync output to retrieve session history or exposing package metadata as retrieval content.

Create an `MF#` invitation by publishing the current file package and asking the hub for an invitation URL:

```bash
rightmemory shared-view invite auth-api-files --label frontend
```

Approved `MQ#` question views are published explicitly:

```bash
rightmemory shared-view publish-question auth-api-ask \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --question-base-url http://127.0.0.1:8765
```

Consumers accept HTTP invitations with `accept-invite`:

```bash
rightmemory shared-view accept-invite http://127.0.0.1:8765/i/<invite-token>
```

For `MF#`, the synced `shared_views.toml` stores the hub URL and credential id. For `MQ#`, the invitation must provide the provider Web Studio question endpoint and an accepted `question_token`; the ask command sends that bearer token, and the provider validates it against `question.toml` token hashes. Bearer tokens stay under `.runtime/shared_views/credentials.toml`.

Web Studio starts with the same share-first provider flow: create and revise a share from natural language, inspect share relationships, and keep the raw `MF#`/`MQ#` builder tools in an advanced section. Direct provider filesystem reads, mounted folder hubs, generic shared-view retrieval, and legacy single-marker shared-view headings are not part of the current product path.

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

### Correction Feedback

RightMemory separates Agent Corrections, which are semantic cases about ordinary agent work, from feedback about proposed RightMemory edits.

Reusable evidence about a settled user redirection may be curated into the fixed Agent Corrections module. `MEMORY_agent-corrections-writing.md` contains **Expression Corrections**, where changing wording, organization, formatting, tone, or presentation alone would resolve the objection. `MEMORY_agent-corrections-design.md` contains **Substance Corrections**, where reasoning, assumptions, decisions, proposed actions, omissions, workflow, or behavior must change. The physical filenames, CLI collection names, and retrieval source ids retain `writing` and `design`; the Expression/Substance test defines their meaning.

Each collection is a priority-curated set of at most 10 entries and 180 non-empty lines, with at most 16 non-empty lines per entry and 200 characters per line; it is not an append-only log or FIFO. Ordinary Retrieve can select relevant entries through `AC#writing` and `AC#design`. The `rightmemory agent-corrections writing` and `rightmemory agent-corrections design` commands remain for an explicitly requested whole-collection review, not a separate ordinary-retrieval pass. Do not duplicate the same lesson in ordinary Agent Behavior or S# unless that representation adds distinct value.

Root `corrections.md` is synchronized RightMemory Edit Feedback, not semantic RightMemory state, graph content, or Agent Corrections. Each entry preserves the relevant candidate text, proposed edit, and accepted edit under [the edit-feedback rules](rightmemory/reference/RIGHTMEMORY_EDIT_CORRECTION_RULES.md); [the example](RIGHTMEMORY_EDIT_CORRECTIONS.example.md) illustrates the format. Update and explicit session review form a tentative curation judgment before consulting relevant edit feedback as a late check. Candidate-backed updater outcomes retain their exact input under `update_records/`, while Git supplies the corresponding semantic diff.

## Agent Roles

RightMemory separates ordinary work from state ownership. It installs four user-selected skills:

```text
memory-retriever
  +--> rightmemory retrieve         read-only retrieval across all three modules

rightmemory-orchestrator
  +--> rightmemory retrieve         conditional retrieval
  +--> user approval                qualifying evidence proposed at a natural boundary
  +--> rightmemory update submit    approved evidence

rightmemory-auto-orchestrator
  +--> rightmemory retrieve         conditional retrieval
  +--> rightmemory update submit    qualifying evidence at a natural boundary

maintain-rightmemory
  +--> MEMORY*.md                   direct coherent maintenance
  +--> PURSUIT*.md                  direct coherent maintenance
  +--> correction surfaces          direct rule-governed curation

transcript review
  +--> candidate extraction         idle supported sessions
  +--> unified update queue         no direct graph edit

unified updater
  +--> MEMORY*.md                   durable context
  +--> PURSUIT*.md                  live intent and continuity
  +--> Agent Correction files       reusable redirection cases
  +--> update_records/*.json        exact candidate batch provenance
```

- `memory-retriever` retrieves relevant context and never submits updates.
- `rightmemory-orchestrator` is approval-gated. Once qualifying evidence is clear and the conversation reaches a natural boundary, it names the apparent module and reason, then submits only after approval. Stale, wrong, misleading, or overbroad retrieved state is submitted immediately so the agent does not continue from known-bad context.
- `rightmemory-auto-orchestrator` applies the same admission bar but submits qualifying evidence automatically at a natural boundary. Completion is not required, and the beginning or end of work alone does not trigger either mode.
- `maintain-rightmemory` is explicit-only. The current agent edits Memory, Pursuit, linked content, and either correction surface directly under the schema and focused rules; it does not call Update, submit candidates, or invoke another RightMemory model role.
- When the user chooses orchestration, use the selected approval-gated or automatic skill for that conversation rather than invoking both. RightMemory does not persist the choice.
- Once selected, `memory-retriever` calls Retrieve for the stated memory need. The two orchestrators retrieve conditionally when stored context could materially affect how the agent understands or approaches the work and skip clearly self-contained requests. Every ordinary Retrieve call may select relevant Agent Corrections through `AC#writing` and `AC#design`.
- `memory-retriever` discloses active or deferred preferences, workflow or behavior guidance, reusable instructions, and Agent Corrections in a fixed `[RightMemory] Retrieved guidance` block; ordinary facts, knowledge, and descriptive context are not disclosed this way.
- Evidence clears the orchestration bar only when omitting it would likely cause poorer future decisions or substantial rediscovery, lose meaningful Pursuit context, or allow a settled reusable failure pattern to recur. Transient progress, routine results, incompleteness by itself, and detail already preserved in project artifacts do not qualify.
- Candidate submission is evidence, not final stored wording, classification, placement, or an instruction to edit a particular module. Session ids provide provenance and batching boundaries, not task identity.
- The unified updater reconciles related candidates and may change Memory, Pursuit, Agent Corrections, any combination of them, or nothing in one isolated transaction.
- Dreamer, Insight, Historian, and Pruner remain Memory-oriented maintenance roles. They must preserve ids and edges referenced from Pursuit. Sync repair transports the wider synchronized surface without taking over semantic updater judgment.
- Standalone mode uses RightMemory's bounded tools, while CLI-agent mode delegates roles to Codex or Claude CLI with role-specific sandbox or permission defaults.

The host agent should avoid reading or editing RightMemory state directly unless the user explicitly selects `maintain-rightmemory`. Other access goes through the command-backed skills and runtime roles, which keeps ownership clear and reduces partial or competing edits.

## Prompt Sources

RightMemory keeps role behavior in one canonical prompt set under `rightmemory/prompts/`:

```text
rightmemory/prompts/retrieve.md
rightmemory/prompts/update.md
rightmemory/prompts/dreamer.md
rightmemory/prompts/insight.md
rightmemory/prompts/reviewer.md
rightmemory/prompts/historian.md
rightmemory/prompts/pruner.md
rightmemory/prompts/sync-reconciler.md
```

Both install modes use these files through the `rightmemory` runtime. Standalone mode loads them into the local Pydantic AI agent and tool loop. CLI-agent mode wraps the same role instructions into prompts sent to Codex CLI or Claude Code CLI. Other command-capable agents can call the same CLI or daemon surface without changing the schema. The retriever and orchestrator skills own the host agent's retrieval decision, approval behavior, evidence threshold, and natural-boundary timing; the canonical prompts own the Retrieve and Update roles invoked behind their commands. `maintain-rightmemory` instead applies the schema and focused rules directly when the user explicitly requests direct maintenance.

Package-owned references complement the prompts: the schema defines representation; Memory, Pursuit, Agent Corrections, RightMemory Edit Feedback, and Shared View rules define focused semantics; and the [Retrieve runtime contract](rightmemory/reference/RETRIEVE_CONTRACT.md) owns Retrieve's input snapshot and terminal-selection mechanics. `reviewer.md` extracts candidate evidence from supported transcripts for unified Update. Explicit session review independently forms tentative proposals, then consults relevant RightMemory Edit Feedback before finalizing them.

## Install Modes

RightMemory has two install modes. The default is `standalone`.

| Mode | Use When | What Gets Installed |
| --- | --- | --- |
| `standalone` | You want RightMemory to run its own local Pydantic AI role agents and tools. | `memory-retriever`, both orchestrator modes, explicit-only `maintain-rightmemory`, their shared definitions, and the `rightmemory` CLI. |
| `cli-agent` | You want RightMemory to delegate each runtime role turn to Codex CLI or Claude Code CLI. | The same four skills and shared definitions plus the `rightmemory` CLI. |

The installer arguments are:

```bash
./install.sh [--mode cli-agent|standalone] [<memory-root> <skills-target>]
```

```powershell
.\install.ps1 [--mode cli-agent|standalone] [<memory-root> <skills-target>]
```

- `<memory-root>` is where Memory, Pursuit, optional Agent Correction collections, optional RightMemory Edit Feedback, sharing state, and `insight_logs/` live.
- `<skills-target>` is where your agent loads skills from, such as `~/.claude/skills` or `~/.codex/skills`.
- With no path arguments, the installer uses `~/.rightmemory` and installs all four skills into `~/.codex/skills` and `~/.claude/skills`.

Both modes require `git` and `uv`. On macOS, Linux, and WSL, the runtime is
installed under `${XDG_DATA_HOME:-$HOME/.local/share}/rightmemory/venv`, and the
`rightmemory` command is written to `~/.local/bin/rightmemory`. On native
Windows, the runtime is installed under `$env:LOCALAPPDATA\RightMemory\venv`,
and the command shim is written to
`$env:LOCALAPPDATA\RightMemory\bin\rightmemory.cmd`. If the command directory is
not on `PATH`, the installer prints shell or PowerShell guidance after install.
The PowerShell installer also prepends that directory for the current session,
so the next `rightmemory` command works immediately; persisting the user `PATH`
remains an explicit user choice.
The Bash and PowerShell entrypoints are small platform bootstraps; both delegate
the install transaction to the same stdlib-only Python core, so state
preservation, Git setup, runtime installation, skills, and semantic upgrades have
one implementation. On Windows, CLI-agent mode supports native provider
executables and standard npm `.cmd` shims that include their matching `.ps1`
shim.

RightMemory can keep the same memory root available across laptops, desktops,
and agent clients. The current managed sync implementation uses a private Git
remote underneath; the user-facing feature is multi-device memory continuity.
Enable the sync loop when you want the runtime to pull before automatic
semantic work and push successful memory changes after they land.

## Everyday Use

1. Tell the agent to use `memory-retriever` for read-only access, `rightmemory-orchestrator` for approval-gated orchestration, `rightmemory-auto-orchestrator` for automatic orchestration, or `maintain-rightmemory` when you explicitly want that agent to edit RightMemory directly. When orchestrating, select one of the two modes for the conversation.
2. Run `rightmemory watch start` for transcript extraction, pruning, consolidation, Insight cycles, and optional sync.
3. Let `memory-retriever` stop after retrieval, let the approval-gated orchestrator submit only after approval, let the automatic orchestrator submit qualifying evidence itself, or let the direct maintainer apply the schema and focused rules.
4. Use `rightmemory status` when you need to inspect watcher, queue, and sync state.

Dreamer consolidates durable Memory when it needs structural cleanup. Insight commits timestamped reflections under `insight_logs/` when broader patterns, risks, or next-step ideas are worth preserving.

## Command Runtime

Both install modes expose the same command surface. The command-backed skills call roles such as `rightmemory retrieve` and `rightmemory update`; the install mode determines who executes the role prompt after the command starts. This executor choice is separate from the user choosing approval-gated or automatic orchestration. `maintain-rightmemory` does not invoke those maintenance roles.

```bash
rightmemory retrieve --session <agent-session-id> "find memory about the standalone mode"
rightmemory update submit --session <agent-session-id> "remember that MCP should stay optional"
rightmemory update pull --session <agent-session-id>
rightmemory update undo --session <agent-session-id> <pending-candidate-id>
rightmemory review scan --once
rightmemory review watch
rightmemory dreamer --session <agent-session-id> "optional consolidation hint"
rightmemory insight --session <agent-session-id> "optional focus hint"
rightmemory insight watch
rightmemory prune
rightmemory prune watch
rightmemory history --session <agent-session-id> "find pruned memory about the old setup"
rightmemory agent-corrections writing
rightmemory agent-corrections design
rightmemory agent-cli cleanup --once
rightmemory shared-view list
rightmemory shared-view build-file <view-id> "intent" --title "View Title" --hub-url <url> --credential-id <id>
rightmemory shared-view build-question <view-id> "intent" --title "View Title"
rightmemory shared-view approve <view-id> --type file
rightmemory shared-view accept-invite <http-invitation>
rightmemory shared-view pull <mf-id>
rightmemory shared-view status <id>
rightmemory shared-view ask <mq-id> "question"
rightmemory status
rightmemory watch start
rightmemory watch status
rightmemory watch stop
rightmemory retrieve chat
rightmemory update chat
rightmemory dreamer chat
rightmemory insight chat
```

`update undo` uses a numeric ID only for this device's local queue. For a
synchronized candidate, use the eight-character UID prefix shown by
`update pull`; this keeps cross-device ID collisions unambiguous. An all-digit
reference of eight or more characters is therefore interpreted as a UID prefix.

For machine callers:

```bash
rightmemory retrieve daemon --stdio-json
rightmemory update daemon --stdio-json
rightmemory dreamer daemon --stdio-json
rightmemory insight daemon --stdio-json
```

The daemon reads JSON lines from stdin and writes JSON lines to stdout:

```json
{"message":"find memory about the standalone mode"}
{"message":"remember that MCP should stay optional"}
```

The runtime is intentionally small:

- Standalone mode uses `pydantic_ai.Agent` as a chat-like agent loop.
- CLI-agent mode delegates the same role turn to Codex CLI or Claude Code CLI. Retrieve may keep one active provider mapping under `<memory-root>/.runtime/agent_cli_sessions/`; other independent role commands are one-shot.
- Standalone retrieve uses complete typed reads for local F# details and S# skills, line-numbered reads for local M# evidence, typed progressive reads for validated MF# graphs and their F#/M#/S# resources, and fixed `AC#writing` / `AC#design` sources for complete Agent Correction entries. CLI-agent emits the same selector as strict JSON. The shared runtime uses the canonical index and Retrieve contract to resolve ids, permitted ranges, hierarchy, source positions, and source-authored Markdown.
- `~/.rightmemory` is the default memory root, and all tool paths must stay inside the configured memory root. Set `RIGHTMEMORY_ROOT` to use a different no-profile root, or use `--profile <name>` / `.rightmemory-profile` for project-specific roots.
- Retrieve, unified Update, transcript-review extraction, history, dreamer, insight, pruner, and sync repair have separate runtime boundaries selected by command line, queue, scanner, or watcher.
- Role-specific executor settings are read from `<memory-root>/rightmemory.toml`.
- Standalone calls with `--session` persist exact Pydantic AI message history under `<memory-root>/.runtime/sessions/<role>/`. In CLI-agent mode, only retrieve persists a provider mapping across commands. Explicit `chat` may reuse one process-local provider thread, while daemon requests and other independent role turns start fresh threads.
- Every new CLI-agent provider thread receives an ownership record under `<memory-root>/.runtime/agent_cli_threads/`, including one-shot and failed isolated work, so transcript review can exclude internal conversations without relying on an active mapping.
- Optional debug tracing appends live JSONL events under `<memory-root>/.runtime/debug/<role>/<session>.jsonl` without changing the canonical session history.
- Use `rightmemory status` for a read-only operational dashboard across the configured memory root. Its `Sync` section reports whether sync is configured, the upstream tracking reference, ahead/behind counts relative to the last-fetched upstream, and the latest recorded sync outcome. Sync watcher process state remains under `Managed Watches`. The dashboard also summarizes Git state, Dreamer and Insight trigger progress, async update queues, bounded last-message previews, and file paths for full logs or state. Status never fetches, pulls, pushes, starts watchers, invokes a model, or writes runtime state. Use `rightmemory watch status` when you need the lower-level managed-watch process view.
- Use `rightmemory validate` for a read-only check of the configured root, or `rightmemory validate --root <path>` for an explicit root such as a maintenance worktree. It requires the canonical root documents, validates the complete Memory/Pursuit graph, any present Agent Correction collections, and `corrections.md`, and exits nonzero on failure.
- A fresh-root install creates and baselines a tracked root `.gitignore` allowlist so Git status surfaces Memory, Pursuit, any admitted Agent Correction collections, optional root `corrections.md`, sharing metadata and provider view sources, `insight_logs/*.md`, immutable `update_records/*.json`, and the narrowly defined `update_queue/` JSON paths; generated shared-view output stays outside the committed surface. Reinstall preserves an existing allowlist exactly. Package reference changes arrive with the installed software rather than through Memory sync.
- Use `rightmemory reference <name>` to print a canonical package-owned reference without resolving or reading a Memory root. Supported names are `schema`, `memory`, `pursuit`, `agent-correction`, `edit-correction`, `shared-view`, and `retrieve-contract`.
- Async `update submit` calls for the same `--session` accumulate as pending candidates and reset that session's configured quiet period. A global worker batches whole session queues, but the updater groups candidates by the work they describe rather than treating a session as one task. Natural-boundary submissions about related evidence are reconciled together; `pull` and `undo` remain per-session. Retrieve can see pending evidence as `Recent submitted RightMemory` before consolidation.
- Automatic unified-update, dreamer, insight, and pruner turns use isolated Git worktrees when they operate on the main state root. Runtime validates complete role-owned results before landing them. Transcript review remains read-only and queues any resulting candidate.
- Standalone daemon context is preserved with Pydantic AI message history.
- MCP support can be added later as an adapter over the same daemon.

### Async Update Config

Submitted updates keep a per-session one-hour quiet period, then the global
worker groups eligible session queues by candidate count:

```toml
[update.async]
target_batch_candidates = 15
max_wait_seconds = 86400
```

`target_batch_candidates` is a fill threshold, not a hard cap. The worker keeps
session queues whole, so a batch may overshoot it. `max_wait_seconds` is
measured from the oldest eligible queue's quiet-period deadline.

`rightmemory status` includes aggregate async update worker and queue state
without requiring a session id. For one session's detailed pending, running,
result, or error state, continue to use `rightmemory update pull --session <id>`.
The queue view is labeled current when the checkout matches the last-fetched
upstream. When Git is behind or diverged, the dashboard warns that local queue
counts may be incomplete; when Git is ahead, it identifies local queue state
that is not yet present upstream.

When Git sync is enabled, published candidates and their coordination state use
the tracked `update_queue/` paths. Before a local outbox candidate is published,
RightMemory synchronizes local state commits; if that fails, the self-contained
candidate remains in the outbox. Its queue commit then follows that state
naturally in Git history without storing a state commit id in ordinary candidate
evidence. A claimant performs another full sync before acquiring the lease.

Every processed candidate batch is retained at
`update_records/<operation-id>.json`. The immutable record contains the exact
candidates and lands in the same commit as any semantic changes. A batch
that produces no semantic edit lands a record-only commit. The commit's
`RightMemory-Operation` trailer and the record filename provide the complete
input-to-diff link without copying the diff, touched paths, or an inferred
per-candidate mapping into the record. Local and synchronized processing use the
same canonical batch identity. Queue files remain transport state and are
removed after fenced settlement; the operation record is the durable input
provenance in both modes. Because records preserve exact candidate messages in
Git, the memory repository and its sync remote must be treated as private.

Before upgrading, drain
live updates on every device with its currently installed runtime. Then update
and reinstall RightMemory everywhere before submitting another candidate; older
runtimes reject the new paths rather than silently admitting state they cannot
validate. Reinstall refuses a live legacy job without `candidate_uid` or any
older pending transcript-review delivery, or any malformed queue, before
changing the installation.

Unsupported or unreadable historical async state also blocks reinstall, even
when drained. After confirming it contains no live work, archive the listed
runtime file and rerun the installer.

Processing synchronized candidates requires an online Git claim. Version one
uses a fixed six-hour lease with no heartbeat; this is long enough for typical
work, while an unusually long CLI-agent command may outlive it. Finalization
refetches and verifies the fencing token, so expiry can duplicate computation but
cannot commit a stale result. With reasonably synchronized device clocks, a
crashed owner delays takeover by about six hours; clock skew can change that
availability delay without weakening fencing correctness.

A candidate may still process offline while it is provably local-only. Once its
first publication attempt begins, it stays online-only until Git proves the
synchronized outcome.

### CLI-Agent Config

CLI-agent mode uses a global provider plus role model settings. Most installs need a retrieve model and a default writer model; dreamer, insight, transcript reviewer, pruner, historian, and sync repair reuse the writer config unless you override them.

A minimal Codex setup:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5"

[update.agent_cli]
model = "gpt-5"
```

Add a role-specific table only when a role should use a different model or provider:

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

`rightmemory retrieve --session <id>` resumes the same registered provider conversation while it remains active. Its first turn receives the canonical role instructions and a stable snapshot of Memory, Pursuit, and any present fixed Agent Correction collections; resumed turns receive changes to that snapshot, newly submitted candidates, and the current query. Local prior questions and answers are not replayed into an already resumed provider thread.

Every other independent CLI-agent role turn starts a fresh provider conversation. An explicit `chat` process may keep one in-memory conversation until that process exits, but it does not create a mapping for another process to resume. This policy is the same for Codex and Claude.

Registered Codex threads expire after 24 hours without a successful turn. Cleanup first detaches an expired retrieve mapping and resets its local delivery state, then deletes the exact owned thread through Codex App Server. It runs opportunistically before top-level CLI-agent work and hourly through the managed `agent-cli-cleanup` watcher. Run a bounded diagnostic pass directly with:

```bash
rightmemory agent-cli cleanup --once
```

Cleanup failures do not fail the role command; pending records are retried. RightMemory never edits Codex history files or SQLite state directly, never imports old unregistered threads, and never deletes Claude sessions automatically.

### Standalone Config

OpenAI-compatible retrieve/update config:

```toml
[retrieve]
max_output_chars = 100000

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

Retrieve retains native per-session model history and asks the model not to reselect unchanged content it already returned. A terminal model selection is always rendered faithfully. `rightmemory retrieve --include-returned --session <id> "<query>"` attaches the current authoritative forms of previously returned graph items, linked sources, and Agent Correction entries to that call's retrieval context without clearing accumulated coverage; later calls return to the normal context policy. Changed content at the same address is surfaced as changed. `max_output_chars` is a safety limit: oversized selections are rejected for model retry rather than truncated.

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

`model_id` is required for each explicit `[<role>.model]` table. `anthropic/...` model ids use `AnthropicModel`; other model ids use `OpenAIChatModel` with `OpenAIProvider`, so OpenAI-compatible local gateways can use `api_base` and `api_key`. `[<role>.model.kwargs]` is forwarded as Pydantic AI model settings and unsupported keys fail fast.

Normalized `deepseek-*` model ids keep that configurable OpenAI-compatible transport while using Pydantic AI's DeepSeek profile, so thinking-mode tool loops avoid forced tool selection and preserve `reasoning_content` between requests.

Standalone configs use role-local model tables such as `[retrieve.model]`, `[update.model]`, `[historian.model]`, `[dreamer.model]`, `[insight.model]`, `[reviewer.model]`, and `[pruner.model]` for the roles you run. In the common setup, configure `[retrieve.model]` for search and `[update.model]` as the default writer model. `[reviewer.model]` is for transcript candidate extraction. Other non-retrieve roles reuse the writer model unless you give them their own table.

Configure `[sync-reconciler.model]` or `[sync-reconciler.agent_cli]` only if sync repair should use a different model from the default writer.

Pruner has lifecycle settings in the same role table:

```toml
[pruner]
generation_commits = 70
revival_grace_checkpoints = 2

[pruner.model]
model_id = "anthropic/example-pruner-model"
```

`generation_commits` counts commits since the latest `prune:` commit. If no prune checkpoint exists, it counts repository history. `revival_grace_checkpoints` controls how many due prune checkpoints a reactivated item is preserved after it reappears in active memory.

To debug in-flight standalone calls, enable append-only trace logs:

```toml
[debug]
trace = true
```

Trace files include run, history-save, and tool events. They may include prompts, model outputs, and tool results. Trace files are append-only, so repeated failures can make them grow quickly; leave tracing off unless you need live debugging.

### Background Watchers

RightMemory can keep transcript review, pruning, Dreamer, Insight, CLI-agent thread cleanup, and sync loops under the same background manager. The normal controls are:

```bash
rightmemory watch start
rightmemory watch status
rightmemory watch stop
rightmemory watch restart
rightmemory --profile my-project watch start
rightmemory --profile my-project status
```

By default these commands manage `review`, `dreamer`, `insight`, `pruner`, and `agent-cli-cleanup`, plus `sync` when `[sync].enabled` is true. Pass a target when you want one loop, such as `rightmemory watch start agent-cli-cleanup`. Managed watcher pid files and logs live under `<memory-root>/.runtime/watch/`.

For a single read-only view of watcher state, Dreamer and Insight trigger
progress, async update queues, recent previews, and paths to the underlying logs
or state files, use `rightmemory status`. `rightmemory watch status`
intentionally stays focused on managed watch process state.

#### Update Provenance

Every queued updater outcome retains its exact candidate batch in
`update_records/<operation-id>.json`. The record lands in the same commit as the
semantic edit; a no-change outcome lands as a record-only commit. Review the
record beside that commit's Git diff to see exactly which evidence produced which
state change. The diff stays authoritative in Git and is not duplicated in the
record. Explicit Update turns without queued candidates create no provenance
artifact.

#### Transcript Review

The transcript-review loop scans idle supported sessions, extracts high-value candidate evidence, and submits that evidence through the unified update queue. It can preserve settled user redirections and explicit feedback on proposed RightMemory edits, but it never edits semantic state directly. Update forms a tentative edit and then consults relevant root `corrections.md`, so session-derived updates benefit from existing RightMemory Edit Feedback without giving the reviewer write authority.

Run the continuous loop directly with:

```bash
rightmemory review watch
```

The watcher starts immediately and runs full-batch scans, then sleeps before checking again. A successful batch triggers another immediate scan so backlog is not delayed by the interval. A failed batch retries after at most 60 seconds. The default idle interval is two hours; override it with `--interval <seconds>`.

For cron, launchd, or other supervisors, call one bounded scan at a time:

```bash
rightmemory review scan --once
```

Each `scan --once` command reviews at most one eligible batch and then exits.
By default a batch contains up to 3 provider sessions.

For debugging an adapter without calling a model:

```bash
rightmemory review normalize --source claude --path ~/.claude/projects/<project>/<session>.jsonl
```

Explicit session-review workflows can check and record the existing reviewed-session state without running the automatic reviewer:

```bash
rightmemory review status codex:<session-id>
rightmemory review mark codex:<session-id>
```

`status` prints `reviewed` or `not reviewed`. `mark` is atomic and idempotent; use it only after the review has completed successfully.

The repository's explicit `review-rightmemory-session` workflow is direct curation rather than automatic candidate extraction. It forms tentative Memory and Agent Correction proposals from the full session, then reads relevant `corrections.md` entries as a late check before seeking approval or editing. It does not modify Pursuit or curate the edit-feedback file during that review.

Add source presets to `<memory-root>/rightmemory.toml`:

```toml
[review]
idle_seconds = 21600
since_days = 3
batch_size = 3

[[review.sources]]
kind = "claude"
path = "~/.claude/projects"

[[review.sources]]
kind = "codex"
path = "~/.codex/sessions"
```

If `[[review.sources]]` is omitted, RightMemory checks the default Codex and
Claude locations. By default it considers transcript files modified in the last
3 days, suppresses provider-local prefix duplicates from forked transcripts,
then reviews time-adjacent eligible representatives in batches of up to 3. When
one eligible transcript is a normalized-turn prefix of a longer transcript from
the same provider, RightMemory reviews the longest representative and records
the shorter covered session under `skipped_duplicate` after representative
success. Review state is stored under `<memory-root>/.runtime/review/state.json`
and records reviewed provider sessions by source and session id. A successful
batch marks every included representative and covered duplicate reviewed only
after extraction succeeds and any resulting candidate is durably submitted; a failed batch marks none.
If the same provider session later changes or resumes,
scanner state treats it as already reviewed unless you clear the corresponding
review state.

### Forgetting And History

RightMemory keeps active durable Memory intentionally perishable. `rightmemory prune` checks whether the repository has accumulated enough commits since the latest `prune:` checkpoint. The default threshold is 70 commits. `rightmemory prune watch` runs the same check periodically, and `rightmemory watch start` starts it by default. When pruning is due, the runtime supplies the pruner with the boundary commit, current head, previous prune ledger, and grace policy. The Memory-oriented pruner removes unchanged durable context only when it is no longer worth keeping, validates the complete graph, preserves ids referenced from Pursuit, and commits with a `prune:` subject.

If a due prune has nothing to remove, the pruner writes an empty `prune: checkpoint` commit. Checkpoint commits are useful because they keep generations based on work done rather than wall-clock time.

The `prune:` commit body is the lightweight ledger. It records the boundary, removed ids, revived ids under grace, and notable skips. A memory item that was pruned and then written back gets grace across two due prune checkpoints by default; after that, the pruner judges it like ordinary active memory again. The memory files do not carry lifecycle metadata.

Ordinary `rightmemory retrieve` searches the current Memory/Pursuit graph and considers relevant Agent Corrections from the fixed collections. `rightmemory history --session <id> "query"` asks the historian to search `prune:` ledgers and Git snapshots for pruned Memory. Historian returns matches as historical context and does not write them back. When old context becomes useful again, submit ordinary update evidence so the updater can judge whether to reactivate it.

### Change-Triggered Dreamer And Insight Cycles

Dreamer and Insight can run background cycles from the same manager:

```bash
rightmemory watch start dreamer
rightmemory watch start insight
```

`rightmemory dreamer watch` checks `<memory-root>/.runtime/dreamer/trigger-state.json` and runs after successful Memory-changing work has accumulated enough points. With the default `[dreamer.watch]` settings, each Memory-changing unified update contributes `1.0` point, the trigger threshold is `50`, and the watcher checks every `3000` seconds.

```toml
[dreamer.watch]
trigger_points = 50
update_candidate_points = 1.0
check_interval_seconds = 3000
```

`rightmemory insight watch` checks `<memory-root>/.runtime/insight/trigger-state.json` and writes timestamped reflection artifacts under `insight_logs/` when useful. Insight reads active Memory and prior Insight logs; it edits neither Memory nor Pursuit and does not read RightMemory edit `corrections.md`. With the default `[insight.watch]` settings, each Memory-changing unified update contributes `1.0` point, the trigger threshold is `150`, and the watcher checks every `3000` seconds.

```toml
[insight.watch]
trigger_points = 150
update_candidate_points = 1.0
check_interval_seconds = 3000
```

A landed Memory-changing transaction adds one configured unit to Dreamer and Insight regardless of how many candidates the updater reconciled. Pursuit-only, no-op, and failed updates add no pressure. Transcript review only submits evidence to the unified queue and adds no pressure by itself. A successful automatic cycle consumes that role's configured threshold after it lands or completes as a valid no-op; failed cycles preserve accumulated points. `rightmemory dreamer watch --interval <seconds>` and `rightmemory insight watch --interval <seconds>` override the trigger-check cadence.

Transcript-review scans share their lock with the corresponding watcher under `.runtime/watch/`; dreamer, insight, and pruner watchers hold their own locks there as well. A competing scan or watcher exits instead of duplicating work. Unified Update also uses one updater execution lock. Isolated roles may do model work in temporary checkouts, and landing uses the shared write lock before changing the main repository.

`rightmemory watch stop` writes a PID-bound cooperative stop request. A sleeping
watcher exits within a few seconds; a watcher doing model work finishes the
current cycle first. When `install.sh` or `install.ps1` finishes, it updates
`<memory-root>/.runtime/install.stamp`. Watchers check that stamp between runs
and while sleeping, then replace themselves with the updated runtime. POSIX
keeps the process identity through `exec`; Windows starts a hidden replacement
and hands over the lock, stop request, PID, and process-creation identity. The
manager therefore continues to recognize and stop the replacement safely even
when Windows assigns it a new PID. Run `rightmemory watch start` or
`rightmemory watch restart` after an upgrade to start newly introduced targets.

### Isolated Automatic Writes

Automatic unified-Update, dreamer, insight, and pruner turns that operate on the main state root run in temporary Git worktrees under `<memory-root>/.runtime/worktrees/` on branches named `rightmemory-isolated-<role>-<uuid>`. Update may commit any meaningful combination of Memory, Pursuit, and Agent Correction files, and runtime adds an immutable candidate record to the same commit for queued work. A queued no-change outcome still commits its candidate record; an explicit Update turn without queued candidates adds no managed artifact. Memory-oriented maintenance roles remain restricted to Memory, while Insight commits `insight_logs/*.md`. Runtime validates complete role-owned results and lands successful commits; empty `prune:` checkpoints are allowed.

Temporary session and provider state lives under `.runtime/isolated-state/` during an isolated turn and is promoted after successful landing or a valid no-op. Standalone turns seed local message history there. CLI-agent turns start speculative provider work in a fresh one-shot session. Successful ownership state is promoted; if later validation or landing fails, the ownership record alone is preserved so the abandoned internal thread remains excluded from transcript review and eligible for cleanup. Other temporary work is discarded, and the original candidate batch or maintenance trigger balance remains the retry source.

Dirty synchronized RightMemory files block automatic semantic writes before a temporary role starts, but runtime gives `sync-reconciler` one bounded repair chance. If repair commits a clean state, the original automatic write restarts from its source input. Otherwise it fails instead of stacking model work on unclear local changes.

### Automatic Global Sync

RightMemory can keep one memory root shared across devices. The current sync implementation uses a private Git remote underneath; GitHub private repositories are the easiest hosted setup, and any SSH or HTTPS Git remote works once the memory repo has an upstream branch.

Enable sync in `<memory-root>/rightmemory.toml`:

```toml
[sync]
enabled = true
stale_pull_after_hours = 24
```

When sync is enabled, runtime code handles remote Git synchronization around automatic semantic work. It checks upstream state before model work and pushes after successful synchronized-state commits land. Incoming commits never merge directly into the active checkout: RightMemory merges the exact fetched commit in a leased candidate worktree, invokes `sync-reconciler` there only for synchronized conflicts or semantic validation failures, validates the complete candidate, and then fast-forwards the active root to that exact commit. The isolated-write dirty-main check is separate from remote sync: local synchronized files can block automatic semantic writes even when `[sync].enabled` is false.

Retrieve adds a small active-use safety net. At most once every five minutes it performs a pull-only upstream check before reading memory, with the fetch bounded to two seconds. A clean incoming commit is validated and admitted for the current request; the foreground path never pushes and never invokes `sync-reconciler`. Offline, timed-out, dirty, conflicting, or invalid incoming state does not block retrieval: the request uses the last valid local state, then starts one detached full sync cycle after the response finishes. That cycle owns reconciliation and push when needed, and a shared nonblocking cycle lock prevents duplicate watcher, retrieve, and deferred work. If a clean refresh brings synchronized update candidates, the update worker is woken only after retrieval completes. Historical retrieval remains local.

The tracked root `.gitignore` is synchronized package-owned control-plane state rather than semantic Memory. Sync admits it through the same regular-file and path-surface checks as other synchronized files, while reinstall never rewrites an existing copy.

Update records and update-queue files are non-graph state, not semantic repair input. Sync transports only canonical immutable `update_records/*.json` plus candidate, recovery, and singleton-lease queue paths, validates their complete schemas before publication, and fails closed on malformed data or conflicts in these paths. `sync-reconciler` never edits or resolves this state, and the queue's global lease fences one updater batch across devices.

Managed watch includes a `sync` target. `rightmemory watch start` starts it when sync is enabled, and `rightmemory watch start sync` runs that target by itself. The watcher is an optional low-latency accelerator; retrieve's bounded check also restores progress during active use when no watcher is running. Every normal watcher cycle fetches the upstream so newly published candidates become locally visible; it pulls immediately when the fetched tip changed. When the tip is unchanged, `stale_pull_after_hours` controls how often a no-change pull checkpoint is recorded. Clean pulls and fresh checks stay deterministic and do not call a model.

Pre-existing dirty or already-invalid active state blocks incoming sync before a candidate can land. If candidate merge, repair, validation, or final publication checks fail, the active branch and semantic files remain unchanged; conflict markers exist only in the candidate. A prepared repair is durably recoverable without another model turn. For `corrections.md`, candidate repair preserves non-identical entries without ranking them; explicit direct maintenance handles later semantic curation. A network push failure after local publication leaves the valid local commit in place and can retry without repeating repair.

Run standalone mode from this repository during development:

```bash
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install -e . --python .venv/bin/python
rightmemory retrieve chat
```

On Windows PowerShell:

```powershell
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install -e . --python .venv\Scripts\python.exe
rightmemory retrieve chat
```

The standalone runtime exposes sandboxed tools rooted at the configured memory root. It does not provide an OS-level jail.

## File Layout

Repository:

```text
RightMemory/
├── README.md
├── docs/
│   ├── DEMO.md
│   └── assets/
├── install.sh
├── install.ps1
├── MEMORY.example.md
├── PURSUITS.example.md
├── RIGHTMEMORY_EDIT_CORRECTIONS.example.md
├── rightmemory/
│   ├── install_core.py
│   ├── platform.py
│   ├── prompts/
│   └── reference/
│       ├── rightmemory-schema.md
│       ├── MEMORY_RULES.md
│       ├── PURSUIT_RULES.md
│       ├── AGENT_CORRECTION_MEMORY_RULES.md
│       ├── RIGHTMEMORY_EDIT_CORRECTION_RULES.md
│       ├── SHARED_VIEW_RULES.md
│       └── RETRIEVE_CONTRACT.md
└── skills/
    ├── maintain-rightmemory/SKILL.md
    ├── memory-retriever-cli/SKILL.md
    ├── rightmemory-orchestrator-cli/SKILL.md
    ├── rightmemory-auto-orchestrator-cli/SKILL.md
    ├── review-rightmemory-session/SKILL.md
    └── provider-transcript-normalizer/SKILL.md
```

`review-rightmemory-session` is an explicit repository workflow, and `provider-transcript-normalizer` is an internal transcript-adapter asset. Neither is part of the four-skill installer surface.

After install:

```text
~/.rightmemory/
├── .git/
├── MEMORY.md
├── MEMORY_<slug>.md
├── MEMORY_agent-corrections-writing.md   # present after Expression cases are admitted
├── MEMORY_agent-corrections-design.md    # present after Substance cases are admitted
├── PURSUITS.md
├── PURSUIT_<slug>.md
├── corrections.md              # created only when edit feedback is admitted
├── insight_logs/
├── update_records/             # immutable exact candidate batches
├── update_queue/               # appears when synchronized queue state exists
└── .runtime/                   # machine-local runtime state

~/.codex/skills/
├── maintain-rightmemory/SKILL.md
├── memory-retriever/SKILL.md
├── rightmemory-orchestrator/SKILL.md
└── rightmemory-auto-orchestrator/SKILL.md
```

On native Windows, the default memory root is `~\.rightmemory`, and the CLI shim
is `%LOCALAPPDATA%\RightMemory\bin\rightmemory.cmd`.

## Design Notes

- Memory and Pursuit are separate document trees in one globally addressable graph; Agent Corrections is a third, non-graph semantic module.
- Human readability is useful, but agent retrieval is the primary design center.
- `MEMORY.md` and `PURSUITS.md` remain useful documents, not routing-only indexes.
- Approval-gated and automatic orchestration share one evidence bar and submit at natural boundaries; the unified updater owns final admission, wording, placement, and lifecycle transitions.
- Automatic state edits remain owned by dedicated RightMemory roles; direct host-agent edits require explicit selection of `maintain-rightmemory`.
- Dreamer consolidation and Insight reflection are explicit because structural cleanup and reflective artifacts have different authority boundaries.
- `corrections.md` stays RightMemory Edit Feedback rather than becoming Memory, Pursuit, Agent Corrections, or graph content.

## License

Copyright 2026 RightL.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
