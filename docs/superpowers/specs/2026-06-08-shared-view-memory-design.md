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

For provider-owned view definitions, a natural storage shape is:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  export.toml
  dist/
```

`view.md` describes the shared view contract, audience, and collaboration
meaning. `retriever.md` stores the view-specific retrieve instructions when the
view uses policy-guided retrieve. `export.toml` stores publishing settings for
hub, export, or local targets. `dist/` is the builder's local generated output:
useful for previewing, staging, and publishing filtered Markdown, but not the
canonical source of the shared view inside the provider's private root.

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

## Reference And Resolver

The shared view link a collaborator receives should behave like an invitation
or stable reference, not like a hand-written transport URL. A user or agent can
accept a reference and record it as an `M#` heading without deciding whether the
view is backed by a hub call, exported Markdown, local files, or a future
transport.

Accepting a shared view creates two things:

- a local `M#` heading whose body explains the collaboration meaning;
- an out-of-band resolver entry that binds the local heading id to a stable
  shared view reference.

Accepting a shared view also establishes the relationship manners for that
connection. A colleague's view, a user's own agent view, and a team-space view
can all use the same `M#` shape while behaving differently for interactions,
caching, and automatic notes. The user-facing act should feel like accepting a
collaboration relationship, not filling out transport configuration.

The local memory should stay focused on meaning. Resolver mechanics belong in a
separate registry/cache that can expand the stable reference into whatever
reachability metadata the runtime needs. The registry may keep the invitation
that created the connection for traceability and relationship manners, but the
durable consumer-side concept is "this local heading points to that shared
view."

## Retrieve Behavior

`retrieve` and `interaction` are separate behaviors. Retrieve gets context from
a shared view. Interaction leaves information for the shared view owner or asks
the owner side to react.

RightMemory retrieve is currently a read-only role that reads local
`MEMORY.md`/`MEMORY_*.md` files and uses judgment over headings, nodes, detail
pointers, and graph context. The `M#` extension should fit that model without
turning the local retriever into a transport planner.

Conceptual flow:

```text
consumer root
  -> local retrieve finds that an M# shared view is relevant
  -> retrieve_shared_view(ref, query)
  -> shared view endpoint returns context with provenance
  -> local retrieve combines local memory and shared context
```

The local retriever should not inspect whether the shared view is backed by a
view-specific retriever prompt, filtered Markdown, or both. It calls a
read-only shared view endpoint with the stable reference and query. The endpoint
answers from its own backing and returns compact context with provenance, such
as "Alice Auth API shared view, refreshed today."

Even when a shared view is backed by filtered `MEMORY*.md`, the consumer
retriever should not receive a path and read those files as if they were local
memory. The shared view endpoint owns that retrieval step. This keeps external
content labeled as external, preserves provider policy, and leaves room for the
provider to change backing without changing the consumer's `M#` heading.

The endpoint behavior can support:

- a document surface backed by published filtered memory;
- a question surface backed by a provider-side retriever or agent;
- a combined surface that decides how to answer from both.

Those are shared-view capabilities, not separate local heading types. `M#`
remains the local connection node.

Shared view retrieval also defines a boundary for transitive references. If
Alice's shared view mentions Bob's shared view, the consumer may use the content
Alice intentionally exposed through her view, but it should not automatically
traverse into Bob's view. The system can suggest accepting another shared view
when it looks relevant; crossing into that view should create its own
connection.

## Availability And Freshness

Shared views should be useful even when the provider side is temporarily
unavailable, while still respecting freshness and access boundaries.

The retrieve endpoint can return three product states:

- **Fresh:** the endpoint reached the current shared view and returned current
  context.
- **Cached:** the endpoint could not refresh, but a previously trusted shared
  view result or published snapshot is available. The response should label the
  source and freshness.
- **Unavailable:** no usable shared context is available, or access is denied.

Offline and timeout failures can use cached context when the connection's
relationship permits it. Revoked access or explicit denial should not be
bypassed with stale cached content unless that retention behavior was part of
the accepted relationship. When a shared view is unavailable, local retrieve
should still return relevant local memory rather than failing the whole request.

Cache retention follows the relationship and the terms of the shared view.
Cached shared context is useful working state, not local memory and not a
hidden permanent copy of the provider's view. A cache record can preserve
source, freshness, and usability state while keeping the consumer memory
focused on the `M#` relationship.

## Lifecycle

The core lifecycle is on demand:

```text
provider creates or updates a view
consumer accepts a reference
consumer records an M# heading
retrieve refreshes the shared view when it is used
interaction sends a note when there is something to say
```

The base product model does not need notification streams or periodic refresh.
Provider updates become visible the next time the consumer retrieves from the
shared view. Managed team spaces or owned agent clusters may later add more
automation, but the shared view concept should work without background sync.

The stable shared view reference should survive owner changes. A view can move
from Alice to a backend team, or from a person to a project root, without
forcing consumers to replace their `M#` headings. Provenance can say who
currently maintains the view and who originally shared it. The consumer memory
may later refine the heading body, but the relationship should not break just
because ownership moved.

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

The provider root can keep build output local. By default, `dist/` under a
private provider root should be treated as generated state and ignored by the
provider root's normal Git history. The published target is where the filtered
`MEMORY*.md` surface becomes durable: a hub-hosted view, an export repository,
an exported package, or a local shared package. Teams that want auditability can
commit the published target or store build metadata such as source view id,
builder version, and checksum, without turning the private root's `dist/` into
the collaboration source of truth.

## Minimal Hub

The hub should stay small in the product model. It is not a full chat system,
task tracker, or organization-management platform.

Its minimal registry records shared view identity, current maintainership, and
meaning:

```text
view reference
maintainer
description
```

The registry answers "what shared view is this, who currently maintains it, and
what is it for?" It does not need user-facing fields for how to retrieve, how
to interact, or whether the provider is currently online. Those are hub runtime
concerns.

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

Interaction manners should come from the relationship, not from the transport.
A shared view owned by a human collaborator normally asks before sending a note.
A shared view owned by the user's own agent group can send notes automatically.
A managed team space can follow workspace norms. The product should express
this as social distance or relationship, not as a user-facing pile of policy
fields.

The default experience should make reading smooth and leaving traces
intentional. Retrieve can be quiet. Interaction should respect whether the
connection represents another person, a trusted agent system, or a team space.

Automatic interaction should stay tied to an active task context. Owned agent
groups can send notes without repeated confirmation, but that should not become
background chatter or an autonomous synchronization loop. The user should be
able to understand which task caused an automatic note.

When a shared view is stale, incomplete, or unhelpful, the natural repair path
is feedback to the shared view rather than consumer-side editing of provider
content. A human-owned view may ask before sending the feedback. An owned agent
view can route the feedback directly to its builder. The provider side then
decides whether to update the view, answer directly, or keep the issue open.

## Local Memory Boundary

The consumer root should not copy shared view content into local memory by
default. The local `M#` heading records the relationship and local
collaboration meaning. Cache stores retrieved shared context. Local memory
stores local consequences: decisions, commitments, tasks, or facts that belong
to the consumer root.

For example, if Alice's shared view says a token field changed, the consumer
root may record "frontend decided to use Alice's v2 contract" when that becomes
local project state. It should not silently absorb Alice's API details as if
they were local memory. Explicit absorption is still possible when a user or
agent asks for it, and absorbed content should keep provenance.

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
link registry that maps each local `M#` heading id to a stable shared view
reference. The local `M#` heading stays readable memory; its body explains the
collaboration meaning, while the registry stores resolution mechanics.

The consumer-side registry should not decide how the shared view is backed. It
should not contain a prompt-vs-Markdown mode flag, and local memory should not
store transport locators in prose. The provider root owns the shared view
contract and decides whether that view answers through a view-specific
retriever prompt, filtered `MEMORY*.md` files, or both.

The product-level object is a shared view invitation or reference rather than a
hand-written locator. A colleague, project owner, or manager agent gives the
recipient an invitation for a shared view. Accepting it should create or suggest
a local `M#` heading and store resolver details out of band. The user-facing
choice is whether to accept the shared view and how to describe its local
collaboration meaning.

The remaining implementation question is the exact registry/cache shape: how
to store the stable reference, invitation provenance, resolved transport
metadata, cache freshness, and revoked/denied state without making those fields
part of memory prose.

The registry/cache also needs to preserve relationship manners and current
maintainer metadata so interaction behavior and owner changes do not require
rewriting the local `M#` heading.

### Shared View Storage

The current storage direction is provider-owned view definitions plus separate
published targets. The provider root can keep `shared_views/<view-id>/` as the
source of the shared view contract and builder settings. Generated filtered
Markdown can appear in that view's `dist/` for preview or publishing, but the
durable collaboration artifact should live in the publish target: hub storage,
an export repository, an exported package, or a local shared package.

The private provider root's `dist/` should default to ignored generated state.
If a team wants reviewable history, it is cleaner to review and commit the
published filtered surface or a build record than to treat provider-local
builder output as the shared memory artifact.

### Retriever Prompt Storage

View-specific retriever prompts belong with the provider root's shared-view
metadata, not in the consumer root. The implementation still needs to define
how a shared view endpoint loads the prompt alongside `view.md`, filtered
Markdown, and publishing metadata.

### Interaction Records

Natural-language interactions need a durable place if they are more than
ephemeral messages. The record should support review, absorption, and closure
without making every interaction a memory node.

The interaction record should also preserve relationship manners: whether the
note was sent after human confirmation, automatically by an owned agent, or
under team-space policy.

Interaction records should connect feedback to the shared view lifecycle. A
note can become evidence for rebuilding a view, answering a collaborator, or
closing a stale shared-view issue, without turning the consumer root into the
owner of provider content.

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
- local retrieve calls a shared view endpoint instead of reading provider
  private memory or external filtered files directly;
- shared view endpoints can answer from retriever prompt backing, filtered
  Markdown backing, or both;
- shared view results preserve provenance and freshness labels;
- unavailable shared views do not fail local retrieve when local matches exist;
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
