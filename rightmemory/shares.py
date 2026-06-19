from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from .git_share_transport import import_git_file_package, is_git_share_url, parse_git_share_url, publish_git_share_package, read_git_file_share
from .share_builder import revise_share_builder, run_share_builder
from .hub.client import HubClient, HubClientError
from .share_models import ShareFilePart, ShareQuestionPart, ShareRelationship, load_shares, save_shares, validate_share_id
from .share_results import ShareOperationResult, format_share_operation_result
from .shared_view_builder import run_file_view_builder, run_question_view_builder
from .shared_view_files import approve_file_view, export_file_view_package, publish_file_view_package, pull_file_view
from .shared_view_models import (
    SharedViewTarget,
    load_connections,
    load_shared_view_credential,
    save_shared_view_credential,
    validate_heading_id,
)
from .shared_view_questions import approve_question_view, register_question_view_with_hub
from .shared_views import accept_shared_view, shared_view_connection_status


QUESTION_READINESS_TIMEOUT_SECONDS = 5


def create_share(
    memory_root: Path,
    share_id: str,
    *,
    title: str | None = None,
    provider_id: str,
    hub_url: str | None = None,
    credential_id: str | None = None,
    request: str | None = None,
    capability: str = "auto",
    file_intent: str | None = None,
    question_intent: str | None = None,
    question_base_url: str | None = None,
    git_url: str | None = None,
    git_branch: str | None = None,
    build_parts: bool = True,
) -> str:
    root = Path(memory_root).expanduser()
    clean_share_id = validate_share_id(share_id)
    clean_git_url = git_url.strip() if git_url and git_url.strip() else None
    if request is not None:
        if file_intent or question_intent:
            raise ValueError("share create --request cannot be combined with --file or --question")
        result = create_share_from_request(
            root,
            share_id=clean_share_id,
            title=title,
            request=request,
            provider_id=provider_id,
            hub_url=hub_url,
            credential_id=credential_id,
            capability=capability,
            question_base_url=question_base_url,
            git_url=clean_git_url,
            git_branch=git_branch,
        )
        return format_share_operation_result(result)
    clean_title = _required_share_value(title, "title").strip()
    clean_provider_id = validate_heading_id(provider_id)
    if clean_git_url:
        if question_intent:
            raise ValueError("Git transport supports file context only")
        clean_hub_url = None
        clean_credential_id = None
    else:
        clean_hub_url = _required_share_value(hub_url, "hub_url").rstrip("/")
        clean_credential_id = validate_heading_id(_required_share_value(credential_id, "credential_id"))
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
        transport="git" if clean_git_url else "http",
        git_url=clean_git_url,
        git_branch=git_branch.strip() if git_branch and git_branch.strip() else None,
        state="draft",
        parts=tuple(parts),
        file=file_part,
        question=question_part,
    )
    save_shares(root, shares)
    return f"created share {clean_share_id}; review generated parts, then run: rightmemory share approve {clean_share_id}"


def create_share_from_request(
    memory_root: Path,
    *,
    share_id: str | None = None,
    title: str | None = None,
    request: str,
    provider_id: str,
    hub_url: str | None = None,
    credential_id: str | None = None,
    capability: str = "auto",
    question_base_url: str | None = None,
    git_url: str | None = None,
    git_branch: str | None = None,
) -> ShareOperationResult:
    root = Path(memory_root).expanduser()
    clean_git_url = git_url.strip() if git_url and git_url.strip() else None
    clean_capability = "file_context" if clean_git_url else capability
    if clean_git_url and question_base_url:
        raise ValueError("Git transport supports file context only")
    return run_share_builder(
        root,
        share_id_hint=validate_share_id(share_id) if share_id else None,
        title_hint=title.strip() if title and title.strip() else None,
        request=_required_share_value(request, "request"),
        provider_id=validate_heading_id(provider_id),
        hub_url=_required_share_value(hub_url, "hub_url").rstrip("/") if not clean_git_url else None,
        credential_id=validate_heading_id(_required_share_value(credential_id, "credential_id")) if not clean_git_url else None,
        capability=clean_capability,
        question_base_url=question_base_url.strip() if question_base_url else None,
        git_url=clean_git_url,
        git_branch=git_branch.strip() if git_branch and git_branch.strip() else None,
    )


def revise_share(
    memory_root: Path,
    share_id: str,
    revision: str,
    *,
    capability: str | None = None,
    question_base_url: str | None = None,
) -> ShareOperationResult:
    return revise_share_builder(
        Path(memory_root).expanduser(),
        validate_share_id(share_id),
        _required_share_value(revision, "revision"),
        capability=capability,
        question_base_url=question_base_url.strip() if question_base_url else None,
    )


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


def publish_share(
    memory_root: Path,
    share_id: str,
    *,
    label: str | None = None,
    expires_at: str | None = None,
    git_url: str | None = None,
    git_branch: str | None = None,
    push: bool = True,
) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    share = _require_share(shares, share_id)
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {share.share_id}")
    if share.state not in {"approved", "published"}:
        raise ValueError(f"share is not approved: {share.share_id}")
    if share.transport == "git" or git_url:
        if label or expires_at:
            raise ValueError("Git share publish does not support label or expires_at")
        return _publish_git_share(root, shares, share, git_url=git_url, git_branch=git_branch, push=push)
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


def _publish_git_share(
    root: Path,
    shares: dict[str, ShareRelationship],
    share: ShareRelationship,
    *,
    git_url: str | None,
    git_branch: str | None,
    push: bool,
) -> str:
    if "question" in share.parts:
        raise ValueError("Git transport supports file context only")
    if "file" not in share.parts or share.file is None:
        raise ValueError("Git share publish requires a file part")
    file_view_id = _required_part_value(share.file.view_id, "file view_id")
    updated_share = share
    if git_url or git_branch:
        updated_share = replace(
            share,
            transport="git",
            git_url=git_url.strip() if git_url and git_url.strip() else share.git_url,
            git_branch=git_branch.strip() if git_branch and git_branch.strip() else share.git_branch,
        )
    if not updated_share.git_url:
        raise ValueError("Git share publish requires --git <repo-url>")
    with TemporaryDirectory() as tempdir:
        package = Path(tempdir) / file_view_id
        export_file_view_package(root, file_view_id, package)
        invitation_url = publish_git_share_package(root, updated_share, package, push=push)
    shares[share.share_id] = _replace_share(updated_share, state="published")
    save_shares(root, shares)
    _record_runtime_invitation(root, share.share_id, invitation_url)
    return f"published share {share.share_id}\ninvitation_url\t{invitation_url}"


def join_share(memory_root: Path, invitation_url: str, *, consumer_label: str | None = None) -> str:
    root = Path(memory_root).expanduser()
    if is_git_share_url(invitation_url):
        return _join_git_share(root, invitation_url)
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


def _join_git_share(root: Path, invitation_url: str) -> str:
    reference = parse_git_share_url(invitation_url)
    described = read_git_file_share(root, reference)
    import_git_file_package(root, described.view_id, described.package_root)
    accept_shared_view(
        root,
        heading_id=described.view_id,
        view_type="file",
        title=described.file_title,
        body=f"Accepted as part of share {described.share_id}.",
        ref=f"rightmemory://mf/{described.view_id}",
        maintainer=described.provider_id,
        accepted_from=invitation_url,
        target=SharedViewTarget(
            kind="git-file",
            view_id=described.view_id,
            git_url=reference.repo_url,
            git_branch=reference.branch,
            git_share_id=described.share_id,
            accepted_from_url=invitation_url,
        ),
    )
    shares = load_shares(root)
    shares[described.share_id] = ShareRelationship(
        share_id=described.share_id,
        role="consumer",
        title=described.title,
        provider_id=described.provider_id,
        state="joined",
        parts=("file",),
        transport="git",
        git_url=reference.repo_url,
        git_branch=reference.branch,
        accepted_from=invitation_url,
        file=ShareFilePart(heading_id=described.view_id),
    )
    save_shares(root, shares)
    return f"joined share {described.share_id}\nfile {described.view_id} pulled: Git file view imported"


def share_status(memory_root: Path, share_id: str | None = None) -> str:
    root = Path(memory_root).expanduser()
    shares = load_shares(root)
    selected = [shares[validate_share_id(share_id)]] if share_id else [shares[key] for key in sorted(shares)]
    lines: list[str] = []
    for share in selected:
        provider = share.provider_id or "-"
        lines.append(f"{share.share_id} provider={provider} state={share.state} parts={','.join(share.parts)}")
        if share.file:
            lines.append(_share_part_status_line(root, share, "file"))
        if share.question:
            lines.append(_share_part_status_line(root, share, "question"))
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


def _share_part_status_line(root: Path, share: ShareRelationship, part_type: str) -> str:
    if part_type == "file":
        part = share.file
        identifier = part.heading_id if part else None
        fallback = part.view_id if part else None
    else:
        part = share.question
        identifier = part.heading_id if part else None
        fallback = part.view_id if part else None
    name = identifier or fallback or "-"
    if share.role == "consumer" and identifier:
        status = shared_view_connection_status(root, identifier)
        raw_state = str(status.get("status") or "unknown")
        if part_type == "file":
            state = "pulled" if raw_state == "imported" else raw_state
        else:
            state = _consumer_question_status_state(root, identifier, raw_state)
        return f"{part_type} {name} {state}"
    if share.state == "published":
        state = "published"
    elif bool(getattr(part, "approved", False)):
        state = "approved"
    else:
        state = "draft"
    return f"{part_type} {name} {state}"


def _consumer_question_status_state(root: Path, heading_id: str, local_state: str) -> str:
    if local_state != "configured":
        return local_state
    try:
        connection = load_connections(root).get(heading_id)
    except ValueError:
        return "unavailable"
    if connection is None:
        return "unavailable"
    target = connection.target
    if target.kind != "http-question" or not target.question_base_url or not target.question_credential_id:
        return "unavailable"
    try:
        credential = load_shared_view_credential(root, target.question_credential_id)
        response = HubClient(
            target.question_base_url,
            credential["token"],
            timeout=QUESTION_READINESS_TIMEOUT_SECONDS,
        ).probe_question(target.view_id or heading_id)
    except (KeyError, ValueError):
        return "unavailable"
    except HubClientError:
        return "unreachable"
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    status = data.get("status") or response.get("status")
    return "ready" if status == "ready" else "unavailable"
