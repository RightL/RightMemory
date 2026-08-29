from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = 1
LOCAL_HOST_ID = "local"
DEFAULT_LOCAL_PROJECT_ID = "local-root"

HOST_KINDS = frozenset({"local", "ssh"})
CONVERSATION_LIFECYCLES = frozenset({"active", "archived"})
CONVERSATION_STATUSES = frozenset(
    {
        "idle",
        "starting",
        "running",
        "waiting_approval",
        "waiting_input",
        "completed",
        "failed",
        "interrupted",
        "unknown",
    }
)
PENDING_REQUEST_STATES = frozenset({"pending", "resolved", "stale"})


class ConversationError(RuntimeError):
    """Stable error information for conversation services and Web routes."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ConversationHost:
    host_id: str
    kind: str
    display_name: str
    ssh_alias: str | None
    codex_command_override: str | None
    platform_hint: str | None
    app_server_version: str | None
    codex_version: str | None
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_seen_at: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConversationProject:
    project_id: str
    host_id: str
    label: str
    cwd: str
    last_used_at: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PursuitConversation:
    conversation_id: str
    pursuit_id: str
    pursuit_title_snapshot: str | None
    host_id: str
    project_id: str
    provider: str
    thread_id: str
    thread_title: str | None
    lifecycle: str
    status: str
    active_turn_id: str | None
    created_at: str
    updated_at: str
    last_activity_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: int
    conversation_id: str | None
    turn_id: str | None
    kind: str
    payload: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PursuitConversationDefault:
    pursuit_id: str
    host_id: str
    project_id: str
    last_used_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PendingServerRequest:
    request_key: str
    host_id: str
    connection_epoch: str
    rpc_id: str | int
    conversation_id: str | None
    thread_id: str | None
    method: str
    payload: dict[str, Any]
    state: str
    created_at: str
    resolved_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
