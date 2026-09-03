from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..git_share_transport import is_git_share_url
from ..config import (
    ROLES,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_insight_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
from ..opening_context import OpeningContextError, build_opening_context
from ..pursuit_store import PursuitStore, PursuitStoreError
from ..session import MemoryWriteLock
from ..share_models import ShareRelationship, load_shares
from ..share_results import capability_from_parts
from ..shares import create_share_from_request, join_share, publish_share, revise_share
from ..shared_view_builder import run_file_view_builder, run_question_view_builder
from ..shared_view_files import (
    approve_file_view,
    invite_file_view,
    list_file_view_publish_events,
    pull_all_file_views,
    pull_file_view,
)
from ..shared_view_models import load_shared_view_credential, list_shared_view_credentials
from ..shared_view_questions import answer_question_view, approve_question_view, ask_question_view, publish_question_view
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
from ..status import collect_status
from .readers import (
    list_insight_artifacts,
    list_log_artifacts,
    list_memory_artifacts,
    read_artifact_text,
    resolve_artifact,
)


class WebStudioService:
    def __init__(self, memory_root: Path, *, allowed_root: Path | None = None, pursuit_batches=None):
        self.memory_root = Path(memory_root).expanduser().resolve()
        self.allowed_root = Path(allowed_root).expanduser().resolve() if allowed_root is not None else self.memory_root
        self.pursuit_batches = pursuit_batches

    def session_data(self, *, csrf_token: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "active_root": str(self.memory_root),
            "csrf_token": csrf_token,
        }
        return data

    def overview(self) -> dict[str, Any]:
        status = collect_status(self.memory_root)
        shared_views = self.shared_views()
        notes = list_shared_view_notes(self.memory_root)
        inbox = list_shared_view_inbox(self.memory_root)
        return {
            "active_root": str(self.memory_root),
            "git": _json_safe(status.git),
            "watches": [_json_safe(watch) for watch in status.watches],
            "dreamer": _json_safe(status.dreamer),
            "insight": _json_safe(status.insight),
            "update": _json_safe(status.update),
            "shared_views": {
                "provider_view_count": len(shared_views["provider_views"]),
                "connection_count": len(shared_views["connections"]),
                "note_count": len(notes),
                "inbox_count": len(inbox),
            },
            "issues": list(status.issues),
        }

    def status(self) -> dict[str, Any]:
        status = collect_status(self.memory_root)
        return _json_safe(status)

    def pursuit_map(self, session_id: str | None = None) -> dict[str, Any]:
        snapshot = PursuitStore(self.memory_root).snapshot(session_id=session_id)
        failure = self.pursuit_batches.failure(self.memory_root) if self.pursuit_batches else None
        if failure:
            snapshot["diagnostics"] = [*snapshot["diagnostics"], failure]
            snapshot["writable"] = False
            snapshot["recovery"] = True
        return snapshot

    def pursuit_context(self, item_id: str, expected_revision: str, session_id: str) -> dict[str, str]:
        snapshot = self.pursuit_map(session_id)
        if snapshot["revision"] != expected_revision:
            raise PursuitStoreError("conflict", "The map changed. Reload it before copying context.", 409)
        store = PursuitStore(self.memory_root)
        if store.pending_state().get("pending"):
            self.flush_pursuit_batch({"expected_revision": expected_revision}, session_id)
            snapshot = self.pursuit_map(session_id)
        if not snapshot["valid"]:
            raise PursuitStoreError(
                "invalid_root", "The map has validation errors.", 422,
                diagnostics=snapshot["diagnostics"],
            )
        item = next((item for item in snapshot["items"] if item["id"] == item_id), None)
        if item is None:
            raise PursuitStoreError("not_found", "The selected Pursuit no longer exists.", 404)
        try:
            context = build_opening_context(self.memory_root, item)
        except OpeningContextError as exc:
            raise PursuitStoreError("conflict", "The map changed. Reload it before copying context.", 409) from exc
        if self.pursuit_map(session_id)["revision"] != snapshot["revision"]:
            raise PursuitStoreError("conflict", "The map changed. Reload it before copying context.", 409)
        return {"text": context.text}

    def apply_pursuit_operation(self, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        expected_revision = _required_payload_str(payload, "expected_revision")
        operation = payload.get("operation")
        if not isinstance(operation, dict):
            raise ValueError("operation must be an object")
        return PursuitStore(self.memory_root).apply(
            operation, expected_revision=expected_revision, session_id=session_id,
        )

    def undo_pursuit_operation(self, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        return PursuitStore(self.memory_root).undo(
            _required_payload_str(payload, "operation_id"),
            expected_revision=_required_payload_str(payload, "expected_revision"),
            session_id=session_id,
        )

    def redo_pursuit_operation(self, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        return PursuitStore(self.memory_root).redo(
            _required_payload_str(payload, "operation_id"),
            expected_revision=_required_payload_str(payload, "expected_revision"),
            session_id=session_id,
        )

    def flush_pursuit_batch(self, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        result = PursuitStore(self.memory_root).flush(
            session_id=session_id,
            expected_revision=_optional_payload_str(payload, "expected_revision"),
        )
        if self.pursuit_batches:
            self.pursuit_batches.clear_failure(self.memory_root)
        return result

    def pursuit_activity(self, session_id: str) -> dict[str, bool]:
        store = PursuitStore(self.memory_root)
        if store.pending_state().get("pending"):
            if not store.owns_pending(session_id):
                raise PursuitStoreError(
                    "session_conflict", "Another browser is editing this map. Finish its saved edits before editing here.", 409,
                )
            if self.pursuit_batches:
                self.pursuit_batches.activity(self.memory_root)
        return {"ok": True}

    def finish_pursuit_session(self, session_id: str) -> None:
        if PursuitStore(self.memory_root).owns_pending(session_id):
            self.flush_pursuit_batch({}, session_id)

    def settings(self) -> dict[str, Any]:
        config_path = self.memory_root / "rightmemory.toml"
        return {
            "active_root": str(self.memory_root),
            "config_path": str(config_path),
            "config_exists": config_path.is_file(),
            "runtime": {
                "review": _settings_loader(load_review_config, self.memory_root),
                "update": _settings_loader(load_async_update_config, self.memory_root),
                "dreamer_watch": _settings_loader(load_dreamer_watch_config, self.memory_root),
                "insight_watch": _settings_loader(load_insight_watch_config, self.memory_root),
                "pruner": _settings_loader(load_pruner_config, self.memory_root),
                "sync": _settings_loader(load_sync_config, self.memory_root),
            },
            "roles": [_role_settings(role, self.memory_root) for role in sorted(ROLES)],
        }

    def memory_files(self) -> dict[str, Any]:
        artifacts = list_memory_artifacts(self.memory_root)
        return {"files": [artifact.summary(self.memory_root) for artifact in artifacts]}

    def memory_file(self, file_id: str) -> dict[str, Any] | None:
        artifact = resolve_artifact(list_memory_artifacts(self.memory_root), file_id)
        if artifact is None:
            return None
        return {**artifact.summary(self.memory_root), "text": read_artifact_text(artifact)}

    def insights(self) -> dict[str, Any]:
        artifacts = list_insight_artifacts(self.memory_root)
        return {"insights": [artifact.summary(self.memory_root) for artifact in artifacts]}

    def insight(self, insight_id: str) -> dict[str, Any] | None:
        artifact = resolve_artifact(list_insight_artifacts(self.memory_root), insight_id)
        if artifact is None:
            return None
        return {**artifact.summary(self.memory_root), "text": read_artifact_text(artifact)}

    def logs(self) -> dict[str, Any]:
        artifacts = list_log_artifacts(self.memory_root)
        return {"logs": [artifact.summary(self.memory_root) for artifact in artifacts]}

    def log(self, log_id: str) -> dict[str, Any] | None:
        artifact = resolve_artifact(list_log_artifacts(self.memory_root), log_id)
        if artifact is None:
            return None
        summary = artifact.summary(self.memory_root)
        try:
            return {**summary, "missing": False, "text": read_artifact_text(artifact)}
        except FileNotFoundError:
            return {**summary, "missing": True, "text": ""}

    def shared_views(self) -> dict[str, Any]:
        return {
            "provider_views": provider_view_summaries(self.memory_root),
            "connections": [_json_safe(connection) for connection in load_connections(self.memory_root).values()],
            "credentials": list_shared_view_credentials(self.memory_root),
        }

    def share_relationships(self) -> dict[str, Any]:
        shares = load_shares(self.memory_root)
        return {"relationships": [_share_summary(self.memory_root, shares[share_id]) for share_id in sorted(shares)]}

    def create_share_relationship(self, payload: dict[str, Any]) -> dict[str, Any]:
        transport = _optional_payload_str(payload, "transport") or "http"
        if transport == "git":
            result = create_share_from_request(
                self.memory_root,
                share_id=_optional_payload_str(payload, "share_id"),
                title=_optional_payload_str(payload, "title"),
                request=_required_payload_str(payload, "request"),
                provider_id=_required_payload_str(payload, "provider_id"),
                hub_url=None,
                credential_id=None,
                capability="file_context",
                question_base_url=None,
                git_url=_required_payload_str(payload, "git_url"),
                git_branch=_optional_payload_str(payload, "git_branch"),
            )
        elif transport == "http":
            result = create_share_from_request(
                self.memory_root,
                share_id=_optional_payload_str(payload, "share_id"),
                title=_optional_payload_str(payload, "title"),
                request=_required_payload_str(payload, "request"),
                provider_id=_required_payload_str(payload, "provider_id"),
                hub_url=_required_payload_str(payload, "hub_url"),
                credential_id=_required_payload_str(payload, "credential_id"),
                capability=_optional_payload_str(payload, "capability") or "auto",
                question_base_url=_optional_payload_str(payload, "question_base_url"),
            )
        else:
            raise ValueError("share transport must be http or git")
        return result.to_json()

    def revise_share_relationship(self, share_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = revise_share(
            self.memory_root,
            share_id,
            _required_payload_str(payload, "revision"),
            capability=_optional_payload_str(payload, "capability"),
            question_base_url=_optional_payload_str(payload, "question_base_url"),
        )
        return result.to_json()

    def publish_share_relationship(self, share_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        message = publish_share(
            self.memory_root,
            share_id,
            label=_optional_payload_str(payload, "label"),
            expires_at=_optional_payload_str(payload, "expires_at"),
            git_url=_optional_payload_str(payload, "git_url"),
            git_branch=_optional_payload_str(payload, "git_branch"),
            push=not bool(payload.get("no_push", False)),
        )
        return {"message": message}

    def build_file_view(self, payload: dict[str, Any]) -> str:
        return run_file_view_builder(
            self.memory_root,
            view_id=_required_payload_str(payload, "view_id"),
            title=_required_payload_str(payload, "title"),
            intent=_required_payload_str(payload, "intent"),
            hub_url=_required_payload_str(payload, "hub_url"),
            credential_id=_required_payload_str(payload, "credential_id"),
        )

    def build_question_view(self, payload: dict[str, Any]) -> str:
        return run_question_view_builder(
            self.memory_root,
            view_id=_required_payload_str(payload, "view_id"),
            title=_required_payload_str(payload, "title"),
            intent=_required_payload_str(payload, "intent"),
        )

    def approve_view(self, view_id: str, payload: dict[str, Any]) -> str:
        view_type = _required_payload_str(payload, "type")
        if view_type == "file":
            return approve_file_view(self.memory_root, view_id)
        if view_type == "question":
            return approve_question_view(self.memory_root, view_id)
        raise ValueError("shared view approve type must be file or question")

    def invite_file_view(self, view_id: str, payload: dict[str, Any]) -> str:
        return invite_file_view(
            self.memory_root,
            view_id,
            hub_url=_optional_payload_str(payload, "hub_url"),
            credential_id=_optional_payload_str(payload, "credential_id"),
            label=_optional_payload_str(payload, "label"),
            expires_at=_optional_payload_str(payload, "expires_at"),
        )

    def publish_question_view(self, view_id: str, payload: dict[str, Any]) -> str:
        return publish_question_view(
            self.memory_root,
            view_id,
            hub_url=_required_payload_str(payload, "hub_url"),
            credential_id=_required_payload_str(payload, "credential_id"),
            question_base_url=_required_payload_str(payload, "question_base_url"),
            label=_optional_payload_str(payload, "label"),
            expires_at=_optional_payload_str(payload, "expires_at"),
        )

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

    def save_credential(self, payload: dict[str, Any]) -> str:
        credential_id = _required_payload_str(payload, "credential_id")
        save_shared_view_credential(
            self.memory_root,
            credential_id,
            kind=_optional_payload_str(payload, "kind") or "http-publish",
            token=_required_payload_str(payload, "token"),
            base_url=_required_payload_str(payload, "hub_url"),
            view_id=_optional_payload_str(payload, "view_id"),
            provider_id=_optional_payload_str(payload, "provider_id"),
        )
        return f"saved shared view credential {credential_id}"

    def accept_invite(self, payload: dict[str, Any]) -> str:
        invitation = _required_payload_str(payload, "invitation")
        with MemoryWriteLock(self.memory_root):
            if is_git_share_url(invitation) or "/i/share/" in invitation:
                return join_share(self.memory_root, invitation)
            if _is_http_url(invitation):
                return accept_http_shared_view_invitation(
                    self.memory_root,
                    invitation,
                    heading_id=_optional_payload_str(payload, "heading_id"),
                    title=_optional_payload_str(payload, "title"),
                    body=_optional_payload_str(payload, "body"),
                    relationship=_optional_payload_str(payload, "relationship"),
                )
            return accept_shared_view_invitation(
                self.memory_root,
                Path(invitation),
            )

    def pull_connection(self, heading_id: str) -> str:
        result = pull_file_view(self.memory_root, heading_id)
        return result.message

    def pull_all_connections(self) -> dict[str, Any]:
        return {"results": [_json_safe(result) for result in pull_all_file_views(self.memory_root)]}

    def connection_status(self, heading_id: str) -> dict[str, Any]:
        return shared_view_connection_status(self.memory_root, heading_id)

    def connection_statuses(self) -> dict[str, Any]:
        return {
            "statuses": [
                shared_view_connection_status(self.memory_root, connection.heading_id)
                for connection in load_connections(self.memory_root).values()
            ]
        }

    def ask_connection(self, heading_id: str, payload: dict[str, Any]) -> str:
        return ask_question_view(self.memory_root, heading_id, _required_payload_str(payload, "question"))

    def answer_question_view(self, view_id: str, payload: dict[str, Any]) -> str:
        return answer_question_view(self.memory_root, view_id, _required_payload_str(payload, "question"))

    def note_connection(self, heading_id: str, payload: dict[str, Any]) -> str:
        return record_shared_view_note(
            self.memory_root,
            heading_id,
            _required_payload_str(payload, "message"),
            confirmed=bool(payload.get("confirmed", False)),
            actor=_optional_payload_str(payload, "actor") or "user",
            task_context=_optional_payload_str(payload, "task_context"),
        )

    def notes(self, heading_id: str) -> dict[str, Any]:
        return {"notes": list_shared_view_notes(self.memory_root, heading_id)}

    def activity(self) -> dict[str, Any]:
        return {
            "notes": list_shared_view_notes(self.memory_root),
            "inbox": list_shared_view_inbox(self.memory_root),
        }

    def set_active_root(self, root: Path) -> dict[str, Any]:
        resolved = resolve_allowed_memory_root(self.allowed_root, root)
        return {"active_root": str(resolved)}


class PursuitBatchLifecycle:
    """Finish durable editor batches even when a browser disappears."""

    IDLE_SECONDS = 5.0
    MAX_SECONDS = 60.0

    def __init__(self, allowed_root: Path):
        self.allowed_root = Path(allowed_root).resolve()
        self._roots = {self.allowed_root}
        self._activity: dict[Path, float] = {}
        self._failures: dict[Path, str] = {}
        self._retry_after: dict[Path, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, root: Path) -> None:
        with self._lock:
            self._roots.add(root)

    def activity(self, root: Path) -> None:
        with self._lock:
            self._activity[root] = time.time()

    def failure(self, root: Path) -> str | None:
        with self._lock:
            return self._failures.get(root)

    def clear_failure(self, root: Path) -> None:
        with self._lock:
            self._failures.pop(root, None)
            self._retry_after.pop(root, None)

    def start(self) -> None:
        # Discover nested roots once on startup, including journals left by expired sessions.
        for directory, children, files in os.walk(self.allowed_root):
            children[:] = [name for name in children if name not in {
                ".git", ".runtime", ".worktree", ".worktrees", "node_modules", ".venv",
            }]
            if "MEMORY.md" in files:
                self.register(resolve_allowed_memory_root(self.allowed_root, directory))
        self.flush_due(force=True)
        self._thread = threading.Thread(target=self._run, name="pursuit-autosave", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        self.flush_due(force=True)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self.flush_due()

    def flush_due(self, *, now: float | None = None, force: bool = False) -> None:
        now = time.time() if now is None else now
        with self._lock:
            roots = list(self._roots)
        for root in roots:
            with self._lock:
                activity = self._activity.get(root, 0.0)
                retry_after = self._retry_after.get(root, 0.0)
            if not force and now < retry_after:
                continue
            try:
                store = PursuitStore(root)
                pending = store.pending_state()
                if not pending.get("pending"):
                    self.clear_failure(root)
                    continue
                idle = now - max(float(pending["updated_at"]), activity)
                duration = now - float(pending["started_at"])
                if not force and idle < self.IDLE_SECONDS and duration < self.MAX_SECONDS:
                    continue
                store.flush_pending()
                self.clear_failure(root)
            except (PursuitStoreError, OSError, ValueError, RuntimeError) as exc:
                message = f"Saved edits could not be committed: {exc} Recovery data is retained; resolve the reported condition and retry."
                with self._lock:
                    previous = self._failures.get(root)
                    self._failures[root] = message
                    self._retry_after[root] = now + self.MAX_SECONDS
                if previous != message:
                    logging.getLogger(__name__).warning("Pursuit autosave for %s: %s", root, message)


def resolve_allowed_memory_root(allowed_root: Path, candidate: Path | str) -> Path:
    base = Path(allowed_root).expanduser().resolve()
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"active root does not exist: {resolved}")
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"active root is outside the configured Web Studio root: {resolved}") from exc
    if not (resolved / "MEMORY.md").is_file():
        raise ValueError(f"active root must contain MEMORY.md: {resolved}")
    return resolved


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _share_summary(memory_root: Path, share: ShareRelationship) -> dict[str, Any]:
    data = _json_safe(share)
    data["capability"] = capability_from_parts(share.parts)
    invitation_url = _runtime_invitation_url(memory_root, share.share_id)
    if invitation_url:
        data["invitation_url"] = invitation_url
    return data


def _runtime_invitation_url(memory_root: Path, share_id: str) -> str | None:
    path = Path(memory_root).expanduser() / ".runtime" / "shares" / f"{share_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = payload.get("invitation_url") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


def _settings_loader(loader, memory_root: Path) -> dict[str, Any]:
    try:
        return {"ok": True, "value": _json_safe(loader(memory_root))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _role_settings(role: str, memory_root: Path) -> dict[str, Any]:
    try:
        config = load_config(role, memory_root=memory_root)
    except Exception as exc:
        return {"role": role, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if config.runtime_mode == "standalone":
        executor = {
            "mode": "standalone",
            "model_id": config.model_id,
            "api_base": config.api_base,
            "api_key": "configured" if config.api_key else "not configured",
        }
    else:
        executor = {
            "mode": "cli-agent",
            "provider": config.agent_cli.provider if config.agent_cli else None,
            "model": config.agent_cli.model if config.agent_cli else None,
            "reasoning_effort": config.agent_cli.reasoning_effort if config.agent_cli else None,
        }
    return {
        "role": role,
        "ok": True,
        "executor": executor,
        "debug_trace": config.debug_trace,
        "max_tool_retries": config.max_tool_retries,
    }


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required field: {key}")
    return value.strip()


def _optional_payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    return value or None


def _optional_payload_str_list(payload: dict[str, Any], key: str) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of strings")
    return [item for item in (item.strip() for item in value) if item]


def _optional_payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value
