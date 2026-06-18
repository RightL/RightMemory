from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .hub.client import HubClient
from .share_models import ShareFilePart, ShareQuestionPart, ShareRelationship, load_shares, save_shares, validate_share_id
from .shared_view_builder import run_file_view_builder, run_question_view_builder
from .shared_view_files import approve_file_view, publish_file_view_package, pull_file_view
from .shared_view_models import SharedViewTarget, load_shared_view_credential, save_shared_view_credential, validate_heading_id
from .shared_view_questions import approve_question_view, register_question_view_with_hub
from .shared_views import accept_shared_view


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
    clean_title = _required_share_value(title, "title").strip()
    clean_provider_id = validate_heading_id(provider_id)
    clean_hub_url = _required_share_value(hub_url, "hub_url").rstrip("/")
    clean_credential_id = validate_heading_id(credential_id)
    parts: list[str] = []
    file_part: ShareFilePart | None = None
    question_part: ShareQuestionPart | None = None
    if file_intent:
        file_view_id = f"{clean_share_id}-files"
        parts.append("file")
        clean_file_intent = file_intent.strip()
        file_part = ShareFilePart(view_id=file_view_id, intent=clean_file_intent, approved=False)
        if build_parts:
            run_file_view_builder(
                root,
                view_id=file_view_id,
                title=f"{clean_title} Files",
                intent=clean_file_intent,
                hub_url=clean_hub_url,
                credential_id=clean_credential_id,
            )
    if question_intent:
        if not question_base_url:
            raise ValueError("share question part requires question_base_url")
        question_view_id = f"{clean_share_id}-ask"
        parts.append("question")
        clean_question_intent = question_intent.strip()
        question_part = ShareQuestionPart(
            view_id=question_view_id,
            intent=clean_question_intent,
            question_base_url=question_base_url.strip(),
            approved=False,
        )
        if build_parts:
            run_question_view_builder(
                root,
                view_id=question_view_id,
                title=f"{clean_title} Questions",
                intent=clean_question_intent,
            )
    if not parts:
        raise ValueError("share create requires --file, --question, or both")
    shares = load_shares(root)
    shares[clean_share_id] = ShareRelationship(
        share_id=clean_share_id,
        role="provider",
        title=clean_title,
        provider_id=clean_provider_id,
        hub_url=clean_hub_url,
        credential_id=clean_credential_id,
        state="draft",
        parts=tuple(parts),
        file=file_part,
        question=question_part,
    )
    save_shares(root, shares)
    return f"created share {clean_share_id}; review generated parts, then run: rightmemory share approve {clean_share_id}"


def approve_share(memory_root: Path, share_id: str) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    share = _require_share(shares, share_id)
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {share.share_id}")
    file_part = share.file
    question_part = share.question
    if "file" in share.parts:
        file_view_id = _required_part_value(file_part.view_id if file_part else None, "file view_id")
        approve_file_view(root, file_view_id)
        file_part = ShareFilePart(
            view_id=file_view_id,
            intent=file_part.intent if file_part else None,
            heading_id=file_part.heading_id if file_part else None,
            approved=True,
        )
    if "question" in share.parts:
        question_view_id = _required_part_value(question_part.view_id if question_part else None, "question view_id")
        approve_question_view(root, question_view_id)
        question_part = ShareQuestionPart(
            view_id=question_view_id,
            intent=question_part.intent if question_part else None,
            heading_id=question_part.heading_id if question_part else None,
            question_base_url=question_part.question_base_url if question_part else None,
            approved=True,
        )
    shares[share.share_id] = _replace_share(share, state="approved", file=file_part, question=question_part)
    save_shares(root, shares)
    return f"approved share {share.share_id}"


def publish_share(memory_root: Path, share_id: str, *, label: str | None = None, expires_at: str | None = None) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    share = _require_share(shares, share_id)
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {share.share_id}")
    if share.state not in {"approved", "published"}:
        raise ValueError(f"share is not approved: {share.share_id}")
    hub_url = _required_share_value(share.hub_url, "hub_url")
    credential_id = _required_share_value(share.credential_id, "credential_id")
    parts_payload: list[dict[str, str]] = []
    if "file" in share.parts:
        file_view_id = _required_part_value(share.file.view_id if share.file else None, "file view_id")
        publish_file_view_package(root, file_view_id, hub_url=hub_url, credential_id=credential_id)
        parts_payload.append({"type": "file", "view_id": file_view_id})
    if "question" in share.parts:
        question_view_id = _required_part_value(share.question.view_id if share.question else None, "question view_id")
        register_question_view_with_hub(
            root,
            question_view_id,
            hub_url=hub_url,
            credential_id=credential_id,
            question_base_url=_required_part_value(
                share.question.question_base_url if share.question else None,
                "question_base_url",
            ),
        )
        parts_payload.append({"type": "question", "view_id": question_view_id})
    client = HubClient(hub_url, _load_publish_token(root, credential_id))
    invitation = client.create_share_invitation(
        share.share_id,
        title=share.title,
        parts=parts_payload,
        label=label,
        expires_at=expires_at,
    )
    invitation_url = invitation.get("invitation_url")
    if not isinstance(invitation_url, str) or not invitation_url:
        raise ValueError("hub did not return an invitation_url")
    _record_runtime_invitation(root, share.share_id, invitation_url)
    shares[share.share_id] = _replace_share(share, state="published")
    save_shares(root, shares)
    return f"published share {share.share_id}\ninvitation_url\t{invitation_url}"


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
    described_parts = {
        str(part.get("view_id")): part for part in described.get("parts", []) if isinstance(part, dict) and part.get("view_id")
    }
    for raw_part in accepted.get("parts", []):
        if not isinstance(raw_part, dict):
            continue
        part_type = str(raw_part.get("type") or "").strip()
        view_id = validate_heading_id(str(raw_part.get("view_id") or ""))
        connection_token = _required_response_value(raw_part.get("connection_token"), "connection_token")
        credential_id = validate_heading_id(f"http-{view_id}")
        save_shared_view_credential(
            root,
            credential_id,
            kind="http-connection",
            token=connection_token,
            base_url=base_url,
            view_id=view_id,
        )
        part_description = described_parts.get(view_id, {})
        if part_type == "file":
            parts.append("file")
            file_part = ShareFilePart(heading_id=view_id)
            accept_shared_view(
                root,
                heading_id=view_id,
                view_type="file",
                title=str(part_description.get("title") or view_id),
                body=f"Accepted as part of share {share_id}.",
                ref=f"rightmemory://mf/{view_id}",
                maintainer=provider_id,
                accepted_from=invitation_url,
                target=SharedViewTarget(
                    kind="http-file",
                    base_url=base_url,
                    view_id=view_id,
                    credential_id=credential_id,
                    accepted_from_url=invitation_url,
                ),
            )
        elif part_type == "question":
            parts.append("question")
            question_credential_id = validate_heading_id(f"{credential_id}-question")
            question_base_url = _required_response_value(part_description.get("question_base_url"), "question_base_url")
            question_token = _required_response_value(raw_part.get("question_token"), "question_token")
            save_shared_view_credential(
                root,
                question_credential_id,
                kind="http-question",
                token=question_token,
                base_url=question_base_url,
                view_id=view_id,
            )
            question_part = ShareQuestionPart(heading_id=view_id, question_base_url=question_base_url)
            accept_shared_view(
                root,
                heading_id=view_id,
                view_type="question",
                title=str(part_description.get("title") or view_id),
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
    lines = [f"joined share {share_id}"]
    if file_part and file_part.heading_id:
        pulled = pull_file_view(root, file_part.heading_id)
        lines.append(f"file {pulled.heading_id} {pulled.status}: {pulled.message}")
    return "\n".join(lines)


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


def _require_share(shares: dict[str, ShareRelationship], share_id: str) -> ShareRelationship:
    clean_share_id = validate_share_id(share_id)
    share = shares.get(clean_share_id)
    if share is None:
        raise KeyError(f"share not found: {clean_share_id}")
    return share


def _replace_share(
    share: ShareRelationship,
    *,
    state: str | None = None,
    file: ShareFilePart | None = None,
    question: ShareQuestionPart | None = None,
) -> ShareRelationship:
    return replace(
        share,
        state=state if state is not None else share.state,
        file=file if file is not None else share.file,
        question=question if question is not None else share.question,
    )


def _required_share_value(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"share {label} is required")
    return value.strip()


def _required_part_value(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"share {label} is required")
    return value.strip()


def _required_response_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"share invitation response missing {label}")
    return value.strip()


def _parse_share_invitation_url(invitation_url: str) -> tuple[str, str]:
    parsed = urlparse(invitation_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("share invitation must be an http(s) URL")
    prefix = "/i/share/"
    if prefix not in parsed.path:
        raise ValueError("share invitation URL must contain /i/share/<token>")
    token = parsed.path.rsplit(prefix, 1)[1].strip("/")
    if not token:
        raise ValueError("share invitation URL must contain /i/share/<token>")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/"), token


def _load_publish_token(memory_root: Path, credential_id: str) -> str:
    credential = load_shared_view_credential(Path(memory_root).expanduser(), credential_id)
    token = credential.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError(f"shared view credential is missing token: {credential_id}")
    return token


def _record_runtime_invitation(memory_root: Path, share_id: str, invitation_url: str) -> None:
    root = Path(memory_root).expanduser()
    path = root / ".runtime" / "shares" / f"{validate_share_id(share_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "share_id": validate_share_id(share_id),
        "invitation_url": invitation_url,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
