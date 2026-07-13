from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

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

    def require_token_any(self, raw_token: str, *, candidates: list[dict[str, str | None]]) -> TokenActor:
        clean_candidates: list[dict[str, str | None]] = []
        for candidate in candidates:
            clean_candidates.append(
                {
                    "action": _validate_action(candidate.get("action") or ""),
                    "provider_id": (
                        _validate_hub_id(candidate["provider_id"], "provider_id")
                        if candidate.get("provider_id")
                        else None
                    ),
                    "view_id": (
                        _validate_hub_id(candidate["view_id"], "view_id") if candidate.get("view_id") else None
                    ),
                }
            )
        for candidate in clean_candidates:
            row = self._find_token(
                raw_token,
                action=candidate["action"] or "",
                provider_id=candidate["provider_id"],
                view_id=candidate["view_id"],
            )
            if row is not None:
                return _token_actor_from_row(row)
        with self._connect() as connection:
            self._apply_migrations(connection)
            self._append_audit_event(
                connection,
                "token.rejected",
                details={
                    "actions": sorted({candidate["action"] for candidate in clean_candidates if candidate["action"]}),
                },
            )
        raise PermissionError("invalid hub token")

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

    def get_view(self, view_id: str) -> dict[str, Any] | None:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        with self._connect() as connection:
            self._apply_migrations(connection)
            row = connection.execute(
                """
                SELECT id, provider_id, title, ref, description, current_version_id, created_at, updated_at
                FROM views
                WHERE id = ?
                """,
                (clean_view_id,),
            ).fetchone()
        if row is None:
            return None
        return _view_from_row(row)

    def get_current_view_version(self, view_id: str) -> dict[str, Any] | None:
        clean_view_id = _validate_hub_id(view_id, "view_id")
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
                    v.created_at AS view_created_at,
                    v.updated_at AS view_updated_at,
                    vv.id AS version_id,
                    vv.package_hash AS package_hash,
                    vv.storage_path AS storage_path,
                    vv.manifest_json AS manifest_json,
                    vv.created_at AS version_created_at,
                    vv.created_by_token_id AS created_by_token_id
                FROM views v
                JOIN view_versions vv ON vv.id = v.current_version_id
                WHERE v.id = ?
                """,
                (clean_view_id,),
            ).fetchone()
        if row is None:
            return None
        return _current_view_version_from_row(self.root, row)

    def create_invitation(
        self,
        view_id: str,
        *,
        actor_id: str | None = None,
        label: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        clean_actor_id = _validate_hub_id(actor_id, "actor_id") if actor_id else None
        clean_label = _optional_string(label)
        clean_expires_at = _normalize_optional_datetime(expires_at, "expires_at")
        with self._connect() as connection:
            self._apply_migrations(connection)
            view = connection.execute(
                "SELECT id, provider_id FROM views WHERE id = ?",
                (clean_view_id,),
            ).fetchone()
            if view is None:
                raise KeyError(f"view not found: {clean_view_id}")
            token = self._create_token(
                connection,
                action="invite",
                provider_id=view["provider_id"],
                view_id=clean_view_id,
                label=clean_label,
            )
            invitation_id = _new_id("inv")
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO invitations(
                    id, view_id, token_id, label, expires_at, revoked_at, created_at, accepted_count
                )
                VALUES(?, ?, ?, ?, ?, NULL, ?, 0)
                """,
                (invitation_id, clean_view_id, token.token_id, clean_label, clean_expires_at, now),
            )
            self._append_audit_event(
                connection,
                "invitation.created",
                actor_id=clean_actor_id,
                provider_id=view["provider_id"],
                view_id=clean_view_id,
                details={"invitation_id": invitation_id, "label": clean_label},
            )
        return {
            "invitation_id": invitation_id,
            "token_id": token.token_id,
            "raw_token": token.raw_token,
            "view_id": clean_view_id,
            "label": clean_label,
            "expires_at": clean_expires_at,
            "created_at": now,
        }

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
        clean_actor_id = _validate_hub_id(actor_id, "actor_id") if actor_id else None
        clean_label = _optional_string(label)
        clean_expires_at = _normalize_optional_datetime(expires_at, "expires_at")
        payload = {
            "share_id": clean_share_id,
            "title": clean_title,
            "provider_id": clean_provider_id,
            "parts": clean_parts,
        }
        with self._connect() as connection:
            self._apply_migrations(connection)
            token = self._create_token(
                connection,
                action="share-invite",
                provider_id=clean_provider_id,
                view_id=None,
                label=clean_label,
            )
            invitation_id = _new_id("sinv")
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO share_invitations(
                    id, share_id, provider_id, token_id, title, label, expires_at, revoked_at, created_at, accepted_count, payload_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)
                """,
                (
                    invitation_id,
                    clean_share_id,
                    clean_provider_id,
                    token.token_id,
                    clean_title,
                    clean_label,
                    clean_expires_at,
                    now,
                    json.dumps(payload, sort_keys=True),
                ),
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

    def register_question_view(
        self,
        view_id: str,
        *,
        provider_id: str,
        title: str,
        description: str,
        question_base_url: str,
        question_token: str,
        created_by_token_id: str | None = None,
    ) -> dict[str, Any]:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        clean_provider_id = _validate_hub_id(provider_id, "provider_id")
        clean_title = _required_string(title, "title")
        clean_description = _optional_string(description)
        clean_question_base_url = _required_string(question_base_url, "question_base_url")
        clean_question_token = _required_string(question_token, "question_token")
        clean_created_by = _validate_hub_id(created_by_token_id, "created_by_token_id") if created_by_token_id else None
        version_id = _new_id("ver")
        ref = f"rightmemory://mq/{clean_view_id}"
        manifest = _question_manifest_json(
            view_id=clean_view_id,
            version_id=version_id,
            title=clean_title,
            ref=ref,
            description=clean_description,
            question_base_url=clean_question_base_url,
            question_token=clean_question_token,
        )
        manifest_json = json.dumps(manifest, sort_keys=True)
        package_hash = sha256(manifest_json.encode("utf-8")).hexdigest()
        storage_path = self.storage_root / "views" / clean_view_id / "questions" / version_id
        relative_storage_path = storage_path.relative_to(self.root).as_posix()
        now = _now_iso()
        try:
            storage_path.mkdir(parents=True, exist_ok=False)
            _atomic_write_text(storage_path / "manifest.json", manifest_json + "\n")
            with self._connect() as connection:
                self._apply_migrations(connection)
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
                        provider_id = COALESCE(views.provider_id, excluded.provider_id),
                        title = excluded.title,
                        ref = excluded.ref,
                        description = excluded.description,
                        current_version_id = excluded.current_version_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_view_id,
                        clean_provider_id,
                        clean_title,
                        ref,
                        clean_description,
                        version_id,
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
                        version_id,
                        clean_view_id,
                        package_hash,
                        relative_storage_path,
                        manifest_json,
                        now,
                        clean_created_by,
                    ),
                )
                self._append_audit_event(
                    connection,
                    "question_view.registered",
                    actor_id=clean_created_by,
                    provider_id=clean_provider_id,
                    view_id=clean_view_id,
                    details={"version_id": version_id},
                )
        except BaseException:
            if storage_path.exists():
                shutil.rmtree(storage_path)
            raise
        return {
            "view_id": clean_view_id,
            "provider_id": clean_provider_id,
            "version_id": version_id,
            "current_version_id": version_id,
            "package_hash": package_hash,
            "title": clean_title,
            "ref": ref,
            "description": clean_description,
        }

    def describe_invitation(self, raw_token: str) -> dict[str, Any] | None:
        token_row = self._find_token(raw_token, action="invite", provider_id=None, view_id=None)
        if token_row is None:
            return None
        with self._connect() as connection:
            self._apply_migrations(connection)
            row = connection.execute(
                """
                SELECT
                    i.id AS invitation_id,
                    i.view_id AS view_id,
                    i.label AS invitation_label,
                    i.expires_at AS expires_at,
                    i.revoked_at AS invitation_revoked_at,
                    i.created_at AS invitation_created_at,
                    i.accepted_count AS accepted_count,
                    v.provider_id AS provider_id,
                    v.title AS title,
                    v.ref AS ref,
                    v.description AS description,
                    v.current_version_id AS current_version_id,
                    vv.manifest_json AS manifest_json
                FROM invitations i
                JOIN views v ON v.id = i.view_id
                LEFT JOIN view_versions vv ON vv.id = v.current_version_id
                WHERE i.token_id = ?
                """,
                (token_row["id"],),
            ).fetchone()
        if row is None or row["invitation_revoked_at"] is not None or _is_expired(row["expires_at"]):
            return None
        return _invitation_from_row(row)

    def describe_share_invitation(self, raw_token: str) -> dict[str, Any] | None:
        row = self._share_invitation_row(raw_token)
        if row is None:
            return None
        payload = _json_object(row["payload_json"])
        if not payload:
            return None
        described_parts: list[dict[str, Any]] = []
        for part in payload.get("parts", []):
            if not isinstance(part, dict):
                continue
            described_part = {
                "type": _required_string(part.get("type"), "share part type"),
                "view_id": _required_string(part.get("view_id"), "share part view_id"),
            }
            view = self.get_view(described_part["view_id"])
            if view is not None:
                described_part["title"] = view["title"]
                if view.get("description"):
                    described_part["description"] = view["description"]
            current = self.get_current_view_version(described_part["view_id"])
            if current is not None:
                metadata = _invitation_metadata_from_json(json.dumps(current["manifest"], sort_keys=True))
                question_base_url = _optional_string(metadata.get("question_base_url"))
                if question_base_url:
                    described_part["question_base_url"] = question_base_url
            described_parts.append(described_part)
        return {
            "share_id": _required_string(payload.get("share_id"), "share_id"),
            "title": _required_string(payload.get("title"), "title"),
            "provider_id": _required_string(payload.get("provider_id"), "provider_id"),
            "parts": described_parts,
        }

    def accept_invitation(self, raw_token: str, *, consumer_label: str | None = None) -> dict[str, Any] | None:
        token_row = self._find_token(raw_token, action="invite", provider_id=None, view_id=None)
        if token_row is None:
            return None
        clean_consumer_label = _optional_string(consumer_label)
        with self._connect() as connection:
            self._apply_migrations(connection)
            invitation = connection.execute(
                """
                SELECT
                    i.id AS invitation_id,
                    i.view_id AS view_id,
                    i.expires_at AS expires_at,
                    i.revoked_at AS invitation_revoked_at,
                    v.provider_id AS provider_id,
                    vv.manifest_json AS manifest_json
                FROM invitations i
                JOIN views v ON v.id = i.view_id
                LEFT JOIN view_versions vv ON vv.id = v.current_version_id
                WHERE i.token_id = ?
                """,
                (token_row["id"],),
            ).fetchone()
            if (
                invitation is None
                or invitation["invitation_revoked_at"] is not None
                or _is_expired(invitation["expires_at"])
            ):
                return None
            connection_token = self._create_token(
                connection,
                action="connect",
                provider_id=invitation["provider_id"],
                view_id=invitation["view_id"],
                label=clean_consumer_label,
            )
            connection_id = _new_id("con")
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO connections(
                    id, invitation_id, view_id, token_id, consumer_label, created_at, revoked_at
                )
                VALUES(?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    connection_id,
                    invitation["invitation_id"],
                    invitation["view_id"],
                    connection_token.token_id,
                    clean_consumer_label,
                    now,
                ),
            )
            connection.execute(
                "UPDATE invitations SET accepted_count = accepted_count + 1 WHERE id = ?",
                (invitation["invitation_id"],),
            )
            self._append_audit_event(
                connection,
                "invitation.accepted",
                actor_id=connection_token.token_id,
                provider_id=invitation["provider_id"],
                view_id=invitation["view_id"],
                details={
                    "invitation_id": invitation["invitation_id"],
                    "connection_id": connection_id,
                    "consumer_label": clean_consumer_label,
                },
            )
        return {
            "connection_id": connection_id,
            "token_id": connection_token.token_id,
            "connection_token": connection_token.raw_token,
            "view_id": invitation["view_id"],
            "consumer_label": clean_consumer_label,
            "created_at": now,
            **_accepted_invitation_metadata(invitation["manifest_json"]),
        }

    def accept_share_invitation(self, raw_token: str, *, consumer_label: str | None = None) -> dict[str, Any] | None:
        row = self._share_invitation_row(raw_token)
        if row is None:
            return None
        payload = _json_object(row["payload_json"])
        if not payload:
            return None
        clean_consumer_label = _optional_string(consumer_label)
        accepted_parts: list[dict[str, Any]] = []
        with self._connect() as connection:
            self._apply_migrations(connection)
            for part in payload.get("parts", []):
                if not isinstance(part, dict):
                    continue
                part_type = _required_string(part.get("type"), "share part type")
                view_id = _validate_hub_id(_required_string(part.get("view_id"), "share part view_id"), "view_id")
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
                accepted_part: dict[str, Any] = {
                    "type": part_type,
                    "view_id": view_id,
                    "connection_id": connection_id,
                    "token_id": connection_token.token_id,
                    "connection_token": connection_token.raw_token,
                }
                if part_type == "question":
                    accepted_part.update(_accepted_invitation_metadata(self._current_manifest_json(connection, view_id)))
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
            "share_id": _required_string(payload.get("share_id"), "share_id"),
            "title": _required_string(payload.get("title"), "title"),
            "provider_id": _required_string(payload.get("provider_id"), "provider_id"),
            "consumer_label": clean_consumer_label,
            "parts": accepted_parts,
        }

    def record_interaction(
        self,
        view_id: str,
        *,
        actor: TokenActor,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        payload_json = json.dumps(payload, sort_keys=True)
        with self._connect() as connection:
            self._apply_migrations(connection)
            connection_row = connection.execute(
                """
                SELECT c.id AS connection_id, v.provider_id AS provider_id
                FROM connections c
                JOIN views v ON v.id = c.view_id
                JOIN tokens t ON t.id = c.token_id
                WHERE c.view_id = ?
                    AND c.token_id = ?
                    AND c.revoked_at IS NULL
                    AND t.revoked_at IS NULL
                """,
                (clean_view_id, actor.token_id),
            ).fetchone()
            if connection_row is None:
                raise PermissionError("connection token is not scoped to this view")
            interaction_id = _new_id("int")
            now = _now_iso()
            connection.execute(
                """
                INSERT INTO interactions(id, view_id, connection_id, actor_id, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    clean_view_id,
                    connection_row["connection_id"],
                    actor.token_id,
                    payload_json,
                    now,
                ),
            )
            self._append_audit_event(
                connection,
                "interaction.created",
                actor_id=actor.token_id,
                provider_id=connection_row["provider_id"],
                view_id=clean_view_id,
                details={"interaction_id": interaction_id},
            )
        return {
            "interaction_id": interaction_id,
            "view_id": clean_view_id,
            "connection_id": connection_row["connection_id"],
            "created_at": now,
        }

    def list_provider_inbox(self, provider_id: str) -> list[dict[str, Any]]:
        return self.list_interactions(provider_id=provider_id)

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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
        if 2 not in applied:
            connection.executescript(_MIGRATION_2)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (2, _now_iso()),
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

    def _share_invitation_row(self, raw_token: str) -> sqlite3.Row | None:
        token_row = self._find_token(raw_token, action="share-invite", provider_id=None, view_id=None)
        if token_row is None:
            return None
        with self._connect() as connection:
            self._apply_migrations(connection)
            row = connection.execute(
                """
                SELECT
                    si.id AS id,
                    si.share_id AS share_id,
                    si.provider_id AS provider_id,
                    si.title AS title,
                    si.label AS label,
                    si.expires_at AS expires_at,
                    si.revoked_at AS revoked_at,
                    si.created_at AS created_at,
                    si.accepted_count AS accepted_count,
                    si.payload_json AS payload_json
                FROM share_invitations si
                WHERE si.token_id = ?
                """,
                (token_row["id"],),
            ).fetchone()
        if row is None or row["revoked_at"] is not None or _is_expired(row["expires_at"]):
            return None
        return row

    def _validate_share_parts(self, provider_id: str, parts: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(parts, list) or not parts:
            raise ValueError("parts must be a non-empty list")
        clean_provider_id = _validate_hub_id(provider_id, "provider_id")
        clean_parts: list[dict[str, str]] = []
        with self._connect() as connection:
            self._apply_migrations(connection)
            for raw_part in parts:
                if not isinstance(raw_part, dict):
                    raise ValueError("share part must be an object")
                part_type = _required_string(raw_part.get("type"), "share part type")
                if part_type not in {"file", "question"}:
                    raise ValueError(f"share part type must be file or question: {part_type!r}")
                view_id = _validate_hub_id(_required_string(raw_part.get("view_id"), "share part view_id"), "view_id")
                view = connection.execute(
                    """
                    SELECT
                        v.id AS id,
                        v.provider_id AS provider_id,
                        v.current_version_id AS current_version_id,
                        v.ref AS ref,
                        vv.manifest_json AS manifest_json
                    FROM views v
                    LEFT JOIN view_versions vv ON vv.id = v.current_version_id
                    WHERE v.id = ?
                    """,
                    (view_id,),
                ).fetchone()
                if view is None:
                    raise KeyError(f"view not found: {view_id}")
                if view["provider_id"] != clean_provider_id:
                    raise ValueError(f"share part view belongs to another provider: {view_id}")
                if not isinstance(view["current_version_id"], str) or not view["current_version_id"]:
                    raise KeyError(f"view has no current version: {view_id}")
                metadata_kind = _optional_string(_invitation_metadata_from_json(view["manifest_json"]).get("kind"))
                actual_kind = metadata_kind or _kind_from_ref(view["ref"]) or "file"
                if actual_kind != part_type:
                    raise ValueError(f"share part type does not match view kind for {view_id}")
                clean_parts.append({"type": part_type, "view_id": view_id})
        return clean_parts

    def _current_manifest_json(self, connection: sqlite3.Connection, view_id: str) -> str:
        clean_view_id = _validate_hub_id(view_id, "view_id")
        row = connection.execute(
            """
            SELECT vv.manifest_json AS manifest_json
            FROM views v
            JOIN view_versions vv ON vv.id = v.current_version_id
            WHERE v.id = ?
            """,
            (clean_view_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"view not found: {clean_view_id}")
        return str(row["manifest_json"])


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
        "invitation_metadata": _selected_invitation_metadata(manifest.invitation_metadata),
    }


def _question_manifest_json(
    *,
    view_id: str,
    version_id: str,
    title: str,
    ref: str,
    description: str | None,
    question_base_url: str,
    question_token: str,
) -> dict[str, Any]:
    return {
        "view_id": view_id,
        "title": title,
        "ref": ref,
        "description": description,
        "maintainer": None,
        "files": [],
        "size_bytes": 0,
        "package_hash": "",
        "version_id": version_id,
        "invitation_metadata": {
            "kind": "question",
            "question_base_url": question_base_url,
            "question_token": question_token,
        },
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


def _normalize_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    return max(1, min(limit, 200))


def _normalize_offset(offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError("offset must be an integer")
    return max(0, offset)


def _view_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "view_id": row["id"],
        "provider_id": row["provider_id"],
        "title": row["title"],
        "ref": row["ref"],
        "description": row["description"],
        "current_version_id": row["current_version_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _admin_view_from_row(row: sqlite3.Row) -> dict[str, Any]:
    manifest = _json_object(row["manifest_json"])
    raw_metadata = manifest.get("invitation_metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
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


def _current_view_version_from_row(root: Path, row: sqlite3.Row) -> dict[str, Any]:
    try:
        manifest = json.loads(row["manifest_json"])
    except json.JSONDecodeError:
        manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    return {
        "view_id": row["view_id"],
        "provider_id": row["provider_id"],
        "title": row["title"],
        "ref": row["ref"],
        "description": row["description"],
        "current_version_id": row["current_version_id"],
        "view_created_at": row["view_created_at"],
        "view_updated_at": row["view_updated_at"],
        "version_id": row["version_id"],
        "package_hash": row["package_hash"],
        "storage_path": row["storage_path"],
        "path": root / row["storage_path"],
        "manifest": manifest,
        "version_created_at": row["version_created_at"],
        "created_by_token_id": row["created_by_token_id"],
    }


def _selected_invitation_metadata(data: dict[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for key in ("kind", "question_base_url", "question_url", "ask_base_url", "question_token"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            selected[key] = value.strip()
    return selected


def _invitation_metadata_from_json(value: object) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        manifest = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(manifest, dict):
        return {}
    metadata = manifest.get("invitation_metadata")
    if not isinstance(metadata, dict):
        return {}
    return _selected_invitation_metadata(metadata)


def _accepted_invitation_metadata(value: object) -> dict[str, str]:
    metadata = _invitation_metadata_from_json(value)
    token = _optional_string(metadata.get("question_token"))
    if token:
        return {"question_token": token}
    return {}


def _kind_from_ref(ref: str | None) -> str | None:
    if isinstance(ref, str) and ref.startswith("rightmemory://mq/"):
        return "question"
    if isinstance(ref, str) and ref.startswith("rightmemory://mf/"):
        return "file"
    return None


def _invitation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    metadata = _invitation_metadata_from_json(row["manifest_json"])
    invitation = {
        "invitation_id": row["invitation_id"],
        "view_id": row["view_id"],
        "provider_id": row["provider_id"],
        "title": row["title"],
        "ref": row["ref"],
        "description": row["description"],
        "current_version_id": row["current_version_id"],
        "label": row["invitation_label"],
        "expires_at": row["expires_at"],
        "accepted_count": row["accepted_count"],
        "created_at": row["invitation_created_at"],
    }
    kind = _optional_string(metadata.get("kind")) or _kind_from_ref(row["ref"])
    if kind:
        invitation["kind"] = kind
    for key in ("question_base_url", "question_url", "ask_base_url"):
        value = _optional_string(metadata.get(key))
        if value:
            invitation[key] = value
    return invitation


def _interaction_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "interaction_id": row["interaction_id"],
        "provider_id": row["provider_id"],
        "view_id": row["view_id"],
        "connection_id": row["connection_id"],
        "actor_id": row["actor_id"],
        "payload": payload,
        "created_at": row["created_at"],
    }


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        expires = _parse_datetime(expires_at)
    except ValueError:
        return True
    return expires <= datetime.now(UTC)


def _normalize_optional_datetime(value: object, label: str) -> str | None:
    clean = _optional_string(value)
    if clean is None:
        return None
    try:
        parsed = _parse_datetime(clean)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO datetime") from exc
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse_datetime(value: str) -> datetime:
    clean = value.strip()
    if clean.endswith("Z"):
        clean = f"{clean[:-1]}+00:00"
    parsed = datetime.fromisoformat(clean)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _required_string(value: object, label: str) -> str:
    clean = _optional_string(value)
    if clean is None:
        raise ValueError(f"{label} must be a non-empty string")
    return clean


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

_MIGRATION_2 = """
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
"""
