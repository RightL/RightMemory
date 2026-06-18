# Hub Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an admin/operator Hub Console for hub-owned shared-view state, and make common `rightmemory hub` commands default to `./rightmemory-hub`.

**Architecture:** Keep Web Studio as the memory-root workflow surface. Add admin-only HTTP APIs to the existing hub FastAPI app, backed by focused `HubStore` read helpers and existing token revocation. Serve a lightweight plain HTML/CSS/JS console from the hub app; no frontend build system.

**Tech Stack:** Python 3.11, FastAPI, SQLite through `HubStore`, `argparse`, `unittest`, FastAPI `TestClient`, plain static HTML/CSS/JS.

---

## File Structure

- Modify `rightmemory/cli.py`
  - Add `DEFAULT_HUB_ROOT = Path("rightmemory-hub")`.
  - Make hub-root positional arguments optional where safe.
  - Preserve explicit root overrides.
  - Keep raw admin/provider tokens printed only at creation time.

- Modify `rightmemory/hub/store.py`
  - Add bounded admin list helpers for providers, views, invitations, connections, interactions, tokens, and audit events.
  - Add `hub_overview()` for console summary counts.
  - Keep secret token hashes private.
  - Reuse `revoke_token()` for invitations and accepted connections.

- Modify `rightmemory/hub/app.py`
  - Add `/api/admin/*` routes requiring admin bearer token.
  - Add `/console` and `/console/static` routes.
  - Keep public invitation, publish, pull, interaction, and provider inbox routes stable.

- Create `rightmemory/hub/static/console.html`
  - Console shell with token input, navigation, tables, and create/revoke forms.

- Create `rightmemory/hub/static/console.css`
  - Quiet operational UI styling.

- Create `rightmemory/hub/static/console.js`
  - Admin API client, tab rendering, create-token, create-invitation, revoke actions.

- Modify `tests/test_cli.py`
  - Cover default hub root behavior for `init`, `status`, `serve`, `token create`, `token list`, and `token revoke`.
  - Preserve explicit root behavior.

- Modify `tests/test_http_hub.py`
  - Cover admin auth.
  - Cover admin overview/list/create/revoke flows.
  - Cover console static route.

- Modify `docs/shared-views-usage.md`
  - Use default hub-root examples.
  - Mention Hub Console as runtime hub administration.

- Modify `README.md`
  - Use default hub-root examples.
  - Mention `GET /console`.

---

### Task 1: Default Hub Root CLI

**Files:**
- Modify: `rightmemory/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests for default hub root**

Add these tests to `JsonRequestTests` in `tests/test_cli.py`, near the existing hub CLI tests:

```python
    def test_hub_commands_default_to_rightmemory_hub_root(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("sys.stdout", stdout):
                    init_result = main(
                        [
                            "hub",
                            "init",
                            "--admin-token",
                            "admin-secret",
                            "--public-base-url",
                            "https://hub.example.test",
                        ]
                    )
                    create_result = main(
                        [
                            "hub",
                            "token",
                            "create",
                            "--provider",
                            "alice",
                            "--label",
                            "publish",
                        ]
                    )
                    status_result = main(["hub", "status"])
                    list_result = main(["hub", "token", "list"])

                lines = stdout.getvalue().splitlines()
                token_id = next(line.split("\t", 1)[1] for line in lines if line.startswith("token_id\t"))
                with patch("sys.stdout", stdout):
                    revoke_result = main(["hub", "token", "revoke", token_id])

            store = HubStore(default_hub)

        output = stdout.getvalue()
        self.assertEqual(init_result, 0)
        self.assertEqual(create_result, 0)
        self.assertEqual(status_result, 0)
        self.assertEqual(list_result, 0)
        self.assertEqual(revoke_result, 0)
        self.assertTrue(store.verify_token("admin-secret", action="admin"))
        self.assertTrue((default_hub / "hub.db").is_file())
        self.assertIn(f"hub_root\t{default_hub.resolve()}", output)
        self.assertIn("public_base_url\thttps://hub.example.test", output)
        self.assertIn("revoked\t", output)

    def test_hub_explicit_root_still_overrides_default(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            explicit_hub = Path(tempdir) / "explicit-hub"
            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("sys.stdout", stdout):
                    result = main(
                        [
                            "hub",
                            "init",
                            str(explicit_hub),
                            "--admin-token",
                            "admin-secret",
                        ]
                    )

        self.assertEqual(result, 0)
        self.assertTrue((explicit_hub / "hub.db").is_file())
        self.assertFalse((default_hub / "hub.db").exists())
        self.assertIn(f"hub_root\t{explicit_hub.resolve()}", stdout.getvalue())

    def test_hub_serve_uses_default_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            default_hub = Path(tempdir) / "rightmemory-hub"
            HubStore(default_hub).initialize(admin_token="admin-secret")

            with patch("rightmemory.cli.DEFAULT_HUB_ROOT", default_hub):
                with patch("rightmemory.cli.uvicorn.run") as run:
                    result = main(["hub", "serve"])

        self.assertEqual(result, 0)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(run.call_args.kwargs["port"], 8765)
```

- [ ] **Step 2: Run failing CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.JsonRequestTests.test_hub_commands_default_to_rightmemory_hub_root tests.test_cli.JsonRequestTests.test_hub_explicit_root_still_overrides_default tests.test_cli.JsonRequestTests.test_hub_serve_uses_default_root
```

Expected: fail because `rightmemory.cli.DEFAULT_HUB_ROOT` does not exist and `hub_root` is currently required.

- [ ] **Step 3: Add hub-root resolver and optional parser arguments**

In `rightmemory/cli.py`, add this constant near the other top-level defaults:

```python
DEFAULT_HUB_ROOT = Path("rightmemory-hub")
```

Replace the hub parser setup in `_hub_main()` with:

```python
    init = subparsers.add_parser("init")
    init.add_argument("hub_root", nargs="?", type=Path)
    init.add_argument("--admin-token")
    init.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    status = subparsers.add_parser("status")
    status.add_argument("hub_root", nargs="?", type=Path)
    token = subparsers.add_parser("token")
    token_subparsers = token.add_subparsers(dest="token_command", required=True)
    token_list = token_subparsers.add_parser("list")
    token_list.add_argument("hub_root", nargs="?", type=Path)
    token_create = token_subparsers.add_parser("create")
    token_create.add_argument("hub_root", nargs="?", type=Path)
    token_create.add_argument("--provider", required=True)
    token_create.add_argument("--label")
    token_revoke = token_subparsers.add_parser("revoke")
    token_revoke.add_argument("hub_root_or_token_id")
    token_revoke.add_argument("token_id", nargs="?")
    serve = subparsers.add_parser("serve")
    serve.add_argument("hub_root", nargs="?", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
```

Add these helpers before `_hub_main()`:

```python
def _resolve_hub_root(hub_root: Path | None) -> Path:
    return (hub_root or DEFAULT_HUB_ROOT).expanduser().resolve()


def _resolve_hub_revoke_args(args: argparse.Namespace) -> tuple[Path, str]:
    if args.token_id is None:
        return _resolve_hub_root(None), args.hub_root_or_token_id
    return _resolve_hub_root(Path(args.hub_root_or_token_id)), args.token_id


def _hub_init_hint(hub_root: Path) -> str:
    if hub_root == _resolve_hub_root(None):
        return "rightmemory hub init"
    return f"rightmemory hub init {hub_root}"
```

Update `_hub_main()` root handling:

```python
    if args.command == "init":
        hub_root = _resolve_hub_root(args.hub_root)
        store = HubStore(hub_root)
        admin_token = args.admin_token or (None if _hub_initialized(store) else secrets.token_urlsafe(32))
        store.initialize(admin_token=admin_token, public_base_url=args.public_base_url)
        if admin_token and not store.verify_token(admin_token, action="admin"):
            raise ValueError("admin token was not installed because a bootstrap admin token already exists")
        config = store.load_config()
        print(f"hub_root\t{hub_root}")
        print("initialized\tyes")
        print(f"public_base_url\t{config.public_base_url}")
        if admin_token:
            print(f"admin_token\t{admin_token}")
        else:
            print("admin_token\tunchanged")
        return 0
    if args.command == "status":
        print(_format_hub_status(_resolve_hub_root(args.hub_root)))
        return 0
    if args.command == "token":
        return _hub_token_main(args)
    if args.command == "serve":
        hub_root = _resolve_hub_root(args.hub_root)
        if not _hub_initialized(HubStore(hub_root)):
            raise ValueError(f"hub is not initialized: {hub_root}. Run: {_hub_init_hint(hub_root)}")
        uvicorn.run(create_hub_app(hub_root), host=args.host, port=args.port)
        return 0
```

Update `_hub_token_main()` root handling:

```python
def _hub_token_main(args: argparse.Namespace) -> int:
    if args.token_command == "revoke":
        hub_root, token_id = _resolve_hub_revoke_args(args)
    else:
        hub_root = _resolve_hub_root(args.hub_root)
        token_id = ""
    store = HubStore(hub_root)
    if not _hub_initialized(store):
        raise ValueError(f"hub is not initialized: {hub_root}. Run: {_hub_init_hint(hub_root)}")
    if args.token_command == "create":
        token = store.create_provider_token(args.provider, label=args.label)
        print(f"token_id\t{token.token_id}")
        print(f"provider_id\t{token.provider_id}")
        print(f"action\t{token.action}")
        if token.label:
            print(f"label\t{token.label}")
        print(f"raw_token\t{token.raw_token}")
        return 0
    if args.token_command == "list":
        for token in store.list_tokens():
            revoked = token["revoked_at"] or "-"
            provider = token["provider_id"] or "-"
            view = token["view_id"] or "-"
            label = token["label"] or "-"
            print(
                "\t".join(
                    [
                        token["token_id"],
                        token["action"],
                        provider,
                        view,
                        label,
                        token["created_at"],
                        revoked,
                    ]
                )
            )
        return 0
    if args.token_command == "revoke":
        if store.revoke_token(token_id):
            print(f"revoked\t{token_id}")
            return 0
        print(f"not_found\t{token_id}")
        return 1
    raise ValueError(f"unknown hub token command: {args.token_command}")
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.JsonRequestTests.test_hub_init_create_token_revoke_and_status_cli tests.test_cli.JsonRequestTests.test_hub_token_list_prints_revocation_handles_without_raw_tokens tests.test_cli.JsonRequestTests.test_hub_serve_runs_uvicorn_app tests.test_cli.JsonRequestTests.test_hub_commands_default_to_rightmemory_hub_root tests.test_cli.JsonRequestTests.test_hub_explicit_root_still_overrides_default tests.test_cli.JsonRequestTests.test_hub_serve_uses_default_root
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
rtk git add rightmemory/cli.py tests/test_cli.py
rtk git commit -m "feat: default hub root for hub commands"
```

Expected: commit succeeds.

---

### Task 2: Hub Store Admin Read Helpers

**Files:**
- Modify: `rightmemory/hub/store.py`
- Test: `tests/test_http_hub.py`

- [ ] **Step 1: Write failing store admin-helper test**

Add this test to `HubStoreTests` in `tests/test_http_hub.py`:

```python
    def test_admin_helpers_list_hub_state_without_secret_material(self):
        store = HubStore(self.root)
        store.initialize(admin_token="admin-secret")
        provider_token = store.create_provider_token("alice", label="publish")
        store.register_question_view(
            "alice-auth-api",
            provider_id="alice",
            title="Alice Auth API",
            description="Public auth facts.",
            question_base_url="https://provider.example.test",
            question_token="question-token",
            created_by_token_id=provider_token.token_id,
        )
        invitation = store.create_invitation("alice-auth-api", actor_id=provider_token.token_id, label="frontend")
        accepted = store.accept_invitation(invitation["raw_token"], consumer_label="frontend")
        self.assertIsNotNone(accepted)
        actor = store.require_token(accepted["connection_token"], action="connect", view_id="alice-auth-api")
        interaction = store.record_interaction(
            "alice-auth-api",
            actor=actor,
            payload={"actor": "assistant", "message": "Docs are stale."},
        )

        overview = store.hub_overview()
        providers = store.list_providers()
        views = store.list_views()
        invitations = store.list_view_invitations("alice-auth-api")
        connections = store.list_connections()
        interactions = store.list_interactions(provider_id="alice")
        audit = store.list_audit_events(limit=50)
        rendered = " ".join(
            [
                str(overview),
                str(providers),
                str(views),
                str(invitations),
                str(connections),
                str(interactions),
                str([event.details for event in audit]),
            ]
        )

        self.assertEqual(overview["provider_count"], 1)
        self.assertEqual(overview["view_count"], 1)
        self.assertGreaterEqual(overview["active_token_count"], 3)
        self.assertEqual(providers[0]["provider_id"], "alice")
        self.assertEqual(views[0]["view_id"], "alice-auth-api")
        self.assertEqual(views[0]["kind"], "question")
        self.assertEqual(views[0]["question_base_url"], "https://provider.example.test")
        self.assertEqual(invitations[0]["token_id"], invitation["token_id"])
        self.assertEqual(connections[0]["connection_id"], accepted["connection_id"])
        self.assertEqual(interactions[0]["interaction_id"], interaction["interaction_id"])
        self.assertIn("question_view.registered", [event.kind for event in audit])
        self.assertNotIn(provider_token.raw_token, rendered)
        self.assertNotIn(invitation["raw_token"], rendered)
        self.assertNotIn(accepted["connection_token"], rendered)
        self.assertNotIn("question-token", str(views))
```

- [ ] **Step 2: Run failing store test**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubStoreTests.test_admin_helpers_list_hub_state_without_secret_material
```

Expected: fail because `hub_overview`, `list_providers`, `list_views`, `list_view_invitations`, `list_connections`, and `list_interactions` do not exist.

- [ ] **Step 3: Add store helper methods**

In `rightmemory/hub/store.py`, replace `list_tokens()` with this compatible version:

```python
    def list_tokens(
        self,
        *,
        action: str | None = None,
        provider_id: str | None = None,
        view_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[object] = []
        if action:
            clauses.append("action = ?")
            values.append(_validate_action(action))
        if provider_id:
            clauses.append("provider_id = ?")
            values.append(_validate_hub_id(provider_id, "provider_id"))
        if view_id:
            clauses.append("view_id = ?")
            values.append(_validate_hub_id(view_id, "view_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend([_normalize_limit(limit), _normalize_offset(offset)])
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                f"""
                SELECT id, action, provider_id, view_id, label, created_at, revoked_at
                FROM tokens
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [
            {
                "token_id": row["id"],
                "action": row["action"],
                "provider_id": row["provider_id"],
                "view_id": row["view_id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]
```

Replace `list_audit_events()` with this compatible version:

```python
    def list_audit_events(
        self,
        *,
        kind: str | None = None,
        provider_id: str | None = None,
        view_id: str | None = None,
        actor_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        values: list[object] = []
        if kind:
            clauses.append("kind = ?")
            values.append(_validate_hub_id(kind, "audit kind"))
        if provider_id:
            clauses.append("provider_id = ?")
            values.append(_validate_hub_id(provider_id, "provider_id"))
        if view_id:
            clauses.append("view_id = ?")
            values.append(_validate_hub_id(view_id, "view_id"))
        if actor_id:
            clauses.append("actor_id = ?")
            values.append(_validate_hub_id(actor_id, "actor_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend([_normalize_limit(limit), _normalize_offset(offset)])
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                f"""
                SELECT id, kind, actor_id, provider_id, view_id, details_json, created_at
                FROM audit_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [_audit_event_from_row(row) for row in rows]
```

Add these methods after `list_audit_events()`:

```python
    def hub_overview(self) -> dict[str, Any]:
        config = self.load_config()
        with self._connect() as connection:
            self._apply_migrations(connection)
            provider_count = connection.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
            view_count = connection.execute("SELECT COUNT(*) FROM views").fetchone()[0]
            token_count = connection.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
            active_token_count = connection.execute(
                "SELECT COUNT(*) FROM tokens WHERE revoked_at IS NULL"
            ).fetchone()[0]
            interaction_count = connection.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
            audit_event_count = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            recent_auth_failures = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE kind = 'token.rejected'"
            ).fetchone()[0]
        return {
            "hub_root": str(self.root.resolve()),
            "initialized": self.db_path.is_file() and self.config_path.is_file(),
            "storage_present": self.storage_root.is_dir(),
            "public_base_url": config.public_base_url,
            "max_package_bytes": config.max_package_bytes,
            "provider_count": provider_count,
            "view_count": view_count,
            "token_count": token_count,
            "active_token_count": active_token_count,
            "interaction_count": interaction_count,
            "audit_event_count": audit_event_count,
            "recent_auth_failure_count": recent_auth_failures,
        }

    def list_providers(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                """
                SELECT
                    p.id AS provider_id,
                    p.label AS label,
                    p.created_at AS created_at,
                    p.updated_at AS updated_at,
                    COUNT(DISTINCT v.id) AS view_count,
                    COUNT(DISTINCT CASE WHEN t.revoked_at IS NULL THEN t.id END) AS active_token_count
                FROM providers p
                LEFT JOIN views v ON v.provider_id = p.id
                LEFT JOIN tokens t ON t.provider_id = p.id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                LIMIT ? OFFSET ?
                """,
                (_normalize_limit(limit), _normalize_offset(offset)),
            ).fetchall()
        return [
            {
                "provider_id": row["provider_id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "view_count": row["view_count"],
                "active_token_count": row["active_token_count"],
            }
            for row in rows
        ]

    def list_views(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[object] = []
        if provider_id:
            clauses.append("v.provider_id = ?")
            values.append(_validate_hub_id(provider_id, "provider_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend([_normalize_limit(limit), _normalize_offset(offset)])
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                f"""
                SELECT
                    v.id AS view_id,
                    v.provider_id AS provider_id,
                    v.title AS title,
                    v.ref AS ref,
                    v.description AS description,
                    v.current_version_id AS current_version_id,
                    v.created_at AS created_at,
                    v.updated_at AS updated_at,
                    vv.package_hash AS package_hash,
                    vv.storage_path AS storage_path,
                    vv.manifest_json AS manifest_json,
                    vv.created_at AS version_created_at,
                    vv.created_by_token_id AS created_by_token_id
                FROM views v
                LEFT JOIN view_versions vv ON vv.id = v.current_version_id
                {where}
                ORDER BY v.updated_at DESC, v.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [_admin_view_from_row(row) for row in rows]

    def get_admin_view(self, view_id: str) -> dict[str, Any] | None:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        views = self.list_views(limit=1, offset=0)
        for view in views:
            if view["view_id"] == clean_view_id:
                return view
        with self._connect() as connection:
            self._apply_migrations(connection)
            row = connection.execute(
                """
                SELECT
                    v.id AS view_id,
                    v.provider_id AS provider_id,
                    v.title AS title,
                    v.ref AS ref,
                    v.description AS description,
                    v.current_version_id AS current_version_id,
                    v.created_at AS created_at,
                    v.updated_at AS updated_at,
                    vv.package_hash AS package_hash,
                    vv.storage_path AS storage_path,
                    vv.manifest_json AS manifest_json,
                    vv.created_at AS version_created_at,
                    vv.created_by_token_id AS created_by_token_id
                FROM views v
                LEFT JOIN view_versions vv ON vv.id = v.current_version_id
                WHERE v.id = ?
                """,
                (clean_view_id,),
            ).fetchone()
        return _admin_view_from_row(row) if row is not None else None

    def list_view_invitations(
        self,
        view_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                """
                SELECT
                    i.id AS invitation_id,
                    i.view_id AS view_id,
                    i.token_id AS token_id,
                    i.label AS label,
                    i.expires_at AS expires_at,
                    i.revoked_at AS invitation_revoked_at,
                    i.created_at AS created_at,
                    i.accepted_count AS accepted_count,
                    t.revoked_at AS token_revoked_at
                FROM invitations i
                LEFT JOIN tokens t ON t.id = i.token_id
                WHERE i.view_id = ?
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT ? OFFSET ?
                """,
                (clean_view_id, _normalize_limit(limit), _normalize_offset(offset)),
            ).fetchall()
        return [_admin_invitation_from_row(row) for row in rows]

    def list_connections(
        self,
        *,
        view_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[object] = []
        if view_id:
            clauses.append("c.view_id = ?")
            values.append(_validate_hub_id(view_id, "view_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend([_normalize_limit(limit), _normalize_offset(offset)])
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                f"""
                SELECT
                    c.id AS connection_id,
                    c.invitation_id AS invitation_id,
                    c.view_id AS view_id,
                    c.token_id AS token_id,
                    c.consumer_label AS consumer_label,
                    c.created_at AS created_at,
                    c.revoked_at AS connection_revoked_at,
                    t.revoked_at AS token_revoked_at,
                    v.provider_id AS provider_id
                FROM connections c
                LEFT JOIN tokens t ON t.id = c.token_id
                JOIN views v ON v.id = c.view_id
                {where}
                ORDER BY c.created_at DESC, c.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [_admin_connection_from_row(row) for row in rows]

    def list_interactions(
        self,
        *,
        provider_id: str | None = None,
        view_id: str | None = None,
        connection_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[object] = []
        if provider_id:
            clauses.append("v.provider_id = ?")
            values.append(_validate_hub_id(provider_id, "provider_id"))
        if view_id:
            clauses.append("i.view_id = ?")
            values.append(_validate_hub_id(view_id, "view_id"))
        if connection_id:
            clauses.append("i.connection_id = ?")
            values.append(_validate_hub_id(connection_id, "connection_id"))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend([_normalize_limit(limit), _normalize_offset(offset)])
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                f"""
                SELECT
                    i.id AS interaction_id,
                    i.view_id AS view_id,
                    i.connection_id AS connection_id,
                    i.actor_id AS actor_id,
                    i.payload_json AS payload_json,
                    i.created_at AS created_at,
                    v.provider_id AS provider_id
                FROM interactions i
                JOIN views v ON v.id = i.view_id
                {where}
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT ? OFFSET ?
                """,
                values,
            ).fetchall()
        return [_interaction_from_row(row) for row in rows]
```

Replace `list_provider_inbox()` with:

```python
    def list_provider_inbox(self, provider_id: str) -> list[dict[str, Any]]:
        return self.list_interactions(provider_id=provider_id)
```

- [ ] **Step 4: Add store serializers and limit helpers**

Add these helpers near the existing serializer helpers in `rightmemory/hub/store.py`:

```python
def _normalize_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    return max(1, min(limit, 200))


def _normalize_offset(offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("offset must be an integer")
    return max(0, offset)


def _admin_view_from_row(row: sqlite3.Row) -> dict[str, Any]:
    manifest = _json_object(row["manifest_json"])
    metadata = manifest.get("invitation_metadata") if isinstance(manifest.get("invitation_metadata"), dict) else {}
    kind = _optional_string(metadata.get("kind")) or _kind_from_ref(row["ref"]) or "file"
    view = {
        "view_id": row["view_id"],
        "provider_id": row["provider_id"],
        "kind": kind,
        "title": row["title"],
        "ref": row["ref"],
        "description": row["description"],
        "current_version_id": row["current_version_id"],
        "package_hash": row["package_hash"],
        "storage_path": row["storage_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version_created_at": row["version_created_at"],
        "created_by_token_id": row["created_by_token_id"],
    }
    question_base_url = _optional_string(metadata.get("question_base_url"))
    if question_base_url:
        view["question_base_url"] = question_base_url
    return view


def _admin_invitation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "invitation_id": row["invitation_id"],
        "view_id": row["view_id"],
        "token_id": row["token_id"],
        "label": row["label"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "accepted_count": row["accepted_count"],
        "revoked_at": row["invitation_revoked_at"] or row["token_revoked_at"],
    }


def _admin_connection_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "connection_id": row["connection_id"],
        "invitation_id": row["invitation_id"],
        "provider_id": row["provider_id"],
        "view_id": row["view_id"],
        "token_id": row["token_id"],
        "consumer_label": row["consumer_label"],
        "created_at": row["created_at"],
        "revoked_at": row["connection_revoked_at"] or row["token_revoked_at"],
    }


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}
```

- [ ] **Step 5: Run store tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubStoreTests
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
rtk git add rightmemory/hub/store.py tests/test_http_hub.py
rtk git commit -m "feat: add hub admin store helpers"
```

Expected: commit succeeds.

---

### Task 3: Admin HTTP API

**Files:**
- Modify: `rightmemory/hub/app.py`
- Test: `tests/test_http_hub.py`

- [ ] **Step 1: Write failing admin API tests**

Add these tests to `HubApiTests` in `tests/test_http_hub.py`:

```python
    def test_admin_routes_require_admin_token(self):
        missing = self.client.get("/api/admin/overview")
        provider = self.client.get("/api/admin/overview", headers=_auth(self.provider_token.raw_token))
        admin = self.client.get("/api/admin/overview", headers=_auth("admin-secret"))

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(provider.status_code, 403)
        self.assertEqual(admin.status_code, 200)
        self.assertTrue(admin.json()["overview"]["initialized"])

    def test_admin_api_lists_creates_and_revokes_hub_state(self):
        package = self.root / "package"
        _write_package(package)
        publish = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(package),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )
        self.assertEqual(publish.status_code, 201)

        created_token = self.client.post(
            "/api/admin/providers/bob/tokens",
            headers=_auth("admin-secret"),
            json={"label": "publish"},
        )
        invitation = self.client.post(
            "/api/admin/views/alice-auth-api/invitations",
            headers=_auth("admin-secret"),
            json={"label": "frontend"},
        )
        invitation_token = invitation.json()["invitation_url"].rsplit("/i/", 1)[1]
        accepted = self.client.post(
            f"/api/invitations/{invitation_token}/accept",
            json={"consumer_label": "frontend"},
        )
        interaction = self.client.post(
            "/api/views/alice-auth-api/interactions",
            headers=_auth(accepted.json()["connection_token"]),
            json={"actor": "assistant", "message": "Docs are stale."},
        )
        overview = self.client.get("/api/admin/overview", headers=_auth("admin-secret"))
        providers = self.client.get("/api/admin/providers", headers=_auth("admin-secret"))
        views = self.client.get("/api/admin/views", headers=_auth("admin-secret"))
        view = self.client.get("/api/admin/views/alice-auth-api", headers=_auth("admin-secret"))
        invitations = self.client.get(
            "/api/admin/views/alice-auth-api/invitations",
            headers=_auth("admin-secret"),
        )
        connections = self.client.get("/api/admin/connections", headers=_auth("admin-secret"))
        inbox = self.client.get("/api/admin/inbox?provider_id=alice", headers=_auth("admin-secret"))
        audit = self.client.get("/api/admin/audit?kind=interaction.created", headers=_auth("admin-secret"))

        revoke_invitation = self.client.post(
            f"/api/admin/invitations/{invitation.json()['token_id']}/revoke",
            headers=_auth("admin-secret"),
        )
        revoke_connection = self.client.post(
            f"/api/admin/connections/{accepted.json()['token_id']}/revoke",
            headers=_auth("admin-secret"),
        )

        self.assertEqual(created_token.status_code, 201)
        self.assertEqual(created_token.json()["provider_id"], "bob")
        self.assertIn("raw_token", created_token.json())
        self.assertEqual(invitation.status_code, 201)
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(interaction.status_code, 201)
        self.assertEqual(overview.status_code, 200)
        self.assertGreaterEqual(overview.json()["overview"]["provider_count"], 2)
        self.assertEqual(providers.status_code, 200)
        self.assertIn("alice", [item["provider_id"] for item in providers.json()["providers"]])
        self.assertEqual(views.status_code, 200)
        self.assertIn("alice-auth-api", [item["view_id"] for item in views.json()["views"]])
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["view"]["kind"], "file")
        self.assertEqual(invitations.status_code, 200)
        self.assertEqual(invitations.json()["invitations"][0]["token_id"], invitation.json()["token_id"])
        self.assertEqual(connections.status_code, 200)
        self.assertEqual(connections.json()["connections"][0]["connection_id"], accepted.json()["connection_id"])
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(inbox.json()["interactions"][0]["interaction_id"], interaction.json()["interaction_id"])
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()["events"][0]["kind"], "interaction.created")
        self.assertEqual(revoke_invitation.status_code, 200)
        self.assertTrue(revoke_invitation.json()["revoked"])
        self.assertEqual(revoke_connection.status_code, 200)
        self.assertTrue(revoke_connection.json()["revoked"])
        self.assertFalse(self.store.verify_token(accepted.json()["connection_token"], action="connect", view_id="alice-auth-api"))
        self.assertNotIn(created_token.json()["raw_token"], str(self.client.get("/api/admin/tokens", headers=_auth("admin-secret")).json()))
```

- [ ] **Step 2: Run failing admin API tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubApiTests.test_admin_routes_require_admin_token tests.test_http_hub.HubApiTests.test_admin_api_lists_creates_and_revokes_hub_state
```

Expected: fail with `404 Not Found` for `/api/admin/overview`.

- [ ] **Step 3: Add admin auth and query helpers**

In `rightmemory/hub/app.py`, add these imports:

```python
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
```

In `create_hub_app()`, after `app = FastAPI(...)`, add static mounting:

```python
    static_root = Path(__file__).parent / "static"
    if static_root.is_dir():
        app.mount("/console/static", StaticFiles(directory=static_root), name="hub-console-static")
```

Add this route before the existing API routes:

```python
    @app.get("/console")
    def console() -> FileResponse:
        index = static_root / "console.html"
        if not index.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="hub console is not installed")
        return FileResponse(index)
```

Add these helpers near `_require_provider_or_admin()`:

```python
def _require_admin(store: HubStore, request: Request):
    token = _bearer_token(request)
    try:
        return store.require_token(token, action="admin")
    except PermissionError as exc:
        if (
            store.verify_token(token, action="publish")
            or store.verify_token(token, action="connect")
            or store.verify_token(token, action="invite")
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin token required") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _query_limit(request: Request, *, default: int = 100) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be an integer") from exc
    return max(1, min(value, 200))


def _query_offset(request: Request) -> int:
    raw = request.query_params.get("offset")
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="offset must be an integer") from exc
    return max(0, value)


def _query_optional_id(request: Request, key: str) -> str | None:
    value = request.query_params.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _audit_event_payload(event) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "kind": event.kind,
        "actor_id": event.actor_id,
        "provider_id": event.provider_id,
        "view_id": event.view_id,
        "details": event.details,
        "created_at": event.created_at,
    }
```

- [ ] **Step 4: Add admin routes**

Inside `create_hub_app()`, after `/health` and before public view routes, add:

```python
    @app.get("/api/admin/overview")
    def admin_overview(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {"overview": store.hub_overview()}

    @app.get("/api/admin/providers")
    def admin_providers(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "providers": store.list_providers(
                limit=_query_limit(request),
                offset=_query_offset(request),
            )
        }

    @app.post("/api/admin/providers/{provider_id}/tokens", status_code=status.HTTP_201_CREATED)
    def admin_create_provider_token(
        provider_id: str,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        actor = _require_admin(store, request)
        data = payload or {}
        try:
            token = store.create_provider_token(provider_id, label=_optional_payload_str(data, "label"))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "token_id": token.token_id,
            "raw_token": token.raw_token,
            "action": token.action,
            "provider_id": token.provider_id,
            "view_id": token.view_id,
            "label": token.label,
            "created_at": token.created_at,
            "created_by_token_id": actor.token_id,
        }

    @app.get("/api/admin/tokens")
    def admin_tokens(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "tokens": store.list_tokens(
                action=_query_optional_id(request, "action"),
                provider_id=_query_optional_id(request, "provider_id"),
                view_id=_query_optional_id(request, "view_id"),
                limit=_query_limit(request),
                offset=_query_offset(request),
            )
        }

    @app.post("/api/admin/tokens/{token_id}/revoke")
    def admin_revoke_token(token_id: str, request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {"token_id": token_id, "revoked": store.revoke_token(token_id)}

    @app.get("/api/admin/views")
    def admin_views(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "views": store.list_views(
                provider_id=_query_optional_id(request, "provider_id"),
                limit=_query_limit(request),
                offset=_query_offset(request),
            )
        }

    @app.get("/api/admin/views/{view_id}")
    def admin_view(view_id: str, request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        view = store.get_admin_view(view_id)
        if view is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
        return {"view": view}

    @app.get("/api/admin/views/{view_id}/invitations")
    def admin_view_invitations(view_id: str, request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "view_id": view_id,
            "invitations": store.list_view_invitations(
                view_id,
                limit=_query_limit(request),
                offset=_query_offset(request),
            ),
        }

    @app.post("/api/admin/views/{view_id}/invitations", status_code=status.HTTP_201_CREATED)
    def admin_create_invitation(
        view_id: str,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        actor = _require_admin(store, request)
        data = payload or {}
        try:
            invitation = store.create_invitation(
                view_id,
                actor_id=actor.token_id,
                label=_optional_payload_str(data, "label"),
                expires_at=_optional_payload_str(data, "expires_at"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        config = store.load_config()
        return {
            "invitation_id": invitation["invitation_id"],
            "token_id": invitation["token_id"],
            "view_id": invitation["view_id"],
            "label": invitation["label"],
            "expires_at": invitation["expires_at"],
            "created_at": invitation["created_at"],
            "invitation_url": f"{config.public_base_url.rstrip('/')}/i/{invitation['raw_token']}",
        }

    @app.post("/api/admin/invitations/{token_id}/revoke")
    def admin_revoke_invitation(token_id: str, request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {"token_id": token_id, "revoked": store.revoke_token(token_id)}

    @app.get("/api/admin/connections")
    def admin_connections(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "connections": store.list_connections(
                view_id=_query_optional_id(request, "view_id"),
                limit=_query_limit(request),
                offset=_query_offset(request),
            )
        }

    @app.post("/api/admin/connections/{token_id}/revoke")
    def admin_revoke_connection(token_id: str, request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {"token_id": token_id, "revoked": store.revoke_token(token_id)}

    @app.get("/api/admin/inbox")
    def admin_inbox(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "interactions": store.list_interactions(
                provider_id=_query_optional_id(request, "provider_id"),
                view_id=_query_optional_id(request, "view_id"),
                connection_id=_query_optional_id(request, "connection_id"),
                limit=_query_limit(request),
                offset=_query_offset(request),
            )
        }

    @app.get("/api/admin/audit")
    def admin_audit(request: Request) -> dict[str, Any]:
        _require_admin(store, request)
        return {
            "events": [
                _audit_event_payload(event)
                for event in store.list_audit_events(
                    kind=_query_optional_id(request, "kind"),
                    provider_id=_query_optional_id(request, "provider_id"),
                    view_id=_query_optional_id(request, "view_id"),
                    actor_id=_query_optional_id(request, "actor_id"),
                    limit=_query_limit(request),
                    offset=_query_offset(request),
                )
            ]
        }
```

- [ ] **Step 5: Run admin API tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubApiTests.test_admin_routes_require_admin_token tests.test_http_hub.HubApiTests.test_admin_api_lists_creates_and_revokes_hub_state
```

Expected: pass.

- [ ] **Step 6: Run all hub HTTP tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub
```

Expected: pass. Some tests may skip when FastAPI `TestClient` support is unavailable in the environment; skips are acceptable if they match the existing skip behavior.

- [ ] **Step 7: Commit**

Run:

```bash
rtk git add rightmemory/hub/app.py tests/test_http_hub.py
rtk git commit -m "feat: add hub admin API"
```

Expected: commit succeeds.

---

### Task 4: Hub Console Static UI

**Files:**
- Create: `rightmemory/hub/static/console.html`
- Create: `rightmemory/hub/static/console.css`
- Create: `rightmemory/hub/static/console.js`
- Test: `tests/test_http_hub.py`

- [ ] **Step 1: Write failing static console test**

Add this test to `HubApiTests` in `tests/test_http_hub.py`:

```python
    def test_console_static_routes_are_served(self):
        page = self.client.get("/console")
        script = self.client.get("/console/static/console.js")
        styles = self.client.get("/console/static/console.css")

        self.assertEqual(page.status_code, 200)
        self.assertIn("RightMemory Hub Console", page.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/admin/overview", script.text)
        self.assertEqual(styles.status_code, 200)
        self.assertIn(".console-shell", styles.text)
```

- [ ] **Step 2: Run failing static console test**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubApiTests.test_console_static_routes_are_served
```

Expected: fail because the static files do not exist.

- [ ] **Step 3: Create console HTML**

Create `rightmemory/hub/static/console.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>RightMemory Hub Console</title>
    <link rel="stylesheet" href="/console/static/console.css">
  </head>
  <body>
    <main class="console-shell">
      <header class="topbar">
        <div>
          <h1>RightMemory Hub Console</h1>
          <p id="health-line">Admin console for shared-view hub state.</p>
        </div>
        <form id="token-form" class="token-form">
          <input id="admin-token" name="admin_token" type="password" autocomplete="off" placeholder="Admin token" required>
          <button type="submit">Connect</button>
        </form>
      </header>

      <nav class="tabs" aria-label="Hub Console Sections">
        <button type="button" data-tab="overview" class="active">Overview</button>
        <button type="button" data-tab="providers">Providers</button>
        <button type="button" data-tab="views">Views</button>
        <button type="button" data-tab="invitations">Invitations</button>
        <button type="button" data-tab="connections">Connections</button>
        <button type="button" data-tab="inbox">Inbox</button>
        <button type="button" data-tab="audit">Audit</button>
        <button type="button" data-tab="tokens">Tokens</button>
      </nav>

      <section id="notice" class="notice" hidden></section>

      <section id="overview" class="panel active">
        <h2>Overview</h2>
        <div id="overview-grid" class="metric-grid"></div>
      </section>

      <section id="providers" class="panel">
        <div class="panel-header">
          <h2>Providers</h2>
          <form id="provider-token-form" class="inline-form">
            <input name="provider_id" placeholder="provider id" required>
            <input name="label" placeholder="token label">
            <button type="submit">Create Token</button>
          </form>
        </div>
        <div id="provider-token-result" class="result-box" hidden></div>
        <div id="providers-table"></div>
      </section>

      <section id="views" class="panel">
        <h2>Views</h2>
        <div id="views-table"></div>
      </section>

      <section id="invitations" class="panel">
        <div class="panel-header">
          <h2>Invitations</h2>
          <form id="invitation-form" class="inline-form">
            <input name="view_id" placeholder="view id" required>
            <input name="label" placeholder="invitation label">
            <input name="expires_at" placeholder="2026-06-17T12:00:00Z">
            <button type="submit">Create Invitation</button>
          </form>
        </div>
        <div id="invitation-result" class="result-box" hidden></div>
        <div id="invitations-table"></div>
      </section>

      <section id="connections" class="panel">
        <h2>Connections</h2>
        <div id="connections-table"></div>
      </section>

      <section id="inbox" class="panel">
        <h2>Inbox</h2>
        <div id="inbox-table"></div>
      </section>

      <section id="audit" class="panel">
        <h2>Audit</h2>
        <div id="audit-table"></div>
      </section>

      <section id="tokens" class="panel">
        <h2>Tokens</h2>
        <div id="tokens-table"></div>
      </section>
    </main>
    <script src="/console/static/console.js"></script>
  </body>
</html>
```

- [ ] **Step 4: Create console CSS**

Create `rightmemory/hub/static/console.css`:

```css
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --text: #18202a;
  --muted: #607080;
  --line: #d9e0e7;
  --accent: #126d7d;
  --danger: #a93535;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
}

button,
input {
  font: inherit;
}

button {
  border: 1px solid var(--line);
  background: #ffffff;
  color: var(--text);
  border-radius: 6px;
  min-height: 34px;
  padding: 6px 10px;
  cursor: pointer;
}

button:hover,
button.active {
  border-color: var(--accent);
  color: var(--accent);
}

input {
  border: 1px solid var(--line);
  border-radius: 6px;
  min-height: 34px;
  padding: 6px 10px;
  background: #ffffff;
  color: var(--text);
}

.console-shell {
  width: min(1280px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 24px 0 40px;
}

.topbar,
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.topbar {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  padding: 18px 20px;
}

.topbar h1,
.panel h2 {
  margin: 0;
  letter-spacing: 0;
}

.topbar p {
  margin: 4px 0 0;
  color: var(--muted);
}

.token-form,
.inline-form,
.panel-header {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 16px 0;
}

.notice,
.result-box {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #eef8f9;
  padding: 10px 12px;
  margin-bottom: 12px;
  white-space: pre-wrap;
}

.panel {
  display: none;
  padding: 18px 20px;
  overflow: auto;
}

.panel.active {
  display: block;
}

.panel-header {
  justify-content: space-between;
  margin-bottom: 12px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

.metric {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px;
}

.metric strong {
  display: block;
  font-size: 20px;
  margin-top: 4px;
}

table {
  width: 100%;
  border-collapse: collapse;
  min-width: 760px;
}

th,
td {
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  padding: 8px;
}

th {
  color: var(--muted);
  font-weight: 600;
}

.danger {
  color: var(--danger);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

@media (max-width: 760px) {
  .topbar,
  .panel-header {
    align-items: stretch;
    flex-direction: column;
  }

  .token-form,
  .inline-form {
    align-items: stretch;
    flex-direction: column;
  }
}
```

- [ ] **Step 5: Create console JavaScript**

Create `rightmemory/hub/static/console.js`:

```javascript
const state = {
  token: window.localStorage.getItem("rightmemory.hub.adminToken") || "",
  views: []
};

const sections = ["overview", "providers", "views", "invitations", "connections", "inbox", "audit", "tokens"];

function $(selector) {
  return document.querySelector(selector);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showNotice(message, isError = false) {
  const node = $("#notice");
  node.hidden = false;
  node.textContent = message;
  node.classList.toggle("danger", isError);
}

async function api(path, options = {}) {
  if (!state.token) {
    throw new Error("Admin token is required.");
  }
  const response = await fetch(path, {
    ...options,
    headers: {
      "Authorization": `Bearer ${state.token}`,
      "Content-Type": "application/json",
      ...(options.headers || {})
    }
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return body;
}

function renderTable(target, columns, rows, actions = () => "") {
  const node = $(target);
  if (!rows.length) {
    node.innerHTML = "<p>No records.</p>";
    return;
  }
  const header = columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join("");
  const body = rows.map((row) => {
    const cells = columns.map((column) => `<td>${column.render ? column.render(row) : escapeHtml(row[column.key])}</td>`).join("");
    return `<tr>${cells}<td>${actions(row)}</td></tr>`;
  }).join("");
  node.innerHTML = `<table><thead><tr>${header}<th>Actions</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderOverview(overview) {
  $("#health-line").textContent = `${overview.public_base_url} · ${overview.initialized ? "initialized" : "uninitialized"}`;
  const items = [
    ["Providers", overview.provider_count],
    ["Views", overview.view_count],
    ["Active Tokens", overview.active_token_count],
    ["Interactions", overview.interaction_count],
    ["Audit Events", overview.audit_event_count],
    ["Auth Failures", overview.recent_auth_failure_count],
    ["Storage", overview.storage_present ? "present" : "missing"],
    ["Max Package Bytes", overview.max_package_bytes]
  ];
  $("#overview-grid").innerHTML = items.map(([label, value]) => (
    `<div class="metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

async function loadOverview() {
  const data = await api("/api/admin/overview");
  renderOverview(data.overview);
}

async function loadProviders() {
  const data = await api("/api/admin/providers");
  renderTable("#providers-table", [
    {key: "provider_id", label: "Provider"},
    {key: "label", label: "Label"},
    {key: "view_count", label: "Views"},
    {key: "active_token_count", label: "Active Tokens"},
    {key: "updated_at", label: "Updated"}
  ], data.providers);
}

async function loadViews() {
  const data = await api("/api/admin/views");
  state.views = data.views;
  renderTable("#views-table", [
    {key: "view_id", label: "View"},
    {key: "provider_id", label: "Provider"},
    {key: "kind", label: "Kind"},
    {key: "title", label: "Title"},
    {key: "current_version_id", label: "Current Version"},
    {key: "question_base_url", label: "Question URL"},
    {key: "updated_at", label: "Updated"}
  ], data.views);
}

async function loadInvitations() {
  const rows = [];
  for (const view of state.views) {
    const data = await api(`/api/admin/views/${encodeURIComponent(view.view_id)}/invitations`);
    rows.push(...data.invitations);
  }
  renderTable("#invitations-table", [
    {key: "invitation_id", label: "Invitation"},
    {key: "view_id", label: "View"},
    {key: "token_id", label: "Token"},
    {key: "label", label: "Label"},
    {key: "accepted_count", label: "Accepted"},
    {key: "revoked_at", label: "Revoked"}
  ], rows, (row) => row.revoked_at ? "" : `<button data-revoke-invitation="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function loadConnections() {
  const data = await api("/api/admin/connections");
  renderTable("#connections-table", [
    {key: "connection_id", label: "Connection"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "consumer_label", label: "Consumer"},
    {key: "token_id", label: "Token"},
    {key: "revoked_at", label: "Revoked"}
  ], data.connections, (row) => row.revoked_at ? "" : `<button data-revoke-connection="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function loadInbox() {
  const data = await api("/api/admin/inbox");
  renderTable("#inbox-table", [
    {key: "interaction_id", label: "Interaction"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "connection_id", label: "Connection"},
    {key: "payload", label: "Message", render: (row) => escapeHtml(row.payload?.message || JSON.stringify(row.payload))},
    {key: "created_at", label: "Created"}
  ], data.interactions);
}

async function loadAudit() {
  const data = await api("/api/admin/audit");
  renderTable("#audit-table", [
    {key: "event_id", label: "Event"},
    {key: "kind", label: "Kind"},
    {key: "actor_id", label: "Actor"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "created_at", label: "Created"}
  ], data.events);
}

async function loadTokens() {
  const data = await api("/api/admin/tokens");
  renderTable("#tokens-table", [
    {key: "token_id", label: "Token"},
    {key: "action", label: "Action"},
    {key: "provider_id", label: "Provider"},
    {key: "view_id", label: "View"},
    {key: "label", label: "Label"},
    {key: "revoked_at", label: "Revoked"}
  ], data.tokens, (row) => row.revoked_at ? "" : `<button data-revoke-token="${escapeHtml(row.token_id)}">Revoke</button>`);
}

async function refreshAll() {
  await loadOverview();
  await loadProviders();
  await loadViews();
  await loadInvitations();
  await loadConnections();
  await loadInbox();
  await loadAudit();
  await loadTokens();
}

function activateTab(name) {
  for (const section of sections) {
    $(`#${section}`).classList.toggle("active", section === name);
    document.querySelector(`[data-tab="${section}"]`).classList.toggle("active", section === name);
  }
}

async function createProviderToken(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const providerId = form.get("provider_id");
  const payload = {label: form.get("label") || null};
  const data = await api(`/api/admin/providers/${encodeURIComponent(providerId)}/tokens`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
  const box = $("#provider-token-result");
  box.hidden = false;
  box.textContent = `Raw token for ${data.provider_id}: ${data.raw_token}`;
  await refreshAll();
}

async function createInvitation(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const viewId = form.get("view_id");
  const payload = {
    label: form.get("label") || null,
    expires_at: form.get("expires_at") || null
  };
  const data = await api(`/api/admin/views/${encodeURIComponent(viewId)}/invitations`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
  const box = $("#invitation-result");
  box.hidden = false;
  box.textContent = data.invitation_url;
  await refreshAll();
}

async function revokeByToken(path, tokenId) {
  await api(`${path}/${encodeURIComponent(tokenId)}/revoke`, {method: "POST", body: "{}"});
  await refreshAll();
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.dataset.tab) {
    activateTab(target.dataset.tab);
    return;
  }
  try {
    if (target.dataset.revokeToken) {
      await revokeByToken("/api/admin/tokens", target.dataset.revokeToken);
    }
    if (target.dataset.revokeInvitation) {
      await revokeByToken("/api/admin/invitations", target.dataset.revokeInvitation);
    }
    if (target.dataset.revokeConnection) {
      await revokeByToken("/api/admin/connections", target.dataset.revokeConnection);
    }
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.token = $("#admin-token").value;
  window.localStorage.setItem("rightmemory.hub.adminToken", state.token);
  try {
    await refreshAll();
    showNotice("Connected.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#provider-token-form").addEventListener("submit", async (event) => {
  try {
    await createProviderToken(event);
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#invitation-form").addEventListener("submit", async (event) => {
  try {
    await createInvitation(event);
  } catch (error) {
    showNotice(error.message, true);
  }
});

if (state.token) {
  $("#admin-token").value = state.token;
  refreshAll().catch((error) => showNotice(error.message, true));
}
```

- [ ] **Step 6: Run static console test**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubApiTests.test_console_static_routes_are_served
```

Expected: pass.

- [ ] **Step 7: Run hub tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub
```

Expected: pass.

- [ ] **Step 8: Commit**

Run:

```bash
rtk git add rightmemory/hub/static/console.html rightmemory/hub/static/console.css rightmemory/hub/static/console.js tests/test_http_hub.py
rtk git commit -m "feat: add hub console UI"
```

Expected: commit succeeds.

---

### Task 5: Docs And Verification

**Files:**
- Modify: `docs/shared-views-usage.md`
- Modify: `README.md`

- [ ] **Step 1: Update shared-view usage docs**

In `docs/shared-views-usage.md`, update the hub bootstrap examples from explicit local root commands to default-root commands:

```bash
rightmemory hub init --public-base-url http://127.0.0.1:8765
rightmemory hub token create --provider alice --label publish
rightmemory hub serve --host 127.0.0.1 --port 8765
```

In the Web Studio section, keep the Web Studio coverage list and add this paragraph after it:

```markdown
Hub Console is available at `/console` on the running hub service. It is the admin/operator surface for runtime hub state: health, providers, tokens, views, invitations, accepted connections, inbox records, and audit events. It does not build shared views or edit memory roots; use Web Studio for provider and consumer workflows.
```

In the operational checks section, add:

```bash
rightmemory hub status
```

- [ ] **Step 2: Update README examples**

In `README.md`, update hub examples to:

```bash
rightmemory hub init --public-base-url http://127.0.0.1:8765
rightmemory hub token create --provider alice --label publish
rightmemory hub serve --host 127.0.0.1 --port 8765
```

Add a short console note after the hub serve example:

```markdown
After the hub is running, open `http://127.0.0.1:8765/console` and enter the admin token printed by `rightmemory hub init`. The console is for hub administration: providers, tokens, views, invitations, connections, inbox, and audit.
```

- [ ] **Step 3: Search for stale explicit-root-only examples**

Run:

```bash
rtk rg -n "rightmemory hub (init|serve|status|token (create|list|revoke)) [./<]" README.md docs/shared-views-usage.md docs/superpowers/specs/2026-06-17-hub-console-design.md
```

Expected: no stale mandatory-root examples in README or shared-view usage docs. The spec may still mention explicit roots as an override.

- [ ] **Step 4: Run focused tests**

Run:

```bash
rtk python -m unittest tests.test_cli tests.test_http_hub
```

Expected: pass, with existing environment-dependent skips only.

- [ ] **Step 5: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: pass with no output.

- [ ] **Step 6: Run full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: pass, with existing skips only.

- [ ] **Step 7: Run diff check**

Run:

```bash
rtk git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 8: Commit docs**

Run:

```bash
rtk git add docs/shared-views-usage.md README.md
rtk git commit -m "docs: document hub console workflow"
```

Expected: commit succeeds.

---

## Self-Review Checklist

- Spec coverage:
  - Default hub root is covered in Task 1.
  - Admin-only console API is covered in Task 3.
  - Hub-owned nouns are covered: providers, tokens, views, invitations, connections, inbox, audit.
  - Static console UI is covered in Task 4.
  - Docs are covered in Task 5.
  - Non-goals are preserved: no hub init or serve from UI, no view building, no memory editing, no provider role UI, no inbox replies.

- Type consistency:
  - Revoke routes target `token_id`.
  - Store list methods return JSON-safe dictionaries except `list_audit_events`, which continues returning `AuditEvent` objects and is serialized in `rightmemory/hub/app.py`.
  - Console JS calls the same `/api/admin/*` routes added in Task 3.

- Security:
  - Raw provider tokens are returned only by provider-token creation.
  - Invitation URLs are returned only when creating invitations.
  - Token list endpoints do not include raw token material.
  - Provider publish tokens receive `403` on admin endpoints.
