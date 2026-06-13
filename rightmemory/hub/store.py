from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tomllib
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .models import AuditEvent, HubConfig, HubStoredPackage, HubToken, TokenActor
from .packages import DEFAULT_MAX_PACKAGE_BYTES, copy_package_version


HUB_DB_FILE = "hub.db"
HUB_CONFIG_FILE = "hub.toml"
HUB_RUNTIME_DIR = ".runtime"
HUB_STORAGE_DIR = "storage"
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8765"
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class HubStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    @property
    def db_path(self) -> Path:
        return self.root / HUB_DB_FILE

    @property
    def storage_root(self) -> Path:
        return self.root / HUB_STORAGE_DIR

    @property
    def runtime_root(self) -> Path:
        return self.root / HUB_RUNTIME_DIR

    @property
    def config_path(self) -> Path:
        return self.root / HUB_CONFIG_FILE

    def initialize(
        self,
        *,
        admin_token: str | None = None,
        public_base_url: str = DEFAULT_PUBLIC_BASE_URL,
        max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.storage_root / "views").mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            _atomic_write_text(
                self.config_path,
                _render_config(
                    HubConfig(
                        public_base_url=public_base_url,
                        max_package_bytes=max_package_bytes,
                    )
                ),
            )
        with self._connect() as connection:
            self._apply_migrations(connection)
            if admin_token:
                self._ensure_admin_token(connection, admin_token)
            self._append_audit_event(connection, "hub.initialized")

    def load_config(self) -> HubConfig:
        if not self.config_path.exists():
            return HubConfig()
        try:
            with self.config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid hub config: {exc}") from exc
        public_base_url = _optional_string(data.get("public_base_url")) or DEFAULT_PUBLIC_BASE_URL
        limits = data.get("limits", {})
        if limits is None:
            limits = {}
        if not isinstance(limits, dict):
            raise ValueError("hub config [limits] must be a TOML table")
        max_package_bytes = limits.get("max_package_bytes", DEFAULT_MAX_PACKAGE_BYTES)
        if isinstance(max_package_bytes, bool) or not isinstance(max_package_bytes, int) or max_package_bytes < 1:
            raise ValueError("hub config limits.max_package_bytes must be a positive integer")
        return HubConfig(public_base_url=public_base_url, max_package_bytes=max_package_bytes)

    def create_provider_token(self, provider_id: str, *, label: str | None = None) -> HubToken:
        clean_provider_id = _validate_hub_id(provider_id, "provider_id")
        clean_label = _optional_string(label)
        with self._connect() as connection:
            self._apply_migrations(connection)
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO providers(id, label, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = COALESCE(excluded.label, providers.label),
                    updated_at = excluded.updated_at
                """,
                (clean_provider_id, clean_label, now, now),
            )
            token = self._create_token(
                connection,
                action="publish",
                provider_id=clean_provider_id,
                view_id=None,
                label=clean_label,
            )
            self._append_audit_event(
                connection,
                "token.created",
                actor_id=token.token_id,
                provider_id=clean_provider_id,
                details={"action": token.action, "label": clean_label},
            )
            return token

    def verify_token(
        self,
        raw_token: str,
        *,
        action: str,
        provider_id: str | None = None,
        view_id: str | None = None,
    ) -> bool:
        return self._find_token(raw_token, action=action, provider_id=provider_id, view_id=view_id) is not None

    def require_token(
        self,
        raw_token: str,
        *,
        action: str,
        provider_id: str | None = None,
        view_id: str | None = None,
    ) -> TokenActor:
        row = self._find_token(raw_token, action=action, provider_id=provider_id, view_id=view_id)
        if row is None:
            with self._connect() as connection:
                self._apply_migrations(connection)
                self._append_audit_event(
                    connection,
                    "token.rejected",
                    provider_id=provider_id,
                    view_id=view_id,
                    details={"action": action},
                )
            raise PermissionError("invalid hub token")
        return _token_actor_from_row(row)

    def revoke_token(self, token_id: str) -> bool:
        clean_token_id = _validate_hub_id(token_id, "token_id")
        with self._connect() as connection:
            self._apply_migrations(connection)
            row = connection.execute(
                "SELECT provider_id, view_id, action FROM tokens WHERE id = ? AND revoked_at IS NULL",
                (clean_token_id,),
            ).fetchone()
            if row is None:
                return False
            now = _now_iso()
            connection.execute(
                "UPDATE tokens SET revoked_at = ? WHERE id = ?",
                (now, clean_token_id),
            )
            self._append_audit_event(
                connection,
                "token.revoked",
                actor_id=clean_token_id,
                provider_id=row["provider_id"],
                view_id=row["view_id"],
                details={"action": row["action"]},
            )
            return True

    def list_audit_events(self) -> list[AuditEvent]:
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                """
                SELECT id, kind, actor_id, provider_id, view_id, details_json, created_at
                FROM audit_events
                ORDER BY created_at, id
                """
            ).fetchall()
        return [_audit_event_from_row(row) for row in rows]

    def store_package_version(
        self,
        package_root: Path,
        *,
        view_id: str,
        version_id: str | None = None,
        provider_id: str | None = None,
        created_by_token_id: str | None = None,
    ) -> HubStoredPackage:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        clean_version_id = _validate_hub_id(version_id or _new_id("ver"), "version_id")
        clean_provider_id = _validate_hub_id(provider_id, "provider_id") if provider_id else None
        config = self.load_config()
        stored = copy_package_version(
            package_root,
            self.storage_root,
            view_id=clean_view_id,
            version_id=clean_version_id,
            max_package_bytes=config.max_package_bytes,
        )
        relative_storage_path = stored.path.relative_to(self.root).as_posix()
        now = _now_iso()
        try:
            with self._connect() as connection:
                self._apply_migrations(connection)
                if clean_provider_id:
                    connection.execute(
                        """
                        INSERT INTO providers(id, label, created_at, updated_at)
                        VALUES(?, NULL, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                        """,
                        (clean_provider_id, now, now),
                    )
                connection.execute(
                    """
                    INSERT INTO views(
                        id, provider_id, title, ref, description, current_version_id, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        ref = excluded.ref,
                        description = excluded.description,
                        current_version_id = excluded.current_version_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_view_id,
                        clean_provider_id,
                        stored.manifest.title,
                        stored.manifest.ref,
                        stored.manifest.description,
                        clean_version_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO view_versions(
                        id, view_id, package_hash, storage_path, manifest_json, created_at, created_by_token_id
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_version_id,
                        clean_view_id,
                        stored.manifest.package_hash,
                        relative_storage_path,
                        json.dumps(_manifest_json(stored), sort_keys=True),
                        now,
                        created_by_token_id,
                    ),
                )
                self._append_audit_event(
                    connection,
                    "view.version.created",
                    actor_id=created_by_token_id,
                    provider_id=clean_provider_id,
                    view_id=clean_view_id,
                    details={"version_id": clean_version_id, "package_hash": stored.manifest.package_hash},
                )
        except BaseException:
            if stored.path.exists():
                shutil.rmtree(stored.path)
            raise
        return stored

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        if 1 not in applied:
            connection.executescript(_MIGRATION_1)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (1, _now_iso()),
            )
        connection.commit()

    def _ensure_admin_token(self, connection: sqlite3.Connection, raw_token: str) -> None:
        existing = connection.execute(
            "SELECT id FROM tokens WHERE action = 'admin' AND label = 'bootstrap' AND revoked_at IS NULL LIMIT 1"
        ).fetchone()
        if existing is not None:
            return
        token = self._create_token(
            connection,
            action="admin",
            provider_id=None,
            view_id=None,
            label="bootstrap",
            raw_token=raw_token,
        )
        self._append_audit_event(
            connection,
            "token.created",
            actor_id=token.token_id,
            details={"action": "admin", "label": "bootstrap"},
        )

    def _create_token(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        provider_id: str | None,
        view_id: str | None,
        label: str | None,
        raw_token: str | None = None,
    ) -> HubToken:
        clean_action = _validate_action(action)
        clean_provider_id = _validate_hub_id(provider_id, "provider_id") if provider_id else None
        clean_view_id = _validate_hub_id(view_id, "view_id") if view_id else None
        clean_label = _optional_string(label)
        token_id = _new_id("tok")
        raw = raw_token if raw_token is not None else secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        now = _now_iso()
        connection.execute(
            """
            INSERT INTO tokens(id, nonce, token_hash, action, provider_id, view_id, label, created_at, revoked_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                token_id,
                nonce,
                _hash_token(raw, nonce),
                clean_action,
                clean_provider_id,
                clean_view_id,
                clean_label,
                now,
            ),
        )
        return HubToken(
            token_id=token_id,
            raw_token=raw,
            action=clean_action,
            provider_id=clean_provider_id,
            view_id=clean_view_id,
            label=clean_label,
            created_at=now,
        )

    def _find_token(
        self,
        raw_token: str,
        *,
        action: str,
        provider_id: str | None,
        view_id: str | None,
    ) -> sqlite3.Row | None:
        if not isinstance(raw_token, str) or not raw_token:
            return None
        clean_action = _validate_action(action)
        clean_provider_id = _validate_hub_id(provider_id, "provider_id") if provider_id else None
        clean_view_id = _validate_hub_id(view_id, "view_id") if view_id else None
        with self._connect() as connection:
            self._apply_migrations(connection)
            rows = connection.execute(
                """
                SELECT id, nonce, token_hash, action, provider_id, view_id, label
                FROM tokens
                WHERE action = ? AND revoked_at IS NULL
                """,
                (clean_action,),
            ).fetchall()
        for row in rows:
            if clean_provider_id is not None and row["provider_id"] != clean_provider_id:
                continue
            if clean_view_id is not None and row["view_id"] is not None and row["view_id"] != clean_view_id:
                continue
            if hmac.compare_digest(row["token_hash"], _hash_token(raw_token, row["nonce"])):
                return row
        return None

    def _append_audit_event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        *,
        actor_id: str | None = None,
        provider_id: str | None = None,
        view_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(id, kind, actor_id, provider_id, view_id, details_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("aud"),
                kind,
                actor_id,
                provider_id,
                view_id,
                json.dumps(details or {}, sort_keys=True),
                _now_iso(),
            ),
        )


def _render_config(config: HubConfig) -> str:
    return (
        f"public_base_url = {_toml_string(config.public_base_url)}\n"
        "\n"
        "[limits]\n"
        f"max_package_bytes = {config.max_package_bytes}\n"
    )


def _manifest_json(stored: HubStoredPackage) -> dict[str, Any]:
    manifest = stored.manifest
    return {
        "view_id": manifest.view_id,
        "title": manifest.title,
        "ref": manifest.ref,
        "description": manifest.description,
        "maintainer": manifest.maintainer,
        "files": list(manifest.files),
        "size_bytes": manifest.size_bytes,
        "package_hash": manifest.package_hash,
        "version_id": stored.version_id,
    }


def _hash_token(raw_token: str, nonce: str) -> str:
    return sha256(f"{nonce}:{raw_token}".encode("utf-8")).hexdigest()


def _token_actor_from_row(row: sqlite3.Row) -> TokenActor:
    return TokenActor(
        token_id=row["id"],
        action=row["action"],
        provider_id=row["provider_id"],
        view_id=row["view_id"],
        label=row["label"],
    )


def _audit_event_from_row(row: sqlite3.Row) -> AuditEvent:
    try:
        details = json.loads(row["details_json"])
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    return AuditEvent(
        id=row["id"],
        kind=row["kind"],
        actor_id=row["actor_id"],
        provider_id=row["provider_id"],
        view_id=row["view_id"],
        details=details,
        created_at=row["created_at"],
    )


def _validate_action(action: str) -> str:
    return _validate_hub_id(action, "token action")


def _validate_hub_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    clean = value.strip()
    if "/" in clean or "\\" in clean or clean in {".", ".."} or ".." in Path(clean).parts:
        raise ValueError(f"{label} contains path traversal: {value!r}")
    if not _ID_RE.fullmatch(clean):
        raise ValueError(f"{label} contains invalid characters: {value!r}")
    return clean


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional hub string fields must be strings")
    value = value.strip()
    return value or None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16)}"


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


_MIGRATION_1 = """
CREATE TABLE providers(
    id TEXT PRIMARY KEY,
    label TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE views(
    id TEXT PRIMARY KEY,
    provider_id TEXT,
    title TEXT NOT NULL,
    ref TEXT NOT NULL,
    description TEXT,
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE view_versions(
    id TEXT PRIMARY KEY,
    view_id TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by_token_id TEXT,
    FOREIGN KEY(view_id) REFERENCES views(id),
    FOREIGN KEY(created_by_token_id) REFERENCES tokens(id)
);

CREATE TABLE invitations(
    id TEXT PRIMARY KEY,
    view_id TEXT NOT NULL,
    token_id TEXT,
    label TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(view_id) REFERENCES views(id),
    FOREIGN KEY(token_id) REFERENCES tokens(id)
);

CREATE TABLE connections(
    id TEXT PRIMARY KEY,
    invitation_id TEXT,
    view_id TEXT NOT NULL,
    token_id TEXT,
    consumer_label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(invitation_id) REFERENCES invitations(id),
    FOREIGN KEY(view_id) REFERENCES views(id),
    FOREIGN KEY(token_id) REFERENCES tokens(id)
);

CREATE TABLE tokens(
    id TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    action TEXT NOT NULL,
    provider_id TEXT,
    view_id TEXT,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(provider_id) REFERENCES providers(id),
    UNIQUE(nonce, token_hash)
);

CREATE TABLE interactions(
    id TEXT PRIMARY KEY,
    view_id TEXT NOT NULL,
    connection_id TEXT,
    actor_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(view_id) REFERENCES views(id),
    FOREIGN KEY(connection_id) REFERENCES connections(id)
);

CREATE TABLE audit_events(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    actor_id TEXT,
    provider_id TEXT,
    view_id TEXT,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_tokens_action ON tokens(action);
CREATE INDEX idx_tokens_provider_view ON tokens(provider_id, view_id);
CREATE INDEX idx_view_versions_view ON view_versions(view_id, created_at);
CREATE INDEX idx_interactions_view ON interactions(view_id, created_at);
CREATE INDEX idx_audit_events_kind ON audit_events(kind, created_at);
"""
