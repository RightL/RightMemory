# Web Studio Shared View Operations Design

## Goal

Make Web Studio complete for the normal provider and consumer shared-view workflow without turning it into hub infrastructure admin.

Web Studio should let one active memory root author, publish, consume, sync, ask, and inspect provider feedback. A separate Hub Console should handle cross-provider hub administration such as provider management, global token management, revocation, audit, and version administration.

## Product Boundary

Web Studio remains scoped to the active memory root.

In scope:

- Provider HTTP inbox for views owned by the active root.
- Recent file-view auto-publish events for the active root.
- Consumer pull-all and status-all controls for configured shared-view connections.
- Saved credential selection so common forms do not require repeated manual `hub_url` and `credential_id` entry.

Out of scope:

- `rightmemory hub init`.
- `rightmemory hub serve`.
- Global hub token management.
- Managing all providers hosted by a hub.
- Full hub audit log.
- Invitation or accepted-connection revocation.
- Inbox replies, resolution state, or two-way conversations.
- Direct provider-root access.

The future Hub Console is a separate product surface. It should be hosted by the hub service and manage providers, invitations, accepted connections, versions, tokens, audit, hub health, and provider inboxes across the hub.

## Current Model

The backend already supports the clean feedback model:

```text
Consumer note -> Hub stores interaction -> Provider reads provider-scoped inbox
```

Consumers call `record_shared_view_note(...)`. For HTTP-backed file or question connections, that posts to the hub interaction endpoint. Providers can read the provider-scoped inbox today through `rightmemory shared-view inbox-http`, which calls the hub provider inbox endpoint.

The missing piece is Web Studio provider visibility. Web Studio can currently send notes and show local runtime activity, but it cannot fetch the hub-hosted provider inbox for views created by the active memory root.

## Feature 1: Provider HTTP Inbox

Add a read-only Provider Inbox panel in Web Studio.

Inputs:

- Saved provider credential id.
- Hub URL, defaulted from the saved credential `base_url`.
- Provider id, defaulted from the saved credential `provider_id`.

Behavior:

- Web Studio calls local service code.
- Local service loads the saved credential token.
- Local service calls the existing hub provider inbox endpoint through `list_http_shared_view_inbox(...)`.
- UI groups interactions by `view_id`.
- Each interaction shows `created_at`, `view_id`, `connection_id`, `actor_id`, message text, task context, and the raw payload when useful.

Failure behavior:

- Missing credential id returns a clear validation error.
- Missing hub URL returns `provider inbox requires a hub URL`.
- Missing provider id returns `provider inbox requires a provider id`.
- Hub authentication or network failures surface as ordinary Web Studio API errors.

Read-only is intentional for this step. Marking resolved or replying needs a separate interaction-state design.

## Feature 2: Auto-Publish Events

Add an Auto-Publish Events panel in Web Studio.

Source:

```text
.runtime/shared_views/publish-events.jsonl
```

Behavior:

- Show newest events first.
- Show `created_at`, `view_id`, `status`, `message`, and `trigger`.
- Ignore malformed JSON lines rather than failing the whole panel.
- Limit the API response to the newest 50 events by default.

This gives providers a visible answer to whether approved `MF#` views are being rebuilt and published after memory writes.

## Feature 3: Pull All And Status All

Add consumer controls for all configured shared-view connections.

Pull all:

- Calls `pull_all_file_views(...)`.
- Only affects file views.
- Returns one result per file connection with `heading_id`, `status`, and `message`.
- Does not write anything to retrieve/session history.

Status all:

- Calls `shared_view_connection_status(...)` for every configured connection.
- Includes both file and question connections.
- Returns structured status objects suitable for display.

These controls are Web Studio workflow convenience. They do not change retrieve behavior. Retrieve already performs silent file-view sync before the retrieve model starts.

## Feature 4: Credential Picker Polish

Expose saved shared-view credentials to Web Studio without exposing raw tokens.

Credential summaries include:

- `credential_id`.
- `kind`.
- `base_url`.
- `provider_id`.
- `view_id`.
- `created_at`.

Credential summaries never include `token`.

Forms that need hub credentials should render a credential selector:

- Build File View.
- Create File Invitation.
- Publish Question Invitation.
- Provider Inbox.

For forms that still need a hub URL override, selecting a credential should fill or default the hub URL from the credential `base_url`. Empty override fields keep the existing backend behavior where the recipe or credential supplies defaults.

## API Shape

Extend Web Studio local APIs:

```text
GET  /api/share/views
POST /api/share/provider-inbox
GET  /api/share/publish-events
POST /api/use/connections/pull-all
GET  /api/use/connections/status-all
```

`GET /api/share/views` should include sanitized credential summaries alongside provider views and consumer connections.

`POST /api/share/provider-inbox` accepts:

```json
{
  "credential_id": "alice-publish",
  "hub_url": "https://hub.example.test",
  "provider_id": "alice"
}
```

`hub_url` and `provider_id` may be omitted when the credential has `base_url` and `provider_id`.

`GET /api/share/publish-events` returns:

```json
{
  "events": [
    {
      "created_at": "2026-06-17T12:00:00+00:00",
      "view_id": "auth-api-files",
      "status": "published",
      "message": "file view published",
      "trigger": "memory-write"
    }
  ]
}
```

`POST /api/use/connections/pull-all` returns:

```json
{
  "results": [
    {
      "heading_id": "auth-api-files",
      "status": "pulled",
      "message": "file view pulled"
    }
  ]
}
```

`GET /api/use/connections/status-all` returns:

```json
{
  "statuses": [
    {
      "heading_id": "auth-api-files",
      "type": "file",
      "target": "http-file",
      "status": "imported",
      "message": "file view import is available"
    }
  ]
}
```

## Implementation Notes

Use existing helper layers where possible:

- `list_http_shared_view_inbox(...)` already implements provider inbox fetch.
- `pull_all_file_views(...)` already implements file-view sync for all file connections.
- `shared_view_connection_status(...)` already implements per-connection status.
- `record_file_view_publish_results(...)` already defines the publish-events JSONL shape.

Add small focused helpers where the existing code only writes data:

- `list_shared_view_credentials(...)` in `shared_view_models.py`.
- `list_file_view_publish_events(...)` in `shared_view_files.py`.

Do not expose credential tokens through Web Studio APIs.

## Testing

Use existing `unittest` style and FastAPI `TestClient` tests.

Coverage required:

- Sanitized credential summaries omit `token`.
- Web Studio provider inbox endpoint calls `list_http_shared_view_inbox(...)` with resolved hub URL and provider id.
- Provider inbox endpoint can use `base_url` and `provider_id` from a saved credential when payload override fields are empty.
- Publish event reader returns newest events first and skips malformed lines.
- Web Studio publish-events endpoint returns the event list.
- Pull-all endpoint calls `pull_all_file_views(...)` and returns structured results.
- Status-all endpoint returns statuses for all configured connections.
- Static shell contains provider inbox, publish events, pull-all, status-all, and credential picker hooks.

Verification commands:

```bash
rtk python -m compileall -q rightmemory tests
rtk python -m unittest tests.test_shared_views tests.test_web_service
rtk python -m unittest discover -s tests
```
