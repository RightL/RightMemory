from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..session import MemoryWriteLock
from ..shared_views import (
    accept_shared_view_invitation,
    build_shared_view,
    define_shared_view,
    export_shared_view,
    list_shared_view_notes,
    load_connections,
    load_shared_view_definition,
    publish_shared_view,
    record_shared_view_note,
    retrieve_shared_view,
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
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser()

    def session_data(self, *, authenticated: bool, csrf_token: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "authenticated": authenticated,
            "active_root": str(self.memory_root),
        }
        if csrf_token:
            data["csrf_token"] = csrf_token
        return data

    def overview(self) -> dict[str, Any]:
        status = collect_status(self.memory_root)
        return {
            "active_root": str(self.memory_root),
            "git": _json_safe(status.git),
            "watches": [_json_safe(watch) for watch in status.watches],
            "dreamer": _json_safe(status.dreamer),
            "insight": _json_safe(status.insight),
            "update": _json_safe(status.update),
            "issues": list(status.issues),
        }

    def status(self) -> dict[str, Any]:
        status = collect_status(self.memory_root)
        return _json_safe(status)

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
        return {**artifact.summary(self.memory_root), "text": read_artifact_text(artifact)}

    def shared_views(self) -> dict[str, Any]:
        provider_views = []
        provider_root = self.memory_root / "shared_views"
        if provider_root.is_dir():
            for metadata in sorted(provider_root.glob("*/export.toml")):
                try:
                    provider_views.append(_json_safe(load_shared_view_definition(self.memory_root, metadata.parent.name)))
                except Exception as exc:
                    provider_views.append(
                        {
                            "view_id": metadata.parent.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        return {
            "provider_views": provider_views,
            "connections": [_json_safe(connection) for connection in load_connections(self.memory_root).values()],
        }

    def define_view(self, payload: dict[str, Any]) -> str:
        return define_shared_view(
            self.memory_root,
            view_id=_required_payload_str(payload, "view_id"),
            title=_required_payload_str(payload, "title"),
            description=_optional_payload_str(payload, "description"),
            audience=_optional_payload_str(payload, "audience"),
            maintainer=_optional_payload_str(payload, "maintainer"),
            retriever_instructions=_optional_payload_str(payload, "instructions"),
            source_globs=_optional_payload_str_list(payload, "source_globs"),
            filter_terms=_optional_payload_str_list(payload, "filter_terms"),
            include_all=bool(payload.get("include_all", False)),
            ref=_optional_payload_str(payload, "ref"),
        )

    def build_view(self, view_id: str, payload: dict[str, Any]) -> str:
        return build_shared_view(
            self.memory_root,
            view_id,
            query=_optional_payload_str(payload, "query"),
            context_lines=_optional_payload_int(payload, "context_lines", 0),
            limit=_optional_payload_int(payload, "limit", 200),
        )

    def export_view(self, view_id: str, payload: dict[str, Any]) -> str:
        return export_shared_view(
            self.memory_root,
            view_id,
            Path(_required_payload_str(payload, "target")),
            replace=bool(payload.get("replace", False)),
            query=_optional_payload_str(payload, "query"),
        )

    def publish_view(self, view_id: str, payload: dict[str, Any]) -> str:
        kind = _optional_payload_str(payload, "kind") or "mounted"
        if kind != "mounted":
            raise ValueError(f"unsupported shared-view publish target for this server: {kind}")
        return publish_shared_view(
            self.memory_root,
            view_id,
            Path(_required_payload_str(payload, "hub")),
            replace=bool(payload.get("replace", False)),
            query=_optional_payload_str(payload, "query"),
        )

    def accept_invite(self, payload: dict[str, Any]) -> str:
        with MemoryWriteLock(self.memory_root):
            return accept_shared_view_invitation(
                self.memory_root,
                Path(_required_payload_str(payload, "invitation")),
                heading_id=_optional_payload_str(payload, "heading_id"),
                title=_optional_payload_str(payload, "title"),
                body=_optional_payload_str(payload, "body"),
                relationship=_optional_payload_str(payload, "relationship"),
                copy_package=not bool(payload.get("no_copy_package", False)),
            )

    def retrieve_connection(self, heading_id: str, payload: dict[str, Any]) -> str:
        return retrieve_shared_view(self.memory_root, heading_id, _required_payload_str(payload, "query"))

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

    def set_active_root(self, root: Path) -> dict[str, Any]:
        resolved = Path(root).expanduser()
        if not resolved.exists():
            raise ValueError(f"active root does not exist: {resolved}")
        self.memory_root = resolved
        return {"active_root": str(self.memory_root)}


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
