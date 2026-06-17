# Web Studio Shared View Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Web Studio provider inbox, publish-event visibility, pull-all/status-all controls, and sanitized credential pickers for the active memory root.

**Architecture:** Keep Web Studio scoped to one active memory root. Add two small read helpers for local runtime state, then expose focused Web Studio service and API methods that reuse existing shared-view helpers. Update the static UI to present workflow controls without adding hub bootstrap or hub-wide admin behavior.

**Tech Stack:** Python 3.11 standard library, FastAPI, existing vanilla JavaScript Web Studio shell, `unittest`, TOML via `tomllib`, JSONL runtime files.

---

## Scope Check

This plan only covers Web Studio workflow completeness for the active root. It does not implement a Hub Console, hub bootstrap UI, revocation, audit browsing, inbox replies, or interaction resolution.

## File Structure

- Modify `rightmemory/shared_view_models.py`: add `list_shared_view_credentials(...)` that returns sanitized credential summaries.
- Modify `rightmemory/shared_view_files.py`: add `list_file_view_publish_events(...)` that reads publish-events JSONL newest first.
- Modify `rightmemory/web/service.py`: expose credential summaries, provider inbox, publish events, pull-all, and status-all through `WebStudioService`.
- Modify `rightmemory/web/app.py`: add Web Studio API routes for provider inbox, publish events, pull-all, and status-all.
- Modify `rightmemory/web/static/app.js`: add UI controls and handlers for provider inbox, publish events, pull-all, status-all, and credential selects.
- Modify `rightmemory/web/static/styles.css`: add compact result-list styling if the current styles do not already support grouped records.
- Modify `tests/test_shared_views.py`: test credential summary sanitization and publish event reading.
- Modify `tests/test_web_service.py`: test new Web Studio APIs and static shell hooks.
- Modify `docs/shared-views-usage.md`: mention Web Studio support for provider inbox, publish events, pull-all, and status-all.

## Task 1: Credential Summary Reader

**Files:**
- Modify: `rightmemory/shared_view_models.py`
- Modify: `tests/test_shared_views.py`

- [ ] **Step 1: Add failing tests for sanitized credential summaries**

Append this test to the existing shared-view model or credential tests in `tests/test_shared_views.py`:

```python
    def test_list_shared_view_credentials_omits_tokens(self):
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="secret-token",
            base_url="https://hub.example.test",
            provider_id="alice",
            view_id="auth-api-files",
        )

        credentials = list_shared_view_credentials(self.root)

        self.assertEqual(
            credentials,
            [
                {
                    "credential_id": "alice-publish",
                    "kind": "http-publish",
                    "base_url": "https://hub.example.test",
                    "provider_id": "alice",
                    "view_id": "auth-api-files",
                    "created_at": credentials[0]["created_at"],
                }
            ],
        )
        self.assertNotIn("token", credentials[0])
```

Add `list_shared_view_credentials` to the import list at the top of `tests/test_shared_views.py`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewModelTests.test_list_shared_view_credentials_omits_tokens
```

Expected: FAIL with an import error or attribute error for `list_shared_view_credentials`.

- [ ] **Step 3: Implement the credential summary reader**

Add this function near `load_shared_view_credential(...)` in `rightmemory/shared_view_models.py`:

```python
def list_shared_view_credentials(memory_root: Path) -> list[dict[str, str]]:
    root = Path(memory_root).expanduser()
    credentials = _load_credentials(root).get("credentials", {})
    if not isinstance(credentials, dict):
        raise ValueError("shared view credential store is invalid")
    summaries: list[dict[str, str]] = []
    for credential_id in sorted(credentials):
        raw = credentials[credential_id]
        if not isinstance(raw, dict):
            continue
        summary: dict[str, str] = {"credential_id": validate_heading_id(str(credential_id))}
        for key in ("kind", "base_url", "provider_id", "view_id", "created_at"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                summary[key] = value
        summaries.append(summary)
    return summaries
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewModelTests.test_list_shared_view_credentials_omits_tokens
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
rtk git add rightmemory/shared_view_models.py tests/test_shared_views.py
rtk git commit -m "feat: list shared view credential summaries"
```

## Task 2: Publish Event Reader

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Modify: `tests/test_shared_views.py`

- [ ] **Step 1: Add failing tests for publish-event reading**

Append this test to `SharedFileViewAutoPublishTests` in `tests/test_shared_views.py`:

```python
    def test_list_file_view_publish_events_returns_newest_valid_events(self):
        events = self.root / ".runtime" / "shared_views" / "publish-events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(
            '{"created_at":"2026-06-17T10:00:00+00:00","view_id":"old","status":"published","message":"old","trigger":"memory-write"}\n'
            'not json\n'
            '{"created_at":"2026-06-17T11:00:00+00:00","view_id":"new","status":"failed","message":"boom","trigger":"memory-write"}\n',
            encoding="utf-8",
        )

        listed = list_file_view_publish_events(self.root)

        self.assertEqual([event["view_id"] for event in listed], ["new", "old"])
        self.assertEqual(listed[0]["status"], "failed")
```

Add `list_file_view_publish_events` to the import list at the top of `tests/test_shared_views.py`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewAutoPublishTests.test_list_file_view_publish_events_returns_newest_valid_events
```

Expected: FAIL with an import error or attribute error for `list_file_view_publish_events`.

- [ ] **Step 3: Implement the publish-event reader**

Add this function after `record_file_view_publish_results(...)` in `rightmemory/shared_view_files.py`:

```python
def list_file_view_publish_events(memory_root: Path, *, limit: int = 50) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    path = root / ".runtime" / "shared_views" / "publish-events.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    records.reverse()
    return records[: max(0, int(limit))]
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewAutoPublishTests.test_list_file_view_publish_events_returns_newest_valid_events
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
rtk git add rightmemory/shared_view_files.py tests/test_shared_views.py
rtk git commit -m "feat: list file view publish events"
```

## Task 3: Web Studio Service Methods

**Files:**
- Modify: `rightmemory/web/service.py`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Add failing service/API tests**

Add these tests to `WebStudioSharedViewApiTests` in `tests/test_web_service.py`:

```python
    def test_shared_views_include_sanitized_credentials(self):
        response = self.client.post(
            "/api/share/credentials",
            json={
                "credential_id": "alice-publish",
                "kind": "http-publish",
                "hub_url": "https://hub.example.test",
                "provider_id": "alice",
                "token": "secret-token",
            },
            headers={"x-csrf-token": self.csrf},
        )

        listing = self.client.get("/api/share/views")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(listing.status_code, 200)
        credentials = listing.json()["data"]["credentials"]
        self.assertEqual(credentials[0]["credential_id"], "alice-publish")
        self.assertEqual(credentials[0]["base_url"], "https://hub.example.test")
        self.assertEqual(credentials[0]["provider_id"], "alice")
        self.assertNotIn("token", credentials[0])

    def test_provider_inbox_uses_saved_credential_defaults(self):
        self.client.post(
            "/api/share/credentials",
            json={
                "credential_id": "alice-publish",
                "kind": "http-publish",
                "hub_url": "https://hub.example.test",
                "provider_id": "alice",
                "token": "secret-token",
            },
            headers={"x-csrf-token": self.csrf},
        )
        with patch("rightmemory.web.service.list_http_shared_view_inbox") as inbox:
            inbox.return_value = [
                {
                    "interaction_id": "int-1",
                    "view_id": "auth-api-files",
                    "connection_id": "conn-1",
                    "payload": {"message": "Docs are stale"},
                }
            ]
            response = self.client.post(
                "/api/share/provider-inbox",
                json={"credential_id": "alice-publish"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        inbox.assert_called_once_with(
            self.root.resolve(),
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            provider_id="alice",
        )
        self.assertEqual(response.json()["data"]["interactions"][0]["view_id"], "auth-api-files")

    def test_publish_events_pull_all_and_status_all_api(self):
        with patch("rightmemory.web.service.list_file_view_publish_events") as events:
            events.return_value = [{"view_id": "auth-api-files", "status": "published"}]
            events_response = self.client.get("/api/share/publish-events")

        with patch("rightmemory.web.service.pull_all_file_views") as pull_all:
            pull_all.return_value = [FileViewPullResult("auth-api-files", "pulled", "file view pulled")]
            pull_response = self.client.post(
                "/api/use/connections/pull-all",
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.shared_view_connection_status") as status:
            status.return_value = {
                "heading_id": "auth-api-files",
                "type": "file",
                "target": "http-file",
                "status": "imported",
                "message": "file view import is available",
            }
            with patch("rightmemory.web.service.load_connections") as connections:
                connection = type("Connection", (), {"heading_id": "auth-api-files"})()
                connections.return_value = {"auth-api-files": connection}
                status_response = self.client.get("/api/use/connections/status-all")

        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(events_response.json()["data"]["events"][0]["status"], "published")
        self.assertEqual(pull_response.status_code, 200)
        self.assertEqual(pull_response.json()["data"]["results"][0]["status"], "pulled")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["data"]["statuses"][0]["status"], "imported")
```

- [ ] **Step 2: Run the API tests and verify they fail**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioSharedViewApiTests.test_shared_views_include_sanitized_credentials tests.test_web_service.WebStudioSharedViewApiTests.test_provider_inbox_uses_saved_credential_defaults tests.test_web_service.WebStudioSharedViewApiTests.test_publish_events_pull_all_and_status_all_api
```

Expected: FAIL because the new service methods and routes are not available.

- [ ] **Step 3: Update `rightmemory/web/service.py` imports**

Change the imports to include the new helpers:

```python
from ..shared_view_files import (
    approve_file_view,
    invite_file_view,
    list_file_view_publish_events,
    pull_all_file_views,
    pull_file_view,
)
from ..shared_view_models import load_shared_view_credential, list_shared_view_credentials
from ..shared_views import (
    accept_http_shared_view_invitation,
    accept_shared_view_invitation,
    list_http_shared_view_inbox,
    list_shared_view_inbox,
    list_shared_view_notes,
    load_connections,
    provider_view_summaries,
    record_shared_view_note,
    save_shared_view_credential,
    shared_view_connection_status,
)
```

- [ ] **Step 4: Add service methods**

In `WebStudioService.shared_views(...)`, include credentials:

```python
    def shared_views(self) -> dict[str, Any]:
        return {
            "provider_views": provider_view_summaries(self.memory_root),
            "connections": [_json_safe(connection) for connection in load_connections(self.memory_root).values()],
            "credentials": list_shared_view_credentials(self.memory_root),
        }
```

Add these methods near the existing shared-view methods:

```python
    def provider_http_inbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        credential_id = _required_payload_str(payload, "credential_id")
        credential = load_shared_view_credential(self.memory_root, credential_id)
        hub_url = _optional_payload_str(payload, "hub_url") or credential.get("base_url")
        provider_id = _optional_payload_str(payload, "provider_id") or credential.get("provider_id")
        if not hub_url:
            raise ValueError("provider inbox requires a hub URL")
        if not provider_id:
            raise ValueError("provider inbox requires a provider id")
        return {
            "interactions": list_http_shared_view_inbox(
                self.memory_root,
                hub_url=hub_url,
                credential_id=credential_id,
                provider_id=provider_id,
            )
        }

    def publish_events(self) -> dict[str, Any]:
        return {"events": list_file_view_publish_events(self.memory_root)}

    def pull_all_connections(self) -> dict[str, Any]:
        return {"results": [_json_safe(result) for result in pull_all_file_views(self.memory_root)]}

    def connection_statuses(self) -> dict[str, Any]:
        return {
            "statuses": [
                shared_view_connection_status(self.memory_root, connection.heading_id)
                for connection in load_connections(self.memory_root).values()
            ]
        }
```

- [ ] **Step 5: Run the focused tests and verify service-level failures remain route-only if routes are missing**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioSharedViewApiTests.test_shared_views_include_sanitized_credentials
```

Expected: PASS after helper tasks are complete.

- [ ] **Step 6: Commit after routes are added in Task 4**

Do not commit this task alone unless the route tests already pass. The service methods are wired by Task 4.

## Task 4: Web Studio API Routes

**Files:**
- Modify: `rightmemory/web/app.py`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Add provider inbox and publish-events routes**

In `rightmemory/web/app.py`, after `publish_question_view(...)` and before `/api/share/questions/{view_id}/ask`, add:

```python
    @app.post("/api/share/provider-inbox")
    def provider_http_inbox(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.provider_http_inbox(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not load provider inbox", technical=str(exc)),
            ) from exc
        return ok_response("provider inbox loaded", data)

    @app.get("/api/share/publish-events")
    def publish_events(service=Depends(current_service)):
        return ok_response("publish events loaded", service.publish_events())
```

- [ ] **Step 2: Add pull-all and status-all routes**

In `rightmemory/web/app.py`, place these routes before the per-connection routes:

```python
    @app.post("/api/use/connections/pull-all")
    def pull_all_connections(
        request: Request,
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.pull_all_connections()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not pull shared views", technical=str(exc)),
            ) from exc
        return ok_response("shared views pulled", data)

    @app.get("/api/use/connections/status-all")
    def connection_statuses(service=Depends(current_service)):
        return ok_response("shared view statuses loaded", service.connection_statuses())
```

- [ ] **Step 3: Run the route tests and verify they pass**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioSharedViewApiTests.test_shared_views_include_sanitized_credentials tests.test_web_service.WebStudioSharedViewApiTests.test_provider_inbox_uses_saved_credential_defaults tests.test_web_service.WebStudioSharedViewApiTests.test_publish_events_pull_all_and_status_all_api
```

Expected: PASS.

- [ ] **Step 4: Commit**

Run:

```bash
rtk git add rightmemory/web/service.py rightmemory/web/app.py tests/test_web_service.py
rtk git commit -m "feat: add shared view ops web APIs"
```

## Task 5: Web Studio UI

**Files:**
- Modify: `rightmemory/web/static/app.js`
- Modify: `rightmemory/web/static/styles.css`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Add failing static-shell assertions**

Extend `WebStudioStaticTests.test_static_shell_loads_assets` with:

```python
        self.assertIn("provider-inbox-form", script.text)
        self.assertIn("publish-events-panel", script.text)
        self.assertIn("pull-all-connections", script.text)
        self.assertIn("status-all-connections", script.text)
        self.assertIn("credential-select", script.text)
```

- [ ] **Step 2: Run the static test and verify it fails**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioStaticTests.test_static_shell_loads_assets
```

Expected: FAIL because the new UI hooks are absent.

- [ ] **Step 3: Add credential option helpers in `app.js`**

Near `renderOptions(...)`, add:

```javascript
function credentialLabel(credential) {
  const parts = [credential.credential_id || ""];
  if (credential.kind) {
    parts.push(credential.kind);
  }
  if (credential.base_url) {
    parts.push(credential.base_url);
  }
  return parts.filter(Boolean).join(" | ");
}

function renderCredentialOptions(credentials) {
  return renderOptions(
    credentials.map((credential) => ({
      value: credential.credential_id,
      label: credentialLabel(credential),
    })),
  );
}
```

- [ ] **Step 4: Update `renderSharedViews(...)` data extraction**

Inside `renderSharedViews(...)`, after connections are loaded, add:

```javascript
  const credentials = payload.data.credentials || [];
  const credentialOptions = renderCredentialOptions(credentials);
  const hasCredentials = credentials.length > 0;
```

- [ ] **Step 5: Add UI controls to the Shared Views panel**

In the returned Shared Views HTML, add these controls inside the `Use a Connected View` panel button rows:

```html
            <button id="pull-all-connections" type="button"${hasFileConnections ? "" : " disabled"}>Pull All</button>
            <button id="status-all-connections" type="button"${connections.length ? "" : " disabled"}>Status All</button>
```

Add this provider inbox section after the `Use a Connected View` panel:

```html
      <section class="panel flow-panel">
        <div class="section-heading">
          <span class="step-badge">8</span>
          <div>
            <h2>Provider Inbox</h2>
          </div>
        </div>
        <form id="provider-inbox-form" class="guided-form">
          <label>
            Credential
            <select class="credential-select" name="credential_id">${credentialOptions}</select>
          </label>
          <details class="advanced">
            <summary>Provider override</summary>
            <label>
              HTTP hub URL
              <input name="hub_url" placeholder="from credential">
            </label>
            <label>
              Provider id
              <input name="provider_id" placeholder="from credential">
            </label>
          </details>
          <div class="button-row">
            <button class="primary" type="submit"${hasCredentials ? "" : " disabled"}>Load Inbox</button>
          </div>
        </form>
      </section>
```

Add this publish-events panel before the result panel:

```html
    <section class="panel wide" id="publish-events-panel">
      <div class="section-heading">
        <h2>Auto-Publish Events</h2>
      </div>
      <div id="publish-events-list"><p>Load events to inspect recent file-view publishing.</p></div>
      <div class="button-row">
        <button id="load-publish-events" type="button">Load Events</button>
      </div>
    </section>
```

For build, invite, and publish forms, replace manual credential id inputs with a `select` using `credentialOptions`, while keeping hub URL override inputs where the backend accepts them.

Use this label wherever the form needs `credential_id`:

```html
          <label>
            Credential
            <select class="credential-select" name="credential_id">${credentialOptions}</select>
          </label>
```

For `build-file-view-form`, keep `hub_url` as a normal input because the builder currently requires it:

```html
          <label>
            HTTP hub URL
            <input name="hub_url" placeholder="https://hub.example.test" required>
          </label>
          <label>
            Credential
            <select class="credential-select" name="credential_id">${credentialOptions}</select>
          </label>
```

For `invite-file-view-form`, keep both fields inside the advanced override:

```html
            <label>
              HTTP hub URL
              <input name="hub_url" placeholder="from recipe">
            </label>
            <label>
              Credential
              <select class="credential-select" name="credential_id">${credentialOptions}</select>
            </label>
```

For `publish-question-view-form`, keep the hub URL and question base URL inputs, and use the credential selector:

```html
          <label>
            HTTP hub URL
            <input name="hub_url" placeholder="https://hub.example.test" required>
          </label>
          <label>
            Credential
            <select class="credential-select" name="credential_id">${credentialOptions}</select>
          </label>
          <label>
            Question base URL
            <input name="question_base_url" placeholder="https://provider.example.test" required>
          </label>
```

- [ ] **Step 6: Add JavaScript handlers**

In `attachSharedViewHandlers()`, add:

```javascript
  const pullAllButton = document.querySelector("#pull-all-connections");
  if (pullAllButton) {
    pullAllButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/use/connections/pull-all", { method: "POST" });
        showSharedViewResult(JSON.stringify(payload.data.results || [], null, 2));
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const statusAllButton = document.querySelector("#status-all-connections");
  if (statusAllButton) {
    statusAllButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/use/connections/status-all");
        showSharedViewResult(JSON.stringify(payload.data.statuses || [], null, 2));
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const providerInboxForm = document.querySelector("#provider-inbox-form");
  if (providerInboxForm) {
    providerInboxForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/provider-inbox", {
          method: "POST",
          body: JSON.stringify({
            credential_id: form.get("credential_id"),
            hub_url: form.get("hub_url"),
            provider_id: form.get("provider_id"),
          }),
        });
        showSharedViewResult(JSON.stringify(payload.data.interactions || [], null, 2));
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }

  const loadPublishEventsButton = document.querySelector("#load-publish-events");
  if (loadPublishEventsButton) {
    loadPublishEventsButton.addEventListener("click", async () => {
      try {
        const payload = await fetchJson("/api/share/publish-events");
        const target = document.querySelector("#publish-events-list");
        if (target) {
          target.innerHTML = renderItems((payload.data.events || []).map((event) => ({
            label: `${event.created_at || ""} ${event.view_id || ""} ${event.status || ""}: ${event.message || ""}`,
          })));
        }
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
```

- [ ] **Step 7: Run the static test and verify it passes**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioStaticTests.test_static_shell_loads_assets
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
rtk git add rightmemory/web/static/app.js rightmemory/web/static/styles.css tests/test_web_service.py
rtk git commit -m "feat: add shared view ops controls to web studio"
```

## Task 6: Usage Documentation

**Files:**
- Modify: `docs/shared-views-usage.md`

- [ ] **Step 1: Update the Web Studio coverage section**

In `docs/shared-views-usage.md`, update the Web Studio section to state that Web Studio covers:

```text
- saved HTTP hub credentials without showing stored tokens
- build/approve/invite MF file views
- build/approve/publish MQ question views
- accept invitations
- pull or status one MF connection
- pull all MF connections
- status all shared-view connections
- ask one MQ connection
- send provider-visible notes
- read provider HTTP inbox for the active provider root
- inspect recent MF auto-publish events
```

Keep hub bootstrap and hub-wide administration listed as CLI or future Hub Console surfaces.

- [ ] **Step 2: Run a docs grep check**

Run:

```bash
rtk rg -n "Provider Inbox|Auto-Publish Events|pull all|status all|hub init|hub serve" docs/shared-views-usage.md
```

Expected: output includes the Web Studio capabilities and still lists hub bootstrap as non-Web-Studio scope.

- [ ] **Step 3: Commit**

Run:

```bash
rtk git add docs/shared-views-usage.md
rtk git commit -m "docs: document shared view ops in web studio"
```

## Task 7: Full Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run compile verification**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: exit code 0.

- [ ] **Step 2: Run focused shared-view and web tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views tests.test_web_service
```

Expected: exit code 0.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: exit code 0. Existing skipped tests and known deprecation warnings are acceptable if there are no failures or errors.

- [ ] **Step 4: Check the worktree**

Run:

```bash
rtk git status --short --branch
```

Expected: clean working tree on the feature branch.
