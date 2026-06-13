# RightMemory Web Studio Design

## Purpose

RightMemory Web Studio is a LAN-accessible control surface for inspecting and operating a RightMemory root. It should make common runtime state visible without sending the user to shell commands and log files, while still giving shared views a complete management workflow.

The product center is a status-first RightMemory workspace:

- understand the active memory root, profile, Git state, watches, role activity, shared views, and recent issues;
- browse active memory files, shared-view source files, and Insight artifacts from the browser;
- inspect recent agent messages, role outputs, and logs where RightMemory already records them;
- define, build, export, publish, accept, retrieve, and note against shared views;
- configure shared-view hubs and web access settings.

This design covers the first coherent web UI and service boundary. Mounted-folder hubs are the local compatibility adapter for the first Web Studio release. HTTP Shared View Hub is the network product target and should fit the same adapter boundary when it lands.

## Scope

The first release covers a useful RightMemory web workspace, with shared views as the main management module and runtime observability as a first-class companion. It includes:

- an overview for active root/profile health, recent issues, watches, shared-view state, and Insight/update status;
- read-only browsing for `MEMORY.md`, `MEMORY_*.md`, provider shared-view source files, generated shared-view previews, and `insight_logs/*.md`;
- bounded log and message viewing for managed watches, web service logs, async update state, and recent role outputs available through existing runtime files;
- provider shared-view workflows: define, edit, build, inspect, export, publish, and inbox;
- consumer shared-view workflows: accept invitations, list connections, retrieve, send notes, view note records, and refresh package snapshots;
- activity views for role events, local note records, provider inbox records, hub interactions, queued/sent state, and recent failures;
- root/profile switching inside one managed web service;
- settings for host, port, operator token, actor name, saved hub paths, default export location, and display preferences.

This release excludes implementing the remote HTTP hub service itself, an account system, per-user permission model, global discovery, full memory editor, model/config editor, and replacement chat client for retrieve/update/dreamer/insight roles. Ordinary memory edits continue through existing RightMemory role workflows; the browser can inspect memory files but does not become a general prose editor in this slice.

## Product Model

The UI should reduce the exposed concept count. Users think in these groups:

- **Overview**: current root/profile health, recent issues, and quick actions.
- **Shared Views**: views this root shares and views this root uses.
- **Memory**: active memory files, shared-view source/preview files, and generated context surfaces.
- **Insights**: browsable Insight markdown files and their recent status.
- **Activity**: role messages, logs, notes, inbox items, hub interactions, and queued/sent records.
- **Status**: managed watches, Git state, async update state, web service state, and relevant runtime paths.
- **Settings**: local service, actor, hub, export, profile, and display settings.

Package paths, hub paths, resolver metadata, cache files, TOML files, session files, and logs remain visible when useful, but they sit behind product concepts such as shared-view relationship, role run, activity item, or memory artifact.

## Architecture

RightMemory Web Studio should be a managed RightMemory service rather than a shell-command form UI. Add a web package inside `rightmemory/`:

```text
rightmemory/web/
  app.py              # FastAPI app factory
  auth.py             # token login, session, and CSRF helpers
  service.py          # Web Studio application service
  models.py           # request/response DTOs
  static/             # packaged vanilla JS/CSS/HTML
```

Use FastAPI with Uvicorn for the backend and serve packaged vanilla JS/CSS/HTML from the Python package. This keeps v1 lightweight while giving typed routes, request validation, structured errors, static asset serving, and test-client support. Prefer avoiding a separate frontend build stack for this release unless implementation uncovers a concrete need.

The UI talks to JSON endpoints. Those endpoints call a Web Studio service that wraps existing RightMemory functions and adds small read-only adapters for observability:

- shared-view functions such as `define_shared_view`, `build_shared_view`, `export_shared_view`, `publish_shared_view`, `accept_shared_view_invitation`, `retrieve_shared_view`, and `record_shared_view_note`;
- status/runtime functions such as `collect_status`, managed watch status, profile resolution, and web process status;
- bounded readers for active memory files, insight logs, generated shared-view previews, watch logs, and runtime state files that RightMemory already treats as inspectable operational artifacts.

Keep a clear adapter boundary for shared-view targets:

```text
Web Studio service
  -> package adapter
  -> local provider-root adapter
  -> mounted-folder hub adapter
  -> HTTP hub adapter, once the hub service exists
```

The mounted-folder hub is the local compatibility adapter for the first Web Studio release, not the whole hub concept. It supports the existing path-based hub structure:

```text
<hub>/registry.toml
<hub>/views/<view-id>/
<hub>/invitations/<view-id>.toml
<hub>/interactions/<view-id>.jsonl
```

The HTTP hub implements the same conceptual operations through network APIs and becomes the preferred network adapter once available.

## Process Management

Add explicit web service controls:

```bash
rightmemory web start
rightmemory web status
rightmemory web stop
rightmemory web restart
```

Also support optional integration with the managed watcher entry point, so `rightmemory watch start` can include the web service when configured. The explicit `rightmemory web` commands remain the user-facing control surface.

Store web PID, logs, token/config metadata, and session/runtime state under `<memory-root>/.runtime/web/`, following the existing managed-process style. The service binds to localhost by default. LAN binding requires explicit host configuration.

## UI Workflows

The opening screen is a status-first Overview for the active root/profile. It summarizes Git state, watch state, async update state, Dreamer/Insight trigger progress, recent role issues, shared-out views, accepted connections, pending inbox/notes, stale build/package warnings, and quick actions.

Main navigation:

- **Overview**: health summary, recent issues, recent role messages/results, and common actions.
- **Shared Views**: provider and consumer workflows in one area, split into “Views I Share” and “Views I Use.”
- **Memory**: read-only file browser for active memory files, shared-view source files, and generated shared-view previews, with search and safe path display.
- **Insights**: list and read `insight_logs/*.md`, show latest Insight status, and link related status/log context.
- **Activity**: show local note records, provider inbox records, hub interaction records, recent role outputs, log excerpts, and queued/sent states.
- **Status**: show RightMemory status, watch status, web service status, relevant log paths, runtime state paths, and root/profile details.
- **Settings**: configure host, port, token, actor name, saved hub paths, default export folder, active root/profile, and display preferences.

Shared-view provider editing uses guided forms by default. Advanced users can edit `view.md`, `retriever.md`, and `export.toml` directly from an advanced panel. Ordinary `MEMORY.md` and `MEMORY_*.md` browsing is read-only in this release so the UI does not bypass role-owned memory semantics.

## API Shape

The API should expose product operations, not raw CLI strings. A representative route shape:

```text
GET  /api/session
POST /api/login
POST /api/logout

GET  /api/profiles
POST /api/active-root

GET  /api/overview
GET  /api/status
GET  /api/status/watches
GET  /api/activity
GET  /api/logs
GET  /api/logs/{log_id}

GET  /api/memory/files
GET  /api/memory/files/{file_id}
GET  /api/insights
GET  /api/insights/{insight_id}

GET  /api/share/views
POST /api/share/views
GET  /api/share/views/{view_id}
POST /api/share/views/{view_id}/build
POST /api/share/views/{view_id}/export
POST /api/share/views/{view_id}/publish
GET  /api/share/views/{view_id}/inbox

GET  /api/use/connections
POST /api/use/accept-invite
POST /api/use/connections/{heading_id}/retrieve
POST /api/use/connections/{heading_id}/note
GET  /api/use/connections/{heading_id}/notes

GET  /api/hubs
POST /api/hubs
GET  /api/hubs/{hub_id}/health
GET  /api/hubs/{hub_id}/invitations
```

Responses should be structured JSON with success/failure, a human-readable message, affected paths, warnings, freshness, provenance, queued/sent state, and bounded preview text where relevant. For build/export/publish, v1 can run synchronously and return a result. Keep the service boundary compatible with later progress reporting.

Root/profile switching is explicit session state. Every API action resolves the active memory root and then uses existing RightMemory validation, path checks, and locks.

Saved UI settings belong in runtime/config state, not memory prose. Hub paths and display preferences are local machine settings rather than durable memory facts.

## Memory And Observability

The Memory, Insights, Activity, and Status areas should make existing RightMemory artifacts easier to inspect without changing their ownership model.

Memory browsing should include:

- `MEMORY.md` and `MEMORY_*.md`;
- provider shared-view source files under `shared_views/<view-id>/`;
- generated shared-view previews under `shared_views/<view-id>/dist/`;
- consumer imported package snapshots under `.runtime/shared_views/imports/` when the connection details call for them.

Insight browsing should include `insight_logs/*.md`, sorted by recency, with title/date extraction when available and a plain Markdown preview.

Activity and log viewing should include bounded previews from:

- managed watch logs under `.runtime/watch/`;
- web service logs under `.runtime/web/`;
- async update state and recent issue summaries already surfaced by `rightmemory status`;
- shared-view interactions, inbox records, notes, cache freshness, and queued/sent state;
- recent role outputs/messages where they are stored in RightMemory runtime state or watch logs.

Readers should be allowlisted and bounded. The web service uses stable file/log identifiers resolved server-side to known RightMemory locations, keeping arbitrary filesystem paths out of the browser API.

## Hub Support

V1 supports mounted-folder hubs through the formal hub adapter, while the service boundary is shaped for the HTTP hub adapter described in the separate HTTP Shared View Hub spec. The GUI should make mounted-folder hubs feel intentional rather than exposing raw path mechanics everywhere, but it should label them as local/mounted hubs rather than as the final network hub form.

Hub features:

- save named hub paths in settings;
- health-check a hub for readable registry, readable invitations, writable publish target, and interaction write ability;
- publish a provider view to a saved hub;
- browse invitations under a saved hub and accept them as consumer connections;
- show hub interactions in Activity;
- show package, hub, or local-provider transport details inside connection detail panels.

Mounted-folder hubs can be hosted on another LAN computer when the folder is mounted locally through SMB, NFS, or a similar filesystem mechanism. Different machines may use different local mount paths; the GUI should let each machine save its own path for the same conceptual hub.

When the HTTP hub adapter exists, Web Studio should present it as the normal network sharing target. Mounted-folder hubs remain useful for local teams, offline workflows, import/export, and compatibility.

## Credential And Local State Boundaries

Web Studio will touch settings with different durability and secrecy requirements. Keep these stores separate:

- Durable memory files keep collaboration meaning and non-secret shared-view references.
- `shared_views.toml` records resolver metadata that is safe to sync, such as target kind, view id, base URL, provider/maintainer metadata, accepted-from provenance, and credential reference ids.
- Local runtime or secret storage keeps operator tokens, HTTP hub tokens, connection tokens, CSRF/session secrets, and other bearer credentials.
- Web UI display preferences and local hub mount paths live in local runtime/config state because they are machine-specific.

HTTP shared-view connections keep bearer tokens out of `shared_views.toml`. The registry points to a local credential id, and retrieval/note operations resolve that credential at runtime.

## Delivery Phases

The product shape is broader than the first implementation pass, so the plan should land it in coherent phases:

1. Web service shell: FastAPI app, login/session/CSRF, root/profile switching, static UI shell, and process management.
2. Observability: overview/status, memory browser, Insight browser, bounded log/message readers, and read allowlists.
3. Shared-view management: provider and consumer workflows over the existing package, local provider, and mounted-folder adapters.
4. Activity polish: interaction timelines, inbox ergonomics, warning states, and cross-links between status, logs, memory files, and shared views.

Each phase should leave the UI usable rather than exposing a half-wired navigation tree.

## Safety And Auth

Because the web UI can perform write-capable operations and expose operational memory context over the LAN, it needs web-specific guardrails while preserving RightMemory's current safety model.

Security behavior:

- first visit requires operator-token login;
- authenticated browsers receive a signed, `HttpOnly`, `SameSite` session cookie;
- write APIs require a CSRF token/header;
- CORS is closed by default;
- trusted hosts are explicit;
- localhost binding is the default, and LAN binding is intentional;
- operator token material is generated and stored under runtime/config state with local-secret file permissions.

Write behavior mirrors CLI semantics:

- routine saves, builds, retrieves, accepts, and normal exports run after validation;
- notes to `human` or `external` relationships require confirmation, matching the current `--confirm` behavior;
- replace/overwrite-style export or publish actions require a small confirmation;
- existing RightMemory locks and validators remain authoritative for file writes.

Read behavior is constrained:

- file and log readers use server-side identifiers instead of raw arbitrary paths;
- previews are bounded by size and line count;
- runtime files that may contain prompts, model outputs, or tool results are treated as sensitive operational context and protected by the same login boundary as write actions.

Errors should return structured JSON with a short user-facing message, technical detail, and a suggested next step when the service can infer one. Validation failures, lock conflicts, missing paths, hub health problems, missing log files, unreadable artifacts, and runtime exceptions should use one response shape so the UI can display them consistently.

## Testing

Testing should follow the repo's existing `unittest` style, with FastAPI `TestClient` for route coverage.

Test coverage should include:

- auth flow: unauthenticated requests blocked, token login works, write APIs require CSRF;
- profile/root switching: actions resolve the active root and do not leak state across profiles;
- credential boundaries: synced registries contain credential references rather than bearer tokens;
- overview/status: Git state, managed watches, async update state, Insight status, and recent issue summaries render through API responses;
- memory browsing: active memory files, shared-view source files, generated previews, and imported snapshots are readable through allowlisted identifiers;
- insight browsing: `insight_logs/*.md` list and preview correctly;
- log/message viewing: bounded previews load for known watch/web/runtime logs and reject arbitrary paths;
- provider shared-view workflows: define, edit, build, inspect, export, publish, and inbox;
- consumer shared-view workflows: accept invitation, list connections, retrieve, note, notes, and refresh package snapshots;
- hub adapter behavior: saved hub paths, health checks, invitation browsing, publish validation, and queued/sent interactions;
- safety behavior: confirmation gates, invalid paths, lock conflicts, missing targets, replace protections, and read bounds;
- process management: `rightmemory web start/status/stop/restart`, plus optional `watch start` integration when configured;
- static UI smoke: packaged HTML/JS/CSS load and the overview can render API data.

Avoid tests that pin visual copy too tightly. Prefer route contracts, state changes, written files, read allowlists, preview bounds, and safety boundaries.

## Implementation Notes

This design adds new runtime/config behavior and new dependencies. The implementation plan should include focused tests around dependency wiring, install behavior, web process state, runtime secrets, generated files, read allowlists, and artifact preview limits. It should also update operational docs and `AGENTS.md` once commands or setup expectations change.

The design does not need a semantic upgrade note unless the implementation changes how existing memory should be organized or interpreted. If the implementation changes `M#` guidance, shared-view schema expectations, or role prompt behavior, add or update a semantic upgrade note at that time.
