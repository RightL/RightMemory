from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


SHARE_REGISTRY_FILE = "shares.toml"
SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SHARE_ROLES = {"provider", "consumer"}
SHARE_STATES = {"draft", "approved", "published", "joined"}
SHARE_PARTS = {"file", "question"}
SHARE_TRANSPORTS = {"http", "git"}


@dataclass(frozen=True)
class ShareFilePart:
    view_id: str | None = None
    intent: str | None = None
    heading_id: str | None = None
    approved: bool = False


@dataclass(frozen=True)
class ShareQuestionPart:
    view_id: str | None = None
    intent: str | None = None
    heading_id: str | None = None
    question_base_url: str | None = None
    approved: bool = False


@dataclass(frozen=True)
class ShareRelationship:
    share_id: str
    role: str
    title: str
    state: str
    parts: tuple[str, ...]
    transport: str = "http"
    provider_id: str | None = None
    hub_url: str | None = None
    credential_id: str | None = None
    git_url: str | None = None
    git_branch: str | None = None
    accepted_from: str | None = None
    file: ShareFilePart | None = None
    question: ShareQuestionPart | None = None


def validate_share_id(value: str) -> str:
    clean = str(value).strip()
    if not SHARE_ID_RE.fullmatch(clean):
        raise ValueError(f"share id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return clean


def load_shares(memory_root: Path) -> dict[str, ShareRelationship]:
    path = Path(memory_root).expanduser() / SHARE_REGISTRY_FILE
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    raw_shares = data.get("shares", {})
    if not isinstance(raw_shares, dict):
        raise ValueError("shares.toml must contain a [shares] table")
    shares: dict[str, ShareRelationship] = {}
    for raw_share_id, raw_share in raw_shares.items():
        share_id = validate_share_id(str(raw_share_id))
        shares[share_id] = _load_share(share_id, raw_share)
    return shares


def save_shares(memory_root: Path, shares: dict[str, ShareRelationship]) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory share relationship registry", ""]
    for share_id in sorted(shares):
        share = _validate_share(shares[share_id])
        key = _toml_key(share.share_id)
        lines.append(f"[shares.{key}]")
        lines.append("version = 1")
        lines.append(f"role = {_toml_string(share.role)}")
        lines.append(f"title = {_toml_string(share.title)}")
        if share.provider_id:
            lines.append(f"provider_id = {_toml_string(share.provider_id)}")
        if share.hub_url:
            lines.append(f"hub_url = {_toml_string(share.hub_url)}")
        if share.credential_id:
            lines.append(f"credential_id = {_toml_string(share.credential_id)}")
        if share.transport != "http":
            lines.append(f"transport = {_toml_string(share.transport)}")
        if share.git_url:
            lines.append(f"git_url = {_toml_string(share.git_url)}")
        if share.git_branch:
            lines.append(f"git_branch = {_toml_string(share.git_branch)}")
        lines.append(f"state = {_toml_string(share.state)}")
        lines.append(f"parts = {_toml_array(share.parts)}")
        if share.accepted_from:
            lines.append(f"accepted_from = {_toml_string(share.accepted_from)}")
        if share.file:
            lines.extend(["", f"[shares.{key}.file]"])
            if share.file.view_id:
                lines.append(f"view_id = {_toml_string(share.file.view_id)}")
            if share.file.heading_id:
                lines.append(f"heading_id = {_toml_string(share.file.heading_id)}")
            if share.file.intent:
                lines.append(f"intent = {_toml_string(share.file.intent)}")
            lines.append(f"approved = {str(share.file.approved).lower()}")
        if share.question:
            lines.extend(["", f"[shares.{key}.question]"])
            if share.question.view_id:
                lines.append(f"view_id = {_toml_string(share.question.view_id)}")
            if share.question.heading_id:
                lines.append(f"heading_id = {_toml_string(share.question.heading_id)}")
            if share.question.intent:
                lines.append(f"intent = {_toml_string(share.question.intent)}")
            if share.question.question_base_url:
                lines.append(f"question_base_url = {_toml_string(share.question.question_base_url)}")
            lines.append(f"approved = {str(share.question.approved).lower()}")
        lines.append("")
    (root / SHARE_REGISTRY_FILE).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _load_share(share_id: str, raw: object) -> ShareRelationship:
    if not isinstance(raw, dict):
        raise ValueError(f"[shares.{share_id}] must be a TOML table")
    role = _required_choice(_optional_string(raw.get("role")), SHARE_ROLES, f"share role for {share_id}")
    title = _required_string(_optional_string(raw.get("title")), f"share title for {share_id}")
    state = _required_choice(_optional_string(raw.get("state")), SHARE_STATES, f"share state for {share_id}")
    transport = _required_choice(_optional_string(raw.get("transport")) or "http", SHARE_TRANSPORTS, f"share transport for {share_id}")
    parts = _load_parts(raw.get("parts"), share_id)
    file_part = _load_file_part(share_id, raw.get("file"), role=role, required="file" in parts)
    question_part = _load_question_part(share_id, raw.get("question"), role=role, required="question" in parts)
    share = ShareRelationship(
        share_id=share_id,
        role=role,
        title=title,
        provider_id=_optional_string(raw.get("provider_id")),
        hub_url=_optional_string(raw.get("hub_url")),
        credential_id=_optional_string(raw.get("credential_id")),
        transport=transport,
        git_url=_optional_string(raw.get("git_url")),
        git_branch=_optional_string(raw.get("git_branch")),
        state=state,
        parts=parts,
        accepted_from=_optional_string(raw.get("accepted_from")),
        file=file_part,
        question=question_part,
    )
    return _validate_share(share)


def _load_file_part(share_id: str, raw: object, *, role: str, required: bool) -> ShareFilePart | None:
    if raw is None:
        if required:
            raise ValueError(f"file part requires [shares.{share_id}.file]")
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"[shares.{share_id}.file] must be a TOML table")
    return ShareFilePart(
        view_id=_optional_string(raw.get("view_id")),
        intent=_optional_string(raw.get("intent")),
        heading_id=_optional_string(raw.get("heading_id")),
        approved=_optional_bool(raw.get("approved", False), f"file part approved for {share_id}"),
    )


def _load_question_part(share_id: str, raw: object, *, role: str, required: bool) -> ShareQuestionPart | None:
    if raw is None:
        if required:
            raise ValueError(f"question part requires [shares.{share_id}.question]")
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"[shares.{share_id}.question] must be a TOML table")
    return ShareQuestionPart(
        view_id=_optional_string(raw.get("view_id")),
        intent=_optional_string(raw.get("intent")),
        heading_id=_optional_string(raw.get("heading_id")),
        question_base_url=_optional_string(raw.get("question_base_url")),
        approved=_optional_bool(raw.get("approved", False), f"question part approved for {share_id}"),
    )


def _validate_share(share: ShareRelationship) -> ShareRelationship:
    share_id = validate_share_id(share.share_id)
    role = _required_choice(_optional_string(share.role), SHARE_ROLES, f"share role for {share_id}")
    title = _required_string(_optional_string(share.title), f"share title for {share_id}")
    state = _required_choice(_optional_string(share.state), SHARE_STATES, f"share state for {share_id}")
    transport = _required_choice(_optional_string(share.transport) or "http", SHARE_TRANSPORTS, f"share transport for {share_id}")
    parts = _normalize_parts(share.parts, share_id)
    file_part = share.file
    question_part = share.question
    git_url = _optional_string(share.git_url)
    git_branch = _optional_string(share.git_branch)
    if transport == "git":
        _required_string(git_url, f"git_url for {share_id}")
        if "question" in parts:
            raise ValueError(f"git share {share_id} supports file parts only")
    if "file" in parts:
        if file_part is None:
            raise ValueError(f"file part requires [shares.{share_id}.file]")
        if role == "provider":
            _required_string(_optional_string(file_part.view_id), f"file part view_id for {share_id}")
            _required_string(_optional_string(file_part.intent), f"file part intent for {share_id}")
        else:
            _required_string(_optional_string(file_part.heading_id), f"file part heading_id for {share_id}")
    if "question" in parts:
        if question_part is None:
            raise ValueError(f"question part requires [shares.{share_id}.question]")
        if role == "provider":
            _required_string(_optional_string(question_part.view_id), f"question part view_id for {share_id}")
            _required_string(_optional_string(question_part.intent), f"question part intent for {share_id}")
        else:
            _required_string(_optional_string(question_part.heading_id), f"question part heading_id for {share_id}")
    return ShareRelationship(
        share_id=share_id,
        role=role,
        title=title,
        state=state,
        parts=parts,
        transport=transport,
        provider_id=_optional_string(share.provider_id),
        hub_url=_optional_string(share.hub_url),
        credential_id=_optional_string(share.credential_id),
        git_url=git_url,
        git_branch=git_branch,
        accepted_from=_optional_string(share.accepted_from),
        file=file_part,
        question=question_part,
    )


def _load_parts(raw_parts: object, share_id: str) -> tuple[str, ...]:
    if not isinstance(raw_parts, list):
        raise ValueError(f"share parts for {share_id} must be a TOML array")
    return _normalize_parts(raw_parts, share_id)


def _normalize_parts(raw_parts: object, share_id: str) -> tuple[str, ...]:
    if not isinstance(raw_parts, (list, tuple)):
        raise ValueError(f"share parts for {share_id} must be a list or tuple")
    parts: list[str] = []
    for raw_part in raw_parts:
        part = _required_choice(_optional_string(raw_part), SHARE_PARTS, f"share part for {share_id}")
        if part not in parts:
            parts.append(part)
    if not parts:
        raise ValueError(f"share {share_id} must include at least one part")
    return tuple(parts)


def _required_choice(value: str | None, allowed: set[str], label: str) -> str:
    clean = _required_string(value, label)
    if clean not in allowed:
        raise ValueError(f"invalid {label}: {clean!r}")
    return clean


def _required_string(value: str | None, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} is required")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional share fields must be strings")
    clean = value.strip()
    return clean or None


def _optional_bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} must be a boolean")


def _toml_key(value: str) -> str:
    return value if _TOML_BARE_KEY_RE.fullmatch(value) else _toml_string(value)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"
