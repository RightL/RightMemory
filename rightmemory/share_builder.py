from __future__ import annotations

import re
from pathlib import Path

from .config import load_config
from .runtime import RightMemoryRuntime
from .share_models import ShareRelationship, load_shares, validate_share_id
from .share_results import (
    ShareCapabilityStatus,
    ShareOperationResult,
    capability_from_parts,
    normalize_share_capability,
)


def run_share_builder(
    memory_root: Path,
    *,
    share_id_hint: str | None,
    request: str,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    capability: str = "auto",
    question_base_url: str | None = None,
    title_hint: str | None = None,
) -> ShareOperationResult:
    root = Path(memory_root).expanduser()
    clean_request = request.strip()
    if not clean_request:
        raise ValueError("share request must not be empty")
    clean_share_id = validate_share_id(share_id_hint) if share_id_hint else None
    clean_capability = normalize_share_capability(capability)
    previous_share_ids = set(load_shares(root))
    final_message = _run_builder_turn(
        root,
        _share_builder_session_id(clean_share_id or _fallback_share_id_hint(title_hint or clean_request)),
        _share_build_message(
            share_id_hint=clean_share_id,
            title_hint=title_hint,
            request=clean_request,
            provider_id=provider_id,
            hub_url=hub_url,
            credential_id=credential_id,
            capability=clean_capability,
            question_base_url=question_base_url,
        ),
    )
    share = _load_created_share(root, clean_share_id, previous_share_ids)
    return _operation_result(
        root,
        share,
        final_message=final_message,
        next_action=f"rightmemory share approve {share.share_id}",
    )


def revise_share_builder(
    memory_root: Path,
    share_id: str,
    revision: str,
    *,
    capability: str | None = None,
    question_base_url: str | None = None,
) -> ShareOperationResult:
    root = Path(memory_root).expanduser()
    clean_share_id = validate_share_id(share_id)
    clean_revision = revision.strip()
    if not clean_revision:
        raise ValueError("share revision must not be empty")
    share = load_shares(root).get(clean_share_id)
    if share is None:
        raise KeyError(f"share relationship does not exist: {clean_share_id}")
    if share.role != "provider":
        raise ValueError(f"share revision requires a provider share: {clean_share_id}")
    final_message = _run_builder_turn(
        root,
        _share_builder_session_id(clean_share_id),
        _share_revise_message(
            share=share,
            revision=clean_revision,
            capability=capability,
            question_base_url=question_base_url,
        ),
    )
    updated = load_shares(root).get(clean_share_id)
    if updated is None:
        raise RuntimeError(f"share builder removed share relationship: {clean_share_id}")
    return _operation_result(
        root,
        updated,
        final_message=final_message,
        next_action=f"rightmemory share approve {clean_share_id}",
    )


def _run_builder_turn(root: Path, session_id: str, message: str) -> str:
    config = load_config("shared-view-builder", memory_root=root)
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(session_id, message)
    finally:
        runtime.cleanup()


def _share_builder_session_id(share_id: str) -> str:
    return f"share-builder-{validate_share_id(share_id)}"


def _share_build_message(
    *,
    share_id_hint: str | None,
    title_hint: str | None,
    request: str,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    capability: str,
    question_base_url: str | None,
) -> str:
    lines = [
        "<share_build>",
        f"share_id_hint: {share_id_hint or ''}",
        f"title_hint: {(title_hint or '').strip()}",
        f"provider_id: {provider_id.strip()}",
        f"hub_url: {hub_url.strip()}",
        f"credential_id: {credential_id.strip()}",
        f"capability: {capability}",
    ]
    if question_base_url:
        lines.append(f"question_base_url: {question_base_url.strip()}")
    lines.extend(
        [
            "request:",
            request,
            "instructions:",
            "- Choose the requested share capability when explicit; for auto, choose file context, live questions, or both.",
            "- Create all selected MF#/MQ# artifacts with the compiler tools.",
            "- Finish by calling create_or_update_share_relationship for one provider share.",
            "- Final answer should summarize what the share contains and what to review next.",
            "</share_build>",
        ]
    )
    return "\n".join(lines)


def _share_revise_message(
    *,
    share: ShareRelationship,
    revision: str,
    capability: str | None,
    question_base_url: str | None,
) -> str:
    lines = [
        "<share_revise>",
        f"share_id: {share.share_id}",
        f"title: {share.title}",
        f"current_capability: {capability_from_parts(share.parts)}",
        f"requested_capability: {normalize_share_capability(capability) if capability else ''}",
    ]
    if question_base_url:
        lines.append(f"question_base_url: {question_base_url.strip()}")
    if share.file and share.file.view_id:
        lines.append(f"current_file_view_id: {share.file.view_id}")
    if share.question and share.question.view_id:
        lines.append(f"current_question_view_id: {share.question.view_id}")
    lines.extend(
        [
            "revision:",
            revision,
            "instructions:",
            "- Update the selected MF#/MQ# artifacts with the compiler tools as needed.",
            "- Finish by calling create_or_update_share_relationship for the same provider share.",
            "- Final answer should summarize what changed and what to review next.",
            "</share_revise>",
        ]
    )
    return "\n".join(lines)


def _load_created_share(root: Path, share_id_hint: str | None, previous_share_ids: set[str]) -> ShareRelationship:
    shares = load_shares(root)
    if share_id_hint:
        share = shares.get(share_id_hint)
        if share is None:
            raise RuntimeError(f"share builder did not create requested share relationship: {share_id_hint}")
        return share
    new_share_ids = sorted(set(shares) - previous_share_ids)
    if len(new_share_ids) == 1:
        return shares[new_share_ids[0]]
    if len(shares) == 1:
        return next(iter(shares.values()))
    raise RuntimeError("share builder did not create exactly one share relationship; retry with a share id")


def _operation_result(
    root: Path,
    share: ShareRelationship,
    *,
    final_message: str,
    next_action: str | None,
) -> ShareOperationResult:
    statuses: list[ShareCapabilityStatus] = []
    if share.file is not None and share.file.view_id:
        preview = root / "shared_views" / share.file.view_id / "dist" / "MEMORY.md"
        statuses.append(
            ShareCapabilityStatus(
                capability="file_context",
                artifact_id=share.file.view_id,
                status="approved" if share.file.approved else "draft",
                preview_path=preview.relative_to(root).as_posix() if preview.is_file() else None,
            )
        )
    if share.question is not None and share.question.view_id:
        preview = root / "shared_views" / share.question.view_id / "retriever.md"
        statuses.append(
            ShareCapabilityStatus(
                capability="live_questions",
                artifact_id=share.question.view_id,
                status="approved" if share.question.approved else "draft",
                preview_path=preview.relative_to(root).as_posix() if preview.is_file() else None,
            )
        )
    return ShareOperationResult(
        share_id=share.share_id,
        title=share.title,
        role=share.role,
        state=share.state,
        capability=capability_from_parts(share.parts),
        builder_final_message=final_message.strip(),
        statuses=tuple(statuses),
        next_action=next_action,
    )


def _fallback_share_id_hint(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(words[:4]) or "share"
