# HTTP Shared View Hub Design

## Purpose

HTTP Shared View Hub is the clean network transport for RightMemory shared views. It lets provider roots publish hosted shared-view snapshots to a self-hosted hub, lets consumer roots accept and retrieve those views through stable HTTP invitations, and stores notes/interactions in per-view inboxes for providers to review later.

The hub is the target product form for team and internet-capable shared-view collaboration. Package export remains useful for one-off handoff. Mounted-folder hubs can remain as a local compatibility path, but they should not define the long-term product architecture.

## Scope

The first HTTP Hub release targets self-hosted internet-capable deployments, not SaaS. It should run on a user's own server, home machine, or VPS behind HTTPS, and it should support a small team or group of trusted collaborators without requiring provider roots to stay online.

In scope:

- provider registration and provider-scoped publish tokens;
- hosted snapshot publishing for shared-view packages;
- immutable view versions with a current-version pointer;
- per-view invitation tokens and stable invitation URLs;
- retrieval from hub-hosted snapshots;
- per-view interaction/inbox records;
- token rotation and revocation;
- audit records for security-relevant events;
- SQLite metadata storage and package/blob storage on disk;
- RightMemory CLI and Web Studio adapters for publish, accept, retrieve, note, and inbox workflows;
- import paths from package exports or mounted-folder hubs.

Out of scope:

- live relay to online provider roots;
- bidirectional chat/threading;
- SaaS organizations, billing, and tenant isolation;
- broad user-account permissions;
- global discovery;
- public hosting operations beyond a self-hosted HTTPS deployment model.

The design should leave room for stronger identity and hosted/cloud storage later, but v1 should be token-based and self-hosted.

## Product Model

The hub is a rendezvous and hosted snapshot service. Providers publish view versions. Consumers accept invitations and retrieve from the hub. Notes return to a hub inbox. The provider root is authoritative for source memory and view definitions; the hub is authoritative for published versions, invitation state, interaction records, and audit records.

Core concepts:

- **Provider**: an entity allowed to publish one or more views.
- **View**: a named shared-view surface with maintainer, description, audience, and current version metadata.
- **View Version**: an immutable published snapshot of a view package.
- **Invitation**: a token-scoped URL that allows a consumer to accept and retrieve one view.
- **Interaction**: a note or feedback record posted by a consumer to a view inbox.
- **Audit Event**: a durable operational/security event.

The hub stores hosted snapshots. It does not ask provider roots live retrieval questions in v1.

## Architecture

The HTTP Hub is its own service, separate from a memory root, but designed to interoperate with RightMemory Web Studio and CLI adapters.

```text
HTTP Shared View Hub
  FastAPI app
  SQLite metadata store
  package/blob storage directory
  token/auth service
  audit/event writer
  optional static admin/status UI
```

Metadata lives in SQLite. Published package files live on disk so snapshots remain inspectable and portable. This avoids using TOML files as a database while keeping the shared-view package artifact visible.

Storage shape:

```text
hub.db
storage/
  views/<view-id>/versions/<version-id>/
    view.md
    retriever.md
    export.toml
    manifest.toml
    dist/
```

Conceptual tables:

```text
providers
views
view_versions
invitations
tokens
interactions
audit_events
```

Each publish creates an immutable view version and updates the view's current-version pointer. Consumers retrieve the current version by default. Accepted connections may record the version they first accepted or most recently retrieved for provenance.

The RightMemory side sees the HTTP Hub through a transport adapter:

```text
RightMemory Web Studio / CLI
  -> package adapter
  -> mounted-folder hub adapter, compatibility
  -> HTTP hub adapter, product target
```

The HTTP Hub API does not need to mimic the mounted-folder hub layout internally. It should, however, preserve the same product semantics: registry, invitations, hosted view content, interactions, and inbox.

## API Shape

Representative endpoints:

```text
GET  /health
GET  /api/hub

POST /api/admin/login
GET  /api/admin/views
GET  /api/admin/audit

POST /api/providers
POST /api/providers/{provider_id}/tokens

POST /api/views
POST /api/views/{view_id}/versions
GET  /api/views/{view_id}
GET  /api/views/{view_id}/versions/{version_id}/manifest

POST /api/views/{view_id}/invitations
GET  /i/{invite_token}

GET  /api/invitations/{invite_token}/view
POST /api/invitations/{invite_token}/retrieve
POST /api/invitations/{invite_token}/interactions

GET  /api/views/{view_id}/inbox
PATCH /api/views/{view_id}/inbox/{interaction_id}
```

This route shape can evolve during implementation, but the operation boundaries should stay clear: admin/provider setup, publishing, invitation acceptance, retrieval, interaction posting, inbox review, and audit inspection.

## Data Flow

Provider publish:

1. Provider builds a shared-view package locally.
2. Provider publishes the package to the hub using a provider token.
3. Hub validates package metadata and safe file paths.
4. Hub stores a new immutable version under package storage.
5. Hub updates current view metadata and writes an audit event.

Invitation and consumer use:

1. Provider or admin creates or rotates a per-view invitation token.
2. Consumer accepts an invitation URL into local RightMemory.
3. Consumer memory records a local `M#` heading and resolver metadata.
4. Consumer retrieves from the hub-hosted snapshot through the HTTP adapter.
5. Hub returns bounded, provenance-labeled shared-view context.

Interaction:

1. Consumer posts a note/interaction to the hub through the accepted connection.
2. Hub stores the interaction under the target view inbox and writes an audit event.
3. Provider reads and marks inbox items through Web Studio or CLI.

Provider roots do not need to be online for consumers to retrieve or post notes.

## Auth And Security

V1 should be token-based but structured for stronger auth later.

Token types:

- **Admin/operator token**: hub setup, provider management, view management, token management, audit access.
- **Provider publish token**: publish versions for allowed providers/views.
- **Invitation token**: accept/retrieve one shared view and post interactions for that view.
- **Inbox access**: provider token scope, or a dedicated inbox token if implementation needs a separate boundary.

Security behavior:

- Store verifier material such as token hashes rather than recoverable raw tokens.
- Show raw tokens once at creation.
- Support token rotation and revocation.
- Scope tokens by provider, view, and action.
- Audit publish, invite creation, retrieve, interaction, inbox read/update, token creation, token revocation, and failed auth.
- Enforce package size limits, request size limits, and bounded retrieve responses.
- Validate package manifests before storing versions.
- Protect package extraction and file serving from path traversal.
- Treat HTTPS as required for internet exposure; self-hosted docs can explain reverse-proxy TLS rather than adding certificate management to v1.

The absence of live provider relay is a security and reliability choice: public consumers interact with the hub-hosted snapshot and hub inbox, not arbitrary provider machines.

## RightMemory Integration

RightMemory should talk to the HTTP Hub through a first-class adapter.

Conceptual config:

```toml
[shared_view_hubs.work]
kind = "http"
base_url = "https://hub.example.com"
token = "..."
```

Provider workflow:

```bash
rightmemory shared-view publish-http <view-id> --hub work
```

Web Studio provider flow:

```text
Views I Share -> Publish -> HTTP Hub -> choose hub -> publish new version
```

Consumer workflow:

```bash
rightmemory shared-view accept-invite https://hub.example.com/i/<token>
```

Web Studio consumer flow:

```text
Views I Use -> Accept Invitation URL
```

Retrieve and note behavior stay the same at the product level. The local `M#` heading records relationship meaning, `shared_views.toml` records resolver metadata, and the resolver target is `kind = "http"` with base URL, token, view id, and version/freshness provenance.

The HTTP adapter becomes the product target. Package export and mounted-folder hubs remain useful for offline/local compatibility, import/export, and migration.

## Migration And Import

The hub should support import paths without requiring automatic migration in v1.

Useful import sources:

- package exports;
- mounted-folder hub registry, invitations, packages, and interactions.

Migration behavior:

- importing a package creates or updates a view and stores a new immutable version;
- importing a mounted-folder hub can translate registry entries into views, package folders into versions, invitation TOML into invitation records, and interaction JSONL into inbox records;
- existing consumers may keep mounted-folder connections until they accept HTTP invitations.

The data model should make manual/import tooling straightforward even if v1 ships with basic import commands.

## Testing

Hub service tests should cover:

- publish creates immutable versions and updates the current pointer;
- invalid package manifests and unsafe paths are rejected;
- invitation tokens retrieve the intended view and cannot access other views;
- provider tokens can publish allowed views and cannot publish unrelated views;
- interactions are stored per view and visible through provider inbox;
- token hashes, rotation, revocation, and failed-auth audit events behave correctly;
- size limits and bounded retrieve responses are enforced;
- SQLite migrations apply cleanly from an empty database.

RightMemory adapter tests should cover:

- Web Studio and CLI can publish a local shared-view package to the HTTP Hub;
- `accept-invite` handles HTTP invitation URLs;
- retrieve and note work through accepted HTTP hub connections;
- connection metadata records hub provenance and version/freshness;
- mounted-folder hub behavior remains separate and compatible.

Avoid tests that depend on exact UI copy or raw token values. Prefer operation contracts, authorization boundaries, stored metadata, package files, and audit records.

## Implementation Notes

This design adds a separate service and persistent hub database. The implementation plan should include database migration handling, local development startup, self-hosted deployment guidance, token generation/storage, package validation, size limits, and adapter wiring.

Docs should present mounted-folder hub as the local compatibility path and HTTP Hub as the network product target once the HTTP adapter exists. The RightMemory Web Studio spec should reference this hub as an adapter target rather than implementing the hub service inside the Web Studio process.
