# Share Relationship Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a first-class `rightmemory share create/approve/publish/join/status` workflow with one bundled hub invitation over optional file and question parts.

**Architecture:** Add a small durable `shares.toml` relationship registry while keeping `MF#` and `MQ#` as low-level primitives. Refactor existing publish code so share publishing can publish file/question parts without creating per-view invitations, then add hub bundled invitation support and CLI orchestration around those primitives.

**Tech Stack:** Python standard library, `tomllib`, dataclasses, existing `unittest` suite, existing FastAPI hub app and `HubClient`, existing RightMemory shared-view modules.

---

## File Structure

- Create `rightmemory/share_models.py`: dataclasses and load/save/validate helpers for `shares.toml`.
- Create `rightmemory/shares.py`: provider and consumer orchestration for `share create`, `approve`, `publish`, `join`, `status`, and `list`.
- Modify `rightmemory/shared_view_files.py`: expose a package-publish primitive that does not create invitations.
- Modify `rightmemory/shared_view_questions.py`: expose a question-register primitive that does not create invitations.
- Modify `rightmemory/hub/store.py`: add bundle invitation tables and store methods.
- Modify `rightmemory/hub/app.py`: add bundled invitation endpoints.
- Modify `rightmemory/hub/client.py`: add bundled invitation client methods.
- Modify `rightmemory/cli.py`: add top-level `share` command dispatch and parser.
- Modify `install.sh`, `rightmemory/session.py`, `rightmemory/sync.py`, `rightmemory/tools.py`, `rightmemory/prompt.py`, `AGENTS.md`, and `README.md`: include `shares.toml` in durable memory allowlists and docs.
- Create `tests/test_shares.py`: relationship registry, orchestration, and CLI-independent share behavior.
- Modify `tests/test_http_hub.py`: bundled invitation store/API behavior.
- Modify `tests/test_cli.py`: `rightmemory share ...` command dispatch.
- Modify `tests/test_install.py`, `tests/test_config.py`, and focused sync/tool tests as needed for allowlist expectations.

## Task 1: Add The `shares.toml` Registry Model

**Files:**
- Create: `rightmemory/share_models.py`
- Create: `tests/test_shares.py`

- [ ] **Step 1: Write registry load/save tests**

Add this initial test module:

```python
import tempfile
import unittest
from pathlib import Path

from rightmemory.share_models import (
    ShareFilePart,
    ShareQuestionPart,
    ShareRelationship,
    load_shares,
    save_shares,
    validate_share_id,
)


class ShareModelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_validate_share_id_accepts_portable_ids(self):
        self.assertEqual(validate_share_id("auth-api_1.dev"), "auth-api_1.dev")

    def test_validate_share_id_rejects_paths(self):
        with self.assertRaises(ValueError):
            validate_share_id("../auth")

    def test_save_and_load_provider_file_question_share(self):
        share = ShareRelationship(
            share_id="auth-api",
            role="provider",
            title="Auth API",
            provider_id="alice",
            hub_url="http://127.0.0.1:8765",
            credential_id="alice-publish",
            state="draft",
            parts=("file", "question"),
            file=ShareFilePart(
                view_id="auth-api-files",
                intent="Expose auth API integration context for frontend agents",
                approved=False,
            ),
            question=ShareQuestionPart(
                view_id="auth-api-ask",
                intent="Let frontend agents ask temporary auth API questions",
                question_base_url="http://127.0.0.1:8766",
                approved=False,
            ),
        )

        save_shares(self.root, {"auth-api": share})
        loaded = load_shares(self.root)

        self.assertEqual(loaded["auth-api"], share)
        text = (self.root / "shares.toml").read_text(encoding="utf-8")
        self.assertIn("[shares.auth-api]", text)
        self.assertIn('parts = ["file", "question"]', text)

    def test_load_rejects_part_without_config(self):
        (self.root / "shares.toml").write_text(
            '[shares.auth-api]\n'
            'version = 1\n'
            'role = "provider"\n'
            'title = "Auth API"\n'
            'state = "draft"\n'
            'parts = ["file"]\n',
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_shares(self.root)

        self.assertIn("file part requires [shares.auth-api.file]", str(caught.exception))
```

- [ ] **Step 2: Run the failing registry tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareModelTests
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.share_models'`.

- [ ] **Step 3: Implement `share_models.py`**

Create `rightmemory/share_models.py` with these public types and helpers:

```python
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


SHARE_REGISTRY_FILE = "shares.toml"
SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SHARE_ROLES = {"provider", "consumer"}
SHARE_STATES = {"draft", "approved", "published", "joined"}
SHARE_PARTS = {"file", "question"}


@dataclass(frozen=True)
class ShareFilePart:
    view_id: str | None = None
    intent: str | None = None
    heading_id: str | None = None
    approved: bool = False


@dataclass(frozen=True)
class ShareQuestionPart:
    view_id: str | None = None
    intent: str | None = None
    heading_id: str | None = None
    question_base_url: str | None = None
    approved: bool = False


@dataclass(frozen=True)
class ShareRelationship:
    share_id: str
    role: str
    title: str
    state: str
    parts: tuple[str, ...]
    provider_id: str | None = None
    hub_url: str | None = None
    credential_id: str | None = None
    accepted_from: str | None = None
    file: ShareFilePart | None = None
    question: ShareQuestionPart | None = None
```

Also implement:

```python
def validate_share_id(value: str) -> str:
    clean = str(value).strip()
    if not SHARE_ID_RE.fullmatch(clean):
        raise ValueError(f"share id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return clean


def load_shares(memory_root: Path) -> dict[str, ShareRelationship]:
    path = Path(memory_root).expanduser() / SHARE_REGISTRY_FILE
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    raw_shares = data.get("shares", {})
    if not isinstance(raw_shares, dict):
        raise ValueError("shares.toml must contain a [shares] table")
    return {
        validate_share_id(str(share_id)): _load_share(validate_share_id(str(share_id)), raw)
        for share_id, raw in raw_shares.items()
    }


def save_shares(memory_root: Path, shares: dict[str, ShareRelationship]) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory share relationship registry", ""]
    for share_id in sorted(shares):
        share = _validate_share(shares[share_id])
        key = _toml_key(share.share_id)
        lines.append(f"[shares.{key}]")
        lines.append("version = 1")
        lines.append(f"role = {_toml_string(share.role)}")
        lines.append(f"title = {_toml_string(share.title)}")
        if share.provider_id:
            lines.append(f"provider_id = {_toml_string(share.provider_id)}")
        if share.hub_url:
            lines.append(f"hub_url = {_toml_string(share.hub_url)}")
        if share.credential_id:
            lines.append(f"credential_id = {_toml_string(share.credential_id)}")
        lines.append(f"state = {_toml_string(share.state)}")
        lines.append(f"parts = {_toml_array(share.parts)}")
        if share.accepted_from:
            lines.append(f"accepted_from = {_toml_string(share.accepted_from)}")
        if share.file:
            lines.extend(["", f"[shares.{key}.file]"])
            if share.file.view_id:
                lines.append(f"view_id = {_toml_string(share.file.view_id)}")
            if share.file.heading_id:
                lines.append(f"heading_id = {_toml_string(share.file.heading_id)}")
            if share.file.intent:
                lines.append(f"intent = {_toml_string(share.file.intent)}")
            lines.append(f"approved = {str(share.file.approved).lower()}")
        if share.question:
            lines.extend(["", f"[shares.{key}.question]"])
            if share.question.view_id:
                lines.append(f"view_id = {_toml_string(share.question.view_id)}")
            if share.question.heading_id:
                lines.append(f"heading_id = {_toml_string(share.question.heading_id)}")
            if share.question.intent:
                lines.append(f"intent = {_toml_string(share.question.intent)}")
            if share.question.question_base_url:
                lines.append(f"question_base_url = {_toml_string(share.question.question_base_url)}")
            lines.append(f"approved = {str(share.question.approved).lower()}")
        lines.append("")
    (root / SHARE_REGISTRY_FILE).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
```

Implement private helpers `_load_share`, `_load_file_part`, `_load_question_part`, `_validate_share`, `_optional_string`, `_toml_key`, `_toml_string`, and `_toml_array`. Validation rules:

- `role` must be `provider` or `consumer`.
- `state` must be `draft`, `approved`, `published`, or `joined`.
- `parts` must contain `file`, `question`, or both.
- If `file` is in `parts`, the share must have a file part table.
- If `question` is in `parts`, the share must have a question part table.
- Provider-side parts must have `view_id` and `intent`.
- Consumer-side parts must have `heading_id`.
- `_toml_key` must match the existing shared-view registry behavior: return the key unchanged only for `^[A-Za-z0-9_-]+$`, otherwise return `_toml_string(value)`. This keeps valid dotted share ids such as `auth-api_1.dev` from becoming nested TOML tables.
- `_toml_string` must use `json.dumps(value)`, and `_toml_array` must render values through `_toml_string`.

- [ ] **Step 4: Run registry tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareModelTests
```

Expected: PASS.

- [ ] **Step 5: Commit registry model**

```bash
rtk git add rightmemory/share_models.py tests/test_shares.py
rtk git commit -m "feat: add share relationship registry"
```

## Task 2: Split Publish Primitives From Invitation Creation

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Modify: `rightmemory/shared_view_questions.py`
- Modify: `tests/test_shared_views.py`

- [ ] **Step 1: Add tests for publish/register without invitations**

Add these tests to `tests/test_shared_views.py` near existing file and question publish tests:

```python
def test_publish_file_view_package_does_not_create_invitation(self):
    save_shared_view_credential(
        self.root,
        "alice-publish",
        kind="http-publish",
        token="publish-token",
        base_url="https://hub.example.test",
        provider_id="alice",
    )
    write_file_view_recipe(
        self.root,
        view_id="auth-api-files",
        title="Auth API Files",
        intent="Expose auth API integration context.",
        include_nodes=("token-expiry",),
        approved=True,
    )
    (self.root / "MEMORY.md").write_text("# Auth {#auth}\n\n- `token-expiry` Tokens expire.\n", encoding="utf-8")
    clients = []

    with patch("rightmemory.shared_view_files.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
        result = publish_file_view_package(
            self.root,
            "auth-api-files",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
        )

    self.assertEqual(result["version_id"], "ver_1")
    self.assertEqual(clients[0].publish_calls[0]["view_id"], "auth-api-files")
    self.assertEqual(clients[0].invitation_calls, [])


def test_register_question_view_with_hub_does_not_create_invitation(self):
    save_shared_view_credential(
        self.root,
        "alice-publish",
        kind="http-publish",
        token="publish-token",
        base_url="https://hub.example.test",
        provider_id="alice",
    )
    write_question_view(
        self.root,
        view_id="auth-api-ask",
        title="Auth API Questions",
        intent="Let frontend agents ask auth questions.",
        retriever_instructions="Answer from auth memory.",
        approved=True,
    )
    clients = []

    with patch("rightmemory.shared_view_questions.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
        result = register_question_view_with_hub(
            self.root,
            "auth-api-ask",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            question_base_url="https://provider.example.test",
        )

    self.assertEqual(result["view_id"], "auth-api-ask")
    self.assertEqual(clients[0].question_registrations[0]["view_id"], "auth-api-ask")
    self.assertEqual(clients[0].invitation_calls, [])
    config = load_question_view(self.root, "auth-api-ask")
    self.assertEqual(len(config.access_token_hashes), 1)
```

Update imports in `tests/test_shared_views.py`:

```python
from rightmemory.shared_view_files import publish_file_view_package
from rightmemory.shared_view_questions import register_question_view_with_hub
```

- [ ] **Step 2: Run focused tests to verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewAutoPublishTests.test_publish_file_view_package_does_not_create_invitation tests.test_shared_views.SharedQuestionViewTests.test_register_question_view_with_hub_does_not_create_invitation
```

Expected: FAIL because `publish_file_view_package` and `register_question_view_with_hub` are not defined.

- [ ] **Step 3: Add `publish_file_view_package`**

In `rightmemory/shared_view_files.py`, add:

```python
def publish_file_view_package(
    memory_root: Path,
    view_id: str,
    *,
    hub_url: str,
    credential_id: str,
) -> dict[str, object]:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    if not recipe.approved:
        raise ValueError(f"file view is not approved: {recipe.view_id}")
    clean_hub_url = _required_text(hub_url, "hub_url")
    clean_credential_id = validate_heading_id(credential_id)
    credential = load_shared_view_credential(root, clean_credential_id)
    with TemporaryDirectory() as tempdir:
        package = Path(tempdir) / recipe.view_id
        export_file_view_package(root, recipe.view_id, package)
        response = HubClient(clean_hub_url, credential["token"]).publish_package(recipe.view_id, package)
    if not isinstance(response, dict):
        raise ValueError("hub did not return a publish response")
    return response
```

Then change `invite_file_view` so it calls `publish_file_view_package(...)` before creating the per-view invitation. Preserve the existing return string.

- [ ] **Step 4: Add `register_question_view_with_hub`**

In `rightmemory/shared_view_questions.py`, add:

```python
def register_question_view_with_hub(
    memory_root: Path,
    view_id: str,
    *,
    hub_url: str,
    credential_id: str,
    question_base_url: str,
) -> dict[str, object]:
    root = Path(memory_root).expanduser()
    config = validate_question_view_source(root, view_id)
    if not config.approved:
        raise ValueError(f"question view is not approved: {config.view_id}")
    clean_hub_url = _required_text(hub_url, "hub_url")
    clean_credential_id = validate_heading_id(credential_id)
    clean_question_base_url = _required_text(question_base_url, "question_base_url")
    credential = load_shared_view_credential(root, clean_credential_id)
    question_token = secrets.token_urlsafe(32)
    updated_hashes = tuple(dict.fromkeys((*config.access_token_hashes, question_token_hash(question_token))))
    updated_config = QuestionViewConfig(
        view_id=config.view_id,
        title=config.title,
        intent=config.intent,
        approved=config.approved,
        start_timeout_seconds=config.start_timeout_seconds,
        answer_timeout_seconds=config.answer_timeout_seconds,
        provider_role=config.provider_role,
        access_token_hashes=updated_hashes,
    )
    _write_text(root / PROVIDER_VIEWS_DIR / config.view_id / "question.toml", _render_question_toml(updated_config))
    response = HubClient(clean_hub_url, credential["token"]).register_question_view(
        config.view_id,
        title=config.title,
        description=config.intent,
        question_base_url=clean_question_base_url,
        question_token=question_token,
    )
    if not isinstance(response, dict):
        raise ValueError("hub did not return a question registration response")
    return response
```

Then change `publish_question_view` so it calls `register_question_view_with_hub(...)` before creating the per-view invitation. Preserve the existing return string.

- [ ] **Step 5: Run focused shared-view tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views
```

Expected: PASS.

- [ ] **Step 6: Commit publish primitive split**

```bash
rtk git add rightmemory/shared_view_files.py rightmemory/shared_view_questions.py tests/test_shared_views.py
rtk git commit -m "feat: split shared-view publish primitives"
```

## Task 3: Add Hub Bundled Share Invitations

**Files:**
- Modify: `rightmemory/hub/store.py`
- Modify: `rightmemory/hub/app.py`
- Modify: `rightmemory/hub/client.py`
- Modify: `tests/test_http_hub.py`

- [ ] **Step 1: Add store-level bundled invitation test**

Add to `HubStoreTests` in `tests/test_http_hub.py`:

```python
def test_share_invitation_accepts_file_and_question_parts(self):
    store = HubStore(self.root)
    store.initialize(admin_token="admin-secret")
    provider_token = store.create_provider_token("alice", label="publish")
    _store_file_package(store, "auth-api-files", provider_token.token_id)
    store.register_question_view(
        "auth-api-ask",
        provider_id="alice",
        title="Auth API Questions",
        description="Ask auth questions.",
        question_base_url="https://provider.example.test",
        question_token="question-token",
        created_by_token_id=provider_token.token_id,
    )

    invitation = store.create_share_invitation(
        "auth-api",
        provider_id="alice",
        title="Auth API",
        parts=[
            {"type": "file", "view_id": "auth-api-files"},
            {"type": "question", "view_id": "auth-api-ask"},
        ],
        actor_id=provider_token.token_id,
        label="frontend",
    )
    described = store.describe_share_invitation(invitation["raw_token"])
    accepted = store.accept_share_invitation(invitation["raw_token"], consumer_label="frontend")

    self.assertIsNotNone(described)
    self.assertEqual(described["share_id"], "auth-api")
    self.assertEqual([part["type"] for part in described["parts"]], ["file", "question"])
    self.assertIsNotNone(accepted)
    self.assertEqual(accepted["share_id"], "auth-api")
    self.assertEqual(len(accepted["parts"]), 2)
    question_part = [part for part in accepted["parts"] if part["type"] == "question"][0]
    self.assertEqual(question_part["question_token"], "question-token")
```

Add helper near existing package helpers:

```python
def _store_file_package(store: HubStore, view_id: str, token_id: str):
    package = store.root / f"package-{view_id}"
    _write_package(package, view_id=view_id)
    return store.store_package_version(
        package,
        view_id=view_id,
        provider_id="alice",
        created_by_token_id=token_id,
    )
```

If existing `_write_package` does not accept `view_id`, update it to default to `"alice-auth-api"` and use the provided `view_id` in `rightmemory-shared-view.toml` and manifest content.

- [ ] **Step 2: Add API/client bundled invitation test**

Add to the HTTP app tests in `tests/test_http_hub.py`:

```python
@unittest.skipUnless(HTTPX2_AVAILABLE, "httpx2 not installed")
def test_share_invitation_api_round_trip(self):
    store = HubStore(self.root)
    store.initialize(admin_token="admin-secret")
    provider_token = store.create_provider_token("alice", label="publish")
    _store_file_package(store, "auth-api-files", provider_token.token_id)
    store.register_question_view(
        "auth-api-ask",
        provider_id="alice",
        title="Auth API Questions",
        description="Ask auth questions.",
        question_base_url="https://provider.example.test",
        question_token="question-token",
        created_by_token_id=provider_token.token_id,
    )
    client = TestClient(create_hub_app(self.root))

    response = client.post(
        "/api/shares/auth-api/invitations",
        headers={"Authorization": f"Bearer {provider_token.raw_token}"},
        json={
            "title": "Auth API",
            "parts": [
                {"type": "file", "view_id": "auth-api-files"},
                {"type": "question", "view_id": "auth-api-ask"},
            ],
            "label": "frontend",
        },
    )
    self.assertEqual(response.status_code, 201)
    invitation_url = response.json()["invitation_url"]
    token = invitation_url.rsplit("/", 1)[1]

    described = client.get(f"/api/share-invitations/{token}/view")
    accepted = client.post(f"/api/share-invitations/{token}/accept", json={"consumer_label": "frontend"})

    self.assertEqual(described.status_code, 200)
    self.assertEqual(accepted.status_code, 201)
    self.assertEqual(accepted.json()["share_id"], "auth-api")
    self.assertEqual(len(accepted.json()["parts"]), 2)
```

- [ ] **Step 3: Run failing hub tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub.HubStoreTests.test_share_invitation_accepts_file_and_question_parts
```

Expected: FAIL because `HubStore.create_share_invitation` is not defined.

- [ ] **Step 4: Add hub store migration**

In `rightmemory/hub/store.py`, add a second schema migration. Do not only edit `_MIGRATION_1`, because existing initialized hub roots already have migration version `1` recorded.

Add `_MIGRATION_2`:

```sql
CREATE TABLE IF NOT EXISTS share_invitations(
    id TEXT PRIMARY KEY,
    share_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    title TEXT NOT NULL,
    label TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    FOREIGN KEY(token_id) REFERENCES tokens(id)
);
CREATE INDEX IF NOT EXISTS idx_share_invitations_token ON share_invitations(token_id);
CREATE INDEX IF NOT EXISTS idx_share_invitations_provider ON share_invitations(provider_id, created_at);
```

Update `_apply_migrations`:

```python
if 2 not in applied:
    connection.executescript(_MIGRATION_2)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
        (2, _now_iso()),
    )
```

Keep `_MIGRATION_1` unchanged except for tests that create brand-new databases through the normal migration path.

- [ ] **Step 5: Add hub store methods**

Add methods to `HubStore`:

```python
def create_share_invitation(
    self,
    share_id: str,
    *,
    provider_id: str,
    title: str,
    parts: list[dict[str, str]],
    actor_id: str | None = None,
    label: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    clean_share_id = _validate_hub_id(share_id, "share_id")
    clean_provider_id = _validate_hub_id(provider_id, "provider_id")
    clean_title = _required_string(title, "title")
    clean_parts = self._validate_share_parts(clean_provider_id, parts)
    clean_label = _optional_string(label)
    clean_expires_at = _normalize_optional_datetime(expires_at, "expires_at")
    clean_actor_id = _validate_hub_id(actor_id, "actor_id") if actor_id else None
    payload = {"share_id": clean_share_id, "title": clean_title, "provider_id": clean_provider_id, "parts": clean_parts}
    with self._connect() as connection:
        self._apply_migrations(connection)
        token = self._create_token(connection, action="share-invite", provider_id=clean_provider_id, view_id=None, label=clean_label)
        invitation_id = _new_id("sinv")
        now = _now_iso()
        connection.execute(
            """
            INSERT INTO share_invitations(
                id, share_id, provider_id, token_id, title, label, expires_at, revoked_at, created_at, accepted_count, payload_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)
            """,
            (invitation_id, clean_share_id, clean_provider_id, token.token_id, clean_title, clean_label, clean_expires_at, now, json.dumps(payload, sort_keys=True)),
        )
        self._append_audit_event(
            connection,
            "share_invitation.created",
            actor_id=clean_actor_id,
            provider_id=clean_provider_id,
            details={"share_id": clean_share_id, "invitation_id": invitation_id, "label": clean_label},
        )
    return {
        "invitation_id": invitation_id,
        "token_id": token.token_id,
        "raw_token": token.raw_token,
        "share_id": clean_share_id,
        "label": clean_label,
        "expires_at": clean_expires_at,
        "created_at": now,
    }
```

Also add:

```python
def describe_share_invitation(self, raw_token: str) -> dict[str, Any] | None:
    row = self._share_invitation_row(raw_token)
    if row is None:
        return None
    return json.loads(row["payload_json"])


def accept_share_invitation(self, raw_token: str, *, consumer_label: str | None = None) -> dict[str, Any] | None:
    row = self._share_invitation_row(raw_token)
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    clean_consumer_label = _optional_string(consumer_label)
    accepted_parts: list[dict[str, Any]] = []
    with self._connect() as connection:
        self._apply_migrations(connection)
        for part in payload["parts"]:
            view_id = part["view_id"]
            connection_token = self._create_token(
                connection,
                action="connect",
                provider_id=row["provider_id"],
                view_id=view_id,
                label=clean_consumer_label,
            )
            connection_id = _new_id("con")
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO connections(id, invitation_id, view_id, token_id, consumer_label, created_at, revoked_at)
                VALUES(?, NULL, ?, ?, ?, ?, NULL)
                """,
                (connection_id, view_id, connection_token.token_id, clean_consumer_label, now),
            )
            accepted_part = {
                "type": part["type"],
                "view_id": view_id,
                "connection_id": connection_id,
                "token_id": connection_token.token_id,
                "connection_token": connection_token.raw_token,
            }
            if part["type"] == "question":
                manifest_json = self._current_manifest_json(connection, view_id)
                accepted_part.update(_accepted_invitation_metadata(manifest_json))
            accepted_parts.append(accepted_part)
        connection.execute(
            "UPDATE share_invitations SET accepted_count = accepted_count + 1 WHERE id = ?",
            (row["id"],),
        )
        self._append_audit_event(
            connection,
            "share_invitation.accepted",
            provider_id=row["provider_id"],
            details={"share_id": row["share_id"], "invitation_id": row["id"], "consumer_label": clean_consumer_label},
        )
    return {
        "share_id": payload["share_id"],
        "title": payload["title"],
        "provider_id": payload["provider_id"],
        "consumer_label": clean_consumer_label,
        "parts": accepted_parts,
    }
```

Add private `HubStore` methods `_share_invitation_row`, `_validate_share_parts`, and `_current_manifest_json`.

`_share_invitation_row` should:

- call `_find_token(raw_token, action="share-invite", provider_id=None, view_id=None)`;
- join `share_invitations` by `token_id`;
- return `None` if the invitation is revoked or expired;
- return the row otherwise.

`_validate_share_parts` must confirm every part has type `file` or `question`, every view exists, and every view belongs to the same provider.

`_current_manifest_json` should select `vv.manifest_json` from the current version for the given view id and raise `KeyError` if no current version exists.

- [ ] **Step 6: Add hub app endpoints**

In `rightmemory/hub/app.py`, add:

```python
@app.post("/api/shares/{share_id}/invitations", status_code=status.HTTP_201_CREATED)
def create_share_invitation(share_id: str, request: Request, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    actor = _require_token(store, request, action="publish")
    _require_actor_provider(actor.provider_id)
    data = payload or {}
    try:
        invitation = store.create_share_invitation(
            share_id,
            provider_id=actor.provider_id,
            title=_required_payload_str(data, "title"),
            parts=_required_payload_parts(data),
            actor_id=actor.token_id,
            label=_optional_payload_str(data, "label"),
            expires_at=_optional_payload_str(data, "expires_at"),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    config = store.load_config()
    return {
        "invitation_id": invitation["invitation_id"],
        "token_id": invitation["token_id"],
        "share_id": invitation["share_id"],
        "invitation_url": f"{config.public_base_url.rstrip('/')}/i/share/{invitation['raw_token']}",
        "expires_at": invitation["expires_at"],
    }


@app.get("/i/share/{token}")
def share_invitation_landing(token: str) -> dict[str, Any]:
    invitation = store.describe_share_invitation(token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="share invitation not found")
    return {
        "share_id": invitation["share_id"],
        "title": invitation["title"],
        "provider_id": invitation["provider_id"],
        "parts": invitation["parts"],
        "api": {
            "view": f"/api/share-invitations/{token}/view",
            "accept": f"/api/share-invitations/{token}/accept",
        },
    }


@app.get("/api/share-invitations/{token}/view")
def describe_share_invitation(token: str) -> dict[str, Any]:
    invitation = store.describe_share_invitation(token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="share invitation not found")
    return invitation


@app.post("/api/share-invitations/{token}/accept", status_code=status.HTTP_201_CREATED)
def accept_share_invitation(token: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    accepted = store.accept_share_invitation(token, consumer_label=_optional_payload_str(payload or {}, "consumer_label"))
    if accepted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="share invitation not found")
    return accepted
```

Add `_required_payload_parts(data)` that returns a list of dictionaries with string `type` and `view_id`, raising `ValueError` for missing or malformed parts.

- [ ] **Step 7: Add hub client methods**

In `rightmemory/hub/client.py`, add:

```python
def create_share_invitation(
    self,
    share_id: str,
    *,
    title: str,
    parts: list[dict[str, str]],
    label: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "parts": parts}
    if label:
        payload["label"] = label
    if expires_at:
        payload["expires_at"] = expires_at
    return self._request(
        "POST",
        f"/api/shares/{urllib.parse.quote(share_id)}/invitations",
        json_body=payload,
        bearer=True,
    )


def get_share_invitation(self, token: str) -> dict[str, Any]:
    return self._request("GET", f"/api/share-invitations/{urllib.parse.quote(token)}/view")


def accept_share_invitation(self, token: str, *, consumer_label: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if consumer_label:
        payload["consumer_label"] = consumer_label
    return self._request("POST", f"/api/share-invitations/{urllib.parse.quote(token)}/accept", json_body=payload)
```

- [ ] **Step 8: Run hub tests**

Run:

```bash
rtk python -m unittest tests.test_http_hub
```

Expected: PASS, with existing skips for unavailable HTTPX2 if applicable.

- [ ] **Step 9: Commit bundled hub invitations**

```bash
rtk git add rightmemory/hub/store.py rightmemory/hub/app.py rightmemory/hub/client.py tests/test_http_hub.py
rtk git commit -m "feat: add bundled share invitations"
```

## Task 4: Add Share Orchestration

**Files:**
- Create: `rightmemory/shares.py`
- Modify: `tests/test_shares.py`

- [ ] **Step 1: Add provider create/approve/publish tests**

Append to `tests/test_shares.py`:

```python
from unittest.mock import patch

from rightmemory.share_models import load_shares
from rightmemory.shared_view_models import load_shared_view_credential, save_shared_view_credential
from rightmemory.shares import approve_share, create_share, publish_share


class ShareProviderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Auth {#auth}\n\n- `token-expiry` Tokens expire.\n", encoding="utf-8")
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )

    def test_create_share_builds_requested_parts_unapproved(self):
        with (
            patch("rightmemory.shares.run_file_view_builder", return_value="wrote file view recipe auth-api-files") as file_builder,
            patch("rightmemory.shares.run_question_view_builder", return_value="wrote question view auth-api-ask") as question_builder,
        ):
            result = create_share(
                self.root,
                "auth-api",
                title="Auth API",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                file_intent="Expose auth API integration context.",
                question_intent="Let frontend agents ask auth questions.",
                question_base_url="https://provider.example.test",
            )

        share = load_shares(self.root)["auth-api"]
        self.assertIn("created share auth-api", result)
        self.assertEqual(share.parts, ("file", "question"))
        self.assertEqual(share.file.view_id, "auth-api-files")
        self.assertEqual(share.question.view_id, "auth-api-ask")
        self.assertFalse(share.file.approved)
        self.assertFalse(share.question.approved)
        file_builder.assert_called_once()
        question_builder.assert_called_once()

    def test_approve_share_approves_all_parts(self):
        _write_canonical_file_and_question_parts(self.root)
        create_share(
            self.root,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )

        result = approve_share(self.root, "auth-api")

        share = load_shares(self.root)["auth-api"]
        self.assertEqual(result, "approved share auth-api")
        self.assertEqual(share.state, "approved")
        self.assertTrue(share.file.approved)
        self.assertTrue(share.question.approved)

    def test_publish_share_creates_one_bundled_invite(self):
        _write_canonical_file_and_question_parts(self.root)
        create_share(
            self.root,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )
        approve_share(self.root, "auth-api")

        with (
            patch("rightmemory.shares.publish_file_view_package", return_value={"view_id": "auth-api-files"}),
            patch("rightmemory.shares.register_question_view_with_hub", return_value={"view_id": "auth-api-ask"}),
            patch("rightmemory.shares.HubClient") as client_type,
        ):
            client_type.return_value.create_share_invitation.return_value = {
                "invitation_url": "https://hub.example.test/i/share/share-token"
            }
            result = publish_share(self.root, "auth-api", label="frontend")

        share = load_shares(self.root)["auth-api"]
        self.assertIn("published share auth-api", result)
        self.assertIn("https://hub.example.test/i/share/share-token", result)
        self.assertEqual(share.state, "published")
        client_type.return_value.create_share_invitation.assert_called_once_with(
            "auth-api",
            title="Auth API",
            parts=[
                {"type": "file", "view_id": "auth-api-files"},
                {"type": "question", "view_id": "auth-api-ask"},
            ],
            label="frontend",
            expires_at=None,
        )
```

Add helper:

```python
def _write_canonical_file_and_question_parts(root: Path):
    from rightmemory.shared_view_files import write_file_view_recipe
    from rightmemory.shared_view_questions import write_question_view

    write_file_view_recipe(
        root,
        view_id="auth-api-files",
        title="Auth API Files",
        intent="Expose auth API integration context.",
        include_nodes=("token-expiry",),
        approved=False,
        publish_hub_url="https://hub.example.test",
        publish_credential_id="alice-publish",
    )
    write_question_view(
        root,
        view_id="auth-api-ask",
        title="Auth API Questions",
        intent="Let frontend agents ask auth questions.",
        retriever_instructions="Answer from auth memory.",
        approved=False,
    )
```

- [ ] **Step 2: Add consumer join/status tests**

Append:

```python
from rightmemory.share_models import ShareRelationship
from rightmemory.shares import join_share, share_status


class ShareConsumerFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

    def test_join_share_accepts_bundle_and_creates_relationship_and_connections(self):
        with (
            patch("rightmemory.shares.HubClient") as client_type,
            patch("rightmemory.shares.pull_file_view") as pull,
        ):
            client_type.return_value.get_share_invitation.return_value = {
                "share_id": "auth-api",
                "title": "Auth API",
                "provider_id": "alice",
                "parts": [
                    {"type": "file", "view_id": "auth-api-files"},
                    {"type": "question", "view_id": "auth-api-ask", "question_base_url": "https://provider.example.test"},
                ],
            }
            client_type.return_value.accept_share_invitation.return_value = {
                "share_id": "auth-api",
                "title": "Auth API",
                "provider_id": "alice",
                "parts": [
                    {
                        "type": "file",
                        "view_id": "auth-api-files",
                        "connection_token": "file-connection-token",
                    },
                    {
                        "type": "question",
                        "view_id": "auth-api-ask",
                        "connection_token": "question-connection-token",
                        "question_token": "question-token",
                    },
                ],
            }
            pull.return_value = type("PullResult", (), {"heading_id": "auth-api-files", "status": "pulled", "message": "file view pulled"})()

            result = join_share(self.root, "https://hub.example.test/i/share/share-token", consumer_label="frontend")

        shares = load_shares(self.root)
        self.assertIn("joined share auth-api", result)
        self.assertEqual(shares["auth-api"].state, "joined")
        connections_text = (self.root / "shared_views.toml").read_text(encoding="utf-8")
        memory_text = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("[connections.auth-api-files]", connections_text)
        self.assertIn("[connections.auth-api-ask]", connections_text)
        self.assertIn("{MF#auth-api-files}", memory_text)
        self.assertIn("{MQ#auth-api-ask}", memory_text)
        self.assertEqual(load_shared_view_credential(self.root, "http-auth-api-files")["token"], "file-connection-token")
        self.assertEqual(load_shared_view_credential(self.root, "http-auth-api-ask-question")["token"], "question-token")

    def test_share_status_summarizes_relationship(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="consumer",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    state="joined",
                    parts=("file",),
                    file=ShareFilePart(heading_id="auth-api-files"),
                )
            },
        )

        result = share_status(self.root, "auth-api")

        self.assertIn("auth-api provider=alice state=joined parts=file", result)
        self.assertIn("file auth-api-files", result)
```

- [ ] **Step 3: Run failing orchestration tests**

Run:

```bash
rtk python -m unittest tests.test_shares
```

Expected: FAIL because `rightmemory.shares` is not defined.

- [ ] **Step 4: Implement provider orchestration**

Create `rightmemory/shares.py` with imports:

```python
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .hub.client import HubClient
from .share_models import ShareFilePart, ShareQuestionPart, ShareRelationship, load_shares, save_shares, validate_share_id
from .shared_view_builder import run_file_view_builder, run_question_view_builder
from .shared_view_files import approve_file_view, publish_file_view_package, pull_file_view
from .shared_view_models import SharedViewTarget, load_shared_view_credential, save_shared_view_credential, validate_heading_id
from .shared_view_questions import approve_question_view, register_question_view_with_hub
from .shared_views import accept_shared_view
```

Add:

```python
def create_share(
    memory_root: Path,
    share_id: str,
    *,
    title: str,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    file_intent: str | None = None,
    question_intent: str | None = None,
    question_base_url: str | None = None,
    build_parts: bool = True,
) -> str:
    root = Path(memory_root).expanduser()
    clean_share_id = validate_share_id(share_id)
    parts: list[str] = []
    file_part = None
    question_part = None
    if file_intent:
        file_view_id = f"{clean_share_id}-files"
        parts.append("file")
        file_part = ShareFilePart(view_id=file_view_id, intent=file_intent, approved=False)
        if build_parts:
            run_file_view_builder(root, view_id=file_view_id, title=f"{title} Files", intent=file_intent, hub_url=hub_url, credential_id=credential_id)
    if question_intent:
        if not question_base_url:
            raise ValueError("share question part requires question_base_url")
        question_view_id = f"{clean_share_id}-ask"
        parts.append("question")
        question_part = ShareQuestionPart(view_id=question_view_id, intent=question_intent, question_base_url=question_base_url, approved=False)
        if build_parts:
            run_question_view_builder(root, view_id=question_view_id, title=f"{title} Questions", intent=question_intent)
    if not parts:
        raise ValueError("share create requires --file, --question, or both")
    shares = load_shares(root)
    shares[clean_share_id] = ShareRelationship(
        share_id=clean_share_id,
        role="provider",
        title=title.strip(),
        provider_id=validate_heading_id(provider_id),
        hub_url=hub_url.rstrip("/"),
        credential_id=validate_heading_id(credential_id),
        state="draft",
        parts=tuple(parts),
        file=file_part,
        question=question_part,
    )
    save_shares(root, shares)
    return f"created share {clean_share_id}; review generated parts, then run: rightmemory share approve {clean_share_id}"
```

Add:

```python
def approve_share(memory_root: Path, share_id: str) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    share = _require_share(shares, share_id)
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {share.share_id}")
    file_part = share.file
    question_part = share.question
    if "file" in share.parts:
        approve_file_view(root, _required_part_value(file_part.view_id if file_part else None, "file view_id"))
        file_part = ShareFilePart(view_id=file_part.view_id, intent=file_part.intent, approved=True)
    if "question" in share.parts:
        approve_question_view(root, _required_part_value(question_part.view_id if question_part else None, "question view_id"))
        question_part = ShareQuestionPart(
            view_id=question_part.view_id,
            intent=question_part.intent,
            question_base_url=question_part.question_base_url,
            approved=True,
        )
    shares[share.share_id] = _replace_share(share, state="approved", file=file_part, question=question_part)
    save_shares(root, shares)
    return f"approved share {share.share_id}"
```

Add:

```python
def publish_share(memory_root: Path, share_id: str, *, label: str | None = None, expires_at: str | None = None) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    share = _require_share(shares, share_id)
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {share.share_id}")
    if share.state not in {"approved", "published"}:
        raise ValueError(f"share is not approved: {share.share_id}")
    parts_payload: list[dict[str, str]] = []
    if "file" in share.parts:
        file_view_id = _required_part_value(share.file.view_id if share.file else None, "file view_id")
        publish_file_view_package(root, file_view_id, hub_url=_required_share_value(share.hub_url, "hub_url"), credential_id=_required_share_value(share.credential_id, "credential_id"))
        parts_payload.append({"type": "file", "view_id": file_view_id})
    if "question" in share.parts:
        question_view_id = _required_part_value(share.question.view_id if share.question else None, "question view_id")
        register_question_view_with_hub(
            root,
            question_view_id,
            hub_url=_required_share_value(share.hub_url, "hub_url"),
            credential_id=_required_share_value(share.credential_id, "credential_id"),
            question_base_url=_required_part_value(share.question.question_base_url if share.question else None, "question_base_url"),
        )
        parts_payload.append({"type": "question", "view_id": question_view_id})
    client = HubClient(_required_share_value(share.hub_url, "hub_url"), _load_publish_token(root, _required_share_value(share.credential_id, "credential_id")))
    invitation = client.create_share_invitation(share.share_id, title=share.title, parts=parts_payload, label=label, expires_at=expires_at)
    invitation_url = invitation.get("invitation_url")
    if not isinstance(invitation_url, str) or not invitation_url:
        raise ValueError("hub did not return an invitation_url")
    _record_runtime_invitation(root, share.share_id, invitation_url)
    shares[share.share_id] = _replace_share(share, state="published")
    save_shares(root, shares)
    return f"published share {share.share_id}\ninvitation_url\t{invitation_url}"
```

Use `load_shared_view_credential` inside `_load_publish_token`.

- [ ] **Step 5: Implement consumer join/status**

In `rightmemory/shares.py`, add:

```python
def join_share(memory_root: Path, invitation_url: str, *, consumer_label: str | None = None) -> str:
    root = Path(memory_root).expanduser()
    base_url, token = _parse_share_invitation_url(invitation_url)
    client = HubClient(base_url)
    described = client.get_share_invitation(token)
    accepted = client.accept_share_invitation(token, consumer_label=consumer_label)
    share_id = validate_share_id(str(accepted.get("share_id") or described.get("share_id")))
    title = str(accepted.get("title") or described.get("title") or share_id)
    provider_id = validate_heading_id(str(accepted.get("provider_id") or described.get("provider_id")))
    parts: list[str] = []
    file_part: ShareFilePart | None = None
    question_part: ShareQuestionPart | None = None
    described_parts = {str(part.get("view_id")): part for part in described.get("parts", []) if isinstance(part, dict)}
    for raw_part in accepted.get("parts", []):
        if not isinstance(raw_part, dict):
            continue
        part_type = str(raw_part.get("type") or "")
        view_id = validate_heading_id(str(raw_part.get("view_id") or ""))
        connection_token = _required_response_value(raw_part.get("connection_token"), "connection_token")
        credential_id = validate_heading_id(f"http-{view_id}")
        save_shared_view_credential(root, credential_id, kind="http-connection", token=connection_token, base_url=base_url, view_id=view_id)
        if part_type == "file":
            parts.append("file")
            file_part = ShareFilePart(heading_id=view_id)
            accept_shared_view(
                root,
                heading_id=view_id,
                view_type="file",
                title=str(described_parts.get(view_id, {}).get("title") or view_id),
                body=f"Accepted as part of share {share_id}.",
                ref=f"rightmemory://mf/{view_id}",
                maintainer=provider_id,
                accepted_from=invitation_url,
                target=SharedViewTarget(kind="http-file", base_url=base_url, view_id=view_id, credential_id=credential_id, accepted_from_url=invitation_url),
            )
        elif part_type == "question":
            parts.append("question")
            question_credential_id = validate_heading_id(f"{credential_id}-question")
            question_base_url = _required_response_value(described_parts.get(view_id, {}).get("question_base_url"), "question_base_url")
            question_token = _required_response_value(raw_part.get("question_token"), "question_token")
            save_shared_view_credential(root, question_credential_id, kind="http-question", token=question_token, base_url=question_base_url, view_id=view_id)
            question_part = ShareQuestionPart(heading_id=view_id, question_base_url=question_base_url)
            accept_shared_view(
                root,
                heading_id=view_id,
                view_type="question",
                title=str(described_parts.get(view_id, {}).get("title") or view_id),
                body=f"Accepted as part of share {share_id}.",
                ref=f"rightmemory://mq/{view_id}",
                maintainer=provider_id,
                accepted_from=invitation_url,
                target=SharedViewTarget(
                    kind="http-question",
                    base_url=base_url,
                    view_id=view_id,
                    credential_id=credential_id,
                    question_base_url=question_base_url,
                    question_credential_id=question_credential_id,
                    accepted_from_url=invitation_url,
                ),
            )
    if not parts:
        raise ValueError("share invitation did not return any accepted parts")
    shares = load_shares(root)
    shares[share_id] = ShareRelationship(
        share_id=share_id,
        role="consumer",
        title=title,
        provider_id=provider_id,
        hub_url=base_url,
        state="joined",
        parts=tuple(dict.fromkeys(parts)),
        accepted_from=invitation_url,
        file=file_part,
        question=question_part,
    )
    save_shares(root, shares)
    pull_messages = []
    if file_part and file_part.heading_id:
        pulled = pull_file_view(root, file_part.heading_id)
        pull_messages.append(f"file {pulled.heading_id} {pulled.status}: {pulled.message}")
    suffix = "\n" + "\n".join(pull_messages) if pull_messages else ""
    return f"joined share {share_id}{suffix}"
```

Add:

```python
def share_status(memory_root: Path, share_id: str | None = None) -> str:
    shares = load_shares(Path(memory_root).expanduser())
    selected = [shares[validate_share_id(share_id)]] if share_id else [shares[key] for key in sorted(shares)]
    lines: list[str] = []
    for share in selected:
        provider = share.provider_id or "-"
        lines.append(f"{share.share_id} provider={provider} state={share.state} parts={','.join(share.parts)}")
        if share.file:
            lines.append(f"file {share.file.heading_id or share.file.view_id or '-'}")
        if share.question:
            lines.append(f"question {share.question.heading_id or share.question.view_id or '-'}")
    return "\n".join(lines).rstrip() + "\n"


def list_shares(memory_root: Path) -> str:
    return share_status(memory_root, None)
```

Add private helpers `_require_share`, `_replace_share`, `_required_share_value`, `_required_part_value`, `_required_response_value`, `_parse_share_invitation_url`, `_load_publish_token`, and `_record_runtime_invitation`. `_parse_share_invitation_url` must require `/i/share/<token>`.

`_load_publish_token` must use `load_shared_view_credential(root, credential_id)` and return the stored token. `_record_runtime_invitation` must write under `.runtime/shares/<share-id>.json`, not `shares.toml`.

- [ ] **Step 6: Run share tests**

Run:

```bash
rtk python -m unittest tests.test_shares
```

Expected: PASS.

- [ ] **Step 7: Commit share orchestration**

```bash
rtk git add rightmemory/shares.py tests/test_shares.py
rtk git commit -m "feat: add share relationship orchestration"
```

## Task 5: Wire The `rightmemory share` CLI

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add CLI dispatch tests**

Add to `tests/test_cli.py`:

```python
class ShareCliTests(unittest.TestCase):
    def test_share_create_dispatches_to_create_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.create_share", return_value="created share auth-api") as create,
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "share",
                    "create",
                    "auth-api",
                    "--title",
                    "Auth API",
                    "--provider",
                    "alice",
                    "--hub-url",
                    "https://hub.example.test",
                    "--credential-id",
                    "alice-publish",
                    "--file",
                    "Expose auth API integration context.",
                    "--question",
                    "Let frontend agents ask auth questions.",
                    "--question-base-url",
                    "https://provider.example.test",
                ])

        self.assertEqual(result, 0)
        self.assertIn("created share auth-api", stdout.getvalue())
        create.assert_called_once()
        self.assertEqual(create.call_args.args[:2], (root, "auth-api"))
        self.assertEqual(create.call_args.kwargs["provider_id"], "alice")
        self.assertEqual(create.call_args.kwargs["file_intent"], "Expose auth API integration context.")

    def test_share_publish_dispatches_to_publish_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.publish_share", return_value="published share auth-api\ninvitation_url\thttps://hub/i/share/token") as publish,
                patch("sys.stdout", stdout),
            ):
                result = main(["share", "publish", "auth-api", "--label", "frontend"])

        self.assertEqual(result, 0)
        publish.assert_called_once_with(root, "auth-api", label="frontend", expires_at=None)
        self.assertIn("https://hub/i/share/token", stdout.getvalue())

    def test_share_join_dispatches_to_join_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.join_share", return_value="joined share auth-api") as join,
                patch("sys.stdout", stdout),
            ):
                result = main(["share", "join", "https://hub.example.test/i/share/token", "--consumer-label", "frontend"])

        self.assertEqual(result, 0)
        join.assert_called_once_with(root, "https://hub.example.test/i/share/token", consumer_label="frontend")
        self.assertIn("joined share auth-api", stdout.getvalue())
```

- [ ] **Step 2: Run failing CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.ShareCliTests
```

Expected: FAIL because `share` is not dispatched.

- [ ] **Step 3: Import share functions in `cli.py`**

Add:

```python
from .shares import approve_share, create_share, join_share, list_shares, publish_share, share_status
```

- [ ] **Step 4: Add top-level share dispatch**

In `main`, after `shared-view` handling and before role parsing, add:

```python
if argv and argv[0] == "share":
    active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
    return _share_main(argv[1:], active.memory_root)
```

- [ ] **Step 5: Add `_share_main` parser**

Add near `_shared_view_main`:

```python
def _share_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory share")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("share_id")
    create.add_argument("--title", required=True)
    create.add_argument("--provider", required=True)
    create.add_argument("--hub-url", required=True)
    create.add_argument("--credential-id", required=True)
    create.add_argument("--file")
    create.add_argument("--question")
    create.add_argument("--question-base-url")
    approve = subparsers.add_parser("approve")
    approve.add_argument("share_id")
    publish = subparsers.add_parser("publish")
    publish.add_argument("share_id")
    publish.add_argument("--label")
    publish.add_argument("--expires-at")
    join = subparsers.add_parser("join")
    join.add_argument("invitation_url")
    join.add_argument("--consumer-label")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("share_id", nargs="?")
    subparsers.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "create":
        print(
            create_share(
                memory_root,
                args.share_id,
                title=args.title,
                provider_id=args.provider,
                hub_url=args.hub_url,
                credential_id=args.credential_id,
                file_intent=args.file,
                question_intent=args.question,
                question_base_url=args.question_base_url,
            )
        )
        return 0
    if args.command == "approve":
        print(approve_share(memory_root, args.share_id))
        return 0
    if args.command == "publish":
        print(publish_share(memory_root, args.share_id, label=args.label, expires_at=args.expires_at))
        return 0
    if args.command == "join":
        with MemoryWriteLock(memory_root):
            print(join_share(memory_root, args.invitation_url, consumer_label=args.consumer_label))
        return 0
    if args.command == "status":
        print(share_status(memory_root, args.share_id), end="")
        return 0
    if args.command == "list":
        print(list_shares(memory_root), end="")
        return 0
    raise ValueError(f"unknown share command: {args.command}")
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.ShareCliTests tests.test_cli.CliEntrypointTests
```

Expected: PASS.

- [ ] **Step 7: Commit CLI wiring**

```bash
rtk git add rightmemory/cli.py tests/test_cli.py
rtk git commit -m "feat: add share cli"
```

## Task 6: Add Durable Allowlist And Documentation

**Files:**
- Modify: `install.sh`
- Modify: `rightmemory/session.py`
- Modify: `rightmemory/sync.py`
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/prompt.py`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/shared-views-usage.md`
- Modify: `tests/test_install.py`
- Modify: `tests/test_config.py`
- Test: existing sync/tool tests that mention shared-view durable paths.

- [ ] **Step 1: Update allowlist tests first**

Update expected `.gitignore` strings in `tests/test_install.py` and `tests/test_config.py` to include:

```text
!shares.toml
```

Place it directly after:

```text
!shared_views.toml
```

Add or update an install test so an existing `shares.toml` is included in the initial baseline:

```python
def test_initial_install_baselines_existing_share_registry(self):
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        memory_root = root / "memory"
        skills_target = root / "skills"
        memory_root.mkdir()
        (memory_root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (memory_root / "shares.toml").write_text(
            '[shares.auth-api]\nversion = 1\nrole = "provider"\ntitle = "Auth API"\nstate = "draft"\nparts = ["file"]\n[shares.auth-api.file]\nview_id = "auth-api-files"\nintent = "Expose auth context."\napproved = false\n',
            encoding="utf-8",
        )

        self._install(memory_root, skills_target)
        status = self._git(memory_root, "status", "--short")
        committed_files = self._git(memory_root, "ls-tree", "--name-only", "-r", "HEAD").splitlines()

    self.assertEqual(status, "")
    self.assertIn("shares.toml", committed_files)
```

- [ ] **Step 2: Run failing allowlist tests**

Run:

```bash
rtk python -m unittest tests.test_install tests.test_config.RuntimeTests.test_write_role_creates_memory_lock_and_gitignore
```

Expected: FAIL because runtime and installer do not allowlist `shares.toml`.

- [ ] **Step 3: Update installer and runtime gitignore writers**

In `install.sh`, add `shares.toml` wherever `shared_views.toml` appears in the baseline file list and generated `.gitignore`.

In `rightmemory/session.py`, add:

```text
!shares.toml
```

after `!shared_views.toml`.

- [ ] **Step 4: Update sync-owned paths and tool scopes**

In `rightmemory/sync.py`, add `"shares.toml"` to `MEMORY_SYNC_PATHS` after `"shared_views.toml"`.

In `rightmemory/tools.py`, add:

```python
SHARE_REGISTRY_PATH = "shares.toml"
```

Allow sync-reconciler read/edit/stage access to `shares.toml` wherever `shared_views.toml` is already allowed. Update the user-facing scope string from:

```text
MEMORY.md, MEMORY_*.md, shared_views.toml, shared_views/<id> source files, or insight_logs/*.md
```

to:

```text
MEMORY.md, MEMORY_*.md, shared_views.toml, shares.toml, shared_views/<id> source files, or insight_logs/*.md
```

- [ ] **Step 5: Update prompts and project docs**

In `rightmemory/prompt.py`, include `shares.toml` in memory root file descriptions and sync-reconciler scope text.

In `AGENTS.md`, update memory-root and durable allowlist bullets to mention `shares.toml`.

In `README.md`, add a short `share` section near the shared-view documentation:

```markdown
`rightmemory share` is the normal relationship-level workflow. A share groups one optional file part and one optional question part under one relationship and one bundled invitation. The lower-level `rightmemory shared-view` commands remain available for advanced use.
```

In `docs/shared-views-usage.md`, add a note at the top that the document describes the lower-level primitives and that the normal relationship-level path is `rightmemory share` once implemented.

- [ ] **Step 6: Run docs and allowlist tests**

Run:

```bash
rtk python -m unittest tests.test_install tests.test_config tests.test_sync tests.test_tools
rtk rg -n "shared_views.toml" README.md AGENTS.md docs/shared-views-usage.md rightmemory/prompt.py
```

Expected: unit tests PASS. Review `rtk rg` output and confirm nearby durable memory lists include `shares.toml` when they describe the root-level durable file set.

- [ ] **Step 7: Commit allowlist and docs**

```bash
rtk git add install.sh rightmemory/session.py rightmemory/sync.py rightmemory/tools.py rightmemory/prompt.py AGENTS.md README.md docs/shared-views-usage.md tests/test_install.py tests/test_config.py tests/test_sync.py tests/test_tools.py
rtk git commit -m "docs: document share relationship workflow"
```

## Task 7: Add End-To-End Share Simulation Test

**Files:**
- Modify: `tests/test_shares.py`

- [ ] **Step 1: Add realistic local end-to-end test**

Add or extend imports near the top of `tests/test_shares.py`:

```python
import importlib.util
import zipfile
from io import BytesIO
from urllib.parse import quote

from fastapi.testclient import TestClient

from rightmemory.hub.app import create_hub_app
from rightmemory.hub.store import HubStore
from rightmemory.shared_view_models import save_shared_view_credential


HTTPX2_AVAILABLE = importlib.util.find_spec("httpx2") is not None
```

Add an in-process transport adapter. This is not a fake hub: it calls the real FastAPI app and `HubStore`; it only replaces socket I/O with `TestClient`.

```python
class _InProcessHubClient:
    client: TestClient | None = None

    def __init__(self, base_url: str, token: str | None = None, *, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def publish_package(self, view_id: str, package_root: Path) -> dict[str, object]:
        response = self._client().post(
            f"/api/views/{quote(view_id)}/versions",
            content=_zip_package(package_root),
            headers=self._headers(bearer=True, content_type="application/zip"),
        )
        return self._json(response, expected={201})

    def register_question_view(
        self,
        view_id: str,
        *,
        title: str,
        description: str,
        question_base_url: str,
        question_token: str,
    ) -> dict[str, object]:
        response = self._client().post(
            f"/api/views/{quote(view_id)}/question",
            headers=self._headers(bearer=True),
            json={
                "title": title,
                "description": description,
                "question_base_url": question_base_url,
                "question_token": question_token,
            },
        )
        return self._json(response, expected={201})

    def create_share_invitation(
        self,
        share_id: str,
        *,
        title: str,
        parts: list[dict[str, str]],
        label: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"title": title, "parts": parts}
        if label:
            payload["label"] = label
        if expires_at:
            payload["expires_at"] = expires_at
        response = self._client().post(
            f"/api/shares/{quote(share_id)}/invitations",
            headers=self._headers(bearer=True),
            json=payload,
        )
        return self._json(response, expected={201})

    def get_share_invitation(self, token: str) -> dict[str, object]:
        response = self._client().get(f"/api/share-invitations/{quote(token)}/view")
        return self._json(response)

    def accept_share_invitation(self, token: str, *, consumer_label: str | None = None) -> dict[str, object]:
        payload = {"consumer_label": consumer_label} if consumer_label else {}
        response = self._client().post(f"/api/share-invitations/{quote(token)}/accept", json=payload)
        return self._json(response, expected={201})

    def download_package(self, view_id: str) -> bytes:
        response = self._client().get(
            f"/api/views/{quote(view_id)}/package",
            headers=self._headers(bearer=True),
        )
        if response.status_code != 200:
            raise AssertionError(f"hub returned {response.status_code}: {response.text}")
        return response.content

    def _client(self) -> TestClient:
        if self.client is None:
            raise AssertionError("in-process hub client is not installed")
        return self.client

    def _headers(self, *, bearer: bool = False, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if bearer:
            if not self.token:
                raise AssertionError("hub token is required")
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["content-type"] = content_type
        return headers

    def _json(self, response, *, expected: set[int] | None = None) -> dict[str, object]:
        expected_statuses = expected or {200}
        if response.status_code not in expected_statuses:
            raise AssertionError(f"hub returned {response.status_code}: {response.text}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise AssertionError("hub response must be a JSON object")
        return payload


def _zip_package(package: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package).as_posix())
    return buffer.getvalue()
```

Add:

```python
@unittest.skipUnless(HTTPX2_AVAILABLE, "FastAPI TestClient requires httpx2 in this environment")
class ShareEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.provider = self.root / "provider"
        self.consumer = self.root / "consumer"
        self.hub = self.root / "hub"
        self.provider.mkdir()
        self.consumer.mkdir()
        (self.provider / "MEMORY.md").write_text(
            "# Auth {#auth}\n\n- `token-expiry` Tokens expire after one hour. -> [rel:auth-api]\n",
            encoding="utf-8",
        )
        (self.consumer / "MEMORY.md").write_text("# Frontend\n", encoding="utf-8")
        self.store = HubStore(self.hub)
        self.store.initialize(admin_token="admin-secret", public_base_url="https://hub.example.test")
        self.provider_token = self.store.create_provider_token("alice", label="publish")
        _InProcessHubClient.client = TestClient(create_hub_app(self.hub))
        self.addCleanup(setattr, _InProcessHubClient, "client", None)

    def test_file_question_share_join_status(self):
        save_shared_view_credential(
            self.provider,
            "alice-publish",
            kind="http-publish",
            token=self.provider_token.raw_token,
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        _write_canonical_file_and_question_parts(self.provider)
        create_share(
            self.provider,
            "auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            file_intent="Expose auth API integration context.",
            question_intent="Let frontend agents ask auth questions.",
            question_base_url="https://provider.example.test",
            build_parts=False,
        )
        approve_share(self.provider, "auth-api")

        with (
            patch("rightmemory.shared_view_files.HubClient", _InProcessHubClient),
            patch("rightmemory.shared_view_questions.HubClient", _InProcessHubClient),
            patch("rightmemory.shares.HubClient", _InProcessHubClient),
        ):
            published = publish_share(self.provider, "auth-api", label="frontend")

            self.assertIn("/i/share/", published)
            invitation_url = published.split("invitation_url\t", 1)[1].strip()

            joined = join_share(self.consumer, invitation_url, consumer_label="frontend")

        self.assertIn("joined share auth-api", joined)
        status = share_status(self.consumer, "auth-api")
        self.assertIn("auth-api provider=alice state=joined parts=file,question", status)
        memory_text = (self.consumer / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("{MF#auth-api-files}", memory_text)
        self.assertIn("{MQ#auth-api-ask}", memory_text)
```

- [ ] **Step 2: Run end-to-end share test**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareEndToEndTests
```

Expected: PASS.

- [ ] **Step 3: Run the focused share suite**

Run:

```bash
rtk python -m unittest tests.test_shares tests.test_shared_views tests.test_http_hub tests.test_cli
```

Expected: PASS, with existing skips if optional HTTP client packages are absent.

- [ ] **Step 4: Commit end-to-end coverage**

```bash
rtk git add tests/test_shares.py
rtk git commit -m "test: verify share relationship flow"
```

## Task 8: Final Verification

**Files:**
- No planned source edits unless verification reveals a defect.

- [ ] **Step 1: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 2: Run full unit suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS. Skips for optional packages are acceptable if they match existing project behavior.

- [ ] **Step 3: Run diff whitespace check**

Run:

```bash
rtk git diff --check
```

Expected: PASS with no output.

- [ ] **Step 4: Inspect final history and status**

Run:

```bash
rtk git status --short
rtk git log --oneline -5
```

Expected: clean worktree and recent commits for registry, publish primitives, hub bundled invitations, orchestration, CLI/docs, and tests.

- [ ] **Step 5: Final handoff**

Report:

- the new command flow;
- the commit range;
- the focused and full verification commands;
- any remaining deliberate non-goals from the spec.
