from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from uuid import uuid4

from ..session import _ensure_runtime_gitignore
from .models import (
    ATTACHMENT_KINDS,
    ATTACHMENT_STATES,
    CONVERSATION_KINDS,
    CONVERSATION_LIFECYCLES,
    CONVERSATION_STATUSES,
    DEFAULT_LOCAL_PROJECT_ID,
    HOST_KINDS,
    LOCAL_HOST_ID,
    PENDING_REQUEST_STATES,
    SCHEMA_VERSION,
    ConversationAttachment,
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
MAX_INITIAL_CONTEXT_BYTES = 256 * 1024
MAX_EVENTS_PER_READ = 1000

_ID_RE = re.compile(r"^[^\x00\r\n]{1,512}$")
_EVENT_KIND_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_METHOD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./-]{0,255}$")
_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNSET = object()

_HOST_COLUMNS = """
    host_id, kind, display_name, ssh_alias, codex_command_override,
    platform_hint, app_server_version, codex_version, capabilities_json,
    last_seen_at, last_error, created_at, updated_at, enabled
"""
_PROJECT_COLUMNS = "project_id, host_id, label, cwd, last_used_at, created_at, updated_at"
_CONVERSATION_COLUMNS = """
    conversation_id, kind, parent_conversation_id, pursuit_id,
    pursuit_title_snapshot, host_id, project_id,
    execution_cwd, provider, thread_id, thread_title, model,
    reasoning_effort, lifecycle, status, active_turn_id,
    initial_context_state, initial_context_text,
    initial_context_accepted_turn_id, last_final_event_id, last_read_event_id,
    created_at, updated_at, last_activity_at
"""
_EVENT_COLUMNS = (
    "event_id, conversation_id, turn_id, kind, payload_json, created_at, marks_final"
)
_ATTACHMENT_COLUMNS = """
    attachment_id, conversation_id, kind, display_name, media_type, byte_size,
    sha256, relative_path, remote_path, state, created_at, updated_at
"""
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
        """Create or upgrade the database and ensure stable local records."""
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

    def update_host_config(
        self,
        host_id: str,
        *,
        display_name: str | object = _UNSET,
        ssh_alias: str | None | object = _UNSET,
        codex_command_override: str | None | object = _UNSET,
        platform_hint: str | None | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> dict[str, Any]:
        """Update user-owned host configuration without touching runtime facts."""
        clean_host_id = _id(host_id, "host_id")
        updates: dict[str, Any] = {}
        if display_name is not _UNSET:
            updates["display_name"] = _text(display_name, "display_name", 200)
        if ssh_alias is not _UNSET:
            clean_alias = _optional_text(ssh_alias, "ssh_alias", 255)
            if clean_alias is not None:
                _ssh_alias(clean_alias)
            updates["ssh_alias"] = clean_alias
        if codex_command_override is not _UNSET:
            updates["codex_command_override"] = _optional_text(
                codex_command_override, "codex_command_override", 2048
            )
        if platform_hint is not _UNSET:
            updates["platform_hint"] = _optional_text(
                platform_hint, "platform_hint", 100
            )
        if enabled is not _UNSET:
            updates["enabled"] = int(_boolean(enabled, "enabled"))
        now = _now_iso()
        updates["updated_at"] = now
        with self._connect() as connection:
            current = self._get_host_row(connection, clean_host_id)
            prospective_alias = updates.get("ssh_alias", current["ssh_alias"])
            if current["kind"] == "local":
                if prospective_alias is not None:
                    raise ConversationError(
                        "invalid_host", "A local host cannot have an SSH alias.", 422
                    )
            elif prospective_alias is None:
                raise ConversationError(
                    "invalid_host", "An SSH host requires an SSH config alias.", 422
                )
            _update_row(
                connection,
                "conversation_hosts",
                "host_id",
                clean_host_id,
                updates,
            )
            row = self._get_host_row(connection, clean_host_id)
        return _host_dict(row)

    def host_has_conversations(self, host_id: str) -> bool:
        clean_host_id = _id(host_id, "host_id")
        with self._connect() as connection:
            self._get_host_row(connection, clean_host_id)
            row = connection.execute(
                "SELECT 1 FROM pursuit_conversations WHERE host_id = ? LIMIT 1",
                (clean_host_id,),
            ).fetchone()
        return row is not None

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

    def update_project(
        self,
        project_id: str,
        *,
        label: str | object = _UNSET,
        cwd: str | object = _UNSET,
    ) -> dict[str, Any]:
        """Update a registered project; conversation execution snapshots stay fixed."""
        clean_project_id = _id(project_id, "project_id")
        updates: dict[str, Any] = {}
        if label is not _UNSET:
            updates["label"] = _text(label, "label", 300)
        if cwd is not _UNSET:
            updates["cwd"] = _cwd(cwd)
        updates["updated_at"] = _now_iso()
        with self._connect() as connection:
            self._get_project_row(connection, clean_project_id)
            try:
                _update_row(
                    connection,
                    "conversation_projects",
                    "project_id",
                    clean_project_id,
                    updates,
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationError(
                    "project_conflict",
                    "That host and working directory are already registered.",
                    409,
                ) from exc
            row = self._get_project_row(connection, clean_project_id)
        return _project_dict(row)

    def project_has_conversations(self, project_id: str) -> bool:
        clean_project_id = _id(project_id, "project_id")
        with self._connect() as connection:
            self._get_project_row(connection, clean_project_id)
            row = connection.execute(
                "SELECT 1 FROM pursuit_conversations WHERE project_id = ? LIMIT 1",
                (clean_project_id,),
            ).fetchone()
        return row is not None

    # Conversations and Pursuit defaults ------------------------------------

    def create_conversation(
        self,
        *,
        pursuit_id: str | None = None,
        host_id: str,
        project_id: str,
        thread_id: str,
        execution_cwd: str | None = None,
        kind: str = "pursuit",
        parent_conversation_id: str | None = None,
        owner_session_id: str | None = None,
        pursuit_title_snapshot: str | None = None,
        thread_title: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        lifecycle: str = "active",
        status: str = "idle",
        active_turn_id: str | None = None,
    ) -> dict[str, Any]:
        # A row represents a provider-confirmed thread. Callers intentionally
        # persist only after thread/start returns a stable, non-empty thread id.
        clean_conversation_id = _id(conversation_id or uuid4().hex, "conversation_id")
        clean_kind = _choice(kind, "conversation kind", CONVERSATION_KINDS)
        clean_parent_id = _optional_id(parent_conversation_id, "parent_conversation_id")
        clean_owner_session_id = _optional_id(owner_session_id, "owner_session_id")
        clean_pursuit_id = _optional_id(pursuit_id, "pursuit_id")
        if clean_kind == "pursuit" and (
            clean_parent_id is not None
            or clean_owner_session_id is not None
            or clean_pursuit_id is None
        ):
            raise ConversationError(
                "invalid_parent_conversation",
                "A Pursuit conversation requires a Pursuit and cannot have a parent or session owner.",
                422,
            )
        if clean_kind == "side_chat" and (
            clean_parent_id is None
            or clean_owner_session_id is None
            or clean_pursuit_id is None
        ):
            raise ConversationError(
                "invalid_parent_conversation",
                "A side chat requires its Pursuit, parent conversation, and session owner.",
                422,
            )
        if clean_kind == "manager" and (
            clean_parent_id is not None
            or clean_owner_session_id is not None
            or clean_pursuit_id is not None
        ):
            raise ConversationError(
                "invalid_parent_conversation",
                "A manager conversation cannot have a Pursuit, parent, or session owner.",
                422,
            )
        clean_host_id = _id(host_id, "host_id")
        clean_project_id = _id(project_id, "project_id")
        clean_thread_id = _id(thread_id, "thread_id")
        clean_execution_cwd = (
            _cwd(execution_cwd) if execution_cwd is not None else None
        )
        clean_pursuit_title = _optional_text(pursuit_title_snapshot, "pursuit_title_snapshot", 500)
        clean_thread_title = _optional_text(thread_title, "thread_title", 500)
        clean_model = _optional_text(model, "model", 512)
        clean_reasoning_effort = _optional_text(
            reasoning_effort, "reasoning_effort", 512
        )
        clean_lifecycle = _choice(lifecycle, "lifecycle", CONVERSATION_LIFECYCLES)
        clean_status = _choice(status, "status", CONVERSATION_STATUSES)
        clean_turn_id = _optional_id(active_turn_id, "active_turn_id")
        now = _now_iso()
        with self._connect() as connection:
            project = self._require_host_project(
                connection, clean_host_id, clean_project_id
            )
            stored_execution_cwd = clean_execution_cwd or project["cwd"]
            if clean_parent_id is not None:
                parent = self._get_conversation_row(connection, clean_parent_id)
                if (
                    parent["kind"] != "pursuit"
                    or parent["pursuit_id"] != clean_pursuit_id
                    or parent["host_id"] != clean_host_id
                    or parent["project_id"] != clean_project_id
                    or parent["execution_cwd"] != stored_execution_cwd
                ):
                    raise ConversationError(
                        "invalid_parent_conversation",
                        "A side chat must retain its parent Pursuit, project, and execution directory.",
                        422,
                    )
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
                    conversation_id, kind, parent_conversation_id,
                    owner_session_id, pursuit_id, pursuit_title_snapshot,
                    host_id, project_id, execution_cwd, provider,
                    thread_id, thread_title, model,
                    reasoning_effort, lifecycle, status, active_turn_id,
                    created_at, updated_at, last_activity_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'codex', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_conversation_id,
                    clean_kind,
                    clean_parent_id,
                    clean_owner_session_id,
                    clean_pursuit_id,
                    clean_pursuit_title,
                    clean_host_id,
                    clean_project_id,
                    stored_execution_cwd,
                    clean_thread_id,
                    clean_thread_title,
                    clean_model,
                    clean_reasoning_effort,
                    clean_lifecycle,
                    clean_status,
                    clean_turn_id,
                    now,
                    now,
                    now,
                ),
            )
            if clean_kind == "pursuit":
                self._set_pursuit_default(
                    connection,
                    clean_pursuit_id,
                    clean_host_id,
                    clean_project_id,
                    now,
                )
            row = self._get_conversation_row(connection, clean_conversation_id)
        return _conversation_dict(row)

    def prepare_initial_context(
        self, conversation_id: str, text: str
    ) -> dict[str, Any]:
        """Persist the exact first-turn context before provider submission."""
        clean_id = _id(conversation_id, "conversation_id")
        clean_text = _initial_context_text(text)
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "prepared":
                if current["initial_context_text"] == clean_text:
                    return _conversation_dict(current)
                _raise_initial_context_transition("prepared", "prepared")
            if current["initial_context_state"] != "eligible":
                _raise_initial_context_transition(
                    current["initial_context_state"], "prepared"
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'prepared',
                    initial_context_text = ?,
                    initial_context_accepted_turn_id = NULL,
                    updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state = 'eligible'
                """,
                (clean_text, now, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if (
                    current["initial_context_state"] == "prepared"
                    and current["initial_context_text"] == clean_text
                ):
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "prepared"
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def mark_initial_context_unknown(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Record that a prepared context may have reached the provider."""
        clean_id = _id(conversation_id, "conversation_id")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "unknown":
                return _conversation_dict(current)
            if current["initial_context_state"] != "prepared":
                _raise_initial_context_transition(
                    current["initial_context_state"], "unknown"
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'unknown', updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state = 'prepared'
                """,
                (now, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if current["initial_context_state"] == "unknown":
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "unknown"
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def mark_initial_context_accepted(
        self, conversation_id: str, turn_id: str
    ) -> dict[str, Any]:
        """Bind the prepared context to the provider-accepted first turn."""
        clean_id = _id(conversation_id, "conversation_id")
        clean_turn_id = _id(turn_id, "turn_id")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "accepted":
                if current["initial_context_accepted_turn_id"] == clean_turn_id:
                    return _conversation_dict(current)
                _raise_initial_context_transition("accepted", "accepted")
            if current["initial_context_state"] not in {"prepared", "unknown"}:
                _raise_initial_context_transition(
                    current["initial_context_state"], "accepted"
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'accepted',
                    initial_context_accepted_turn_id = ?,
                    updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state IN ('prepared', 'unknown')
                """,
                (clean_turn_id, now, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if (
                    current["initial_context_state"] == "accepted"
                    and current["initial_context_accepted_turn_id"]
                    == clean_turn_id
                ):
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "accepted"
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def reset_initial_context_to_prepared(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Retry an uncertain submission using the exact persisted context."""
        clean_id = _id(conversation_id, "conversation_id")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "prepared":
                return _conversation_dict(current)
            if current["initial_context_state"] != "unknown":
                _raise_initial_context_transition(
                    current["initial_context_state"], "prepared"
                )
            if current["initial_context_text"] is None:
                raise ConversationError(
                    "initial_context_unavailable",
                    "The uncertain conversation has no prepared initial context to retry.",
                    409,
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'prepared', updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state = 'unknown'
                  AND initial_context_text IS NOT NULL
                """,
                (now, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if current["initial_context_state"] == "prepared":
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "prepared"
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def reset_initial_context_to_eligible(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Reopen an unprepared unknown context after verified empty history."""
        clean_id = _id(conversation_id, "conversation_id")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "eligible":
                return _conversation_dict(current)
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
            if (
                current["initial_context_state"] != "unknown"
                or current["initial_context_text"] is not None
                or current["initial_context_accepted_turn_id"] is not None
                or current["active_turn_id"] is not None
                or turn_evidence is not None
            ):
                _raise_initial_context_transition(
                    current["initial_context_state"], "eligible"
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'eligible', updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state = 'unknown'
                  AND initial_context_text IS NULL
                  AND initial_context_accepted_turn_id IS NULL
                  AND active_turn_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM conversation_events
                      WHERE conversation_id = ?
                        AND (turn_id IS NOT NULL OR kind GLOB 'turn.*')
                  )
                """,
                (now, clean_id, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if current["initial_context_state"] == "eligible":
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "eligible"
                )
            row = self._get_conversation_row(connection, clean_id)
        return _conversation_dict(row)

    def mark_initial_context_skipped(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Record that initial-context submission does not apply here."""
        clean_id = _id(conversation_id, "conversation_id")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if current["initial_context_state"] == "skipped":
                return _conversation_dict(current)
            if current["initial_context_state"] not in {
                "eligible",
                "prepared",
                "unknown",
            }:
                _raise_initial_context_transition(
                    current["initial_context_state"], "skipped"
                )
            cursor = connection.execute(
                """
                UPDATE pursuit_conversations
                SET initial_context_state = 'skipped', updated_at = ?
                WHERE conversation_id = ?
                  AND initial_context_state IN ('eligible', 'prepared', 'unknown')
                """,
                (now, clean_id),
            )
            if cursor.rowcount != 1:
                current = self._get_conversation_row(connection, clean_id)
                if current["initial_context_state"] == "skipped":
                    return _conversation_dict(current)
                _raise_initial_context_transition(
                    current["initial_context_state"], "skipped"
                )
            row = self._get_conversation_row(connection, clean_id)
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
        kind: str | None = None,
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
        if kind is not None:
            clauses.append("kind = ?")
            values.append(_choice(kind, "conversation kind", CONVERSATION_KINDS))
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
        pursuit_id: str | None | object = _UNSET,
        pursuit_title_snapshot: str | None | object = _UNSET,
        thread_title: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        reasoning_effort: str | None | object = _UNSET,
        lifecycle: str | object = _UNSET,
        status: str | object = _UNSET,
        active_turn_id: str | None | object = _UNSET,
        touch_activity: bool = False,
        emit_state_event: bool = False,
    ) -> dict[str, Any]:
        clean_id = _id(conversation_id, "conversation_id")
        updates: dict[str, Any] = {}
        if pursuit_id is not _UNSET:
            updates["pursuit_id"] = _optional_id(pursuit_id, "pursuit_id")
        if pursuit_title_snapshot is not _UNSET:
            updates["pursuit_title_snapshot"] = _optional_text(
                pursuit_title_snapshot, "pursuit_title_snapshot", 500
            )
        if thread_title is not _UNSET:
            updates["thread_title"] = _optional_text(thread_title, "thread_title", 500)
        if model is not _UNSET:
            updates["model"] = _optional_text(model, "model", 512)
        if reasoning_effort is not _UNSET:
            updates["reasoning_effort"] = _optional_text(
                reasoning_effort, "reasoning_effort", 512
            )
        if lifecycle is not _UNSET:
            updates["lifecycle"] = _choice(lifecycle, "lifecycle", CONVERSATION_LIFECYCLES)
        if status is not _UNSET:
            updates["status"] = _choice(status, "status", CONVERSATION_STATUSES)
        if active_turn_id is not _UNSET:
            updates["active_turn_id"] = _optional_id(active_turn_id, "active_turn_id")
        clean_touch = _boolean(touch_activity, "touch_activity")
        clean_emit_state = _boolean(emit_state_event, "emit_state_event")
        now = _now_iso()
        updates["updated_at"] = now
        if clean_touch:
            updates["last_activity_at"] = now
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if "pursuit_id" in updates:
                requested_pursuit_id = updates["pursuit_id"]
                if current["kind"] == "manager":
                    if requested_pursuit_id is not None:
                        raise ConversationError(
                            "invalid_conversation",
                            "A manager conversation cannot be attached to a Pursuit.",
                            422,
                        )
                elif requested_pursuit_id is None:
                    raise ConversationError(
                        "invalid_conversation",
                        "Only a manager conversation can omit its Pursuit.",
                        422,
                    )
                elif current["kind"] == "side_chat":
                    parent = self._get_conversation_row(
                        connection, current["parent_conversation_id"]
                    )
                    if parent["pursuit_id"] != requested_pursuit_id:
                        raise ConversationError(
                            "invalid_parent_conversation",
                            "A side chat must retain its parent Pursuit.",
                            422,
                        )
            _update_row(connection, "pursuit_conversations", "conversation_id", clean_id, updates)
            if "pursuit_id" in updates and current["kind"] == "pursuit":
                self._set_pursuit_default(
                    connection,
                    updates["pursuit_id"],
                    current["host_id"],
                    current["project_id"],
                    now,
                )
            row = self._get_conversation_row(connection, clean_id)
            if clean_emit_state:
                _insert_conversation_state_event(connection, row, now)
        return _conversation_dict(row)

    def has_turn_evidence(self, conversation_id: str) -> bool:
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_id)
            if (
                current["active_turn_id"] is not None
                or current["initial_context_accepted_turn_id"] is not None
            ):
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

    def provider_turn_fingerprint(self, conversation_id: str) -> dict[str, Any]:
        """Return a bounded fingerprint of every durable provider turn id."""
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_id)
            rows = connection.execute(
                """
                WITH provider_turns(provider_turn_id) AS (
                    SELECT turn_id
                    FROM conversation_events
                    WHERE conversation_id = ? AND turn_id IS NOT NULL
                    UNION
                    SELECT json_extract(payload_json, '$.latest_provider_turn_id')
                    FROM conversation_events
                    WHERE conversation_id = ?
                      AND kind = 'thread.reconciled'
                      AND typeof(
                          json_extract(payload_json, '$.latest_provider_turn_id')
                      ) = 'text'
                    UNION
                    SELECT initial_context_accepted_turn_id
                    FROM pursuit_conversations
                    WHERE conversation_id = ?
                      AND initial_context_accepted_turn_id IS NOT NULL
                )
                SELECT provider_turn_id
                FROM provider_turns
                ORDER BY provider_turn_id
                """,
                (clean_id, clean_id, clean_id),
            ).fetchall()
        return provider_turn_id_fingerprint(
            str(row["provider_turn_id"]) for row in rows
        )

    def has_provider_turn_id(self, conversation_id: str, turn_id: str) -> bool:
        clean_id = _id(conversation_id, "conversation_id")
        clean_turn_id = _id(turn_id, "turn_id")
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_id)
            row = connection.execute(
                """
                SELECT 1 FROM (
                    SELECT turn_id AS provider_turn_id
                    FROM conversation_events
                    WHERE conversation_id = ? AND turn_id = ?
                    UNION ALL
                    SELECT json_extract(payload_json, '$.latest_provider_turn_id')
                    FROM conversation_events
                    WHERE conversation_id = ?
                      AND kind = 'thread.reconciled'
                      AND json_extract(
                          payload_json, '$.latest_provider_turn_id'
                      ) = ?
                    UNION ALL
                    SELECT initial_context_accepted_turn_id
                    FROM pursuit_conversations
                    WHERE conversation_id = ?
                      AND initial_context_accepted_turn_id = ?
                )
                LIMIT 1
                """,
                (
                    clean_id,
                    clean_turn_id,
                    clean_id,
                    clean_turn_id,
                    clean_id,
                    clean_turn_id,
                ),
            ).fetchone()
        return row is not None

    def pending_user_message_event_id(self, conversation_id: str) -> int | None:
        """Return the unsatisfied user event newer than all provider-turn evidence."""
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_id)
            row = connection.execute(
                """
                SELECT event.event_id
                FROM conversation_events AS event
                WHERE event.conversation_id = ?
                  AND event.kind = 'user.message'
                  AND event.event_id > COALESCE((
                      SELECT MAX(turn_event.event_id)
                      FROM conversation_events AS turn_event
                      WHERE turn_event.conversation_id = ?
                        AND turn_event.turn_id IS NOT NULL
                  ), 0)
                ORDER BY event.event_id DESC
                LIMIT 1
                """,
                (clean_id, clean_id),
            ).fetchone()
        return int(row["event_id"]) if row is not None else None

    def latest_turn_start_uncertainty(
        self, conversation_id: str
    ) -> dict[str, Any] | None:
        """Return the newest durable turn/start uncertainty marker."""
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_id)
            row = connection.execute(
                f"""
                SELECT {_EVENT_COLUMNS}
                FROM conversation_events
                WHERE conversation_id = ?
                  AND kind = 'protocol.error'
                  AND json_extract(payload_json, '$.operation') = 'turn/start'
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (clean_id,),
            ).fetchone()
        return _event_dict(row) if row is not None else None

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
            if (
                current["initial_context_state"] not in {"eligible", "prepared"}
                or current["active_turn_id"] is not None
                or current["initial_context_accepted_turn_id"] is not None
                or turn_evidence is not None
            ):
                raise ConversationError(
                    "conversation_has_turn_history",
                    "A conversation that may have reached the provider cannot change provider threads.",
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
                  AND initial_context_state IN ('eligible', 'prepared')
                  AND active_turn_id IS NULL
                  AND initial_context_accepted_turn_id IS NULL
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

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete operational conversation state while retaining detached events."""
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pursuit_conversations WHERE conversation_id = ?",
                (clean_id,),
            )
        return cursor.rowcount == 1

    def side_chat_belongs_to_session(
        self,
        conversation_id: str,
        owner_session_id: str,
    ) -> bool:
        """Return whether a side chat belongs to one signed browser session."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_owner_session_id = _id(owner_session_id, "owner_session_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM pursuit_conversations
                WHERE conversation_id = ?
                  AND kind = 'side_chat'
                  AND owner_session_id = ?
                """,
                (clean_conversation_id, clean_owner_session_id),
            ).fetchone()
        return row is not None

    def purge_side_chat(self, conversation_id: str) -> list[dict[str, Any]]:
        """Atomically erase one side chat's durable transcript and metadata."""
        clean_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            conversation = self._get_conversation_row(connection, clean_id)
            if conversation["kind"] != "side_chat":
                raise ConversationError(
                    "not_side_chat", "Only a side chat can be purged here.", 409
                )
            attachment_rows = connection.execute(
                f"SELECT {_ATTACHMENT_COLUMNS} FROM conversation_attachments "
                "WHERE conversation_id = ? ORDER BY created_at, attachment_id",
                (clean_id,),
            ).fetchall()
            connection.execute(
                "DELETE FROM conversation_events WHERE conversation_id = ?",
                (clean_id,),
            )
            connection.execute(
                "DELETE FROM pending_server_requests WHERE conversation_id = ?",
                (clean_id,),
            )
            connection.execute(
                "DELETE FROM conversation_attachments WHERE conversation_id = ?",
                (clean_id,),
            )
            cursor = connection.execute(
                "DELETE FROM pursuit_conversations WHERE conversation_id = ?",
                (clean_id,),
            )
            if cursor.rowcount != 1:
                raise ConversationError(
                    "conversation_not_found", "The side chat was already closed.", 404
                )
        return [_attachment_dict(row) for row in attachment_rows]

    def purge_side_chats(self) -> tuple[int, list[dict[str, Any]]]:
        """Atomically erase all side chats left by an ended app runtime."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT conversation_id FROM pursuit_conversations "
                "WHERE kind = 'side_chat'"
            ).fetchall()
            if not rows:
                return 0, []
            side_chat_ids = (
                "SELECT conversation_id FROM pursuit_conversations "
                "WHERE kind = 'side_chat'"
            )
            attachment_rows = connection.execute(
                f"SELECT {_ATTACHMENT_COLUMNS} FROM conversation_attachments "
                f"WHERE conversation_id IN ({side_chat_ids}) "
                "ORDER BY created_at, attachment_id"
            ).fetchall()
            connection.execute(
                f"DELETE FROM conversation_events "
                f"WHERE conversation_id IN ({side_chat_ids})"
            )
            connection.execute(
                f"DELETE FROM pending_server_requests "
                f"WHERE conversation_id IN ({side_chat_ids})"
            )
            connection.execute(
                f"DELETE FROM conversation_attachments "
                f"WHERE conversation_id IN ({side_chat_ids})"
            )
            cursor = connection.execute(
                "DELETE FROM pursuit_conversations WHERE kind = 'side_chat'"
            )
        return int(cursor.rowcount), [_attachment_dict(row) for row in attachment_rows]

    def list_side_chats(
        self,
        *,
        parent_conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List temporary side chats left in the durable recovery store."""
        clean_parent_id = _optional_id(
            parent_conversation_id, "parent_conversation_id"
        )
        with self._connect() as connection:
            if clean_parent_id is None:
                rows = connection.execute(
                    f"SELECT {_CONVERSATION_COLUMNS} FROM pursuit_conversations "
                    "WHERE kind = 'side_chat' "
                    "ORDER BY last_activity_at DESC, conversation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_CONVERSATION_COLUMNS} FROM pursuit_conversations "
                    "WHERE kind = 'side_chat' AND parent_conversation_id = ? "
                    "ORDER BY last_activity_at DESC, conversation_id",
                    (clean_parent_id,),
                ).fetchall()
        return [_conversation_dict(row) for row in rows]

    def cleanup_side_chats(self) -> int:
        """Discard all side chats after their browser/app session is lost."""
        count, _attachments = self.purge_side_chats()
        return count

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
        mark_final: bool = False,
        owner_session_id: str | None = None,
    ) -> dict[str, Any]:
        clean_kind = _event_kind(kind)
        clean_conversation_id = _optional_id(conversation_id, "conversation_id")
        clean_turn_id = _optional_id(turn_id, "turn_id")
        clean_mark_final = _boolean(mark_final, "mark_final")
        clean_owner_session_id = _optional_id(owner_session_id, "owner_session_id")
        stored_payload = dict(payload)
        if "__rightmemory_session_scope" in stored_payload:
            raise ConversationError(
                "invalid_input", "Event payload uses a reserved field.", 422
            )
        if clean_owner_session_id is not None:
            stored_payload["__rightmemory_session_scope"] = _session_scope(
                clean_owner_session_id
            )
        payload_json = _json_object(
            stored_payload, "event payload", MAX_EVENT_PAYLOAD_BYTES
        )
        if clean_mark_final and clean_conversation_id is None:
            raise ConversationError(
                "invalid_input",
                "A final event must belong to a conversation.",
                422,
            )
        now = _now_iso()
        with self._connect() as connection:
            if clean_conversation_id is not None:
                self._get_conversation_row(connection, clean_conversation_id)
            cursor = connection.execute(
                """
                INSERT INTO conversation_events(
                    conversation_id, turn_id, kind, payload_json, created_at,
                    marks_final
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_conversation_id,
                    clean_turn_id,
                    clean_kind,
                    payload_json,
                    now,
                    int(clean_mark_final),
                ),
            )
            event_id = int(cursor.lastrowid)
            if clean_conversation_id is not None:
                if clean_mark_final:
                    connection.execute(
                        """
                        UPDATE pursuit_conversations
                        SET updated_at = ?, last_activity_at = ?,
                            last_final_event_id = ?
                        WHERE conversation_id = ?
                        """,
                        (now, now, event_id, clean_conversation_id),
                    )
                else:
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
                    f"SELECT {_EVENT_COLUMNS} FROM conversation_events "
                    "WHERE event_id > ? ORDER BY event_id LIMIT ?",
                    (clean_cursor, clean_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {_EVENT_COLUMNS} FROM conversation_events "
                    "WHERE event_id > ? AND conversation_id = ? "
                    "ORDER BY event_id LIMIT ?",
                    (clean_cursor, clean_conversation_id, clean_limit),
                ).fetchall()
        return [_event_dict(row) for row in rows]

    def read_events_for_session(
        self,
        owner_session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Read root events while hiding side chats owned by other sessions."""
        clean_owner_session_id = _id(owner_session_id, "owner_session_id")
        session_scope = _session_scope(clean_owner_session_id)
        clean_cursor = _cursor(after_event_id)
        clean_limit = _limit(limit, maximum=MAX_EVENTS_PER_READ)
        event_columns = ", ".join(
            f"event.{column.strip()}" for column in _EVENT_COLUMNS.split(",")
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {event_columns}
                FROM conversation_events AS event
                LEFT JOIN pursuit_conversations AS conversation
                  ON conversation.conversation_id = event.conversation_id
                WHERE event.event_id > ?
                  AND (
                      event.conversation_id IS NULL
                      OR conversation.kind IN ('pursuit', 'manager')
                      OR (
                          conversation.kind = 'side_chat'
                          AND conversation.owner_session_id = ?
                      )
                  )
                  AND (
                      json_extract(event.payload_json, '$.__rightmemory_session_scope') IS NULL
                      OR json_extract(event.payload_json, '$.__rightmemory_session_scope') = ?
                  )
                ORDER BY event.event_id
                LIMIT ?
                """,
                (
                    clean_cursor,
                    clean_owner_session_id,
                    session_scope,
                    clean_limit,
                ),
            ).fetchall()
        events = [_event_dict(row) for row in rows]
        for event in events:
            event["payload"].pop("__rightmemory_session_scope", None)
        return events

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

    def read_events_before(
        self,
        conversation_id: str,
        *,
        before_event_id: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return the newest events before one cursor in chronological order."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_cursor = _positive_event_id(before_event_id, "before_event_id")
        clean_limit = _limit(limit, maximum=MAX_EVENTS_PER_READ)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_EVENT_COLUMNS} FROM conversation_events "
                "WHERE conversation_id = ? AND event_id < ? "
                "ORDER BY event_id DESC LIMIT ?",
                (clean_conversation_id, clean_cursor, clean_limit),
            ).fetchall()
        rows.reverse()
        return [_event_dict(row) for row in rows]

    def latest_event_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(event_id), 0) FROM conversation_events").fetchone()
        return int(row[0])

    def mark_final_event(
        self,
        conversation_id: str,
        event_id: int,
    ) -> dict[str, Any]:
        """Record the newest final-response event for unread-state projection."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_event_id = _positive_event_id(event_id, "event_id")
        now = _now_iso()
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_conversation_id)
            _require_conversation_event(
                connection, clean_conversation_id, clean_event_id
            )
            connection.execute(
                """
                UPDATE conversation_events
                SET marks_final = 1
                WHERE event_id = ? AND conversation_id = ?
                """,
                (clean_event_id, clean_conversation_id),
            )
            connection.execute(
                """
                UPDATE pursuit_conversations
                SET last_final_event_id = ?, updated_at = ?
                WHERE conversation_id = ?
                  AND (
                      last_final_event_id IS NULL
                      OR last_final_event_id < ?
                  )
                """,
                (
                    clean_event_id,
                    now,
                    clean_conversation_id,
                    clean_event_id,
                ),
            )
            row = self._get_conversation_row(connection, clean_conversation_id)
        return _conversation_dict(row)

    def acknowledge_read(
        self,
        conversation_id: str,
        event_id: int | None = None,
        *,
        emit_state_event: bool = False,
    ) -> dict[str, Any]:
        """Advance the durable read cursor, defaulting to the latest final."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_event_id = (
            _positive_event_id(event_id, "event_id")
            if event_id is not None
            else None
        )
        clean_emit_state = _boolean(emit_state_event, "emit_state_event")
        now = _now_iso()
        with self._connect() as connection:
            current = self._get_conversation_row(connection, clean_conversation_id)
            target = (
                clean_event_id
                if clean_event_id is not None
                else current["last_final_event_id"]
            )
            if target is not None:
                _require_conversation_event(
                    connection,
                    clean_conversation_id,
                    int(target),
                    must_mark_final=True,
                )
                connection.execute(
                    """
                    UPDATE pursuit_conversations
                    SET last_read_event_id = ?, updated_at = ?
                    WHERE conversation_id = ?
                      AND (
                          last_read_event_id IS NULL
                          OR last_read_event_id < ?
                      )
                    """,
                    (target, now, clean_conversation_id, target),
                )
            row = self._get_conversation_row(connection, clean_conversation_id)
            if clean_emit_state:
                _insert_conversation_state_event(connection, row, now)
        return _conversation_dict(row)

    def append_conversation_state_event(
        self, conversation_id: str
    ) -> dict[str, Any]:
        """Append a full summary without treating publication as new activity."""
        clean_conversation_id = _id(conversation_id, "conversation_id")
        with self._connect() as connection:
            row = self._get_conversation_row(connection, clean_conversation_id)
            event = _insert_conversation_state_event(
                connection,
                row,
                _now_iso(),
            )
        return event

    # Composer attachments --------------------------------------------------

    def create_attachment(
        self,
        *,
        conversation_id: str,
        kind: str,
        display_name: str,
        media_type: str,
        byte_size: int,
        sha256: str,
        relative_path: str,
        attachment_id: str | None = None,
        remote_path: str | None = None,
        state: str = "staged",
    ) -> dict[str, Any]:
        clean_attachment_id = _id(
            attachment_id or uuid4().hex, "attachment_id"
        )
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clean_kind = _choice(kind, "attachment kind", ATTACHMENT_KINDS)
        clean_display_name = _text(display_name, "display_name", 500)
        clean_media_type = _text(media_type, "media_type", 255)
        clean_byte_size = _byte_size(byte_size)
        clean_sha256 = _sha256(sha256)
        clean_relative_path = _relative_path(relative_path)
        clean_remote_path = _optional_text(remote_path, "remote_path", 8192)
        clean_state = _choice(state, "attachment state", ATTACHMENT_STATES)
        now = _now_iso()
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_conversation_id)
            try:
                connection.execute(
                    """
                    INSERT INTO conversation_attachments(
                        attachment_id, conversation_id, kind, display_name,
                        media_type, byte_size, sha256, relative_path,
                        remote_path, state, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_attachment_id,
                        clean_conversation_id,
                        clean_kind,
                        clean_display_name,
                        clean_media_type,
                        clean_byte_size,
                        clean_sha256,
                        clean_relative_path,
                        clean_remote_path,
                        clean_state,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConversationError(
                    "attachment_conflict",
                    "That attachment identity is already registered.",
                    409,
                ) from exc
            row = self._get_attachment_row(connection, clean_attachment_id)
        return _attachment_dict(row)

    def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        clean_id = _id(attachment_id, "attachment_id")
        with self._connect() as connection:
            row = self._get_attachment_row(connection, clean_id, required=False)
        return _attachment_dict(row) if row is not None else None

    def list_attachments(
        self,
        conversation_id: str,
        *,
        kind: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        clean_conversation_id = _id(conversation_id, "conversation_id")
        clauses = ["conversation_id = ?"]
        values: list[Any] = [clean_conversation_id]
        if kind is not None:
            clauses.append("kind = ?")
            values.append(_choice(kind, "attachment kind", ATTACHMENT_KINDS))
        if state is not None:
            clauses.append("state = ?")
            values.append(_choice(state, "attachment state", ATTACHMENT_STATES))
        with self._connect() as connection:
            self._get_conversation_row(connection, clean_conversation_id)
            rows = connection.execute(
                f"SELECT {_ATTACHMENT_COLUMNS} FROM conversation_attachments "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at, attachment_id",
                values,
            ).fetchall()
        return [_attachment_dict(row) for row in rows]

    def update_attachment(
        self,
        attachment_id: str,
        *,
        remote_path: str | None | object = _UNSET,
        state: str | object = _UNSET,
    ) -> dict[str, Any]:
        clean_id = _id(attachment_id, "attachment_id")
        updates: dict[str, Any] = {}
        if remote_path is not _UNSET:
            updates["remote_path"] = _optional_text(
                remote_path, "remote_path", 8192
            )
        if state is not _UNSET:
            updates["state"] = _choice(
                state, "attachment state", ATTACHMENT_STATES
            )
        updates["updated_at"] = _now_iso()
        with self._connect() as connection:
            self._get_attachment_row(connection, clean_id)
            _update_row(
                connection,
                "conversation_attachments",
                "attachment_id",
                clean_id,
                updates,
            )
            row = self._get_attachment_row(connection, clean_id)
        return _attachment_dict(row)

    def delete_attachment(self, attachment_id: str) -> bool:
        clean_id = _id(attachment_id, "attachment_id")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversation_attachments WHERE attachment_id = ?",
                (clean_id,),
            )
        return cursor.rowcount == 1

    def clear_attachment_remote_path(
        self,
        attachment_id: str,
        *,
        expected_remote_path: str,
    ) -> bool:
        """Clear one remote generation only when its exact path is still current."""
        clean_id = _id(attachment_id, "attachment_id")
        clean_expected = _text(
            expected_remote_path, "expected_remote_path", 8192
        )
        now = _now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_attachments
                SET remote_path = NULL, updated_at = ?
                WHERE attachment_id = ? AND remote_path = ?
                """,
                (now, clean_id, clean_expected),
            )
        return cursor.rowcount == 1

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

    def list_pending_requests_for_session(
        self,
        owner_session_id: str,
        *,
        host_id: str | None = None,
        conversation_id: str | None = None,
        state: str | None = "pending",
    ) -> list[dict[str, Any]]:
        """Read global requests plus side-chat requests owned by one session."""
        clean_owner_session_id = _id(owner_session_id, "owner_session_id")
        clauses = [
            "("
            "request.conversation_id IS NULL "
            "OR conversation.kind IN ('pursuit', 'manager') "
            "OR (conversation.kind = 'side_chat' AND conversation.owner_session_id = ?)"
            ")"
        ]
        values: list[Any] = [clean_owner_session_id]
        if host_id is not None:
            clauses.append("request.host_id = ?")
            values.append(_id(host_id, "host_id"))
        if conversation_id is not None:
            clauses.append("request.conversation_id = ?")
            values.append(_id(conversation_id, "conversation_id"))
        if state is not None:
            clauses.append("request.state = ?")
            values.append(
                _choice(state, "pending request state", PENDING_REQUEST_STATES)
            )
        pending_columns = ", ".join(
            f"request.{column.strip()}" for column in _PENDING_COLUMNS.split(",")
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {pending_columns}
                FROM pending_server_requests AS request
                LEFT JOIN pursuit_conversations AS conversation
                  ON conversation.conversation_id = request.conversation_id
                WHERE {' AND '.join(clauses)}
                ORDER BY request.created_at, request.request_key
                """,
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
            connection.executescript(_SCHEMA_V7)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("PRAGMA journal_mode = WAL")
            return
        if version in {1, 2, 3, 4, 5, 6}:
            # DDL does not implicitly open a sqlite3 transaction. Take the
            # write lock, then re-read the version so concurrent initializers
            # cannot partially or repeatedly apply this operational upgrade.
            # The v6 parent-table rebuild must drop a table referenced by
            # every durable child table. Foreign-key enforcement has to be
            # disabled before BEGIN; validation runs before commit.
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                locked_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if locked_version == 1:
                    connection.execute(
                        "ALTER TABLE pursuit_conversations ADD COLUMN model TEXT"
                    )
                    connection.execute(
                        "ALTER TABLE pursuit_conversations ADD COLUMN reasoning_effort TEXT"
                    )
                    locked_version = 2
                if locked_version == 2:
                    connection.execute(
                        """
                        ALTER TABLE pursuit_conversations
                        ADD COLUMN kind TEXT NOT NULL DEFAULT 'pursuit'
                            CHECK(kind IN ('pursuit', 'side_chat'))
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE pursuit_conversations
                        ADD COLUMN parent_conversation_id TEXT
                            REFERENCES pursuit_conversations(conversation_id)
                            ON DELETE SET NULL
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE pursuit_conversations
                        ADD COLUMN last_final_event_id INTEGER
                            REFERENCES conversation_events(event_id)
                            ON DELETE SET NULL
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE pursuit_conversations
                        ADD COLUMN last_read_event_id INTEGER
                            REFERENCES conversation_events(event_id)
                            ON DELETE SET NULL
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS pursuit_conversations_by_kind
                        ON pursuit_conversations(kind, last_activity_at DESC)
                        """
                    )
                    connection.execute(_ATTACHMENT_TABLE_V5_SQL)
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS conversation_attachments_by_conversation
                        ON conversation_attachments(conversation_id, created_at, attachment_id)
                        """
                    )
                    locked_version = 3
                if locked_version == 3:
                    connection.execute(
                        """
                        ALTER TABLE pursuit_conversations
                        ADD COLUMN owner_session_id TEXT
                        """
                    )
                    connection.execute("PRAGMA user_version = 4")
                    locked_version = 4
                if locked_version == 4:
                    connection.execute(
                        """
                        ALTER TABLE conversation_events
                        ADD COLUMN marks_final INTEGER NOT NULL DEFAULT 0
                            CHECK(marks_final IN (0, 1))
                        """
                    )
                    # Version four knew only the latest final event for each
                    # conversation. Preserve every marker that can be
                    # recovered; later finals retain their own durable flag.
                    connection.execute(
                        """
                        UPDATE conversation_events
                        SET marks_final = 1
                        WHERE event_id IN (
                            SELECT last_final_event_id
                            FROM pursuit_conversations
                            WHERE last_final_event_id IS NOT NULL
                        )
                        """
                    )
                    connection.execute("PRAGMA user_version = 5")
                    locked_version = 5
                if locked_version == 5:
                    # SQLite cannot widen a CHECK constraint in place.
                    connection.execute(
                        "ALTER TABLE conversation_attachments "
                        "RENAME TO conversation_attachments_v5"
                    )
                    connection.execute(_ATTACHMENT_TABLE_V6_SQL)
                    connection.execute(
                        f"""
                        INSERT INTO conversation_attachments({_ATTACHMENT_COLUMNS})
                        SELECT {_ATTACHMENT_COLUMNS}
                        FROM conversation_attachments_v5
                        """
                    )
                    connection.execute("DROP TABLE conversation_attachments_v5")
                    connection.execute(
                        """
                        CREATE INDEX conversation_attachments_by_conversation
                        ON conversation_attachments(
                            conversation_id, created_at, attachment_id
                        )
                        """
                    )
                    connection.execute("PRAGMA user_version = 6")
                    locked_version = 6
                if locked_version == 6:
                    legacy_conversation_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM pursuit_conversations"
                        ).fetchone()[0]
                    )
                    connection.execute(_CONVERSATION_TABLE_V7_SQL)
                    connection.execute(
                        """
                        WITH legacy AS (
                            SELECT
                                conversation.*,
                                project.cwd AS execution_cwd_snapshot,
                                COALESCE(
                                    conversation.active_turn_id,
                                    (
                                        SELECT event.turn_id
                                        FROM conversation_events AS event
                                        WHERE event.conversation_id =
                                                conversation.conversation_id
                                          AND event.turn_id IS NOT NULL
                                        ORDER BY event.event_id
                                        LIMIT 1
                                    )
                                ) AS accepted_turn_id,
                                EXISTS(
                                    SELECT 1
                                    FROM conversation_events AS event
                                    WHERE event.conversation_id =
                                            conversation.conversation_id
                                      AND event.kind = 'user.message'
                                ) AS has_user_message
                            FROM pursuit_conversations AS conversation
                            JOIN conversation_projects AS project
                              ON project.project_id = conversation.project_id
                             AND project.host_id = conversation.host_id
                        )
                        INSERT INTO pursuit_conversations_v7(
                            conversation_id, kind, parent_conversation_id,
                            owner_session_id, pursuit_id,
                            pursuit_title_snapshot, host_id, project_id,
                            execution_cwd, provider, thread_id, thread_title,
                            model, reasoning_effort, lifecycle, status,
                            active_turn_id, initial_context_state,
                            initial_context_text,
                            initial_context_accepted_turn_id,
                            last_final_event_id, last_read_event_id,
                            created_at, updated_at, last_activity_at
                        )
                        SELECT
                            conversation_id, kind, parent_conversation_id,
                            owner_session_id, pursuit_id,
                            pursuit_title_snapshot, host_id, project_id,
                            execution_cwd_snapshot, provider, thread_id,
                            thread_title, model, reasoning_effort, lifecycle,
                            status, active_turn_id,
                            CASE
                                WHEN accepted_turn_id IS NOT NULL THEN 'accepted'
                                WHEN has_user_message THEN 'unknown'
                                ELSE 'eligible'
                            END,
                            NULL, accepted_turn_id, last_final_event_id,
                            last_read_event_id, created_at, updated_at,
                            last_activity_at
                        FROM legacy
                        """
                    )
                    copied_conversation_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM pursuit_conversations_v7"
                        ).fetchone()[0]
                    )
                    if copied_conversation_count != legacy_conversation_count:
                        raise ConversationError(
                            "storage_corrupt",
                            "The conversation database upgrade could not preserve every conversation.",
                            500,
                        )
                    connection.execute("DROP TABLE pursuit_conversations")
                    connection.execute(
                        "ALTER TABLE pursuit_conversations_v7 "
                        "RENAME TO pursuit_conversations"
                    )
                    connection.execute(
                        """
                        CREATE INDEX pursuit_conversations_by_pursuit
                        ON pursuit_conversations(
                            pursuit_id, last_activity_at DESC
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX pursuit_conversations_by_host
                        ON pursuit_conversations(host_id, last_activity_at DESC)
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX pursuit_conversations_by_kind
                        ON pursuit_conversations(kind, last_activity_at DESC)
                        """
                    )
                    connection.execute("PRAGMA user_version = 7")
                    locked_version = 7
                if locked_version != SCHEMA_VERSION:
                    raise ConversationError(
                        "unsupported_schema",
                        f"Unsupported conversation database schema version: {locked_version}.",
                        500,
                    )
                violations = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise ConversationError(
                        "storage_corrupt",
                        "The conversation database upgrade found invalid foreign keys.",
                        500,
                    )
                connection.commit()
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.execute("PRAGMA foreign_keys = ON")
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

    def _get_attachment_row(
        self,
        connection: sqlite3.Connection,
        attachment_id: str,
        *,
        required: bool = True,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            f"SELECT {_ATTACHMENT_COLUMNS} FROM conversation_attachments "
            "WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()
        if row is None and required:
            raise ConversationError(
                "attachment_not_found", "The conversation attachment was not found.", 404
            )
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
    ) -> sqlite3.Row:
        self._get_host_row(connection, host_id)
        project = self._get_project_row(connection, project_id)
        if project["host_id"] != host_id:
            raise ConversationError("project_host_mismatch", "The project does not belong to that host.", 422)
        return project

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
        kind=row["kind"],
        parent_conversation_id=row["parent_conversation_id"],
        pursuit_id=row["pursuit_id"],
        pursuit_title_snapshot=row["pursuit_title_snapshot"],
        host_id=row["host_id"],
        project_id=row["project_id"],
        execution_cwd=row["execution_cwd"],
        provider=row["provider"],
        thread_id=row["thread_id"],
        thread_title=row["thread_title"],
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        lifecycle=row["lifecycle"],
        status=row["status"],
        active_turn_id=row["active_turn_id"],
        initial_context_state=row["initial_context_state"],
        initial_context_text=row["initial_context_text"],
        initial_context_accepted_turn_id=row[
            "initial_context_accepted_turn_id"
        ],
        last_final_event_id=(
            int(row["last_final_event_id"])
            if row["last_final_event_id"] is not None
            else None
        ),
        last_read_event_id=(
            int(row["last_read_event_id"])
            if row["last_read_event_id"] is not None
            else None
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_activity_at=row["last_activity_at"],
    ).to_dict()


def _attachment_dict(row: sqlite3.Row) -> dict[str, Any]:
    return ConversationAttachment(
        attachment_id=row["attachment_id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        display_name=row["display_name"],
        media_type=row["media_type"],
        byte_size=int(row["byte_size"]),
        sha256=row["sha256"],
        relative_path=row["relative_path"],
        remote_path=row["remote_path"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    ).to_dict()


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    return ConversationEvent(
        event_id=int(row["event_id"]),
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        kind=row["kind"],
        payload=_decode_json_object(row["payload_json"]),
        created_at=row["created_at"],
        marks_final=bool(row["marks_final"]),
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


def _insert_conversation_state_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    created_at: str,
) -> dict[str, Any]:
    """Publish a summary change in the same transaction as its mutation."""
    conversation = _conversation_dict(row)
    conversation.pop("initial_context_text", None)
    payload_json = _json_object(
        {"conversation": conversation},
        "event payload",
        MAX_EVENT_PAYLOAD_BYTES,
    )
    cursor = connection.execute(
        """
        INSERT INTO conversation_events(
            conversation_id, turn_id, kind, payload_json, created_at,
            marks_final
        )
        VALUES(?, NULL, 'conversation.state', ?, ?, 0)
        """,
        (row["conversation_id"], payload_json, created_at),
    )
    event_row = connection.execute(
        f"SELECT {_EVENT_COLUMNS} FROM conversation_events WHERE event_id = ?",
        (int(cursor.lastrowid),),
    ).fetchone()
    return _event_dict(event_row)


def _require_conversation_event(
    connection: sqlite3.Connection,
    conversation_id: str,
    event_id: int,
    *,
    must_mark_final: bool = False,
) -> None:
    row = connection.execute(
        "SELECT conversation_id, marks_final FROM conversation_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ConversationError(
            "event_not_found", "The conversation event was not found.", 404
        )
    if row["conversation_id"] != conversation_id:
        raise ConversationError(
            "event_conversation_mismatch",
            "The event does not belong to that conversation.",
            422,
        )
    if must_mark_final and not bool(row["marks_final"]):
        raise ConversationError(
            "event_not_final",
            "Only a completed final response can be acknowledged as read.",
            422,
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


def _positive_event_id(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConversationError(
            "invalid_input", f"{field} must be a positive integer.", 422
        )
    return value


def _byte_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > 9_223_372_036_854_775_807
    ):
        raise ConversationError(
            "invalid_input", "byte_size must be a non-negative 64-bit integer.", 422
        )
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ConversationError(
            "invalid_input", "sha256 must be a 64-character lowercase digest.", 422
        )
    return value


def _relative_path(value: object) -> str:
    clean = _text(value, "relative_path", 8192).replace("\\", "/")
    if _WINDOWS_ABSOLUTE_RE.match(clean):
        raise ConversationError(
            "invalid_input", "relative_path must remain inside attachment storage.", 422
        )
    path = PurePosixPath(clean)
    if path.is_absolute() or ".." in path.parts:
        raise ConversationError(
            "invalid_input", "relative_path must remain inside attachment storage.", 422
        )
    normalized = str(path)
    if normalized in {"", "."}:
        raise ConversationError(
            "invalid_input", "relative_path must name a stored attachment.", 422
        )
    return normalized


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


def _initial_context_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConversationError(
            "invalid_input", "initial context must be non-empty text.", 422
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConversationError(
            "invalid_input", "initial context must be valid UTF-8 text.", 422
        ) from exc
    if len(encoded) > MAX_INITIAL_CONTEXT_BYTES:
        raise ConversationError(
            "payload_too_large",
            f"initial context exceeds the {MAX_INITIAL_CONTEXT_BYTES}-byte storage limit.",
            413,
        )
    return value


def _raise_initial_context_transition(current: str, requested: str) -> None:
    raise ConversationError(
        "initial_context_conflict",
        f"Initial context cannot move from {current} to {requested}.",
        409,
    )


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


def provider_turn_id_fingerprint(turn_ids: Iterable[str]) -> dict[str, Any]:
    """Fingerprint one provider-turn membership set in deterministic order."""
    clean_ids = sorted({_id(turn_id, "turn_id") for turn_id in turn_ids})
    digest = hashlib.sha256()
    for turn_id in clean_ids:
        encoded = turn_id.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return {"count": len(clean_ids), "sha256": digest.hexdigest()}


def _session_scope(owner_session_id: str) -> str:
    return hashlib.sha256(owner_session_id.encode("utf-8")).hexdigest()


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


_SCHEMA_V7 = """
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
    kind TEXT NOT NULL DEFAULT 'pursuit'
        CHECK(kind IN ('pursuit', 'side_chat', 'manager')),
    parent_conversation_id TEXT,
    owner_session_id TEXT,
    pursuit_id TEXT,
    pursuit_title_snapshot TEXT,
    host_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    execution_cwd TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'codex' CHECK(provider = 'codex'),
    thread_id TEXT NOT NULL,
    thread_title TEXT,
    model TEXT,
    reasoning_effort TEXT,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'archived')),
    status TEXT NOT NULL CHECK(status IN (
        'idle', 'starting', 'running', 'waiting_approval', 'waiting_input',
        'completed', 'failed', 'interrupted', 'unknown'
    )),
    active_turn_id TEXT,
    initial_context_state TEXT NOT NULL DEFAULT 'eligible' CHECK(
        initial_context_state IN (
            'eligible', 'prepared', 'unknown', 'accepted', 'skipped'
        )
    ),
    initial_context_text TEXT,
    initial_context_accepted_turn_id TEXT,
    last_final_event_id INTEGER,
    last_read_event_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    CHECK(
        (kind = 'manager' AND pursuit_id IS NULL)
        OR (kind IN ('pursuit', 'side_chat') AND pursuit_id IS NOT NULL)
    ),
    UNIQUE(host_id, thread_id),
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id, host_id)
        REFERENCES conversation_projects(project_id, host_id) ON DELETE RESTRICT,
    FOREIGN KEY(parent_conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE SET NULL,
    FOREIGN KEY(last_final_event_id)
        REFERENCES conversation_events(event_id) ON DELETE SET NULL,
    FOREIGN KEY(last_read_event_id)
        REFERENCES conversation_events(event_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS pursuit_conversations_by_pursuit
    ON pursuit_conversations(pursuit_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS pursuit_conversations_by_host
    ON pursuit_conversations(host_id, last_activity_at DESC);
CREATE INDEX IF NOT EXISTS pursuit_conversations_by_kind
    ON pursuit_conversations(kind, last_activity_at DESC);

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
    marks_final INTEGER NOT NULL DEFAULT 0 CHECK(marks_final IN (0, 1)),
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS conversation_events_by_conversation
    ON conversation_events(conversation_id, event_id);

CREATE TABLE IF NOT EXISTS conversation_attachments(
    attachment_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'pasted_text', 'file')),
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    remote_path TEXT,
    state TEXT NOT NULL CHECK(state IN ('staged', 'sent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS conversation_attachments_by_conversation
    ON conversation_attachments(conversation_id, created_at, attachment_id);

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


_CONVERSATION_TABLE_V7_SQL = """
CREATE TABLE pursuit_conversations_v7(
    conversation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'pursuit'
        CHECK(kind IN ('pursuit', 'side_chat', 'manager')),
    parent_conversation_id TEXT,
    owner_session_id TEXT,
    pursuit_id TEXT,
    pursuit_title_snapshot TEXT,
    host_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    execution_cwd TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'codex' CHECK(provider = 'codex'),
    thread_id TEXT NOT NULL,
    thread_title TEXT,
    model TEXT,
    reasoning_effort TEXT,
    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'archived')),
    status TEXT NOT NULL CHECK(status IN (
        'idle', 'starting', 'running', 'waiting_approval', 'waiting_input',
        'completed', 'failed', 'interrupted', 'unknown'
    )),
    active_turn_id TEXT,
    initial_context_state TEXT NOT NULL DEFAULT 'eligible' CHECK(
        initial_context_state IN (
            'eligible', 'prepared', 'unknown', 'accepted', 'skipped'
        )
    ),
    initial_context_text TEXT,
    initial_context_accepted_turn_id TEXT,
    last_final_event_id INTEGER,
    last_read_event_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    CHECK(
        (kind = 'manager' AND pursuit_id IS NULL)
        OR (kind IN ('pursuit', 'side_chat') AND pursuit_id IS NOT NULL)
    ),
    UNIQUE(host_id, thread_id),
    FOREIGN KEY(host_id) REFERENCES conversation_hosts(host_id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id, host_id)
        REFERENCES conversation_projects(project_id, host_id) ON DELETE RESTRICT,
    FOREIGN KEY(parent_conversation_id)
        REFERENCES pursuit_conversations_v7(conversation_id) ON DELETE SET NULL,
    FOREIGN KEY(last_final_event_id)
        REFERENCES conversation_events(event_id) ON DELETE SET NULL,
    FOREIGN KEY(last_read_event_id)
        REFERENCES conversation_events(event_id) ON DELETE SET NULL
)
"""


_ATTACHMENT_TABLE_V5_SQL = """
CREATE TABLE IF NOT EXISTS conversation_attachments(
    attachment_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'pasted_text')),
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    remote_path TEXT,
    state TEXT NOT NULL CHECK(state IN ('staged', 'sent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE CASCADE
)
"""


_ATTACHMENT_TABLE_V6_SQL = """
CREATE TABLE conversation_attachments(
    attachment_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'pasted_text', 'file')),
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    sha256 TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    remote_path TEXT,
    state TEXT NOT NULL CHECK(state IN ('staged', 'sent')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id)
        REFERENCES pursuit_conversations(conversation_id) ON DELETE CASCADE
)
"""
