# RightMemory Web Studio Implementation Plan

> For agentic workers: use `superpowers:subagent-driven-development` task-by-task. Keep verification coordinated: workers run focused tests for their changed slice, while the controller owns the final compile check and full suite.

**Goal:** Implement a LAN-capable Web Studio for RightMemory that exposes status, memory/insight/log browsing, shared-view workflows, settings, and managed web service controls through a browser UI.

**Architecture:** Web Studio is a managed FastAPI service under a memory root. The backend exposes product APIs over existing RightMemory functions and bounded artifact readers. A packaged vanilla HTML/CSS/JS interface calls those APIs. Shared-view operations reuse package, local, mounted-folder hub, and HTTP hub adapters rather than creating a separate web-only model.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, vanilla JS/CSS/HTML packaged in `rightmemory/web/static`, existing RightMemory CLI/status/watch/shared-view modules, `unittest`, and FastAPI `TestClient`.

---

## Scope Check

This plan implements the first coherent Web Studio release:

- token login, signed session cookie, CSRF for write APIs, closed CORS, and explicit host binding;
- active root/profile session state;
- overview/status/watch/update/insight summaries;
- bounded memory, insight, log, and activity artifact readers;
- provider and consumer shared-view APIs over existing adapters, including HTTP hub support when the HTTP plan is implemented;
- local web settings for host, port, actor, display preferences, and saved hubs;
- `rightmemory web start|status|stop|restart`;
- a usable static UI with navigation for Overview, Shared Views, Memory, Insights, Activity, Status, and Settings.

This plan does not implement a full memory prose editor, model/config editor, replacement chat client, multi-user account system, or global discovery.

## File Structure

- Create `rightmemory/web/__init__.py`
- Create `rightmemory/web/auth.py`: operator token, session signing, CSRF helpers.
- Create `rightmemory/web/models.py`: response DTO helpers and JSON-safe serializers.
- Create `rightmemory/web/readers.py`: allowlisted memory/insight/log/activity readers.
- Create `rightmemory/web/service.py`: Web Studio service over RightMemory modules.
- Create `rightmemory/web/app.py`: FastAPI app factory and routes.
- Create `rightmemory/web/process.py`: managed web process status/start/stop helpers.
- Create `rightmemory/web/static/index.html`
- Create `rightmemory/web/static/app.js`
- Create `rightmemory/web/static/styles.css`
- Modify `rightmemory/cli.py`: add `rightmemory web start|status|stop|restart`.
- Modify `rightmemory/watch.py` only if optional watch integration is implemented.
- Modify `pyproject.toml`: ensure package data and web dependencies are included.
- Create `tests/test_web_auth.py`
- Create `tests/test_web_service.py`
- Create `tests/test_web_cli.py`
- Update `README.md` and `AGENTS.md`.

## Cross-Agent Verification Rule

- Workers run focused tests for their own area, such as `uv run python -m unittest tests.test_web_auth`.
- Workers should not run the full suite if another worker or the controller already owns that broad verification pass.
- The controller runs `uv run python -m compileall -q rightmemory tests` and the full `uv run python -m unittest discover -s tests` after all Web Studio tasks land.

## Task 1: Auth, Settings, And App Shell

**Files:**
- Create `rightmemory/web/auth.py`
- Create `rightmemory/web/models.py`
- Create `rightmemory/web/app.py`
- Create `rightmemory/web/service.py`
- Create `rightmemory/web/static/index.html`
- Modify `pyproject.toml`
- Create `tests/test_web_auth.py`

- [ ] **Step 1: Write failing auth tests**

Use `TestClient(create_web_app(memory_root=root))` and assert:

- `GET /api/session` returns unauthenticated state before login;
- protected read APIs return `401` before login;
- `POST /api/login` with the operator token sets an `HttpOnly`, `SameSite` session cookie and returns a CSRF token;
- write APIs reject missing or wrong CSRF token;
- write APIs accept the returned CSRF token;
- CORS headers are absent by default.

- [ ] **Step 2: Implement local secret settings**

Store Web Studio local state under `<memory-root>/.runtime/web/`:

```text
operator-token.sha256
session-secret
settings.json
web.pid
web.log
```

Generate the operator token when missing and hash it on disk. Expose a helper that can return the one-time generated token to CLI `web start` output. Use HMAC signing for session cookies and CSRF tokens with the session secret.

- [ ] **Step 3: Implement app factory and static shell**

`create_web_app(memory_root, *, default_root=None)` returns a FastAPI app. Serve `/` from packaged `index.html`, static assets under `/static`, and JSON routes under `/api`. The first static UI can render navigation and load `/api/session`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_web_auth
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: add web studio auth shell"
```

## Task 2: Overview, Status, Memory, Insights, And Logs

**Files:**
- Create `rightmemory/web/readers.py`
- Modify `rightmemory/web/service.py`
- Modify `rightmemory/web/app.py`
- Create `tests/test_web_service.py`

- [ ] **Step 1: Write failing service tests**

Cover:

- `/api/overview` returns active root, git summary, watch summaries, update state, insight state, shared-view counts, and recent issues;
- `/api/status` returns the structured status dashboard;
- `/api/memory/files` lists `MEMORY.md`, `MEMORY_*.md`, provider source files, generated previews, and imported package snapshots through stable ids;
- `/api/memory/files/{file_id}` rejects arbitrary paths and returns bounded Markdown preview for known ids;
- `/api/insights` lists `insight_logs/*.md` by recency;
- `/api/logs` lists known watch/web logs;
- `/api/logs/{log_id}` returns bounded tail text and rejects unknown ids.

- [ ] **Step 2: Implement allowlisted readers**

Build file ids server-side from known roots. Keep previews bounded by byte and line limits. Treat unreadable/missing files as structured not-found responses rather than raw tracebacks.

- [ ] **Step 3: Implement overview/status serializers**

Reuse `collect_status`, `load_connections`, `list_shared_view_notes`, and `list_shared_view_inbox`. Convert dataclasses and paths into JSON-safe dictionaries with relative display paths where useful.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_web_service.WebStudioReadApiTests
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: expose web studio observability apis"
```

## Task 3: Shared-View And Hub APIs

**Files:**
- Modify `rightmemory/web/service.py`
- Modify `rightmemory/web/app.py`
- Extend `tests/test_web_service.py`

- [ ] **Step 1: Write failing shared-view API tests**

Cover:

- `GET /api/share/views` lists provider definitions and consumer connections;
- `POST /api/share/views` defines a provider view;
- `POST /api/share/views/{view_id}/build` builds a view;
- `POST /api/share/views/{view_id}/export` exports a package with replace confirmation;
- `POST /api/share/views/{view_id}/publish` publishes to mounted-folder hub or HTTP hub depending on request kind;
- `POST /api/use/accept-invite` accepts filesystem invitations and HTTP URLs;
- `POST /api/use/connections/{heading_id}/retrieve` returns shared-view context;
- `POST /api/use/connections/{heading_id}/note` enforces confirmation for human/external relationships;
- `GET /api/use/connections/{heading_id}/notes` returns local notes.

- [ ] **Step 2: Implement request handlers over service methods**

Keep route handlers thin. Validation and file/path safety should come from `shared_views.py` and the web service allowlists. Return a consistent response shape:

```json
{
  "ok": true,
  "message": "...",
  "data": {},
  "warnings": [],
  "paths": []
}
```

- [ ] **Step 3: Add saved hub settings**

Store named mounted-folder and HTTP hubs in `.runtime/web/settings.json`. Keep bearer credentials in the shared-view credential store or hub credential store, referenced by id.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_web_service.WebStudioSharedViewApiTests
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: manage shared views from web studio"
```

## Task 4: Managed Web Process CLI

**Files:**
- Create `rightmemory/web/process.py`
- Modify `rightmemory/cli.py`
- Create `tests/test_web_cli.py`

- [ ] **Step 1: Write failing process tests**

Cover:

- `rightmemory web status` reports stopped when no PID exists;
- `rightmemory web start --host 127.0.0.1 --port 0` records PID and log path while invoking Uvicorn in a managed subprocess;
- `rightmemory web stop` sends SIGTERM to a managed web process and removes stale PID files;
- `rightmemory web restart` composes stop then start;
- LAN host binding is explicit and appears in settings.

- [ ] **Step 2: Implement process helpers**

Mirror the managed watch style: PID under `.runtime/web/web.pid`, log under `.runtime/web/web.log`, process command uses `python -m rightmemory.web.app --serve ...`, and status verifies the process command before treating a PID as managed.

- [ ] **Step 3: Add CLI parser branch**

Add top-level:

```text
rightmemory web start --host 127.0.0.1 --port 8766
rightmemory web status
rightmemory web stop --timeout 30
rightmemory web restart --host 127.0.0.1 --port 8766
```

Print the URL and log path on start/status.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_web_cli
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: manage web studio service"
```

## Task 5: Static UI Experience

**Files:**
- Modify `rightmemory/web/static/index.html`
- Modify `rightmemory/web/static/app.js`
- Modify `rightmemory/web/static/styles.css`
- Extend `tests/test_web_service.py` for static smoke tests

- [ ] **Step 1: Write static smoke tests**

Assert `/` serves HTML, `/static/app.js` and `/static/styles.css` load, and the HTML references the expected static assets.

- [ ] **Step 2: Build the usable first screen**

Create an app layout with:

- persistent sidebar/navigation;
- Overview as the first screen;
- tabs or panels for Shared Views, Memory, Insights, Activity, Status, and Settings;
- login view when unauthenticated;
- forms for common shared-view actions;
- bounded previews for memory, logs, and insights.

Use a restrained operational style with dense, scannable information. Avoid landing-page composition; the app should open directly into the workspace once logged in.

- [ ] **Step 3: Implement frontend API client and states**

Add a small `fetchJson` helper that attaches CSRF for writes, handles structured errors, and updates the current panel. Use progressive enhancement so the static shell still loads if an API call fails.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m unittest tests.test_web_service.WebStudioStaticTests
```

- [ ] **Step 5: Commit**

Commit message:

```bash
git commit -m "feat: add web studio interface"
```

## Task 6: Docs And Final Verification

**Files:**
- Modify `README.md`
- Modify `AGENTS.md`
- Optionally refine `docs/superpowers/specs/2026-06-13-rightmemory-web-studio-design.md` if route names changed while preserving intent.

- [ ] **Step 1: Document Web Studio usage**

Add concise docs for:

- `rightmemory web start|status|stop|restart`;
- localhost default and explicit LAN binding;
- first-login operator token handling;
- what the browser can inspect and change;
- credential and runtime-state boundaries.

- [ ] **Step 2: Update agent notes**

Add the new web commands to `AGENTS.md` development/debugging notes.

- [ ] **Step 3: Run final verification**

Run:

```bash
uv run python -m compileall -q rightmemory tests
uv run python -m unittest discover -s tests
```

- [ ] **Step 4: Commit**

Commit message:

```bash
git commit -m "docs: document rightmemory web studio"
```
