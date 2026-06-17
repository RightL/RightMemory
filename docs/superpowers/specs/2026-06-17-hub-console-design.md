# Hub Console Design

## Context

The shared-view hub is the HTTP control plane for remote `MF#` and `MQ#` sharing. Web Studio now covers the active memory root workflow: providers build, approve, publish, invite, and inspect feedback; consumers accept, pull, status, ask, and send notes. The remaining gap is hub-level operation: inspecting and administering the state owned by the hub service itself.

Hub Console should not become another place to build views or edit memory. It should be the operator console for the remote shared-view control plane.

Current hub API support is intentionally small:

- health;
- publish `MF#` package versions;
- register `MQ#` question metadata;
- create invitations;
- describe and accept invitations;
- download packages with accepted connection tokens;
- post consumer interactions;
- read provider inbox records.

Current CLI/store support also includes hub initialization, provider token creation, token listing, token revocation, status, and audit-event listing. Hub Console V1 should expose the runtime administration pieces through authenticated HTTP APIs and a served console UI.

## Goals

Hub Console V1 is an admin/operator-only console hosted by the hub service.

It should let an operator:

- see hub health and configuration;
- list providers;
- create provider publish tokens;
- list tokens without raw secrets;
- revoke tokens;
- list hub views;
- inspect `MF#` and `MQ#` view metadata;
- create and revoke invitation URLs;
- list accepted connections;
- revoke accepted connection tokens;
- read provider inbox interactions;
- browse audit events with useful filters.

It should also make hub CLI bootstrap less fussy by letting the common commands work without an explicit root path.

## Non-Goals

V1 will not:

- initialize or serve the hub from the console UI;
- build or edit shared views;
- edit provider memory roots;
- answer `MQ#` questions;
- add MQ background queues;
- add inbox replies or conversation threads;
- provide provider-scoped login or provider self-service UI;
- read or write hub SQLite files directly from frontend code;
- expose raw stored tokens after creation.

Provider day-to-day workflow remains in Web Studio. Hub Console is for hub-owned state, revocation, audit, and debugging.

## Default Hub Root

`rightmemory hub init` and `rightmemory hub serve` should work with no explicit `hub_root` argument.

Use `./rightmemory-hub` as the default hub root. The explicit positional root remains available for non-default deployments.

Do not add an environment-variable override in V1. A single visible default plus an explicit positional override is enough.

Apply the same default consistently to all hub subcommands that currently require `hub_root`:

- `rightmemory hub init`;
- `rightmemory hub status`;
- `rightmemory hub serve`;
- `rightmemory hub token list`;
- `rightmemory hub token create`;
- `rightmemory hub token revoke`.

Examples:

```bash
rightmemory hub init
rightmemory hub token create --provider alice --label publish
rightmemory hub serve
rightmemory hub status
```

If `serve`, `status`, or `token` runs before initialization, the error should name the resolved default root and suggest `rightmemory hub init`.

The default `public_base_url` remains `http://127.0.0.1:8765`. The default `serve` host remains `127.0.0.1`; the default port remains `8765`.

## Architecture

Hub Console is served by the hub FastAPI app:

- UI route: `GET /console`;
- static assets under `GET /console/static/...`;
- admin API route family: `/api/admin/...`.

The console frontend talks only to hub HTTP APIs. It does not read hub files, inspect SQLite directly, or depend on a local memory root.

V1 uses an admin token only. The operator enters or configures an admin token, and console API requests send it as a bearer token. Provider-scoped console access can be designed later if multi-provider self-service becomes important.

## Admin API

Add a small admin API layer around existing hub store capabilities.

V1 routes:

```text
GET  /api/admin/overview
GET  /api/admin/providers
POST /api/admin/providers/{provider_id}/tokens

GET  /api/admin/tokens
POST /api/admin/tokens/{token_id}/revoke

GET  /api/admin/views
GET  /api/admin/views/{view_id}

GET  /api/admin/views/{view_id}/invitations
POST /api/admin/views/{view_id}/invitations
POST /api/admin/invitations/{token_id}/revoke

GET  /api/admin/connections
POST /api/admin/connections/{token_id}/revoke

GET  /api/admin/inbox
GET  /api/admin/audit
```

All `/api/admin/*` routes require a valid admin token. Provider publish tokens must not work for these routes in V1.

Invitation and connection revocation should use token revocation internally when those resources are represented by scoped hub tokens. The API route names still express the user action directly.

## Console UI

The UI should be quiet and operational, not a marketing page.

Primary navigation:

- Overview;
- Providers;
- Views;
- Invitations;
- Connections;
- Inbox;
- Audit;
- Tokens.

Overview shows health and counts: initialized, public base URL, storage present, package size limit, provider count, view count, active token count, recent audit failures, and recent inbox count.

Providers shows provider IDs and labels. It includes a create-token action that returns the raw provider token once, with clear copy affordance.

Views shows all hub-hosted views grouped or filtered by provider. Each row shows view ID, type, title, current version/register time, package hash for `MF#`, question base URL for `MQ#`, and last update time when available.

Invitations shows invitation tokens by view without showing raw reusable secrets after creation. It supports creating a new invitation URL and revoking an invitation token.

Connections shows accepted consumer connections by view. It shows consumer label, connection ID, created time, and token status. It supports revoking one accepted connection.

Inbox shows hub-stored consumer interactions. It supports filtering by provider, view, and connection. V1 is read-only.

Audit shows audit events with filters by kind, provider, view, token, and time range.

Tokens shows all token records without raw token values. It supports revocation.

## Data Handling

Raw token values are displayed only when created. Stored token hashes and verifier material are never exposed.

Console responses should use bounded lists with explicit limits. V1 can start with simple `limit` and `offset` query parameters for inbox, audit, tokens, views, invitations, and connections.

Rows should include stable IDs so later actions can target a token, invitation, connection, provider, or view without relying on display text.

## Error Handling

Unauthenticated admin API requests return `401`.

Authenticated non-admin tokens return `403`.

Revoke actions are idempotent enough for UI use: revoking an already-revoked token should return a clear "already revoked" response or a normal success with current revoked state.

Creation forms validate IDs before sending when practical, but the hub remains the source of truth. Server errors should return concise user-facing messages plus technical detail in the JSON response where existing hub conventions allow it.

## Testing

Tests should cover:

- hub commands default to `./rightmemory-hub` when no root is supplied;
- explicit hub root still overrides the default;
- `serve` uses the default root and existing default host/port;
- admin routes reject missing or invalid tokens;
- provider publish tokens cannot call admin routes;
- overview returns health and counts;
- provider token creation returns raw token once and list endpoints do not expose it;
- token revocation prevents later use;
- views, invitations, connections, inbox, and audit list endpoints return bounded JSON;
- console static route is served.

## Implementation Notes

The clean implementation path is to add store list helpers where missing, then expose them through `/api/admin/*`, then add the console shell.

The hub app should keep existing public and provider/consumer endpoints stable. Hub Console is additive.

The Web Studio shared-view docs should mention that hub bootstrap remains CLI, while Hub Console handles runtime hub administration after the hub is running.
