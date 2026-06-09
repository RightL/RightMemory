# Shared View Memory Design

## Problem

RightMemory currently models structure inside one memory root: a heading tree,
addressable nodes, detail files, graph edges, profiles, and role-owned memory
operations. Profiles give different projects or contexts separate memory roots,
but they do not describe how roots owned by different people, projects, teams,
or departments collaborate.

The motivating team case is social before it is technical. A colleague, team
lead, project owner, or manager agent can share a memory link during real work:
"I own this area; use this view when you need that context." The receiving root
should be able to record that link as part of its own memory, with local
collaboration meaning, without opening the provider's private memory.

The product challenge is to let roots connect through useful, privacy-aware
shared surfaces rather than through nested repositories, direct file reads, or
an unrealistic global discovery system.

## Goals

Add a product model for cross-root collaboration:

- `M#` marks a local heading node that represents an external shared view.
- A shared view is a retrievable collaboration surface owned by another memory
  root, not the provider's whole private memory.
- The receiving root records why the shared view matters locally in the `M#`
  heading body and nearby graph context.
- A reusable View Builder helps create, filter, and maintain shared views.
- Shared views can be backed by view-specific retriever prompts, filtered
  `MEMORY*.md` files, or both.
- Collaborators can interact with shared views in natural language; the
  provider side decides whether to answer, continue discussion, update shared
  memory, update private memory, or create follow-up work.

## Non-Goals

This design does not turn RightMemory into a full team chat, task tracker, or
permissioned collaboration suite. It also avoids making `M#` a Git submodule or
filesystem mount. Git can still transport memory roots, but the cross-root
product concept is a shared view, not nested repository ownership.

The design also does not assume that roots discover each other automatically.
Connections normally arise from real collaboration: people exchange links,
manager agents set up project context, or a project initialization flow records
the relevant shared views.

## Core Concepts

### Memory Root

A memory root remains the authority boundary. It owns private memory, local
runtime state, role config, retrieve behavior, and any shared views it exposes.
A root can represent a person, project, team, department, or long-lived
collaboration domain.

### Shared View

A shared view is a collaboration-facing perspective of a provider memory root.
It may cover a project API, a person's current responsibility, onboarding
context, meeting decisions, incident context, or another scoped collaboration
surface.

A shared view is not a promise that the consumer can read the provider's private
memory. The provider root controls what the view may reveal and how the view
handles incoming interaction.

### `M#` Heading

`M#` is a heading marker. It behaves like a local addressable heading node while
pointing outward to an external shared view.

Example:

```md
### Alice Auth API {M#alice-auth-api}

Alice owns auth API collaboration context. Use this shared view for login,
token, and authorization integration questions.
```

The heading body records the local collaboration meaning: who shared the view,
what relationship it supports, when to use it, and how it relates to nearby
project or work memory. Edges can connect the `M#` heading to local nodes when
that helps retrieval.

The text after `M#` can use the same kind of human-chosen short id style that
RightMemory already uses for heading slugs and node ids. The durable design
need is stable local reference, not a globally meaningful path syntax.

## View Builder

The View Builder is the reusable capability behind shared views. It helps a
provider create or maintain a view from private memory, a request, an intended
audience, and collaboration context.

The builder can produce two kinds of backing:

### View-Specific Retriever Prompt

The builder can produce instructions for a retriever that answers through a
specific shared view. This supports policy-guided retrieve. The prompt can say
what the view is for, what to emphasize, what to omit, and how to avoid private
or irrelevant material.

For example, a backend API view prompt can focus on API contracts, test
environment details, integration risks, and known handoff issues while avoiding
personal preferences, raw debug history, and unrelated project context.

### Filtered Memory Markdown

The builder can also produce filtered `MEMORY*.md` files that materialize the
shared view. A normal retriever can then read that shared Markdown surface.

This fits cases where the shared view should be stable, auditable, easy to
review, or shared through ordinary Git-backed files.

A shared view may use a retriever prompt, filtered Markdown, or both. The
consumer root should not need to know the backing mode when it records or uses
the local `M#` heading.

## Connection Formation

The practical connection flow is link exchange, not global search.

A colleague can give a view link. A project root can record the shared views
that matter to its work. A manager agent can help a team establish the M#
headings that reflect current responsibilities and collaboration surfaces.

The receiving memory records the relationship locally:

```md
### Backend API Collaboration {M#backend-api}

The backend team uses this shared view for API integration context. Frontend
auth work should retrieve it before changing login or token flows.
```

This keeps the connection useful in the consumer's own tree and graph. The
provider still owns the shared view itself.

When an existing view is not enough, either side can ask a provider or manager
agent to build a better view. The reusable work is not "discover every root"; it
is "build the right shared view for this collaboration."

## Retrieve Behavior

When a consumer uses an `M#` heading, the runtime should route the request to
the provider's shared view rather than reading the provider's private memory
files directly.

Conceptual flow:

```text
consumer root
  -> local M# heading
  -> provider shared view
  -> shared-view answer
```

The provider root decides whether that answer is produced through
policy-guided retrieve, filtered shared Markdown, or a built/updated shared
view.

## Transport Model

Transport describes how a consumer reaches a provider shared view. It is
separate from backing, which describes how the provider creates or stores the
view content.

The product model has three transport families:

- **Hub transport:** the natural team-collaboration path. A team or company hub
  handles identity, access, invitations, retrieve calls, interactions, and
  audit. The hub can relay requests to an online provider agent/service, or it
  can host/cache published shared views so the provider does not need to be
  online.
- **Export transport:** the lightweight no-hub path. The provider exports a
  filtered shared memory surface and the consumer imports or syncs it. Git can
  be one export carrier, but export can also use local folders, archives, file
  sync, or other distribution mechanisms. Export is read-friendly and
  asynchronous; it should not assume the consumer can write back to the
  provider's repository.
- **Local transport:** the same-machine or same-owner path. A consumer can
  reach another local root through local commands or daemon calls. This fits
  personal multi-root use, development, and testing.

Transport and backing are related but not identical:

- hub + retriever prompt is natural;
- hub + filtered Markdown is natural;
- export + filtered Markdown is natural;
- export + retriever prompt is awkward unless it can call back to a provider;
- local + retriever prompt is natural;
- local + filtered Markdown is natural.

Git branch visibility should not be treated as a privacy boundary for sharing
filtered memory from a private root. When Git is used for export, the safer
shape is a separate shared repository or exported package containing only the
filtered shared memory surface.

## Minimal Hub

The hub should stay small in the product model. It is not a full chat system,
task tracker, or organization-management platform.

Its minimal registry records shared view identity and meaning:

```text
provider
view
description
```

The registry answers "who provides which shared view, and what is it for?" It
does not need user-facing fields for how to retrieve, how to interact, or
whether the provider is currently online. Those are hub runtime concerns.

Hub retrieve can work in two ways:

- **Relay:** the hub forwards a retrieve request to an online provider
  agent/service.
- **Hosted:** the hub answers from a published or cached filtered Markdown
  shared view.

The consumer should not need to know which path answered the request.

Hub interaction is intentionally lightweight: a collaborator leaves a note on a
shared view. The hub records the note, notifies or exposes it to the provider,
and lets the provider reply, update the shared view, or close it. The minimal
model should not require a full thread system, task state machine, or explicit
intent form.

Publishing to the hub follows the View Builder model: the provider creates a
shared view, publishes it to the hub, and the hub registers the provider, view,
and description. A published view may be hosted as filtered Markdown or served
through relay to the provider.

## Natural-Language Interaction

Shared views should support communication as well as retrieval. A collaborator
can speak to a shared view naturally:

```text
This response schema looks stale; the actual response includes token_expires_at.
```

```text
Frontend needs to know by Tuesday whether this field is stable.
```

The user-facing model should stay message-first rather than requiring a rigid
form with intent and state fields. The provider side can infer whether the
message is a question, correction, update suggestion, clarification, or
follow-up request.

The provider root then decides how to handle it: answer, keep talking, update
the shared view, update private memory, create follow-up work, or close it.

## Privacy And Trust Boundary

An `M#` heading does not grant access to a provider root. It records a local
reference to a provider-owned shared view.

Privacy is protected by the shared view backing:

- a retriever prompt that constrains what the shared view may return;
- filtered `MEMORY*.md` files that remove private or irrelevant content;
- provider-side handling of natural-language interactions.

The View Builder should treat privacy and relevance as part of view quality.
A good shared view is not a mirror of private memory; it is a collaboration
surface shaped for a receiver or purpose.

## Open Questions And Current Decisions

### M# Resolution

Resolution has a settled direction: the consumer root should keep a separate
link registry that maps each local `M#` heading id to a provider identity and
shared view id. The local `M#` heading stays readable memory; its body explains
the collaboration meaning, while the registry stores resolution mechanics.

The consumer-side registry should not decide how the shared view is backed. It
should not contain a prompt-vs-Markdown mode flag. The provider root owns the
shared view contract and decides whether that view answers through a
view-specific retriever prompt, filtered `MEMORY*.md` files, or both.

The remaining resolution question is provider reachability. A shared-view link
should identify the provider and view separately from the transport used to
reach that provider. The next design pass should focus on shared view
invitations and transport setup rather than asking users to hand-write local
paths, Git remotes, or service endpoints into memory prose.

The product-level object should be a shared view invitation rather than a
hand-written locator. A colleague, project owner, or manager agent gives the
recipient an invitation for a shared view. Accepting it should create or suggest
a local `M#` heading and store whatever resolver details the system needs out
of band. The user-facing choice is whether to accept the shared view and how to
describe its local collaboration meaning; local path, Git remote, or future
service endpoint mechanics belong behind the resolver.

### Shared View Storage

Filtered Markdown views need a storage shape. They could live inside the
provider root, in a sibling shared-view root, or in a generated area that has
clear Git and privacy behavior.

### Retriever Prompt Storage

View-specific retriever prompts need a storage and loading model. They may
belong with the provider root's shared-view metadata rather than in the
consumer root.

### Interaction Records

Natural-language interactions need a durable place if they are more than
ephemeral messages. The record should support review, absorption, and closure
without making every interaction a memory node.

### Schema Integration

The schema should define `M#` heading syntax, allowed levels, graph edge
behavior, and validation. This likely needs prompt/schema updates and focused
tests, and may need a semantic upgrade note because it changes how existing
memory can represent external collaboration surfaces.

## Testing And Validation

When implemented, tests should focus on durable invariants rather than prompt
prose:

- schema validation accepts `M#` headings where intended;
- graph id uniqueness and edge validation include `M#` headings;
- prompt assembly teaches retrieve/update roles the `M#` meaning;
- consumer memory can record `M#` headings without provider private memory
  access;
- View Builder outputs produce either retriever prompt backing, filtered
  Markdown backing, or both;
- interaction handling stays separate from direct provider memory writes.

## Upgrade Impact

This is a schema and role-behavior extension. Existing memory roots should keep
working without modification. Existing memories do not need automatic rewriting,
but users may later add `M#` headings to represent collaboration surfaces that
were previously described only in prose.

Because this changes how memory can be organized and interpreted, an
implementation should include a semantic upgrade note for Dreamer. The note
should help older memory consolidate ad hoc external collaboration references
into `M#` shared-view headings when useful.
