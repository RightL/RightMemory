# MF And MQ Shared View Redesign

## Purpose

RightMemory shared views need a clean product boundary. The current design mixes
published file snapshots, direct provider-root filesystem reads, hub-side search,
provider retriever prompts, and interaction notes under one `M#` concept. That
is hard for users to understand and hard for agents to use correctly.

This redesign replaces the single shared-view marker with two explicit shared
view types:

- `MF#`: mirrored file shared view.
- `MQ#`: provider question shared view.

All shared-view transport goes through HTTP, even when provider and consumer are
on the same machine. Direct `--provider-root` access is removed.

## Goals

- Make file sharing and provider-side questions separate concepts.
- Remove direct provider-root reads and generic shared-view retrieval.
- Let `MF#` retrieve use ordinary local file reading after silent HTTP sync.
- Let `MQ#` ask a provider synchronously without becoming a queued note system.
- Move durable semantics into the canonical schema where possible.
- Keep role prompts small and focused on role behavior.
- Replace the manual flag-heavy builder with natural-language agent builders
  that write reviewable view artifacts.

## Non-Goals

- No compatibility or migration path for old `M#` shared views; this feature is
  still in development and has no real users.
- No bidirectional interaction threads.
- No queued `MQ#` ask results.
- No hub-side search as the normal `MF#` retrieval path.
- No encrypted Git transport in this redesign.

## Core Concepts

### `MF#` Mirrored File View

An `MF#` heading records a local relationship to a provider-owned file
projection. The provider builds and publishes a package. The consumer silently
pulls the package into the existing import location before retrieve runs:

```text
.runtime/shared_views/imports/<mf-id>/
```

After pull, the retrieve agent reads the imported files with normal file tools.
There is no `MF#` hub-side query/search step.

### `MQ#` Provider Question View

An `MQ#` heading records a local relationship to a provider-owned question
endpoint. The consumer can ask the provider a live question through HTTP. The
provider side owns memory access, token spend, timeout policy, and private
retrieval instructions.

`MQ#` is synchronous ask-or-unavailable. It is not an async queue. If the user
wants durable follow-up, they use `shared-view note`.

## Schema

The canonical schema in `skills/rightmemory-schema.md` should define `MF#` and
`MQ#` as first-class heading markers.

Addressable heading examples:

```md
### Auth API Files {MF#auth-api-files} -> [rel:frontend-login]
### Auth API Questions {MQ#auth-api-ask} -> [rel:frontend-login]
```

Schema rules:

- `F#`, `S#`, `MF#`, and `MQ#` headings all use the slug as the graph id.
- Edges target `rel:auth-api-files`, not `rel:MF#auth-api-files`.
- `#`, `##`, and `###` are normal addressable heading levels.
- `####` is a terminal reference heading level.
- `####` may use `F#`, `S#`, `MF#`, or `MQ#`.
- No structural child content belongs under `####`: no child headings and no
  node lines.
- `MF#` means mirrored file shared view. Resolver metadata and credentials live
  outside memory prose.
- `MQ#` means provider question shared view. Resolver metadata and credentials
  live outside memory prose.
- Heading bodies record local relationship meaning: who or what the view
  represents, when to use it, and how it relates to nearby work.
- Heading bodies must not store HTTP URLs, credentials, provider prompts, or
  transport internals.

Validator changes:

- Parse `MF#` and `MQ#` heading markers.
- Remove `M#` support.
- Allow `F#`, `S#`, `MF#`, and `MQ#` at `####`.
- Preserve duplicate id and edge-target validation across ordinary, file,
  skill, file-view, and question-view headings.
- Enforce that `####` headings are terminal.

## `MF#` Lifecycle

Provider setup:

1. User asks for a file view in natural language.
2. Builder agent inspects provider memory and writes a reviewable recipe.
3. User approves the recipe.
4. The recipe becomes the source of truth for that file view.

Provider files:

```text
shared_views/<view-id>/
  view.md
  recipe.toml
  dist/
    MEMORY.md
    manifest.toml
```

`view.md` is the public contract. `recipe.toml` is the deterministic projection
recipe. `dist/` is generated output.

After every successful provider memory write, RightMemory rebuilds and publishes
affected `MF#` views:

```text
provider memory write commits
  -> find affected MF# recipes
  -> rebuild package from current memory
  -> publish new HTTP version
```

This avoids dirty flags, manual publish as the normal path, and local-fresh but
remote-stale states. Safety comes from approving the recipe before automatic
publishing begins.

Consumer retrieve:

```text
rightmemory retrieve
  -> load MF# connections
  -> pull latest packages over HTTP into .runtime/shared_views/imports/<mf-id>/
  -> if pull fails and a local import exists, keep the last import
  -> do not add sync status to retrieve prompt or session history
  -> start retrieve agent
  -> retrieve agent reads imported files with normal file tools
```

If pull fails and no local import exists, the `MF#` file surface is unavailable.
That status should not be injected into retrieve session history.

Manual debug commands:

```bash
rightmemory shared-view pull <mf-id>
rightmemory shared-view status <id>
```

## `MQ#` Lifecycle

Provider setup:

1. User asks for a provider question view in natural language.
2. Builder agent writes the public view contract and provider-private retrieval
   policy.
3. User approves the view.

Provider files:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  question.toml
```

`view.md` is public. `retriever.md` is provider-private and is used only by the
provider-side answering agent. `question.toml` stores question endpoint policy,
such as allowed consumers, provider agent settings, and timeout behavior.

Consumer ask:

```bash
rightmemory shared-view ask <mq-id> "question"
```

Ask behavior:

- The HTTP request reaches the provider question endpoint.
- The provider must start processing within a default ten-second start window.
- If the provider does not start, return currently unavailable.
- If the provider starts, wait until the answer returns or a three-minute
  timeout is reached.
- If the provider started but does not finish in time, return unavailable.
- No queue is created.
- No request id or later polling is created.

Web UI should expose the same operation:

```text
Shared Views -> Ask -> choose MQ# view -> submit question -> answer/unavailable
```

## Retrieve And Orchestration

`rightmemory retrieve` should not expose an `ask_shared_view` tool. Retrieve
must stay a read-oriented memory operation.

Retrieve behavior:

- For relevant `MF#`, read local synced imported files with ordinary tools.
- For relevant `MQ#`, report that provider-question context may be useful.
- The `MQ#` report includes the local `mq_id` and brief local relationship
  context from memory.
- Retrieve does not invent a suggested question and does not call the provider.

Main agent or orchestrator behavior:

- After retrieve, if an `MQ#` provider question view would materially help the
  current task, the main agent may call:

```bash
rightmemory shared-view ask <mq-id> "question"
```

This keeps provider token spend outside retrieve session history and lets the
main agent phrase the question from the actual task context.

## Builder

The manual flag-heavy builder should be replaced by natural-language builder
commands.

File view builder:

```bash
rightmemory shared-view build-file <view-id> "Expose auth API integration context for frontend agents"
```

The builder writes `view.md`, `recipe.toml`, and generated preview output under
`dist/`.

The recipe should support:

- include heading subtree by id;
- include individual node by id;
- include memory or detail file;
- exclude heading or node;
- render expanded heading subtrees.

Question view builder:

```bash
rightmemory shared-view build-question <view-id> "Let frontend agents ask temporary auth API questions"
```

The builder writes `view.md`, provider-private `retriever.md`, and
`question.toml`. It does not generate `dist/` by default.

`export.toml` should be removed or replaced by `recipe.toml` and
`question.toml`.

## Web Studio Integration

Web Studio now has a guided Shared Views panel. The redesign should reuse that
guided surface, but replace the legacy flow rather than preserving it.

Provider flow:

- Offer separate creation paths for file views and question views.
- File view creation starts from natural-language intent and produces a
  reviewable `recipe.toml`, not primary filter-term fields.
- Question view creation starts from natural-language intent and produces
  `view.md`, provider-private `retriever.md`, and `question.toml`.
- Manual build/publish buttons are not the normal file-view lifecycle once an
  `MF#` recipe is approved; provider memory writes rebuild and publish affected
  file views automatically.
- HTTP is the only transport shown for normal sharing.

Consumer flow:

- Accepted connections should be displayed by type: `MF#` or `MQ#`.
- `MF#` offers pull/status/debug actions, but ordinary retrieval happens through
  `rightmemory retrieve` after silent sync.
- `MQ#` offers a direct Ask control that calls the same backend operation as
  `rightmemory shared-view ask`.
- The existing generic Retrieve action for connected views should be removed.
- Note remains a separate explicit feedback action for both view types.

Web API changes:

- Replace `/api/use/connections/{id}/retrieve` with `MF#` pull/status endpoints
  and an `MQ#` ask endpoint.
- Replace legacy define/build/export/publish endpoints with builder-oriented
  file-view and question-view endpoints.
- Keep credential and invitation handling, but make accepted invitation metadata
  explicit about the resulting view type.

## Interactions

`shared-view note` remains the explicit async feedback path:

```bash
rightmemory shared-view note <view-id> "message"
```

Interaction behavior:

- Notes are one-way consumer-to-provider feedback.
- Notes can target `MF#` or `MQ#` views.
- Notes are HTTP-only in the new model.
- Notes are not automatic fallback for failed `MQ#` asks.
- Provider inbox collects explicit notes, not retrieve calls, pulls, or failed
  asks.

Examples:

- `MF#`: the file view is stale, incomplete, or wrong.
- `MQ#`: follow-up is needed later, outside the synchronous ask path.

Bidirectional replies and threaded interactions are deferred in
`docs/IMPROVEMENT_IDEA_DECISIONS.md`.

## Removal

Remove outright:

- `M#` shared-view syntax.
- `--provider-root`.
- provider-root retrieval code.
- generic `shared-view retrieve`.
- hub-side search as the `MF#` retrieval path.
- `retriever.md` from `MF#` packages.
- queued `MQ#` ask behavior.
- Web Studio's generic connected-view Retrieve action.

Replace with:

- `MF#` and `MQ#` schema markers.
- `shared-view pull`.
- `shared-view ask`.
- `shared-view status`.
- natural-language `build-file` and `build-question`.

## Prompt Changes

Most semantics belong in the schema. Role prompts should stay small.

`rightmemory/prompts/retrieve.md`:

- Follow schema-defined `MF#` and `MQ#` behavior.
- For relevant `MF#`, read local synced imported files with normal read/search
  tools.
- For relevant `MQ#`, report available provider-question context with `mq_id`
  and local relationship context.
- Do not call provider question endpoints.

`rightmemory/prompts/update.md`:

- When recording durable shared-view relationships, use schema-defined `MF#` or
  `MQ#` headings.
- Keep provider content out of local memory unless it became a local decision,
  task, or consequence.

`rightmemory/prompts/dreamer.md`:

- Preserve `MF#` and `MQ#` headings as relationship/reference nodes.
- Keep their bodies focused on local use and meaning.

`rightmemory/prompts/reviewer.md`:

- Review memory changes for correct `MF#` and `MQ#` boundary preservation.

`rightmemory/prompt.py`:

- Remove old `M#` and `retrieve_shared_view` guidance.
- Let retrieve role read synced `MF#` imports through ordinary tools.
- Do not expose an `ask_shared_view` retrieve tool.

## HTTP Hub Changes

The hub remains the network boundary.

For `MF#`, the hub stores and serves published packages. Consumers pull packages
before retrieve and read them locally.

For `MQ#`, the configured HTTP question endpoint routes synchronous questions to
the provider-side answering worker. A hub may act as the HTTP routing boundary,
but the answer is produced by the provider-side worker. The provider worker
reads private provider memory and private `retriever.md`; consumers never read
the provider root.

For notes, the hub stores explicit one-way interaction records.

## Testing

Schema and validation:

- accepts `MF#` and `MQ#` at `#`, `##`, `###`, and `####`;
- rejects old `M#`;
- validates graph ids and edges against the slug;
- enforces terminal `####` behavior.

`MF#`:

- builder writes `view.md`, `recipe.toml`, and `dist/`;
- provider memory write rebuilds and publishes affected views;
- consumer retrieve pulls before model start;
- pull status does not enter retrieve prompt or session history;
- stale local import fallback works;
- no `retriever.md` appears in file packages;
- ordinary read/search tools can access allowed imported package files.

`MQ#`:

- builder writes `view.md`, `retriever.md`, and `question.toml`;
- CLI ask returns answer when provider starts and finishes in time;
- CLI ask returns unavailable when provider does not start within the default
  ten-second start window;
- CLI ask returns unavailable when provider starts but times out;
- retrieve reports `MQ#` availability without asking or suggesting a question;
- Web UI and CLI use the same backend ask operation.
- Web Studio guided flow creates file/question views separately and no longer
  exposes the generic connected-view Retrieve action.

Removal:

- `--provider-root` is gone;
- generic `shared-view retrieve` is gone;
- provider-root retrieval tests are removed or rewritten under the new model.
