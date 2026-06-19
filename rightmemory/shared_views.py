from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .hub.client import HubClient, HubClientError
from .shared_view_models import (
    PROVIDER_VIEWS_DIR,
    RUNTIME_DIR,
    REGISTRY_FILE,
    RELATIONSHIPS,
    SharedViewConnection,
    SharedViewTarget,
    load_connections,
    load_shared_view_credential,
    save_connections,
    save_shared_view_credential,
    validate_heading_id,
)


def accept_http_shared_view_invitation(
    memory_root: Path,
    invitation_url: str,
    *,
    heading_id: str | None = None,
    title: str | None = None,
    body: str | None = None,
    relationship: str | None = None,
    credential_id: str | None = None,
    consumer_label: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    base_url, invite_token = _parse_http_invitation_url(invitation_url)
    client = HubClient(base_url)
    view_info = client.get_invitation_view(invite_token)
    view_type = _view_type_from_invitation(view_info)
    target_kind = "http-file" if view_type == "file" else "http-question"
    question_base_url = _question_base_url_from_invitation(view_info) if view_type == "question" else None
    accepted = client.accept_invitation(invite_token, consumer_label=consumer_label)
    remote_view_id = validate_heading_id(str(accepted.get("view_id") or view_info.get("view_id")))
    local_heading_id = validate_heading_id(heading_id or remote_view_id)
    local_credential_id = validate_heading_id(credential_id or f"http-{local_heading_id}")
    connection_token = accepted.get("connection_token")
    if not isinstance(connection_token, str) or not connection_token:
        raise ValueError("HTTP shared-view invitation did not return a connection_token")
    question_token: str | None = None
    question_credential_id: str | None = None
    if view_type == "question":
        raw_question_token = accepted.get("question_token")
        if not isinstance(raw_question_token, str) or not raw_question_token:
            raise ValueError("HTTP shared-view invitation did not return a question_token")
        question_token = raw_question_token
        question_credential_id = validate_heading_id(f"{local_credential_id}-question")
    save_shared_view_credential(
        root,
        local_credential_id,
        kind="http-connection",
        token=connection_token,
        base_url=base_url,
        view_id=remote_view_id,
    )
    if question_token and question_credential_id and question_base_url:
        save_shared_view_credential(
            root,
            question_credential_id,
            kind="http-question",
            token=question_token,
            base_url=question_base_url,
            view_id=remote_view_id,
        )
    return accept_shared_view(
        root,
        heading_id=local_heading_id,
        view_type=view_type,
        title=title or str(view_info.get("title") or remote_view_id),
        body=body if body is not None else _default_http_invitation_body(view_info, view_type),
        ref=str(view_info.get("ref") or f"rightmemory://{'mf' if view_type == 'file' else 'mq'}/{remote_view_id}"),
        relationship=relationship or "human",
        maintainer=_optional_string(view_info.get("provider_id")),
        description=_optional_string(view_info.get("description")),
        accepted_from=invitation_url,
        target=SharedViewTarget(
            kind=target_kind,
            base_url=base_url,
            view_id=remote_view_id,
            credential_id=local_credential_id,
            question_base_url=question_base_url,
            question_credential_id=question_credential_id,
            version_id=_optional_string(view_info.get("current_version_id")),
            accepted_from_url=invitation_url,
        ),
    )


def accept_shared_view_invitation(*args, **kwargs) -> str:
    raise ValueError("local shared-view package invitations are no longer supported; use HTTP invitations")


def accept_shared_view(
    memory_root: Path,
    *,
    heading_id: str,
    view_type: str,
    title: str,
    body: str,
    ref: str,
    relationship: str = "human",
    maintainer: str | None = None,
    description: str | None = None,
    accepted_from: str | None = None,
    target: SharedViewTarget | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{relationship}`")
    if view_type not in {"file", "question"}:
        raise ValueError("shared view type must be file or question")
    resolved_target = target or SharedViewTarget()
    expected_kinds = {"http-file", "git-file"} if view_type == "file" else {"http-question"}
    if resolved_target.kind not in {"none", *expected_kinds}:
        expected = " or ".join(sorted(expected_kinds))
        raise ValueError(f"{view_type} shared view requires {expected} target")
    connections = load_connections(root)
    connections[clean_heading_id] = SharedViewConnection(
        heading_id=clean_heading_id,
        view_type=view_type,
        ref=ref.strip(),
        relationship=relationship,
        maintainer=_optional_string(maintainer),
        description=_optional_string(description),
        accepted_from=_optional_string(accepted_from),
        target=resolved_target,
    )
    _ensure_memory_heading(root, view_type=view_type, heading_id=clean_heading_id, title=title, body=body)
    save_connections(root, connections)
    return f"accepted shared view {clean_heading_id}"


def record_shared_view_note(
    memory_root: Path,
    heading_id: str,
    message: str,
    *,
    confirmed: bool = False,
    actor: str = "user",
    task_context: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    clean_message = message.strip()
    if not clean_message:
        raise ValueError("shared view note message must not be empty")
    connection = load_connections(root).get(clean_heading_id)
    if connection is None:
        return f"shared view {clean_heading_id} is not registered"
    if connection.relationship in {"human", "external"} and not confirmed:
        maintainer = f" for {connection.maintainer}" if connection.maintainer else ""
        return f"confirmation required before sending note{maintainer}: {clean_message}"
    if connection.target.kind not in {"http-file", "http-question"}:
        return f"shared view {clean_heading_id} does not have an HTTP interaction target"
    record: dict[str, object] = {
        "created_at": _now_iso(),
        "heading_id": clean_heading_id,
        "view_type": connection.view_type,
        "ref": connection.ref,
        "relationship": connection.relationship,
        "maintainer": connection.maintainer,
        "actor": actor,
        "message": clean_message,
    }
    task_context_text = _optional_string(task_context)
    if task_context_text:
        record["task_context"] = task_context_text
    try:
        credential = load_shared_view_credential(root, connection.target.credential_id or "")
        response = HubClient(connection.target.base_url or "", credential["token"]).post_interaction(
            connection.target.view_id or clean_heading_id,
            record,
        )
        status = str(response.get("status") or "recorded")
    except (KeyError, ValueError, HubClientError) as exc:
        status = "failed"
        record["error"] = str(exc)
    record["status"] = status
    _append_jsonl(_notes_path(root, clean_heading_id), record)
    if status == "failed":
        return f"failed to send shared view note for {clean_heading_id}"
    return f"recorded shared view note for {clean_heading_id}"


def list_shared_view_notes(memory_root: Path, heading_id: str | None = None) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    notes_root = root / RUNTIME_DIR / "notes"
    if not notes_root.is_dir():
        return []
    files = [notes_root / f"{validate_heading_id(heading_id)}.jsonl"] if heading_id else sorted(notes_root.glob("*.jsonl"))
    records: list[dict[str, object]] = []
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def list_shared_view_inbox(memory_root: Path, view_id: str | None = None) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    inbox_root = root / RUNTIME_DIR / "inbox"
    if not inbox_root.is_dir():
        return []
    files = [inbox_root / f"{validate_heading_id(view_id)}.jsonl"] if view_id else sorted(inbox_root.glob("*.jsonl"))
    records: list[dict[str, object]] = []
    for path in files:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    return records


def list_http_shared_view_inbox(
    memory_root: Path,
    *,
    hub_url: str,
    credential_id: str,
    provider_id: str,
) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    credential = load_shared_view_credential(root, validate_heading_id(credential_id))
    response = HubClient(hub_url.rstrip("/"), credential["token"]).provider_inbox(validate_heading_id(provider_id))
    interactions = response.get("interactions", [])
    if not isinstance(interactions, list):
        raise ValueError("HTTP hub inbox response must contain an interactions list")
    return [item for item in interactions if isinstance(item, dict)]


def provider_view_summaries(memory_root: Path) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    views_root = root / PROVIDER_VIEWS_DIR
    if not views_root.is_dir():
        return []
    summaries: list[dict[str, object]] = []
    for view_dir in sorted(path for path in views_root.iterdir() if path.is_dir()):
        view_type = "file" if (view_dir / "recipe.toml").is_file() else "question" if (view_dir / "question.toml").is_file() else "unknown"
        if view_type == "unknown":
            continue
        summaries.append(
            {
                "view_id": view_dir.name,
                "type": view_type,
                "has_view": (view_dir / "view.md").is_file(),
                "approved": _provider_view_approved(view_dir, view_type),
            }
        )
    return summaries


def shared_view_connection_status(memory_root: Path, heading_id: str) -> dict[str, object]:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    connection = load_connections(root).get(clean_heading_id)
    if connection is None:
        return {"heading_id": clean_heading_id, "status": "unavailable", "message": "shared view connection not found"}
    status: dict[str, object] = {
        "heading_id": clean_heading_id,
        "type": connection.view_type,
        "target": connection.target.kind,
        "status": "configured",
    }
    if connection.target.base_url:
        status["base_url"] = connection.target.base_url
    if connection.target.question_base_url:
        status["question_base_url"] = connection.target.question_base_url
    if connection.target.view_id:
        status["remote_view_id"] = connection.target.view_id
    if connection.view_type == "file":
        import_path = root / RUNTIME_DIR / "imports" / clean_heading_id / "dist" / "MEMORY.md"
        status["imported"] = import_path.is_file()
        status["status"] = "imported" if import_path.is_file() else "not_pulled"
        status["message"] = "file view import is available" if import_path.is_file() else "file view has not been pulled"
    else:
        status["message"] = "question view endpoint is configured"
    return status


def _provider_view_approved(view_dir: Path, view_type: str) -> bool:
    metadata = view_dir / ("recipe.toml" if view_type == "file" else "question.toml")
    if not metadata.is_file():
        return False
    try:
        with metadata.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return False
    return bool(data.get("approved", False)) if isinstance(data, dict) else False


def _ensure_memory_heading(root: Path, *, view_type: str, heading_id: str, title: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    memory = root / "MEMORY.md"
    text = memory.read_text(encoding="utf-8") if memory.exists() else "# Shared Views {#shared-views}\n"
    marker = "MF#" if view_type == "file" else "MQ#"
    if f"{{{marker}{heading_id}}}" in text:
        return
    entry = f"\n\n### {title.strip()} {{{marker}{heading_id}}}\n\n{body.strip()}\n"
    memory.write_text(text.rstrip() + entry, encoding="utf-8")


def _view_type_from_invitation(view_info: dict[str, object]) -> str:
    raw_kind = view_info.get("kind")
    if raw_kind == "question":
        return "question"
    ref = view_info.get("ref")
    if isinstance(ref, str) and ref.startswith("rightmemory://mq/"):
        return "question"
    return "file"


def _question_base_url_from_invitation(view_info: dict[str, object]) -> str:
    value = (
        _optional_string(view_info.get("question_base_url"))
        or _optional_string(view_info.get("question_url"))
        or _optional_string(view_info.get("ask_base_url"))
    )
    if not value:
        raise ValueError("HTTP question shared-view invitations must include question_base_url")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HTTP question shared-view endpoint must be an http(s) URL")
    return value.rstrip("/")


def _default_http_invitation_body(view_info: dict[str, object], view_type: str) -> str:
    description = _optional_string(view_info.get("description"))
    if description:
        return description
    if view_type == "file":
        return "Use this mirrored file view when its provider context is relevant."
    return "Use this provider question view when a live provider-side answer would help."


def _parse_http_invitation_url(invitation_url: str) -> tuple[str, str]:
    parsed = urlparse(invitation_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HTTP shared-view invitation must be an http(s) URL")
    prefix = "/i/"
    if prefix not in parsed.path:
        raise ValueError("HTTP shared-view invitation URL must contain /i/<token>")
    token = parsed.path.rsplit(prefix, 1)[1].strip("/")
    if not token:
        raise ValueError("HTTP shared-view invitation URL must contain /i/<token>")
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return base_url.rstrip("/"), token


def _notes_path(root: Path, heading_id: str) -> Path:
    return root / RUNTIME_DIR / "notes" / f"{heading_id}.jsonl"


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
