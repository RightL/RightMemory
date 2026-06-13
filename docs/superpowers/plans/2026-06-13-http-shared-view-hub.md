# HTTP Shared View Hub Implementation Plan

> For agentic workers: use `superpowers:subagent-driven-development` task-by-task. Keep tests focused per task; the controller coordinates broad suite runs so workers do not repeatedly run the same expensive checks.

**Goal:** Implement the first self-hosted HTTP hub for RightMemory shared views, plus the RightMemory-side HTTP adapter needed for publish, accept, retrieve, note, and inbox workflows.

**Architecture:** The hub is a separate FastAPI service rooted at a hub directory. SQLite owns metadata, package files stay visible on disk under immutable version directories, and RightMemory memory roots store safe resolver metadata in `shared_views.toml` while bearer credentials stay under local runtime state.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, SQLite, TOML package artifacts, existing RightMemory CLI/runtime patterns, `unittest`, and FastAPI `TestClient`.

---

## Scope Check

This plan implements the HTTP hub service and the client adapter needed by the CLI and Web Studio. It includes:

- hub initialization, startup app factory, SQLite migrations, token hashing, audit records, and package storage;
- provider publish tokens, invitation tokens, accepted connection tokens, rotation/revocation primitives, and scoped auth checks;
- immutable view versions with a current pointer;
- deterministic retrieval from hosted package snapshots;
- consumer interactions and provider inbox reads;
- RightMemory credential storage under `.runtime/shared_views/credentials.json`;
- CLI commands for hub service setup and HTTP shared-view workflows.

This plan does not implement SaaS tenancy, billing, broad user accounts, global discovery, live provider-root relay, or a polished hub admin UI.

## File Structure

- Create `rightmemory/hub/__init__.py`: public hub imports.
- Create `rightmemory/hub/models.py`: dataclasses and response helpers.
- Create `rightmemory/hub/store.py`: SQLite schema, migrations, persistence, token hashing, audit helpers.
- Create `rightmemory/hub/packages.py`: package validation, safe copy, manifest loading, lexical retrieval.
- Create `rightmemory/hub/app.py`: FastAPI app factory and HTTP route handlers.
- Create `rightmemory/hub/client.py`: urllib-based hub client for RightMemory adapter code.
- Modify `rightmemory/shared_views.py`: add HTTP target metadata, runtime credential store, publish/accept/retrieve/note/inbox adapter helpers.
- Modify `rightmemory/cli.py`: add `rightmemory hub ...`, `shared-view publish-http`, HTTP invitation accept support, and HTTP inbox/list helpers.
- Modify `pyproject.toml`: add FastAPI and Uvicorn dependencies.
- Create `tests/test_http_hub.py`: hub service/store/package behavior.
- Create or extend `tests/test_shared_views.py`: HTTP adapter and credential-boundary behavior.
- Extend `tests/test_cli.py`: hub and shared-view HTTP command coverage.
- Update `README.md` and `AGENTS.md`: document new commands and operational boundary.

## Cross-Agent Verification Rule

- Workers run the focused tests for their slice, such as `uv run python -m unittest tests.test_http_hub`.
- If another worker has already run a broad suite, later workers should mention that and skip repeating it unless their change touches shared infrastructure.
- The controller runs the final compile check and full test suite.

## Task 1: Hub Store, Tokens, And Package Validation

**Files:**
- Create `rightmemory/hub/models.py`
- Create `rightmemory/hub/store.py`
- Create `rightmemory/hub/packages.py`
- Create `rightmemory/hub/__init__.py`
- Create `tests/test_http_hub.py`
- Modify `pyproject.toml`

- [ ] **Step 1: Write failing tests for core storage**

Add tests that create a temporary hub root and assert:

```python
store = HubStore(root)
store.initialize(admin_token="admin-secret")
provider_token = store.create_provider_token("alice", label="publish")
self.assertTrue(store.verify_token(provider_token.raw_token, action="publish", provider_id="alice"))
self.assertFalse(store.verify_token("wrong", action="publish", provider_id="alice"))
self.assertIn("token.created", [event.kind for event in store.list_audit_events()])
```

Also assert revoked tokens fail, token rows store hashes rather than raw token strings, and a new empty root receives `hub.db`, `storage/`, and runtime/config files.

- [ ] **Step 2: Write failing tests for package validation**

Create a minimal package with `view.md`, `export.toml`, `rightmemory-shared-view.toml`, and `dist/MEMORY.md`. Assert valid packages load a manifest-like object. Add rejection tests for:

- missing required files;
- path traversal entries when copying package contents;
- symlinked files that escape the package root;
- packages over the configured size limit.

- [ ] **Step 3: Implement models and SQLite migrations**

Use a small migration table and create tables for providers, views, view_versions, invitations, connections, tokens, interactions, and audit_events. Keep IDs text-based, UTC timestamps as ISO strings, and token verifier material as SHA-256 hashes with a per-token nonce.

Store a minimal config file under the hub root for limits and public base URL. Use `secrets.token_urlsafe(32)` for new tokens and return raw tokens once from creation helpers.

- [ ] **Step 4: Implement package validation and immutable copy**

Validate package files by walking the source directory with `Path.rglob("*")`, rejecting symlinks that resolve outside the source root and any relative path containing `..`. Copy files into a temporary version directory, then atomically rename into `storage/views/<view-id>/versions/<version-id>/`.

Use `export.toml` or invitation metadata to confirm `view_id` matches the publish target. Compute a package hash over relative paths and bytes so immutable versions are content-addressable enough for audit/debugging.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_http_hub.HubStoreTests tests.test_http_hub.HubPackageTests
```

Expected: the new storage/package tests pass.

- [ ] **Step 6: Commit**

Commit message:

```bash
git commit -m "feat: add http shared view hub storage"
```

## Task 2: Hub FastAPI Routes

**Files:**
- Modify `rightmemory/hub/app.py`
- Modify `rightmemory/hub/store.py`
- Modify `rightmemory/hub/packages.py`
- Extend `tests/test_http_hub.py`

- [ ] **Step 1: Write failing route tests**

Use FastAPI `TestClient` against `create_hub_app(hub_root)`. Cover:

- `GET /health` returns an initialized status;
- provider token can publish a package through `POST /api/views/{view_id}/versions`;
- publish creates a new immutable version and updates `current_version_id`;
- `POST /api/views/{view_id}/invitations` returns a URL with `/i/<token>`;
- `GET /api/invitations/{token}/view` describes the intended view;
- `POST /api/invitations/{token}/accept` returns an accepted connection token;
- accepted token can retrieve the intended view;
- accepted token cannot retrieve another view;
- accepted token can post an interaction;
- provider/admin token can read the provider inbox.

- [ ] **Step 2: Implement route auth helpers**

Read bearer tokens from `Authorization: Bearer <token>`. Keep route code small by using helpers such as:

```python
token = _bearer_token(request)
actor = store.require_token(token, action="publish", provider_id=provider_id, view_id=view_id)
```

Write failed-auth audit events without echoing raw token material.

- [ ] **Step 3: Implement publish/invitation/accept routes**

Use package validation from Task 1. Each publish creates a version row and updates the view's current pointer in one SQLite transaction. Invitation creation stores a token hash scoped to the view. Accept creates a connection row and returns the accepted connection id plus raw accepted token.

- [ ] **Step 4: Implement retrieval and interaction routes**

Retrieval should run deterministic lexical matching over the current version's `dist/MEMORY.md`, return bounded snippets, and include view id, version id, freshness, and provenance. Interaction posting stores JSON payload fields with actor/message/task context.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_http_hub.HubApiTests
```

Skip the broad suite here unless Task 1 did not already run its focused tests.

- [ ] **Step 6: Commit**

Commit message:

```bash
git commit -m "feat: serve http shared view hub api"
```

## Task 3: RightMemory HTTP Adapter And Credential Boundary

**Files:**
- Modify `rightmemory/shared_views.py`
- Create or modify `tests/test_shared_views.py`

- [ ] **Step 1: Write failing adapter tests**

Add tests for:

- saving/loading a connection target with `kind = "http"`, `base_url`, `credential_id`, `view_id`, and optional `version_id`;
- `shared_views.toml` contains the credential id but not raw bearer tokens;
- credential storage writes raw tokens under `.runtime/shared_views/credentials.json` with local file permissions;
- HTTP retrieval uses the stored accepted connection token and formats hub snippets with provenance;
- HTTP notes send through the hub interaction endpoint and still require confirmation for human/external relationships.

Represent the expected synced target shape as:

```toml
[connections."alice-auth-api".target]
kind = "http"
base_url = "https://hub.example.test"
view_id = "alice-auth-api"
credential_id = "conn-alice-auth-api"
version_id = "v1"
```

- [ ] **Step 2: Extend target model safely**

Add optional fields to `SharedViewTarget` for `base_url`, `credential_id`, `version_id`, and `accepted_from_url`. Keep existing target kinds loading unchanged. Add `"http"` to `TARGET_KINDS` and validate that HTTP targets have `base_url`, `view_id`, and `credential_id`.

- [ ] **Step 3: Add credential store helpers**

Store local secrets at `.runtime/shared_views/credentials.json`:

```json
{
  "credentials": {
    "conn-alice-auth-api": {
      "kind": "http-connection",
      "token": "...",
      "created_at": "..."
    }
  }
}
```

Write atomically, set file mode `0600` when the platform allows it, and keep helper APIs narrow: save, read, delete, list metadata without tokens.

- [ ] **Step 4: Add HTTP retrieve/note/inbox adapter functions**

Use `rightmemory.hub.client.HubClient` so shared-view code does not know HTTP request details. Convert hub retrieval JSON into `_SharedViewCache` and route interactions through `_deliver_interaction_record`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_shared_views.SharedViewRegistryTests tests.test_shared_views.SharedViewHttpAdapterTests
```

- [ ] **Step 6: Commit**

Commit message:

```bash
git commit -m "feat: add http shared view adapter"
```

## Task 4: CLI Hub Lifecycle And HTTP Workflows

**Files:**
- Modify `rightmemory/cli.py`
- Modify `rightmemory/shared_views.py`
- Extend `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Cover:

- `rightmemory hub init <hub-root>` creates the hub and prints the root;
- `rightmemory hub token create <hub-root> --provider alice --label publish` prints a raw token once;
- `rightmemory hub serve <hub-root> --host 127.0.0.1 --port 8765` calls Uvicorn with the app factory;
- `rightmemory shared-view publish-http alice-auth-api --hub-url http://hub.test --credential-id alice-publisher` calls the HTTP client with the package created from the local view;
- `rightmemory shared-view accept-invite http://hub.test/i/token` stores an HTTP target and local credential;
- `rightmemory shared-view retrieve` and `note` work through an HTTP target.

- [ ] **Step 2: Add `rightmemory hub` parser branch**

Add the top-level `hub` branch before role parsing. Use explicit subcommands:

```text
rightmemory hub init <hub-root>
rightmemory hub serve <hub-root> --host 127.0.0.1 --port 8765
rightmemory hub status <hub-root>
rightmemory hub token create <hub-root> --provider <id> --label <label>
rightmemory hub token revoke <hub-root> <token-id>
```

- [ ] **Step 3: Add shared-view HTTP commands**

Add:

```text
rightmemory shared-view publish-http <view-id> --hub-url <url> --credential-id <id>
rightmemory shared-view inbox-http --hub-url <url> --credential-id <id> --view-id <view-id>
```

Extend `accept-invite` so HTTP/HTTPS invitation URLs use the HTTP adapter path while local filesystem paths keep the existing package behavior.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_cli.SharedViewCliTests tests.test_cli.HubCliTests
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: wire http hub cli workflows"
```

## Task 5: Docs And Integration Verification

**Files:**
- Modify `README.md`
- Modify `AGENTS.md`
- Optionally update `docs/superpowers/specs/2026-06-13-http-shared-view-hub-design.md` if implementation clarified route names without changing intent.

- [ ] **Step 1: Document the implemented lifecycle**

Add concise operational docs for:

- hub root layout;
- init, serve, status, token create/revoke;
- provider publish;
- consumer accept/retrieve/note;
- credential storage boundary;
- reverse-proxy HTTPS expectation for internet exposure.

- [ ] **Step 2: Update agent notes**

Add the new commands to `AGENTS.md` development/debugging notes without turning design explanation into operational instructions.

- [ ] **Step 3: Run final HTTP hub verification**

Run:

```bash
uv run python -m compileall -q rightmemory tests
uv run python -m unittest tests.test_http_hub tests.test_shared_views tests.test_cli
```

The controller runs the full suite after Web Studio lands.

- [ ] **Step 4: Commit**

Commit message:

```bash
git commit -m "docs: document http shared view hub"
```
