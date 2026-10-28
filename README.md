# RightMemory

**Context, guidance, and reusable work that help agents need less repeated direction.**

RightMemory helps agents carry useful understanding from one session to the next: context about the user and other people, guidance learned from collaboration, and experience that makes complex work easier to repeat. One daily entry gathers and retrieves evidence; three domain curators turn it into useful long-term state. The user also owns a separate Pursuit map of ongoing directions.

Memory and Pursuit remain Markdown document trees in one addressable graph. Guidance, bounded correction cases, work experiences, and skills remain inspectable files alongside that graph. Shared views and Git sync support continuity across sessions, devices, clients, and collaborating teams.

## Why RightMemory

- **Less repeated explanation:** preserve circumstances, choices, reasons, and relationships that help a later agent understand what matters.
- **Work that can be repeated:** retain methods, result standards, human contributions, and resources that let a short later request produce a meaningful outcome.
- **Learning during ordinary use:** capture plausible useful evidence without requiring a finished general rule or a manual review of every candidate.
- **Clear ownership:** ordinary Memory, behavioral guidance, and reusable work have separate curators with a shared core for update and maintenance.
- **Portable, inspectable state:** retain natural-language files, relevant assets, provenance, and Git history across supported executors and devices.

## The RightMemory Semantic Model

| Domain | What it preserves | Curator |
| --- | --- | --- |
| **Memory** | Ordinary context about the user, other people, projects, choices, and circumstances. | Memory curator and `maintain-rightmemory`. |
| **Agent guidance** | Applicable behavioral guidance and useful concrete user-redirection cases. | Guidance curator and `maintain-agent-guidance`. |
| **Work experience and skills** | Useful work cases, reusable methods, and supporting resources. | Skills curator and `maintain-work-skills`. |

Each domain uses one short shared core for candidate updates and requested broader maintenance. Pursuit remains the human-owned direction map; ordinary learning does not create or change map items.

### What Belongs Where?

All useful evidence enters through one `rightmemory submit` path. Memory, guidance, and skills inspect the same candidate independently, so the submitting agent does not need to decide its final lesson or destination.

The capture threshold is lower than the threshold for a stable generalization. Preserve a useful interaction even when recurrence or its broader meaning remains uncertain. Curators compare the evidence with current state, preserve scope, and decide whether to improve guidance, retain a concrete experience, develop a skill, or make no durable change.

Pending evidence is retrievable before curation. After all three domains finish, the program removes its raw source. Curators preserve independently useful context, concrete cases, or work experience in their own state, including material that does not yet support a general rule or polished skill. Agent Corrections remains the bounded Expression/Substance case library within the guidance domain.

Canonical sources: [Memory core](rightmemory/reference/MEMORY_RULES.md), [Guidance core](rightmemory/reference/AGENT_GUIDANCE_RULES.md), [Work Skills core](rightmemory/reference/WORK_SKILLS_RULES.md), [Pursuit Rules](rightmemory/reference/PURSUIT_RULES.md), [Agent Corrections Rules](rightmemory/reference/AGENT_CORRECTION_MEMORY_RULES.md), and [Schema](rightmemory/reference/rightmemory-schema.md).

## Who It Is For

RightMemory is aimed at developers who spend serious time with coding agents and want durable context plus live continuity across new sessions, devices, and agent clients. It is especially useful when agents need to remember decisions and preferences, resume active commitments, or inspect the evidence behind past updates.

## Quick Start

To use your current assistant's native agents without a RightMemory model backend, follow [Assistant execution](docs/ASSISTANT_EXECUTION.md). It uses `rightmemory assistant context` for retrieval, the shared submit/curate controls, a daily skill, and native file tools. Use the installer’s `--mode assistant` with explicit memory-root and skill-directory paths. The default installer paths below configure the existing internal runtime.

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

The default install uses standalone mode, creates `~/.rightmemory`, installs the `rightmemory` CLI, and installs six skills into both `~/.codex/skills` and `~/.claude/skills`: `rightmemory-auto-orchestrator`, `maintain-pursuit-map`, `maintain-rightmemory`, `maintain-agent-guidance`, `maintain-work-skills`, and `rightmemory-manager`. The maintenance skills and Manager handle explicit user requests; Manager acts as a long-lived coordination conversation for a Pursuit direction in Codex App. On Windows, `~` means your PowerShell home directory. Any agent that can run shell commands, including Gemini CLI-style workflows, can call the CLI directly; the packaged skill install currently targets Codex and Claude Code.

If you want RightMemory roles to run through the Codex SDK or Claude Code CLI:

```bash
./install.sh --mode cli-agent ~/.rightmemory ~/.codex/skills
rightmemory doctor agent-cli
```

```powershell
.\install.ps1 --mode cli-agent ~\.rightmemory ~\.codex\skills
rightmemory doctor agent-cli
```

Before using the internal runtime, configure its role backend in `<memory-root>/rightmemory.toml` (by default `~/.rightmemory/rightmemory.toml`). See [Standalone Config](#standalone-config) for model endpoints and credentials, or [CLI-Agent Config](#cli-agent-config) for Codex SDK / Claude Code CLI execution. The `rightmemory mcp` transport connects your host agent to this runtime; it does not replace the configured backend or use the host's model automatically. Native assistant execution instead uses `rightmemory assistant`, including its optional `assistant mcp` transport.

For the internal runtime's MCP transport, follow [MCP setup](docs/MCP.md) to register `rightmemory mcp` as a local stdio server. Connect the server in your host and check its retrieval, Memory submission, and shared learning tools, including `rightmemory_retrieve` and the unified `rightmemory_submit`. Tool discovery does not verify backend execution or a completed update. Do not also load a RightMemory orchestrator skill in that MCP conversation; command-only clients use the CLI skills instead.

After connecting MCP, add a short instruction to your agent guidance file, such as
`AGENTS.md` for Codex or `CLAUDE.md` for Claude Code:

```markdown
Read the RightMemory MCP tool instructions, then automatically use the RightMemory MCP tools according to those instructions.
```

Read-only retrieval and approval-gated orchestration remain optional source skills. Choose one ordinary orchestration variant for a conversation; the independent maintenance skills and Manager are available alongside that choice. The shared schema and focused rule documents define valid state, while client skills define access and maintenance workflows.

Open the visual map with `rightmemory pursuit`. It starts or reuses the existing Web Studio and opens an automatic local browser session; check the intended root before editing. See [Pursuit Map](docs/PURSUIT_MAP.md) for the editor and its safety boundaries.

Web Studio starts on demand and keeps running after its launching command or browser closes. Use `rightmemory web stop` to stop it; starting it does not register Windows sign-in or boot tasks. On Windows, background processes use explicit or automatic job breakaway when permitted, and may inherit jobs that do not kill processes on close. Startup is refused when the immediate job forbids breakaway and kills processes on close; enclosing host jobs can still impose lifetime limits.

To work from a direction, right-click its node or open **More**, choose **Copy context**, and paste the Markdown into an ordinary Codex App task or conversation alongside your request. The copied background comes from the current canonical graph: the selected direction, its direct incoming and outgoing neighbors, their logical heading ancestors, and direct connections. Readable titles and relationships carry the context without internal identifiers or runtime metadata. Copying leaves the map and Git history unchanged.

Use **Manager**, the `rightmemory-manager` skill in Codex App, to make a conversation the long-lived coordination point for a Pursuit direction. Manager coordinates separate temporary worker tasks, helps decide next steps, and synthesizes results without performing substantive project work. You can pin the conversation manually in Codex App; RightMemory stores no conversation association or task status. Worker task activity does not automatically change Pursuit or Memory, and Manager invokes maintenance workflows only when you explicitly ask to change RightMemory.

Codex App owns projects, tasks, conversations, and execution. RightMemory keeps the user's directions and context in the map, with no persistent task links or status synchronization. A task's progress, failure, or completion does not change Pursuit. Web Studio accepts loopback hosts only because its automatic browser sessions are not a remote-access security boundary.

For background pruning, Insight reflections, and optional sync, start the background manager:

```bash
rightmemory watch start
```

## Demo Flow

A typical conversation asks a new agent to continue work or repeat a familiar task. The daily entry retrieves relevant Memory, guidance, experiences, skills, and raw evidence, then applies them to the current request.

At a natural boundary, the daily entry submits useful evidence once. The same interaction can improve ordinary context, guidance, and work skills independently. A configured worker processes ready domains automatically; native execution delegates them through the current assistant. A submission receipt confirms capture, and a completion receipt confirms publication. Fully processed raw candidates leave current retrieval, while useful retained material remains.

The user can later request broader maintenance through the corresponding domain skill. The Pursuit map remains under human control. See [docs/DEMO.md](docs/DEMO.md) for a recording script.

## What It Gives You

- One daily entry per executor mode, with separate Memory, guidance, and work-skills curation.
- Useful user and people context, reusable correction cases, and work experience that can mature into skills.
- Relevant raw evidence available before curation, with per-domain processing and explicit publication receipts.
- Native assistant execution or the existing standalone and delegated CLI-agent executors.
- A shared Memory/Pursuit graph with typed relationships and source-authored retrieval.
- Multi-device continuity, controlled sharing, and Git history without silent migration of existing roots.

## Install Options And Updates

For a custom memory root or skill target:

```bash
./install.sh ~/.rightmemory ~/.codex/skills
```

```powershell
.\install.ps1 ~\.rightmemory ~\.codex\skills
```

CLI-agent mode delegates role execution to the Codex Python SDK or Claude Code CLI while preserving the same `rightmemory` command surface. The Codex SDK includes a pinned local Codex runtime and talks to it through App Server:

```bash
./install.sh --mode cli-agent ~/.rightmemory ~/.codex/skills
```

```powershell
.\install.ps1 --mode cli-agent ~\.rightmemory ~\.codex\skills
```

Install creates semantic state only when bootstrapping a new root. A fresh root receives `MEMORY.md`, `PURSUITS.md`, and the tracked root `.gitignore` control-plane allowlist, then Git baselines the complete synchronized state. The schema and semantic rules live with the installed RightMemory package rather than in each Memory or skills root. A valid, complete pre-existing root is preserved byte-for-byte; reinstall may refresh package-owned runtime files and installed skills, but it does not refresh the allowlist or examples, synthesize missing semantic files, or migrate user state. A valid, complete pre-existing Markdown root without Git history is committed exactly as found.

RightMemory targets the current model rather than maintaining compatibility with every historical format. When an upgrade exposes old state that needs cleanup, use [Updating an Existing RightMemory Root](docs/UPGRADING.md) to give an agent the current destinations and meaningful changes. The agent can inspect the user's actual root and make the requested transition; the product does not need a data-conversion pipeline for each older version.

Reinstall validates current state and protects unfinished curation before replacing the runtime. It refuses incomplete or invalid roots before installation writes. A live configured worker may finish its current task at a safe boundary; any remaining native, resumable, or unfinished-publication task blocks runtime replacement. Resolve that task through its current controls before trying again. Existing memory files remain protected even when they are not currently reachable from `MEMORY.md`; installation does not treat them as disposable examples.

The memory root must be a standalone, non-bare Git working tree. An existing target nested inside another working tree, a bare repository, or an unusable `.git` entry is refused before installation writes anything.

Reinstall refreshes the selected daily entry and the three domain maintenance skills. Internal modes also install map maintenance and Manager. Switching modes removes an exact current installer-managed copy of the other daily entry; customized skill directories remain untouched. Historical templates, data, and reference copies are not scanned or cleaned by the installer. Use the upgrade explanation for agent-assisted cleanup when needed.

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
`rightmemory.toml`, `.runtime/`, change history, watcher state, shared candidates, curation
tasks, sessions, and insight logs.

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
| MCP memory adapters | Tool integration | A file schema and command runtime exposed through a local [MCP stdio adapter](docs/MCP.md) |

## Document Model And Reference

Memory and Pursuit are represented as ordinary Markdown document trees in one graph:

- `MEMORY.md` and `MEMORY_<id>.md` hold ordinary knowledge, personal context, preferences, decisions, and constraints.
- `PURSUITS.md` and reachable `PURSUIT_<id>.md` files hold the user's direction map and ordered Focus references.

Guidance, work experiences, and skills live under `learning/` outside the graph. Agent Corrections retains its two fixed, non-graph files for concrete Expression and Substance cases.

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

A Pursuit item is an addressable heading with a title, position in the tree, and optional free-form Markdown body. The body can explain meaning or stable entry context; it has no required task fields. The user decides when to create, rename, move, focus, or remove directions. When a direction is completed, abandoned, or superseded, remove it through an authorized map edit after considering independently durable consequences for Memory. Git preserves earlier map states.

Common Memory domains include projects, responsibilities, and `# User Context`, with relevant circumstances, relationships, choices, and reasons. New behavioral learning is curated under `learning/guidance/`; existing Cross-Session Agent Behavior remains readable until a concrete scoped maintenance task addresses it.

### Headings

`#`, `##`, and `###` are normal tree layers and may contain Memory or Pursuit content. They can have `{#slug}` anchors and heading-level edges when the whole subtree is useful as a graph target.

Addressable `#`, `##`, and `###` headings may also have body paragraphs directly under the heading. Those paragraphs describe the heading itself. Use a heading body when the text explains the whole concept; in Memory, use child nodes when a fact should stand on its own.

Use `{F#slug}` instead of `{#slug}` when a heading is backed by a sibling parsed detail file. F# is root-relative: Memory maps to `MEMORY_<slug>.md`, while Pursuit maps to `PURSUIT_<slug>.md`. Graph edges still target `slug`, not `F#slug`.

`####` is the deepest heading level allowed in a parsed graph file. A `#### Topic {F#slug}` heading points to the root-relative detail file; it may have body paragraphs that summarize or explain that file, but no nodes or child headings in its containing file.

### Memory Nodes

Memory nodes are addressable statements under a heading:

```md
- `<node-id>` <description> → [edge1, edge2, ...]
```

Heading ids and Memory node ids share one namespace across both trees. A Memory node with no edges still writes `→ []`; a heading with no edges may omit the edge list. Pursuit map items use headings, with ordinary Markdown for their bodies. Graph-node bullets are invalid in Pursuit. Its `Focus` entries are standalone heading-id references, not graph nodes.

### Detail Files

Any `#`, `##`, or `###` heading can use its id as a parsed detail-file target by writing `{F#slug}`. For example, Memory `{F#alpha-runtime}` maps to `MEMORY_alpha-runtime.md`; the same form in Pursuit maps to `PURSUIT_alpha-runtime.md`.

Move Memory child content into a detail file when a heading becomes too dense, especially past about 15 direct node lines. Count only direct node lines, not child headings or `####` pointers. After moving content out, keep the F# heading and optional summary in the parent file.

The Pursuit editor creates `F#` boundaries automatically when a logical branch would exceed the physical heading limit. The canvas still shows one tree. Existing boundaries are retained rather than repeatedly splitting and inlining unchanged branches.

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

Each collection is a priority-curated set of at most 10 entries and 180 non-empty lines, with at most 16 non-empty lines per entry and 200 characters per line; it is not an append-only log or FIFO. Ordinary Retrieve can select relevant entries through `AC#writing` and `AC#design`. The `rightmemory agent-corrections writing` and `rightmemory agent-corrections design` commands remain for an explicitly requested whole-collection review, not a separate ordinary-retrieval pass. The guidance curator owns these collections and avoids duplicating the same case in another library. Generalized guidance belongs under `learning/guidance/`; graph-linked S# instruction resources remain supported.

Root `corrections.md` is synchronized RightMemory Edit Feedback, not semantic RightMemory state, graph content, or Agent Corrections. Each entry preserves the relevant candidate text, proposed edit, and accepted edit under [the edit-feedback rules](rightmemory/reference/RIGHTMEMORY_EDIT_CORRECTION_RULES.md); [the example](RIGHTMEMORY_EDIT_CORRECTIONS.example.md) illustrates the format. Curators form a tentative judgment before consulting relevant edit feedback as a late check. Curation records processing identities and hashes, while Git supplies the corresponding semantic diff and earlier evidence. Completed raw candidates are not retained as a second active evidence archive.

## Agent Roles

Choose the daily entry for the executor in use: `rightmemory-assistant` delegates to native agents; `rightmemory-auto-orchestrator` uses the configured runtime; internal MCP supplies the corresponding tool instructions. These are ways to perform the same ordinary work.

| Domain | Update | Requested maintenance |
| --- | --- | --- |
| Ordinary Memory | Reconcile shared candidates into useful context | `maintain-rightmemory` |
| Behavioral guidance and correction cases | Reconcile shared candidates into applicable guidance or cases | `maintain-agent-guidance` |
| Work experiences and skills | Preserve useful experience and improve reusable methods | `maintain-work-skills` |
| Pursuit map | Ordinary learning leaves it unchanged | Human editor or `maintain-pursuit-map` |

One `rightmemory submit` call records evidence for all three curators. Submission does not set final wording, classification, or a destination file. Session ids provide provenance; they are not task identities.

Configured submission wakes one curation worker, which runs ready domains through their configured executors. Native execution uses model-free `curate begin`, delegates the prepared worktree to a native agent, and calls `curate complete`. Updates process bounded batches; remaining candidates stay pending for later batches. Each domain can complete without a semantic edit. The program removes a raw candidate after all three domains finish, leaving whatever context, guidance, cases, or experience the curators retained.

All three maintenance skills use the same preparation and completion controls with `--mode maintain --scope`. Maintenance starts from scoped existing material and does not consume pending candidates. The program validates allowed changes and controls publication; agents use the domain's shared core to judge the content.

Retrieve considers relevant graph context, guidance, correction cases, experiences, skills, and pending evidence. Current user instructions and current evidence take precedence over stale or overbroad stored interpretations. Applicable guidance should affect the work itself.

`rightmemory-manager` coordinates separate worker tasks for a Pursuit direction and uses the appropriate maintenance workflow when the user asks for changes. Insight, Historian, Pruner, and sync repair retain their bounded responsibilities. Optional read-only and approval-gated source skills remain available for clients that explicitly choose them; they are not installed as additional daily entries.

## Prompt Sources

RightMemory keeps role behavior in one canonical prompt set under `rightmemory/prompts/`:

```text
rightmemory/prompts/retrieve.md
rightmemory/prompts/update.md
rightmemory/prompts/guidance-curator.md
rightmemory/prompts/skills-curator.md
rightmemory/prompts/insight.md
rightmemory/prompts/historian.md
rightmemory/prompts/pruner.md
rightmemory/prompts/sync-reconciler.md
```

The internal executor modes use these files through the `rightmemory` runtime; native assistant tasks receive the same domain cores and role instructions. Standalone mode loads them into the local Pydantic AI agent and tool loop. CLI-agent mode wraps the same role instructions into prompts sent through the Codex SDK or Claude Code CLI. Other command-capable agents can call the same CLI or daemon surface without changing the schema. The automatic orchestrator skill owns the host agent's retrieval decision, evidence threshold, and natural-boundary timing; the canonical prompts and shared domain cores own the bounded retrieval and curation roles behind its commands. Optional source skills define their narrower workflows when installed manually.

Package-owned references complement the prompts: the schema defines representation; Memory, Agent Guidance, Work Skills, Pursuit, Agent Corrections, RightMemory Edit Feedback, and Shared View rules define focused semantics; and the [Retrieve runtime contract](rightmemory/reference/RETRIEVE_CONTRACT.md) owns Retrieve's input snapshot and terminal-selection mechanics. Each requested domain maintenance skill uses its corresponding package-owned core.

## Install Modes

RightMemory has three install modes. The default is `standalone`.

| Mode | Use When | What Gets Installed |
| --- | --- | --- |
| `standalone` | You want RightMemory to run its own local Pydantic AI role agents and tools. | The CLI, automatic daily entry, three domain maintenance skills, map maintenance, and Manager. |
| `cli-agent` | You want RightMemory to delegate each runtime role turn to the Codex SDK or Claude Code CLI. | The same CLI and skills, with delegated role execution. |
| `assistant` | You want the current assistant to supply native agents and file tools. | The CLI, native daily entry, and all three domain maintenance skills. Explicit root and skill paths are required. |

The installer arguments are:

```bash
./install.sh [--mode assistant|cli-agent|standalone] [<memory-root> <skills-target>]
```

```powershell
.\install.ps1 [--mode assistant|cli-agent|standalone] [<memory-root> <skills-target>]
```

- `<memory-root>` is where Memory, Pursuit, optional Agent Correction collections, optional RightMemory Edit Feedback, learning sources, sharing state, and `insight_logs/` live.
- `<skills-target>` is where your agent loads skills from, such as `~/.claude/skills` or `~/.codex/skills`.
- With no path arguments, the installer uses `~/.rightmemory` and installs `rightmemory-auto-orchestrator`, `maintain-rightmemory`, `maintain-agent-guidance`, `maintain-work-skills`, `maintain-pursuit-map`, and `rightmemory-manager` into `~/.codex/skills` and `~/.claude/skills`.

All modes require `git` and `uv`. On macOS, Linux, and WSL, the runtime is
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
preservation, Git setup, runtime installation, and skills have
one implementation. On its supported platforms, the Codex SDK supplies its
matching native Codex runtime. Claude CLI discovery continues to support native executables and,
on Windows, standard npm `.cmd` shims with matching `.ps1` shims.

On reinstall, the shared installer asks a live configured curation worker to stop
at a task boundary before replacing runtime files. If the worker cannot stop
safely, or a task remains unfinished, installation refuses runtime replacement.
After a successful internal-mode install, pending work is resumed with the newly
installed Python. Assistant mode leaves execution to native agents.

RightMemory can keep the same memory root available across laptops, desktops,
and agent clients. The current managed sync implementation uses a private Git
remote underneath; the user-facing feature is multi-device memory continuity.
Enable the sync loop when you want the runtime to pull before automatic
semantic work and push successful memory changes after they land.

## Everyday Use

1. Let the installed daily entry retrieve context and submit useful evidence according to its instructions. Use an optional ordinary orchestration variant when you want read-only or approval-gated access.
2. Let the configured worker or native agents process ready domains. For requested broader maintenance, use `maintain-rightmemory`, `maintain-agent-guidance`, or `maintain-work-skills`. The internal runtime's `watch start` still serves pruning, Insight, and optional sync.
3. Open `rightmemory pursuit` or explicitly request `maintain-pursuit-map` to change your direction map. Use a node's **Copy context** action to bring its background into Codex App, where you can set up a long-lived Manager conversation for that direction to coordinate worker tasks. Ordinary updates leave the map unchanged.
4. Use `rightmemory status` for watcher, curation, and sync state, or `rightmemory curate status` for curation tasks and pending evidence.

The three domain maintenance skills let your host agent improve the requested state using each domain's shared learning core. Insight commits timestamped reflections under `insight_logs/` when broader patterns, risks, or next-step ideas are worth preserving.

## Command Runtime

Every executor uses the same submission and curation lifecycle. `curate begin` prepares a task without a model client, while `curate run` executes it with the configured role backend. Native assistant execution uses begin/delegate/complete. Optional source skills can call the same surface when installed manually.

```bash
rightmemory retrieve --session <agent-session-id> "find memory about the standalone mode"
rightmemory submit --root <memory-root> --session <agent-session-id> --request-id <id> --file <evidence.txt>
rightmemory curate --root <memory-root> status
rightmemory curate --root <memory-root> run --domain memory --mode update
rightmemory curate --root <memory-root> begin --domain skills --mode maintain --scope "improve the report workflow"
rightmemory curate --root <memory-root> complete --task <task-id>
rightmemory curate --root <memory-root> begin --domain skills --retry <task-id>
rightmemory curate --root <memory-root> withdraw --candidate <candidate-id>
rightmemory curate --root <memory-root> worker status
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
rightmemory insight chat
```

Submission uses the executor selected by installation: assistant mode is native;
internal modes are configured. `--executor native|configured` makes this explicit.
Reuse `--request-id` and identical evidence for an interrupted submission. A withdrawal
prevents further processing of a pending candidate; it does not undo conclusions
already published by a curator.

For machine callers:

```bash
rightmemory retrieve daemon --stdio-json
rightmemory insight daemon --stdio-json
```

The daemon reads JSON lines from stdin and writes JSON lines to stdout:

```json
{"message":"find memory about the standalone mode"}
```

The runtime is intentionally small:

- Standalone mode uses `pydantic_ai.Agent` as a chat-like agent loop.
- CLI-agent mode delegates the same role turn to the Codex SDK or Claude Code CLI. Retrieve keeps one active provider fork mapping per logical session under `<memory-root>/.runtime/agent_cli_sessions/` and uses a reusable internal provider conversation that holds its stable prefix; other independent role commands are one-shot. Each Codex role turn uses a short-lived SDK/App Server connection so the completed helper thread has no long-lived writer.
- Agent-based retrieve uses complete typed reads for local F# details and S# skills, line-numbered reads for local M# evidence, typed progressive reads for validated MF# graphs and their F#/M#/S# resources, and fixed `AC#writing` / `AC#design` sources for complete Agent Correction entries. CLI-agent emits the same selector as strict JSON. The shared runtime uses the canonical index and Retrieve contract to resolve ids, permitted ranges, hierarchy, source positions, and source-authored Markdown.
- Embedding retrieve uses the same CLI and MCP entrypoints, with a configurable model service and an incrementally refreshed local source index. It ranks each complete query independently and returns a bounded list of original source entries for the calling agent to judge.
- `~/.rightmemory` is the default memory root, and all tool paths must stay inside the configured memory root. Set `RIGHTMEMORY_ROOT` to use a different no-profile root, or use `--profile <name>` / `.rightmemory-profile` for project-specific roots.
- Retrieve, Memory Update, guidance and skills curation, history, insight, pruner, and sync repair have separate runtime boundaries selected by command line, curation task, or watcher.
- Role-specific executor settings are read from `<memory-root>/rightmemory.toml`.
- Standalone calls with `--session` persist exact Pydantic AI message history under `<memory-root>/.runtime/sessions/<role>/`. In CLI-agent mode, only retrieve persists a provider mapping across commands. A new logical Retrieve session forks a reusable stable-prefix base, while explicit `chat` may reuse one process-local provider thread and other independent role turns start fresh threads.
- Every new CLI-agent provider conversation receives an ownership record under `<memory-root>/.runtime/agent_cli_threads/`, including Retrieve prefix bases and their forks, one-shot turns, and failed isolated work. Cleanup uses those ownership records independently of an active session mapping.
- Optional debug tracing appends live JSONL events under `<memory-root>/.runtime/debug/<role>/<session>.jsonl` without changing the canonical session history.
- Use `rightmemory status` for a read-only operational dashboard across the configured memory root. Its `Sync` section reports whether sync is configured, the upstream tracking reference, ahead/behind counts relative to the last-fetched upstream, and the latest recorded sync outcome. Sync watcher process state remains under `Managed Watches`. The dashboard also summarizes Git state, Insight trigger progress, curation tasks, pending evidence, bounded last-message previews, and file paths for full logs or state. Status never fetches, pulls, pushes, starts watchers, invokes a model, or writes runtime state. Use `rightmemory watch status` when you need the lower-level managed-watch process view.
- Use `rightmemory validate` for a read-only check of the configured root, or `rightmemory validate --root <path>` for an explicit root such as a maintenance worktree. It requires the canonical root documents, validates the complete Memory/Pursuit graph, any present Agent Correction collections, `corrections.md`, and canonical learning state, and exits nonzero on failure.
- A fresh-root install creates and baselines a tracked root `.gitignore` allowlist so Git status surfaces Memory, Pursuit, any admitted Agent Correction collections, optional root `corrections.md`, sharing metadata and provider view sources, `insight_logs/*.md`, and canonical `learning/` files; generated shared-view output stays outside the committed surface. Reinstall preserves an existing allowlist exactly. Package reference changes arrive with the installed software rather than through Memory sync.
- Use `rightmemory reference <name>` to print a canonical package-owned reference without resolving or reading a Memory root. Supported names are `schema`, `memory`, `guidance`, `work-skills`, `pursuit`, `agent-correction`, `edit-correction`, `shared-view`, and `retrieve-contract`.
- Shared submission persists a candidate once, and each domain records its own processing. Pending candidates are ordinary `L#` retrieval sources. Fully processed evidence is removed by program finalization.
- Domain curation, Insight, and Pruner use isolated Git worktrees. Runtime validates complete role-owned results before landing them.
- Standalone daemon context is preserved with Pydantic AI message history.
- The local [MCP stdio adapter](docs/MCP.md) exposes the same runtime, shared candidate store, and curation lifecycle as the CLI.

### Curation Execution and Recovery

Memory, guidance, and skills accumulate pending evidence independently. Their shared
batch settings live in `rightmemory.toml`:

```toml
[curation.batch]
trigger_candidates = 15
target_batch_candidates = 30
max_wait_seconds = 86400
```

A domain becomes ready as soon as it has 15 pending candidates, including submissions
from sessions that are still active. Below that threshold, each session has a one-hour
quiet period from its latest pending submission. The oldest quiet session starts a
batch after a further 24 hours, so small queues can make progress. A new submission
resets its own session's quiet period; other sessions keep their deadlines.

When a trigger is reached, the program records the eligible evidence for that domain.
Each run processes part of that evidence, up to 30 candidates or 24,000 evidence
characters. The first candidate is included even when it exceeds the character limit.
Remaining recorded evidence stays ready for the next run, including across restarts
and devices. Submissions arriving later wait for their own count or time trigger.

Configured submission wakes a single background worker. It processes ready domains
and waits for the next deadline without invoking a model. New submissions interrupt
that wait, and installation can stop the waiting worker. The native path exposes the
same readiness on tool calls for the main assistant to delegate; it does not run an
unattended timer. `begin` and `run` respect batching, while maintenance and recovery of
an existing task do not wait for new candidates.

`rightmemory curate status` reports pending evidence, waiting deadlines, and active tasks. A task retains
its candidate identities across retries. After an interrupted completion or lost reply,
inspect the same task and retry `complete`; a prepared result can publish without
repeating semantic work. `effects_pending` likewise calls for completion recovery,
not another model turn.

Prefer resuming the original task. When edits must be abandoned or reconciled anew,
confirm the previous worker has stopped, then use
`curate begin --domain <domain> --retry <task-id>`. This can explicitly take over a
claim from another checkout. Replacement fences the old publication and does not
carry over unpublished edits. Claims are per domain and do not expire on a timer.
The configured worker makes at most two automatic attempts at the same work, waiting
one hour after the first failure. The retry time survives restarts and appears in
worker status; other ready domains can proceed meanwhile. A failure before a task is
created belongs to that pending evidence, so a later submission does not inherit an
exhausted retry budget. After resolving a failure, `curate worker wake` permits another
attempt; a deliberate task retry gets a new attempt budget.

Batch records, claims, processing receipts, and withdrawals are program-owned. Receipts retain
candidate identities and hashes, not another copy of the evidence body. Completing the
last domain removes raw evidence from current state, including for no-change judgments.
Curators retain useful concrete cases or experiences before completing their work.
Git history is not rewritten by that removal.

### CLI-Agent Config

CLI-agent mode uses a global provider plus role model settings. Most installs need a retrieve model and a default writer model; guidance-curator, skills-curator, insight, pruner, historian, and sync repair reuse the writer config unless you override them.

A minimal Codex setup:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5.6-luna"
reasoning_effort = "high"

[update.agent_cli]
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"
```

`reasoning_effort` is an optional Codex-only role setting. RightMemory passes it on each SDK turn, so roles can use different effort levels without changing the user's global Codex config. Accepted values are `minimal`, `low`, `medium`, `high`, and `xhigh`.

Add a role-specific table only when a role should use a different model or provider:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5"

[insight.agent_cli]
provider = "claude"
model = "sonnet"
```

Use `rightmemory doctor agent-cli` after configuring CLI-agent mode. It checks that role config resolves to CLI-agent execution, the Codex SDK runtime or configured Claude command is available, and read/write role probes can complete.

CLI-agent Retrieve keeps one reusable internal prefix-base provider conversation for each exact combination of canonical role instructions, stable Memory/Pursuit/Agent Correction snapshot, and effective provider configuration. RightMemory initializes the base once with an empty-selection turn, then Codex or Claude forks it for each new `rightmemory retrieve --session <id>` session. The bootstrap result is internal and does not enter that session's delivery coverage.

If prefix initialization or forking fails, RightMemory retires that base and starts the request in a fresh conversation containing the complete prefix.

Each logical Retrieve session owns its provider fork, conversation history, and delivery coverage. Later calls resume that fork and append snapshot diffs, newly submitted candidates, and the current query after the inherited snapshot; RightMemory does not replay local prior questions and answers. A different exact prefix is assigned a different base, while existing sessions continue on their own forks.

Every other independent CLI-agent role turn starts a fresh provider conversation. An explicit `chat` process may keep one in-memory conversation until that process exits, but it does not create a mapping for another process to resume. This policy is the same for Codex and Claude.

RightMemory records prefix-base and fork ownership. Ordinary role threads expire after one hour without successful activity, while reusable prefix bases expire after 24 hours; successful child activity refreshes its base. An explicit `chat` process leases its current thread until that process exits. After each Codex role turn, RightMemory unsubscribes and closes the turn's connection, then immediately archives the helper thread so it stays out of the Codex task list. A stored thread is unarchived only while RightMemory resumes or forks it and is archived again when the turn ends. Retention still controls deletion: cleanup removes due children before their bases and retains a base while an active child references it. It runs opportunistically before top-level CLI-agent work. During idle periods, expired threads remain until the next eligible activity or a manual cleanup pass. Run a bounded diagnostic pass directly with:

```bash
rightmemory agent-cli cleanup --once
```

Cleanup first detaches an expired Retrieve mapping and resets its local delivery state, then deletes the exact owned Codex thread through App Server. Failures do not fail the role command and pending records are retried. RightMemory never edits Codex history files or SQLite state directly, never imports old unregistered threads, and never deletes Claude sessions automatically.

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

Agent-based retrieve retains native per-session model history and asks the model not to reselect unchanged content it already returned. A terminal model selection is always rendered faithfully. `rightmemory retrieve --include-returned --session <id> "<query>"` attaches the current authoritative forms of previously returned graph items, linked sources, and Agent Correction entries to that call's retrieval context without clearing accumulated coverage; later calls return to the normal context policy. Changed content at the same address is surfaced as changed. `max_output_chars` is a safety limit: oversized selections are rejected for model retry rather than truncated.

Anthropic-compatible Insight config:

```toml
[insight.model]
model_id = "anthropic/example-insight-model"
api_base = "https://api.example.com/anthropic"
api_key = "<token>"
```

`model_id` is required for each explicit `[<role>.model]` table. `anthropic/...` model ids use `AnthropicModel`; other model ids use `OpenAIChatModel` with `OpenAIProvider`, so OpenAI-compatible local gateways can use `api_base` and `api_key`. `[<role>.model.kwargs]` is forwarded as Pydantic AI model settings and unsupported keys fail fast.

Normalized `deepseek-*` model ids keep that configurable OpenAI-compatible transport while using Pydantic AI's DeepSeek profile, so thinking-mode tool loops avoid forced tool selection and preserve `reasoning_content` between requests.

Standalone configs use role-local model tables such as `[retrieve.model]`, `[update.model]`, `[guidance-curator.model]`, `[skills-curator.model]`, `[historian.model]`, `[insight.model]`, and `[pruner.model]` for the roles you run. In the common setup, configure `[retrieve.model]` for search and `[update.model]` as the default writer model. Other non-retrieve roles reuse the writer model unless you give them their own table.

Configure `[sync-reconciler.model]` or `[sync-reconciler.agent_cli]` only if sync repair should use a different model from the default writer.

### Embedding Retrieval

Embedding retrieval is an optional alternative to the agent-based retriever in either installation mode. One complete query goes to the selected embedding model, **Nemotron 3 Embed 1B** (the default) or **Jina Embeddings v5 Text Nano Retrieval**, which selects forty source entries; **Jina reranker v3.5** ranks those candidates and RightMemory returns up to ten. There is no relevance-score cutoff or additional LLM selection call. Related entries can be returned even when Memory does not contain the requested answer; the calling agent decides which entries apply.

Add these retrieval settings, keeping the existing `[retrieve.agent_cli]` or `[retrieve.model]` table as the fallback:

```toml
[retrieve]
backend = "embedding"
max_output_chars = 100000

[retrieve.embedding]
url = "http://127.0.0.1:8766"
candidate_count = 40
result_count = 10
timeout_seconds = 60
```

The same `rightmemory retrieve --session <id> "<complete need>"` command and `rightmemory_retrieve(session_id, need)` MCP tool work with this setting. Writer and other role settings remain independent. The default backend is `agent`.

If the service cannot connect, times out, or returns an error or invalid response, retrieval sends the same query to the configured agent retriever. The existing `[retrieve.agent_cli]` or `[retrieve.model]` settings supply that fallback; a global `[agent_cli]` provider also works. The agent is started only when needed and retains its normal session, rendering, and output rules, including its usual latency and token cost. The embedding ten-entry limit does not apply to its output. A warning records the fallback, with an event in the debug trace when enabled. Every subsequent query tries embedding retrieval again. Without agent settings, service failures are reported directly. Invalid Memory, source consistency failures, and local output-limit errors remain visible rather than triggering fallback; successful results are left for the calling agent to judge.

The source index comes from the canonical graph, including F# details, Pursuit context, both Agent Correction collections, valid mirrored MF# imports and their backing resources, and current pending submissions. Pending submissions retain their unsettled status. Local MQ# relationship context is searchable; live provider questions retain the separate `shared-view ask` command. Unsafe or invalid referenced sources fail retrieval instead of disappearing silently.

Retrieval output follows the agent retriever’s grouped Markdown style: shared headings and parent context appear once, original entry identifiers remain in the text, and linked material and Agent Corrections keep their source labels. Ordinary entries have no extra filename/line wrapper. Each selected graph entry retains its own source text and the parent context needed to interpret it, including applicable Pursuit Focus. Selecting a heading does not expand all its children. M# evidence uses source-addressed line passages with complete fenced blocks; S# resources return the complete skill. Learning sources use `L#` identifiers: raw evidence and SKILL.md return complete sources, while other learning Markdown can return relevant passages. A long entry can have multiple search passages but occupies one candidate/result slot: embedding search selects its best-matching passage for reranking and the original complete entry is returned. Ordinary entries remain intact up to 2,000 characters. Thus ten results bounds the entry count, not a token budget. The output character limit raises an actionable error rather than silently cutting instructions. Model input limits also fail explicitly instead of truncating a query or passage.

Each query searches the current source snapshot independently, so the caller supplies a self-contained need and repeated queries may return the same entries. `--include-returned` has no additional effect in this backend. Session bookkeeping lives under `.runtime/sessions/retrieve-embedding/`, separately from the agent retriever's history. Embedding vectors are cached under `.runtime/embedding_retrieval/`; edits, removals, ancestor changes, and embedding-model changes refresh the relevant cached material. Source changes during a model request trigger one fresh attempt before returning an error. The cache is derived state and can be rebuilt.

Install the model service on the GPU host; ordinary clients do not need these dependencies:

```bash
python -m pip install '.[embedding-service]'
rightmemory embedding-service \
  --embedding-adapter nemotron3 --reranker-adapter jina-v3.5 \
  --embedding-model /path/to/Nemotron-3-Embed-1B-BF16/snapshot \
  --reranker-model /path/to/jina-reranker-v3.5/snapshot \
  --device cuda:0 --port 8766
```

For the smaller Jina nano embedding model, select its adapter and the merged retrieval snapshot; keep the same reranker and client configuration:

```bash
rightmemory embedding-service --embedding-adapter jina-v5-nano --embedding-model /path/to/jina-embeddings-v5-text-nano-retrieval --reranker-model /path/to/jina-reranker-v3.5/snapshot --device cuda:0 --port 8766
```

Use [`jinaai/jina-embeddings-v5-text-nano-retrieval`](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano-retrieval), with the Safetensors weights, `config.json`, `tokenizer.json`, `tokenizer_config.json`, `configuration_eurobert.py`, and `modeling_eurobert.py` from the same snapshot. This adapter loads the merged retrieval weights directly; the base multi-task nano checkpoint, adapter-only weights, GGUF, and ONNX files are not substitutes. It uses `Query: ` / `Document: ` prefixes, right padding, last non-padding token pooling, and L2 normalization. The output is 768 dimensions with an 8,192-token input limit including prefixes and tokenizer special tokens; longer inputs fail without truncation. CUDA inference uses BF16, while `--device cpu` uses FP32. No additional PEFT, sentence-transformers, or flash-attention dependency is needed for this adapter. The model is licensed [CC BY-NC 4.0](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano-retrieval#license); check Jina's terms for commercial use.

After starting the service, check `http://127.0.0.1:8766/info` (PowerShell: `Invoke-RestMethod http://127.0.0.1:8766/info`). With nano selected, it reports an `embedding_model` identity starting with `jina-v5-nano:` and `dimensions = 768`. Then use the normal retrieve command above. Switching from Nemotron rebuilds the derived embedding cache on the next retrieval; the first query may take longer. This does not change the Memory source files. Real-checkpoint quality, latency, and GPU memory use should be evaluated on the model host.

Supply downloaded snapshots, for example from ModelScope. The service does not download models; it loads local files offline and keeps both models resident. The supported model interfaces are documented by [NVIDIA](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16) and [Jina](https://huggingface.co/jinaai/jina-reranker-v3.5). Both Jina snapshots include custom model code, which the service executes locally; use trusted official snapshots. The service serializes GPU requests and defaults to loopback. For direct access on a trusted private network, start it with `--host <server-private-IP>` and configure clients with `url = "http://<server-private-IP>:8766"`. Encoding sends source passages to that configured host; reranking sends the query and candidate passages.

Embedding and reranking adapters are selected independently. The default pair remains Nemotron 1B / Jina v3.5; `jina-v5-nano` changes only the embedding model. To support another model family, implement the small `EmbeddingAdapter` or `RerankerAdapter` interface in `rightmemory/embedding_models.py` and register it in the corresponding adapter dictionary. An embedding adapter returns dense vectors for cosine search; a reranker returns every candidate index in ranked order. The adapters own model loading, input formatting, and token limits. The service reports embedding dimensions, batch size, and candidate capacity, which the client uses instead of model-specific constants. A compatible external service can also implement `/info`, `/embed`, and `/rerank`.

Embedding cache identities cover snapshot contents, adapter settings, and an adapter revision. Each built-in embedding adapter records its input prefixes, pooling, normalization, precision, and library versions; bump its revision for behavior changes not represented in those settings. Changing embedding identity rebuilds the vector cache; replacing only the reranker reuses it. Adding another model family requires an adapter rather than just pointing an existing adapter at unrelated weights.

An API key is optional when binding to loopback or an explicit private IP address (RFC 1918 IPv4 or IPv6 unique-local). Without a service key, requests are accepted without credentials. To enable authentication, set `RIGHTMEMORY_EMBEDDING_API_KEY` on the service and the matching `[retrieve.embedding].api_key` on clients. Public, wildcard, and other hostname bindings require a key. The service is started separately and is not launched by the normal installer or a retrieve call.

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

RightMemory can keep pruning, Insight, and sync loops under the same background manager. The normal controls are:

```bash
rightmemory watch start
rightmemory watch status
rightmemory watch stop
rightmemory watch restart
rightmemory --profile my-project watch start
rightmemory --profile my-project status
```

By default these commands manage `insight` and `pruner`, plus `sync` when `[sync].enabled` is true. Pass a target when you want one loop, such as `rightmemory watch start insight`. Managed watcher pid files and logs live under `<memory-root>/.runtime/watch/`.

For a single read-only view of watcher state, Insight trigger
progress, curation tasks, pending evidence, and paths to the underlying logs
or state files, use `rightmemory status`. `rightmemory watch status`
intentionally stays focused on managed watch process state.

#### Curation Provenance

Each domain's publication records which candidate identities and hashes it processed.
The semantic diff remains in Git. Pending candidate files contain the submitted body;
after all domains finish, the program removes that body from current state rather than
keeping an additional raw-history collection. Curated cases and experiences preserve
useful evidence in a form chosen for later work. Existing Git commits are unchanged.

### Forgetting And History

RightMemory keeps active durable Memory intentionally perishable. `rightmemory prune` checks whether the repository has accumulated enough commits since the latest `prune:` checkpoint. The default threshold is 70 commits. `rightmemory prune watch` runs the same check periodically, and `rightmemory watch start` starts it by default. When pruning is due, the runtime supplies the pruner with the boundary commit, current head, previous prune ledger, and grace policy. The Memory-oriented pruner removes unchanged durable context only when it is no longer worth keeping, validates the complete graph, preserves ids referenced from Pursuit, and commits with a `prune:` subject.

If a due prune has nothing to remove, the pruner writes an empty `prune: checkpoint` commit. Checkpoint commits are useful because they keep generations based on work done rather than wall-clock time.

The `prune:` commit body is the lightweight ledger. It records the boundary, removed ids, revived ids under grace, and notable skips. A memory item that was pruned and then written back gets grace across two due prune checkpoints by default; after that, the pruner judges it like ordinary active memory again. The memory files do not carry lifecycle metadata.

Ordinary `rightmemory retrieve` searches the current Memory/Pursuit graph and considers relevant guidance, fixed Agent Corrections, work experiences, skills, and raw learning evidence. `rightmemory history --session <id> "query"` asks the historian to search `prune:` ledgers and Git snapshots for pruned Memory. Historian returns matches as historical context and does not write them back. When old context becomes useful again, submit the new evidence so the Memory curator can judge whether to reactivate it.

### Change-Triggered Insight Cycles

Insight can run background cycles through the manager:

```bash
rightmemory watch start insight
```

`rightmemory insight watch` checks `<memory-root>/.runtime/insight/trigger-state.json` and writes timestamped reflection artifacts under `insight_logs/` when useful. Insight reads active Memory and prior Insight logs; it edits neither Memory nor Pursuit and does not read RightMemory edit `corrections.md`. With the default `[insight.watch]` settings, each Memory-changing curation update contributes `1.0` point, the trigger threshold is `150`, and the watcher checks every `3000` seconds.

```toml
[insight.watch]
trigger_points = 150
update_candidate_points = 1.0
check_interval_seconds = 3000
```

A landed Memory-changing Update transaction adds one configured unit to Insight regardless of how many candidates the updater reconciled. No-op and failed updates add no pressure. A successful automatic cycle consumes the configured threshold after it lands or completes as a valid no-op; failed cycles preserve accumulated points. `rightmemory insight watch --interval <seconds>` overrides the trigger-check cadence.

Insight and Pruner watchers hold their own locks under `.runtime/watch/`. A competing watcher exits instead of duplicating work. Configured curation uses one worker and per-task execution ownership. Isolated roles may do model work in temporary checkouts, and landing uses the shared write lock before changing the main repository.

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

Domain curation prepares persistent isolated worktrees for native or configured agents. Each curator can change only its domain's accepted files, and program finalization validates the result, records processing, and publishes it against the current shared claim. Pursuit is outside ordinary curation's writable surface. A no-change judgment still completes processing; it does not require a model-authored record.

Insight and Pruner run in temporary Git worktrees under `<memory-root>/.runtime/worktrees/`. Pruner remains restricted to ordinary Memory, while Insight commits `insight_logs/*.md`. Runtime validates and lands complete role-owned results; empty `prune:` checkpoints are allowed.

Temporary session and provider state lives under `.runtime/isolated-state/` during an isolated turn and is promoted after successful landing or a valid no-op. Standalone turns seed local message history there. CLI-agent turns start speculative provider work in a fresh one-shot session. Successful ownership state is promoted; if later validation or landing fails, the ownership record alone is preserved so the abandoned internal thread remains eligible for cleanup. Other temporary work is discarded, and the original candidate batch or maintenance trigger balance remains the retry source.

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

Retrieve adds a small active-use safety net. At most once every five minutes it performs a pull-only upstream check before reading memory, with the fetch bounded to two seconds. A clean incoming commit is validated and admitted for the current request; the foreground path never pushes and never invokes `sync-reconciler`. Offline, timed-out, dirty, conflicting, or invalid incoming state does not block retrieval: the request uses the last valid local state, then starts one detached full sync cycle after the response finishes. That cycle owns reconciliation and push when needed, and a shared nonblocking cycle lock prevents duplicate watcher, retrieve, and deferred work. If a clean refresh brings pending candidates, configured curation is woken after retrieval completes; native execution receives readiness notices. Historical retrieval remains local.

The tracked root `.gitignore` is synchronized package-owned control-plane state rather than semantic Memory. Sync admits it through the same regular-file and path-surface checks as other synchronized files, while reinstall never rewrites an existing copy.

Shared candidates, processing receipts, claims, and withdrawal records are program-owned non-graph state. Sync validates them and mechanically merges compatible immutable evidence or records; a conflicting claim is refused rather than sent to a model. Finalization and merge remove fully processed or withdrawn raw candidates so another device cannot revive them accidentally. `sync-reconciler` may repair curated guidance, experiences, and skills within its allowed surface, while program state stays outside model edits.

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

For Codex-backed CLI-agent development, install the optional SDK extra with
`uv --cache-dir .uv-cache pip install -e ".[codex-sdk]" --python <venv-python>`.

The standalone runtime exposes sandboxed tools rooted at the configured memory root. It does not provide an OS-level jail.

## File Layout

Repository:

```text
RightMemory/
├── README.md
├── DESIGN_NOTES.md
├── docs/
│   ├── DEMO.md
│   ├── PURSUIT_MAP.md
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
    ├── maintain-pursuit-map/SKILL.md
    ├── maintain-rightmemory/SKILL.md
    ├── memory-retriever-cli/SKILL.md
    ├── rightmemory-orchestrator-cli/SKILL.md
    ├── rightmemory-auto-orchestrator-cli/SKILL.md
    ├── rightmemory-manager/SKILL.md
    ├── maintain-agent-guidance/SKILL.md
    └── maintain-work-skills/SKILL.md
```

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
├── learning/                   # pending evidence, curated sources, program state
├── insight_logs/
└── .runtime/                   # machine-local runtime state

~/.codex/skills/
├── rightmemory-auto-orchestrator/SKILL.md
├── maintain-rightmemory/SKILL.md
├── maintain-agent-guidance/SKILL.md
├── maintain-work-skills/SKILL.md
├── maintain-pursuit-map/SKILL.md
└── rightmemory-manager/SKILL.md
```

On native Windows, the default memory root is `~\.rightmemory`, and the CLI shim
is `%LOCALAPPDATA%\RightMemory\bin\rightmemory.cmd`.

## Development Checks

Run the full suite from the checkout using a Python environment with the project
and its `codex-sdk` extra installed:

```bash
python -m tests
python -m compileall -q rightmemory tests
```

The test runner uses up to 24 processes, limited to the CPU count. Workers share
a queue within selected independent test modules, taking the next test as they
finish and starting known long cases early. Each worker keeps
its own module and class setup and cleanup. Git tests use separate disposable
repositories per case. Other modules run in one process each.
Timing-sensitive Windows process checks run alone before the parallel tests.
Use `python -m tests --jobs N` to choose a lower process limit when sharing the
machine. Workers disable automatic Git maintenance for their disposable test
repositories and use an empty Git template instead of repeatedly copying sample
hooks. On Windows, workers use Git for Windows' underlying executable when
available to avoid launching an extra process for every Git command.

## Design Notes

- Ordinary Memory, agent guidance, and work skills have separate curators; Memory and the human-owned Pursuit map retain their shared graph.
- Memory supports agent retrieval; Pursuit also has a human-facing visual editor over the same canonical Markdown.
- `MEMORY.md` and `PURSUITS.md` remain useful documents, not routing-only indexes.
- The daily entry submits useful evidence at natural boundaries; each domain curator owns its judgment, wording, and placement. The user owns Pursuit semantics.
- Direct host-agent edits require an explicit maintenance request, using the workflow responsible for that content.
- Each curator shares its short core between candidate updates and requested maintenance; Insight preserves reflective artifacts under its separate runtime boundary.
- `corrections.md` stays RightMemory Edit Feedback rather than becoming Memory, Pursuit, Agent Corrections, or graph content.

See [DESIGN_NOTES.md](DESIGN_NOTES.md) for the Pursuit map's ownership and storage rationale.

## License

Copyright 2026 RightL.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
