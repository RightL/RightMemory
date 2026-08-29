from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ..session import _ensure_runtime_gitignore
from .models import (
    CONVERSATION_LIFECYCLES,
    CONVERSATION_STATUSES,
    DEFAULT_LOCAL_PROJECT_ID,
    HOST_KINDS,
    LOCAL_HOST_ID,
    PENDING_REQUEST_STATES,
    SCHEMA_VERSION,
    ConversationError,
    ConversationEvent,
    ConversationHost,
    ConversationProject,
    PendingServerRequest,
    PursuitConversation,
    PursuitConversationDefault,
)


DATABASE_RELATIVE_PATH = Path(".runtime") / "web" / "conversations.sqlite3"
MAX_EVENT_PAYLOAD_BYTES = 256 * 1024
MAX_PENDING_PAYLOAD_BYTES = 128 * 1024
MAX_CAPABILITIES_BYTES = 64 * 1024
MAX_EVENTS_PER_READ = 1000

_ID_RE = re.compile(r"^[^\x00\r\n]{1,512}$")
_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,255}$")
_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNSET = object()

_HOST_COLUMNS = """
    host_id, kind, display_name, ssh_alias, codex_command_override,
    platform_hint, app_server_version, codex_version, capabilities_json,
    last_seen_at, last_error, created_at, updated_at, enabled
"""
_PROJECT_COLUMNS = "project_id, host_id, label, cwd, last_used_at, created_at, updated_at"
_CONVERSATION_COLUMNS = """
    conversation_id, pursuit_id, pursuit_title_snapshot, host_id, project_id,
    provider, thread_id, thread_title, lifecycle, status, active_turn_id,
    created_at, updated_at, last_activity_at
"""
_EVENT_COLUMNS = "event_id, conversation_id, turn_id, kind, payload_json, created_at"
_DEFAULT_COLUMNS = "pursuit_id, host_id, project_id, last_used_at"
_PENDING_COLUMNS = """
    request_key, host_id, connection_epoch, rpc_id_json, conversation_id,
    thread_id, method, payload_json, state, created_at, resolved_at
"""


class ConversationStore:
    """Root-local operational state for user-owned Codex conversations.

    The store never reads or edits the Pursuit graph. A Pursuit identifier here
    is an operational attachment plus a title snapshot, not a graph edge.
    """

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / DATABASE_RELATIVE_PATH

    def initialize(self) -> dict[str, Any]:
        """Create the version-one database and stable local records."""
        if not self.root.is_dir():
            raise ConversationError("invalid_root", "The active memory root must be an existing directory.", 422)
        with self._connect() as connection:
            recovered_stale_requests = _stale_pending_rows(connection, _now_iso())
            local_host = self._get_host_row(connection, LOCAL_HOST_ID)
            local_project = self._get_project_row(connection, DEFAULT_LOCAL_PROJECT_ID)
        return {
            "schema_version": SCHEMA_VERSION,
            "database_path": str(self.db_path),
            "recovered_stale_request_count": recovered_stale_requests,
            "local_host": _host_dict(local_host),
            "default_local_project": _project_dict(local_project),
        }

    # Hosts -----------------------------------------------------------------

    def upsert_host(
        self,
        *,
        kind: str,
        display_name: str,
        host_id: str | None = None,
        ssh_alias: str | None = None,
        codex_command_override: str | None = None,
        platform_hint: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        clean_kind = _choice(kind, "host kind", HOST_KINDS)
        clean_host_id = _id(host_id or (LOCAL_HOST_ID if clean_kind == "local" else uuid4().hex), "host_id")
        clean_name = _text(display_name, "display_name", 200)
        clean_alias = _optional_text(ssh_alias, "ssh_alias", 255)
        clean_command = _optional_text(codex_command_override, "codex_command_override", 2048)
        clean_platform = _optional_text(platform_hint, "platform_hint", 100)
        clean_enabled = _boolean(enabled, "enabled")
        if clean_kind == "local":
            if clean_host_id != LOCAL_HOST_ID:
                raise ConversationError("invalid_host", "The local host uses the stable 'local' identity.", 422)
            if clean_alias is not None:
                raise ConversationError("invalid_host", "A local host cannot have an SSH alias.", 422)
        elif clean_alias is None:
            raise ConversationError("invalid_host", "An SSH host requires an SSH config alias.", 422)
        else:
            _ssh_alias(clean_alias)

        now = _now_iso()
        with self._connect() as connection:
            existing = self._get_host_row(connection, clean_host_id, required=False)
            if existing is not None and existing["kind"] != clean_kind:
                raise ConversationError("host_conflict", "A host identity cannot change kind.", 409)
            connection.execute(
                """
                INSERT INTO conversation_hosts(
                    host_id, kind, display_name, ssh_alias, codex_command_override,
                    platform_hint, capabilities_json, created_at, updated_at, enabled
                ) VALUES(?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    ssh_alias = excluded.ssh_alias,
                    codex_command_override = excluded.codex_command_override,
                    platform_hint = excluded.platform_hint,
                    updated_at = excluded.updated_at,
                    enabled = excluded.enabled
                """,
                (
                    clean_host_id,
                    clean_kind,
                    clean_name,
                    clean_alias,
                    clean_command,
                    clean_platform,
                    now,
                    now,
                    int(clean_enabled),
                ),
            )
            row = self._get_host_row(connection, clean_host_id)
        return _host_dict(row)

    def get_host(self, host_id: str) -> dict[str, Any] | None:
        clean_host_id = _id(host_id, "host_id")
        with self._connect() as connection:
            row = self._get_host_row(connection, clean_host_id, required=False)
        return _host_dict(row) if row is not None else None

    def list_hosts(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        clean_enabled_only = _boolean(enabled_only, "enabled_only")
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_HOST_COLUMNS} FROM conversation_hosts "
                + ("WHERE enabled = 1 " if clean_enabled_only else "")
                + "ORDER BY kind, display_name COLLATE NOCASE, host_id"
            ).fetchall()
        return [_host_dict(row) for row in rows]

    def update_host_runtime(
        self,
        host_id: str,
        *,
        app_server_version: str | None | object = _UNSET,
        codex_version: str | None | object = _UNSET,
        capabilities: dict[str, Any] | object = _UNSET,
        last_seen_at: str | None | object = _UNSET,
        last_error: str | None | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> dict[str, Any]:
        clean_host_id = _id(host_id, "host_id")
        updates: dict[str, Any] = {}
        if app_server_version is not _UNSET:
            updates["app_server_version"] = _optional_text(app_server_version, "app_server_version", 100)
        if codex_version is not _UNSET:
            updates["codex_version"] = _optional_text(codex_version, "codex_version", 100)
        if capabilities is not _UNSET:
            updates["capabilities_json"] = _json_object(capabilities, "capabilities", MAX_CAPABILITIES_BYTES)
        if last_seen_at is not _UNSET:
            updates["last_seen_at"] = _optional_timestamp(last_seen_at, "last_seen_at")
        if last_error is not _UNSET:
            updates["last_error"] = _optional_log_text(last_error, "last_error", 2000)
        if enabled is not _UNSET:
            updates["enabled"] = int(_boolean(enabled, "enabled"))
        updates["updated_at"] = _now_iso()
        with self._connect() as connection:
            self._get_host_row(connection, clean_host_id)
            _update_row(connection, "conversation_hosts", "host_id", clean_host_id, updates)
            row = self._get_host_row(connection, clean_host_id)
        return _host_dict(row)

    # Projects ---------------------------------------------------------------

    def create_project(
        self,
        *,
        host_id: str,
        cwd: str,
        label: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        clean_host_id = _id(host_id, "host_id")
        clean_project_id = _id(project_id or uuid4().hex, "project_id")
        clean_cwd = _cwd(cwd)
        clean_label = _text(label, "label", 300)
        now = _now_iso()
        with self._connect() as connection:
            self._get_host_row(connection, clean_host_id)
            conflict = connection.execute(
                "SELECT project_id FROM conversation_projects WHERE host_id = ? AND cwd = ?",
                (clean_host_id, clean_cwd),
            ).fetchone()
            if conflict is not None:
                raise ConversationError("project_conflict", "That host and working directory are already registered.", 409)
            if self._get_project_row(connection, clean_project_id, required=False) is not None:
                raise ConversationError("project_conflict", "That project identity is already registered.", 409)
            connection.execute(
                """
                INSERT INTO conversation_projects(
                    project_id, host_id, label, cwd, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (clean_project_id, clean_host_id, clean_label, clean_cwd, now, now),
            )
            row = self._get_project_row(connection, clean_project_id)
        return _project_dict(row)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        clean_project_id = _id(project_id, "project_id")
        with self._connect() as connection:
            row = self._get_project_row(connection, clean_project_id, required=False)
        return _project_dict(row) if row is not None else None

    def list_projects(self, *, host_id: str | None = None) -> list[dict[str, Any]]:
        clean_host_id = _id(host_id, "host_id") if host_id is not None else None
        with self._connect() as connection:
            if clean_host_id is None:
                rows = connection.execute(
                    f"SELECT {_PROJECT_COLUMNS} FROM conversation_projects "
                    "ORDER BY COALESCE(last_used_at, updated_at) DESC, label COLLATE NOCASE, project_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_PROJECT_COLUMNS} FROM conversation_projects WHERE host_id = ? "
                    "ORDER BY COALESCE(last_used_at, updated_at) DESC, label COLLATE NOCASE, project_id",
                    (clean_host_id,),
                ).fetchall()
        return [_project_dict(row) for row in rows]

    # Conversations and Pursuit defaults ------------------------------------

    def create_conversation(
        self,
        *,
        pursuit_id: str,
        host_id: str,
        project_id: str,
        thread_id: str,
        pursuit_title_snapshot: str | None = None,
        thread_title: str | None = None,
        conversation_id: str | None = None,
        lifecycle: str = "active",
        status: str = "idle",
        active_turn_id: str | None = None,
    ) -> dict[str, Any]:
        # A row represents a provider-confirmed thread. Callers intentionally
        # persist only after thread/start returns a stable, non-empty thread id.
        clean_conversation_id = _id(conversation_id or uuid4().hex, "conversation_id")
        clean_pursuit_id = _id(pursuit_id, "pursuit_id")
        clean_host_id = _id(host_id, "host_id")
        clean_project_id = _id(project_id, "project_id")
        clean_thread_id = _id(thread_id, "thread_id")
        clean_pursuit_title = _optional_text(pursuit_title_snapshot, "pursuit_title_snapshot", 500)
        clean_thread_title = _optional_text(thread_title, "thread_title", 500)
        clean_lifecycle = _choice(lifecycle, "lifecycle", CONVERSATION_LIFECYCLES)
        clean_status = _choice(status, "status", CONVERSATION_STATUSES)
        clean_turn_id = _optional_id(active_turn_id, "active_turn_id")
        now = _now_iso()
        with self._connect() as connection:
            self._require_host_project(connection, clean_host_id, clean_project_id)
            if self._get_conversation_row(connection, clean_conversation_id, required=False) is not None:
                raise ConversationError("conversation_conflict", "That conversation identity is already registered.", 409)
            conflict = connection.execute(
                "SELECT conversation_id FROM pursuit_conversations WHERE host_id = ? AND thread_id = ?",
                (clean_host_id, clean_thread_id),
            ).fetchone()
            if conflict is not None:
                raise ConversationError(
                    "thread_already_attached",
                    "That provider thread is already attached on this host.",
                    409,
                )
            connection.execute(
                """
                INSERT INTO pursuit_conversations(
                    conversation_id, pursuit_id, pursuit_title_snapshot, host_id,
                    project_id, provider, thread_id, thread_title, lifecycle,
                    status, active_turn_id, created_at, updated_at, last_activity_at
                ) VALUES(?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_conversation_id,
                    clean_pursuit_id,
                    clean_pursuit_title,
                    clean_host_id,
                    clean_project_id,
                    clean_thread_id,
                    clean_thread_title,
                    clean_lifecycle,
                    clean_status,
                    clean_turn_id,
                    now,
                    now,
                    now,
                ),
            )
            self._set_pursuit_default(connection, clean_pursuit_id, clean_host_id, clean_project_id, now)
            row = self._get_conversation_row(connection, clean_conversation_id)
        return _conversation_dict(row)

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            row = self._get_conversation_row(connection, clean_id, required=False)
        return _conversation_dict(row) if row is not None else None

    def find_conversation(self, host_id: str, thread_id: str) -> dict[str, Any] | None:
        clean_host_id = _id(host_id, "host_id")
        clean_thread_id = _id(thread_id, "thread_id")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM pursuit_conversations "
                "WHERE host_id = ? AND thread_id = ?",
                (clean_host_id, clean_thread_id),
            ).fetchone()
        return _conversation_dict(row) if row is not None else None

    def list_conversations(
        self,
        *,
        pursuit_id: str | None = None,
        host_id: str | None = None,
        lifecycle: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if pursuit_id is not None:
            clauses.append("pursuit_id = ?")
            values.append(_id(pursuit_id, "pursuit_id"))
        if host_id is not None:
            clauses.append("host_id = ?")
            values.append(_id(host_id, "host_id"))
        if lifecycle is not None:
            clauses.append("lifecycle = ?")
            values.append(_choice(lifecycle, "lifecycle", CONVERSATION_LIFECYCLES))
        if status is not None:
            clauses.append("status = ?")
            values.append(_choice(status, "status", CONVERSATION_STATUSES))
        clean_limit = _limit(limit, maximum=1000)
        clean_offset = _offset(offset)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((clean_limit, clean_offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_CONVERSATION_COLUMNS} FROM pursuit_conversations {where} "
                "ORDER BY last_activity_at DESC, conversation_id LIMIT ? OFFSET ?",
                values,
            ).fetchall()
        return [_conversation_dict(row) for row in rows]

    def update_conversation(
        self,
        conversation_id: str,
        *,
        pursuit_id: str | object = _UNSET,
        pursuit_title_snapshot: str | None | object = _UNSET,
        thread_title: str | None | object = _UNSET,
        lifecycle: str | object = _UNSET,
        status: str | object = _UNSET,
        active_turn_id: str | None | object = _UNSET,
        touch_activity: bool = False,
    ) -> dict[str, Any]:
        clean_id = _id(conversation_id, "conversation_id")
        updates: dict[str, Any] = {}
        if pursuit_id is not _UNSET:
            updates["pursuit_id"] = _id(pursuit_id, "pursuit_id")
        if pursuit_title_snapshot is not _UNSET:
            updates["pursuit_title_snapshot"] = _optional_text(
                pursuit_title_snapshot, "pursuit_title_snapshot", 500
            )
        if thread_title is not _UNSET:
            updates["thread_title"] = _optional_text(thread_title, "thread_title", 500)
        if lifecycle is not _UNSET:
            updates["lifecycle"] = _choice(lifecycle, "lifecycle", CONVERSATION_LIFECYCLES)
        if status is not _UNSET:
            updates["status"] = _choice(status, "status", CONVERSATION_STATUSES)
        if active_turn_id is not _UNSET:
            updates["active_turn_id"] = _optional_id(active_turn_id, "active_turn_id")
        clean_touch = _boolean(touch_activity, "touch_activity")
        now = _now_iso()
        updates["updated_at"] = now
        if clean_touch:
            updates["last_activity_at"] = now
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            _update_row(connection, "pursuit_conversations", "conversation_id", clean_id, updates)
            if "pursuit_id" in updates:
                self._set_pursuit_default(
                    connection,
                    updates["pursuit_id"],
                    current["host_id"],
                    current["project_id"],
                    now,
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def has_turn_evidence(self, conversation_id: str) -> bool:
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["active_turn_id"] is not None:
                return True
            row = connection.execute(
                """
                SELECT 1
                FROM conversation_events
                WHERE conversation_id = ?
                  AND (turn_id IS NOT NULL OR kind GLOB 'turn.*')
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
        return row is not None

    def rebind_unstarted_thread(
        self,
        conversation_id: str,
        *,
        expected_thread_id: str,
        replacement_thread_id: str,
        thread_title: str | None = None,
    ) -> dict[str, Any]:
        """Replace a provider thread only before any turn was accepted."""
        clean_id = _id(conversation_id, "conversation_id")
        clean_expected = _id(expected_thread_id, "expected_thread_id")
        clean_replacement = _id(replacement_thread_id, "replacement_thread_id")
        clean_title = _optional_text(thread_title, "thread_title", 500)
        now = _now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_conversation_row(connection, clean_id)
            if current["thread_id"] != clean_expected:
                raise ConversationError(
                    "thread_changed",
                    "The conversation provider thread changed before it could be replaced.",
                    409,
                )
            turn_evidence = connection.execute(
                """
                SELECT 1
                FROM conversation_events
                WHERE conversation_id = ?
                  AND (turn_id IS NOT NULL OR kind GLOB 'turn.*')
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
            if current["active_turn_id"] is not None or turn_evidence is not None:
                raise ConversationError(
                    "conversation_has_turn_history",
                    "A conversation with accepted turn history cannot change provider threads.",
                    409,
                )
            conflict = connection.execute(
                """
                SELECT conversation_id
                FROM pursuit_conversations
                WHERE host_id = ? AND thread_id = ? AND conversation_id != ?
                """,
                (current["host_id"], clean_replacement, clean_id),
            ).fetchone()
            if conflict is not None:
                raise ConversationError(
                    "thread_already_attached",
                    "That provider thread is already attached on this host.",
                    409,
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET thread_id = ?, thread_title = ?, updated_at = ?, last_activity_at = ?
                WHERE conversation_id = ?
                  AND thread_id = ?
                  AND active_turn_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM conversation_events
                      WHERE conversation_id = ?
                        AND (turn_id IS NOT NULL OR kind GLOB 'turn.*')
                  )
                """,
                (
                    clean_replacement,
                    clean_title,
                    now,
                    now,
                    clean_id,
                    clean_expected,
                    clean_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationError(
                    "thread_changed",
                    "The conversation provider thread changed before it could be replaced.",
                    409,
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def archive_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.update_conversation(
            conversation_id,
            lifecycle="archived",
            status="idle",
            active_turn_id=None,
            touch_activity=True,
        )

    def mark_orphaned_conversations_unknown(self) -> int:
        """Fence live-status caches that cannot survive a runtime restart."""
        now = _now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET status = 'unknown', updated_at = ?, last_activity_at = ?
                WHERE lifecycle = 'active'
                  AND status IN ('starting', 'running', 'waiting_approval', 'waiting_input')
                """,
                (now, now),
            )
            changed = cursor.rowcount
        return int(changed)

    def set_pursuit_default(self, pursuit_id: str, host_id: str, project_id: str) -> dict[str, Any]:
        clean_pursuit_id = _id(pursuit_id, "pursuit_id")
        clean_host_id = _id(host_id, "host_id")
        clean_project_id = _id(project_id, "project_id")
        now = _now_iso()
        with self._connect() as connection:
            self._require_host_project(connection, clean_host_id, clean_project_id)
            self._set_pursuit_default(connection, clean_pursuit_id, clean_host_id, clean_project_id, now)
            row = connection.execute(
                f"SELECT {_DEFAULT_COLUMNS} FROM pursuit_conversation_preferences WHERE pursuit_id = ?",
                (clean_pursuit_id,),
            ).fetchone()
        return _default_dict(row)

    def get_pursuit_default(self, pursuit_id: str) -> dict[str, Any] | None:
        clean_pursuit_id = _id(pursuit_id, "pursuit_id")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_DEFAULT_COLUMNS} FROM pursuit_conversation_preferences WHERE pursuit_id = ?",
                (clean_pursuit_id,),
            ).fetchone()
        return _default_dict(row) if row is not None else None

    # Durable browser event cursor ------------------------------------------

    def append_event(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        conversation_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        clean_kind = _event_kind(kind)
        payload_json = _json_object(payload, "event payload", MAX_EVENT_PAYLOAD_BYTES)
        clean_conversation_id = _optional_id(conversation_id, "conversation_id")
        clean_turn_id = _optional_id(turn_id, "turn_id")
        now = _now_iso()
        with self._connect() as connection:
            if clean_conversation_id is not None:
                self._get_conversation_row(connection, clean_conversation_id)
            cursor = connection.execute(
                """
                INSERT INTO conversation_events(conversation_id, turn_id, kind, payload_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (clean_conversation_id, clean_turn_id, clean_kind, payload_json, now),
            )
            event_id = int(cursor.lastrowid)
            if clean_conversation_id is not None:
                connection.execute(
                    """
                    UPDATE pursuit_conversations
                    SET updated_at = ?, last_activity_at = ?
                    WHERE conversation_id = ?
                    """,
                    (now, now, clean_conversation_id),
                )
            row = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM conversation_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return _event_dict(row)

    def read_events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 200,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clean_cursor = _cursor(after_event_id)
        clean_limit = _limit(limit, maximum=MAX_EVENTS_PER_READ)
        clean_conversation_id = _optional_id(conversation_id, "conversation_id")
        with self._connect() as connection:
            if clean_conversation_id is None:
                rows = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM conversation_events WHERE event_id > ? "
                    "ORDER BY event_id LIMIT ?",
                    (clean_cursor, clean_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM conversation_events "
                    "WHERE event_id > ? AND conversation_id = ? ORDER BY event_id LIMIT ?",
                    (clean_cursor, clean_conversation_id, clean_limit),
                ).fetchall()
        return [_event_dict(row) for row in rows]

    def list_events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 200,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.read_events(
            after_event_id=after_event_id,
            limit=limit,
            conversation_id=conversation_id,
        )

    def latest_events(self, conversation_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return the newest bounded window in chronological display order."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_limit = _limit(limit, maximum=MAX_EVENTS_PER_READ)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM conversation_events "
                "WHERE conversation_id = ? ORDER BY event_id DESC LIMIT ?",
                (clean_conversation_id, clean_limit),
            ).fetchall()
        rows.reverse()
        return [_event_dict(row) for row in rows]

    def latest_event_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(event_id), 0) FROM conversation_events").fetchone()
        return int(row[0])

    # Server-initiated requests ---------------------------------------------

    def create_pending_request(
        self,
        *,
        host_id: str,
        connection_epoch: str | int,
        rpc_id: str | int,
        method: str,
        payload: dict[str, Any],
        conversation_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        clean_host_id = _id(host_id, "host_id")
        clean_epoch = _epoch(connection_epoch)
        rpc_id_json = _rpc_id_json(rpc_id)
        clean_method = _method(method)
        clean_turn_id = _optional_id(turn_id, "turn_id")
        normalized_payload = dict(payload)
        if clean_turn_id is not None:
            supplied_turn_id = normalized_payload.get("turnId")
            if supplied_turn_id is not None and supplied_turn_id != clean_turn_id:
                raise ConversationError(
                    "invalid_request_binding",
                    "The pending request payload names a different turn.",
                    422,
                )
            # Persist the normalized identity even when Codex supplied only
            # the nested {turn: {id}} form.
            normalized_payload["turnId"] = clean_turn_id
        payload_json = _json_object(
            normalized_payload, "pending request payload", MAX_PENDING_PAYLOAD_BYTES
        )
        clean_conversation_id = _optional_id(conversation_id, "conversation_id")
        clean_thread_id = _optional_id(thread_id, "thread_id")
        request_key = _request_key(clean_host_id, clean_epoch, rpc_id_json)
        now = _now_iso()
        with self._connect() as connection:
            self._get_host_row(connection, clean_host_id)
            if clean_conversation_id is not None:
                conversation = self._get_conversation_row(connection, clean_conversation_id)
                if conversation["host_id"] != clean_host_id:
                    raise ConversationError(
                        "invalid_request_binding",
                        "The pending request host does not match its conversation.",
                        422,
                    )
                if clean_thread_id is None:
                    clean_thread_id = conversation["thread_id"]
                elif clean_thread_id != conversation["thread_id"]:
                    raise ConversationError(
                        "invalid_request_binding",
                        "The pending request thread does not match its conversation.",
                        422,
                    )
            existing = self._get_pending_row(connection, clean_host_id, clean_epoch, rpc_id_json)
            if existing is not None:
                _raise_pending_duplicate(existing["state"])
            try:
                connection.execute(
                    """
                    INSERT INTO pending_server_requests(
                        request_key, host_id, connection_epoch, rpc_id_json,
                        conversation_id, thread_id, method, payload_json, state,
                        created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        request_key,
                        clean_host_id,
                        clean_epoch,
                        rpc_id_json,
                        clean_conversation_id,
                        clean_thread_id,
                        clean_method,
                        payload_json,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._get_pending_row(connection, clean_host_id, clean_epoch, rpc_id_json)
                if existing is not None:
                    _raise_pending_duplicate(existing["state"])
                raise
            row = self._get_pending_row(connection, clean_host_id, clean_epoch, rpc_id_json)
        return _pending_dict(row)

    def get_pending_request(
        self,
        host_id: str,
        connection_epoch: str | int,
        rpc_id: str | int,
    ) -> dict[str, Any] | None:
        clean_host_id = _id(host_id, "host_id")
        clean_epoch = _epoch(connection_epoch)
        rpc_id_json = _rpc_id_json(rpc_id)
        with self._connect() as connection:
            row = self._get_pending_row(connection, clean_host_id, clean_epoch, rpc_id_json)
        return _pending_dict(row) if row is not None else None

    def get_pending_request_by_key(self, request_key: str) -> dict[str, Any] | None:
        clean_key = _pending_request_key(request_key)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests WHERE request_key = ?",
                (clean_key,),
            ).fetchone()
        return _pending_dict(row) if row is not None else None

    def resolve_pending_request(
        self,
        host_id: str,
        connection_epoch: str | int,
        rpc_id: str | int,
    ) -> dict[str, Any]:
        clean_host_id = _id(host_id, "host_id")
        clean_epoch = _epoch(connection_epoch)
        rpc_id_json = _rpc_id_json(rpc_id)
        now = _now_iso()
        with self._connect() as connection:
            row = self._get_pending_row(connection, clean_host_id, clean_epoch, rpc_id_json)
            if row is None:
                older = connection.execute(
                    """
                    SELECT state FROM pending_server_requests
                    WHERE host_id = ? AND rpc_id_json = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (clean_host_id, rpc_id_json),
                ).fetchone()
                if older is not None:
                    raise ConversationError(
                        "stale_request",
                        "That request belongs to a different connection epoch.",
                        409,
                    )
                raise ConversationError("request_not_found", "The pending server request was not found.", 404)
            resolved = _resolve_pending_row(connection, row, now)
        return _pending_dict(resolved)

    def resolve_pending_request_by_key(
        self,
        request_key: str,
        *,
        host_id: str,
        connection_epoch: str | int,
    ) -> dict[str, Any]:
        """Claim one response while fencing it to the current live connection."""
        clean_key = _pending_request_key(request_key)
        clean_host_id = _id(host_id, "host_id")
        clean_epoch = _epoch(connection_epoch)
        now = _now_iso()
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests WHERE request_key = ?",
                (clean_key,),
            ).fetchone()
            if row is None:
                raise ConversationError("request_not_found", "The pending server request was not found.", 404)
            if row["host_id"] != clean_host_id or row["connection_epoch"] != clean_epoch:
                raise ConversationError(
                    "stale_request",
                    "That request does not belong to the current host connection epoch.",
                    409,
                )
            resolved = _resolve_pending_row(connection, row, now)
        return _pending_dict(resolved)

    def mark_pending_requests_stale(self, host_id: str, connection_epoch: str | int) -> int:
        clean_host_id = _id(host_id, "host_id")
        clean_epoch = _epoch(connection_epoch)
        now = _now_iso()
        with self._connect() as connection:
            self._get_host_row(connection, clean_host_id)
            cursor = connection.execute(
                """
                UPDATE pending_server_requests
                SET state = 'stale', resolved_at = ?, payload_json = '{}'
                WHERE host_id = ? AND connection_epoch = ? AND state = 'pending'
                """,
                (now, clean_host_id, clean_epoch),
            )
            changed = cursor.rowcount
        return int(changed)

    def mark_conversation_requests_stale(
        self,
        conversation_id: str,
        *,
        turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fence pending requests for one conversation or one of its turns."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_turn_id = _optional_id(turn_id, "turn_id")
        now = _now_iso()
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_conversation_id)
            rows = connection.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests "
                "WHERE conversation_id = ? AND state = 'pending' "
                "ORDER BY created_at, request_key",
                (clean_conversation_id,),
            ).fetchall()
            selected: list[sqlite3.Row] = []
            for row in rows:
                if clean_turn_id is not None:
                    payload = _decode_json_object(row["payload_json"])
                    if payload.get("turnId") != clean_turn_id:
                        continue
                selected.append(row)
            changed: list[sqlite3.Row] = []
            for row in selected:
                cursor = connection.execute(
                    """
                    UPDATE pending_server_requests
                    SET state = 'stale', resolved_at = ?, payload_json = '{}'
                    WHERE request_key = ? AND state = 'pending'
                    """,
                    (now, row["request_key"]),
                )
                if cursor.rowcount == 1:
                    changed.append(row)
        return [_pending_dict(row) for row in changed]

    def list_pending_requests(
        self,
        *,
        host_id: str | None = None,
        conversation_id: str | None = None,
        state: str | None = "pending",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if host_id is not None:
            clauses.append("host_id = ?")
            values.append(_id(host_id, "host_id"))
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            values.append(_id(conversation_id, "conversation_id"))
        if state is not None:
            clauses.append("state = ?")
            values.append(_choice(state, "pending request state", PENDING_REQUEST_STATES))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests {where} "
                "ORDER BY created_at, request_key",
                values,
            ).fetchall()
        return [_pending_dict(row) for row in rows]

    # Connection and row helpers --------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.root.is_dir():
            raise ConversationError("invalid_root", "The active memory root must be an existing directory.", 422)
        try:
            _ensure_runtime_gitignore(self.root / ".runtime")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=5.0)
        except (OSError, sqlite3.Error) as exc:
            raise ConversationError("storage_error", "The conversation database could not be opened.", 500) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                self._ensure_schema(connection)
                self._ensure_defaults(connection)
                yield connection
        except ConversationError:
            raise
        except sqlite3.Error as exc:
            raise ConversationError("storage_error", "The conversation database operation failed.", 500) from exc
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.executescript(_SCHEMA_V1)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("PRAGMA journal_mode = WAL")
            return
        if version != SCHEMA_VERSION:
            raise ConversationError(
                "unsupported_schema",
                f"Unsupported conversation database schema version: {version}.",
                500,
            )

    def _ensure_defaults(self, connection: sqlite3.Connection) -> None:
        now = _now_iso()
        local_host = connection.execute(
            "SELECT 1 FROM conversation_hosts WHERE host_id = ?", (LOCAL_HOST_ID,)
        ).fetchone()
        if local_host is None:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_hosts(
                    host_id, kind, display_name, capabilities_json,
                    created_at, updated_at, enabled
                ) VALUES(?, 'local', 'Local', '{}', ?, ?, 1)
                """,
                (LOCAL_HOST_ID, now, now),
            )
        local_project = connection.execute(
            "SELECT 1 FROM conversation_projects WHERE project_id = ?", (DEFAULT_LOCAL_PROJECT_ID,)
        ).fetchone()
        if local_project is None:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_projects(
                    project_id, host_id, label, cwd, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    DEFAULT_LOCAL_PROJECT_ID,
                    LOCAL_HOST_ID,
                    self.root.name or "Active root",
                    str(self.root),
                    now,
                    now,
                ),
            )

    def _get_host_row(
        self,
        connection: sqlite3.Connection,
        host_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            f"SELECT {_HOST_COLUMNS} FROM conversation_hosts WHERE host_id = ?",
            (host_id,),
        ).fetchone()
        if row is None and required:
            raise ConversationError("host_not_found", "The conversation host was not found.", 404)
        return row

    def _get_project_row(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM conversation_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None and required:
            raise ConversationError("project_not_found", "The conversation project was not found.", 404)
        return row

    def _get_conversation_row(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            f"SELECT {_CONVERSATION_COLUMNS} FROM pursuit_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None and required:
            raise ConversationError("conversation_not_found", "The conversation was not found.", 404)
        return row

    def _get_pending_row(
        self,
        connection: sqlite3.Connection,
        host_id: str,
        connection_epoch: str,
        rpc_id_json: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests "
            "WHERE host_id = ? AND connection_epoch = ? AND rpc_id_json = ?",
            (host_id, connection_epoch, rpc_id_json),
        ).fetchone()

    def _require_host_project(
        self,
        connection: sqlite3.Connection,
        host_id: str,
        project_id: str,
    ) -> None:
        self._get_host_row(connection, host_id)
        project = self._get_project_row(connection, project_id)
        if project["host_id"] != host_id:
            raise ConversationError("project_host_mismatch", "The project does not belong to that host.", 422)

    def _set_pursuit_default(
        self,
        connection: sqlite3.Connection,
        pursuit_id: str,
        host_id: str,
        project_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pursuit_conversation_preferences(pursuit_id, host_id, project_id, last_used_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(pursuit_id) DO UPDATE SET
                host_id = excluded.host_id,
                project_id = excluded.project_id,
                last_used_at = excluded.last_used_at
            """,
            (pursuit_id, host_id, project_id, now),
        )
        connection.execute(
            "UPDATE conversation_projects SET last_used_at = ?, updated_at = ? WHERE project_id = ?",
            (now, now, project_id),
        )


def _host_dict(row: sqlite3.Row) -> dict[str, Any]:
    return ConversationHost(
        host_id=row["host_id"],
        kind=row["kind"],
        display_name=row["display_name"],
        ssh_alias=row["ssh_alias"],
        codex_command_override=row["codex_command_override"],
        platform_hint=row["platform_hint"],
        app_server_version=row["app_server_version"],
        codex_version=row["codex_version"],
        capabilities=_decode_json_object(row["capabilities_json"]),
        last_seen_at=row["last_seen_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        enabled=bool(row["enabled"]),
    ).to_dict()


def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
    return ConversationProject(
        project_id=row["project_id"],
        host_id=row["host_id"],
        label=row["label"],
        cwd=row["cwd"],
        last_used_at=row["last_used_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).to_dict()


def _conversation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return PursuitConversation(
        conversation_id=row["conversation_id"],
        pursuit_id=row["pursuit_id"],
        pursuit_title_snapshot=row["pursuit_title_snapshot"],
        host_id=row["host_id"],
        project_id=row["project_id"],
        provider=row["provider"],
        thread_id=row["thread_id"],
        thread_title=row["thread_title"],
        lifecycle=row["lifecycle"],
        status=row["status"],
        active_turn_id=row["active_turn_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_activity_at=row["last_activity_at"],
    ).to_dict()


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return ConversationEvent(
        event_id=int(row["event_id"]),
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        kind=row["kind"],
        payload=_decode_json_object(row["payload_json"]),
        created_at=row["created_at"],
    ).to_dict()


def _default_dict(row: sqlite3.Row) -> dict[str, Any]:
    return PursuitConversationDefault(
        pursuit_id=row["pursuit_id"],
        host_id=row["host_id"],
        project_id=row["project_id"],
        last_used_at=row["last_used_at"],
    ).to_dict()


def _pending_dict(row: sqlite3.Row) -> dict[str, Any]:
    rpc_id = json.loads(row["rpc_id_json"])
    return PendingServerRequest(
        request_key=row["request_key"],
        host_id=row["host_id"],
        connection_epoch=row["connection_epoch"],
        rpc_id=rpc_id,
        conversation_id=row["conversation_id"],
        thread_id=row["thread_id"],
        method=row["method"],
        payload=_decode_json_object(row["payload_json"]),
        state=row["state"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    ).to_dict()


def _update_row(
    connection: sqlite3.Connection,
    table: str,
    identity_column: str,
    identity: str,
    updates: dict[str, Any],
) -> None:
    columns = ", ".join(f"{column} = ?" for column in updates)
    connection.execute(
        f"UPDATE {table} SET {columns} WHERE {identity_column} = ?",
        (*updates.values(), identity),
    )


def _id(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ConversationError("invalid_input", f"{field} must be a string.", 422)
    clean = value.strip()
    if _ID_RE.fullmatch(clean) is None:
        raise ConversationError("invalid_input", f"{field} must be a bounded, non-empty identifier.", 422)
    return clean


def _optional_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _id(value, field)


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConversationError("invalid_input", f"{field} must be a string.", 422)
    clean = value.strip()
    if not clean or len(clean) > maximum or any(character in clean for character in "\x00\r\n"):
        raise ConversationError("invalid_input", f"{field} must be a bounded, single-line value.", 422)
    return clean


def _optional_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum)


def _optional_log_text(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConversationError("invalid_input", f"{field} must be a string.", 422)
    clean = value.strip()
    if not clean:
        return None
    if len(clean) > maximum or "\x00" in clean:
        raise ConversationError("invalid_input", f"{field} must be bounded text.", 422)
    return clean


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ConversationError(
            "invalid_input",
            f"{field} must be one of: {', '.join(sorted(choices))}.",
            422,
        )
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConversationError("invalid_input", f"{field} must be a boolean.", 422)
    return value


def _cwd(value: object) -> str:
    clean = _text(value, "cwd", 8192)
    if not (clean.startswith("/") or clean.startswith("\\\\") or _WINDOWS_ABSOLUTE_RE.match(clean)):
        raise ConversationError("invalid_input", "cwd must be an absolute local or remote path.", 422)
    return clean


def _ssh_alias(value: str) -> str:
    if _SSH_ALIAS_RE.fullmatch(value) is None:
        raise ConversationError("invalid_input", "ssh_alias must be one concrete OpenSSH config alias.", 422)
    return value


def _event_kind(value: object) -> str:
    if not isinstance(value, str) or _EVENT_KIND_RE.fullmatch(value) is None:
        raise ConversationError("invalid_input", "Event kind must be a normalized lower-case name.", 422)
    return value


def _method(value: object) -> str:
    if not isinstance(value, str) or _METHOD_RE.fullmatch(value) is None:
        raise ConversationError("invalid_input", "RPC method must be a bounded protocol method name.", 422)
    return value


def _epoch(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConversationError("invalid_input", "connection_epoch must be a string or integer.", 422)
    return _text(str(value), "connection_epoch", 256)


def _rpc_id_json(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConversationError("invalid_input", "rpc_id must be a string or integer.", 422)
    if isinstance(value, str):
        value = _id(value, "rpc_id")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request_key(host_id: str, epoch: str, rpc_id_json: str) -> str:
    encoded = "\x00".join((host_id, epoch, rpc_id_json)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pending_request_key(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_KEY_RE.fullmatch(value) is None:
        raise ConversationError("invalid_input", "request_key must be a 64-character lowercase digest.", 422)
    return value


def _json_object(value: object, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, dict):
        raise ConversationError("invalid_input", f"{field} must be a JSON object.", 422)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConversationError("invalid_input", f"{field} must be JSON-safe.", 422) from exc
    size = len(encoded.encode("utf-8"))
    if size > maximum_bytes:
        raise ConversationError(
            "payload_too_large",
            f"{field} exceeds the {maximum_bytes}-byte storage limit.",
            413,
        )
    return encoded


def _decode_json_object(encoded: str) -> dict[str, Any]:
    try:
        value = json.loads(encoded)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConversationError("storage_corrupt", "Stored conversation JSON is invalid.", 500) from exc
    if not isinstance(value, dict):
        raise ConversationError("storage_corrupt", "Stored conversation JSON is not an object.", 500)
    return value


def _optional_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    clean = _text(value, field, 100)
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError as exc:
        raise ConversationError("invalid_input", f"{field} must be an ISO-8601 timestamp.", 422) from exc
    if parsed.tzinfo is None:
        raise ConversationError("invalid_input", f"{field} must include a timezone.", 422)
    return clean


def _cursor(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversationError("invalid_input", "after_event_id must be a non-negative integer.", 422)
    return value


def _limit(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ConversationError("invalid_input", f"limit must be between 1 and {maximum}.", 422)
    return value


def _offset(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversationError("invalid_input", "offset must be a non-negative integer.", 422)
    return value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _raise_pending_duplicate(state: str) -> None:
    if state == "stale":
        raise ConversationError("stale_request", "That server request is stale and cannot be reused.", 409)
    raise ConversationError("duplicate_request", "That server request identity was already recorded.", 409)


def _raise_resolution_conflict(row: sqlite3.Row | None) -> None:
    if row is None:
        raise ConversationError("request_not_found", "The pending server request was not found.", 404)
    if row["state"] == "resolved":
        raise ConversationError("duplicate_response", "That server request was already resolved.", 409)
    raise ConversationError("stale_request", "That server request is stale and cannot be resolved.", 409)


def _resolve_pending_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    resolved_at: str,
) -> sqlite3.Row:
    if row["state"] == "resolved":
        raise ConversationError("duplicate_response", "That server request was already resolved.", 409)
    if row["state"] == "stale":
        raise ConversationError("stale_request", "That server request is stale and cannot be resolved.", 409)
    cursor = connection.execute(
        """
        UPDATE pending_server_requests
        SET state = 'resolved', resolved_at = ?, payload_json = '{}'
        WHERE request_key = ? AND state = 'pending'
        """,
        (resolved_at, row["request_key"]),
    )
    if cursor.rowcount != 1:
        current = connection.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests WHERE request_key = ?",
            (row["request_key"],),
        ).fetchone()
        _raise_resolution_conflict(current)
    resolved = connection.execute(
        f"SELECT {_PENDING_COLUMNS} FROM pending_server_requests WHERE request_key = ?",
        (row["request_key"],),
    ).fetchone()
    if resolved is None:
        raise ConversationError("storage_error", "The resolved server request could not be read.", 500)
    return resolved


def _stale_pending_rows(connection: sqlite3.Connection, resolved_at: str) -> int:
    """Invalidate approvals from a provider process that did not survive restart."""
    cursor = connection.execute(
        """
        UPDATE pending_server_requests
        SET state = 'stale', resolved_at = ?, payload_json = '{}'
        WHERE state = 'pending'
        """,
        (resolved_at,),
    )
    return int(cursor.rowcount)


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS conversation_hosts(
    host_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('local', 'ssh')),
    display_name TEXT NOT NULL,
    ssh_alias TEXT,
    codex_command_override TEXT,
    platform_hint TEXT,
    app_server_version TEXT,
    codex_version TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
);

CREATE TABLE IF NOT EXISTS conversation_projects(
    project_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    label TEXT NOT NULL,
    cwd TEXT NOT NULL,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host_id, cwd),
    UNIQUE(project_id, host_id),
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS pursuit_conversations(
    conversation_id TEXT PRIMARY KEY,
    pursuit_id TEXT NOT NULL,
    pursuit_title_snapshot TEXT,
    host_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'codex' CHECK(provider = 'codex'),
    thread_id TEXT NOT NULL,
    thread_title TEXT,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'archived')),
    status TEXT NOT NULL CHECK(status IN (
        'idle', 'starting', 'running', 'waiting_approval', 'waiting_input',
        'completed', 'failed', 'interrupted', 'unknown'
    )),
    active_turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    UNIQUE(host_id, thread_id),
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id, host_id)
        REFERENCES conversation_projects(project_id, host_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS pursuit_conversations_by_pursuit
    ON pursuit_conversations(pursuit_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS pursuit_conversations_by_host
    ON pursuit_conversations(host_id, last_activity_at DESC);

CREATE TABLE IF NOT EXISTS pursuit_conversation_preferences(
    pursuit_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id, host_id)
        REFERENCES conversation_projects(project_id, host_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS conversation_events(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    turn_id TEXT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS conversation_events_by_conversation
    ON conversation_events(conversation_id, event_id);

CREATE TABLE IF NOT EXISTS pending_server_requests(
    request_key TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    connection_epoch TEXT NOT NULL,
    rpc_id_json TEXT NOT NULL,
    conversation_id TEXT,
    thread_id TEXT,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'resolved', 'stale')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(host_id, connection_epoch, rpc_id_json),
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS pending_server_requests_by_state
    ON pending_server_requests(host_id, state, created_at);
"""
