from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .models import ConversationError


ATTACHMENT_DIRECTORY = Path(".runtime") / "web" / "attachments"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_IMAGE_COUNT = 8
MAX_TEXT_COUNT = 4
MAX_TOTAL_COUNT = 8
ORPHAN_CLEANUP_MINIMUM_AGE_SECONDS = 24 * 60 * 60
ORPHAN_CLEANUP_SCAN_LIMIT = 512

_IMAGE_TYPES = {
    "image/png": ("image", ".png"),
    "image/jpeg": ("image", ".jpg"),
}
_MANAGED_FILE_NAME = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|txt)\Z")
_TEMPORARY_FILE_NAME = re.compile(r"upload-[A-Za-z0-9_-]+\.tmp\Z")
_CLIENT_ATTACHMENT_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    attachment_id: str
    kind: str
    display_name: str
    media_type: str
    content: bytes
    sha256: str
    suffix: str

    @property
    def byte_size(self) -> int:
        return len(self.content)


def maximum_upload_bytes(media_type: object) -> int:
    normalized, _charset = _parse_media_type(media_type)
    if normalized in _IMAGE_TYPES:
        return MAX_IMAGE_BYTES
    if normalized == "text/plain":
        return MAX_TEXT_BYTES
    raise ConversationError(
        "unsupported_attachment",
        "Pasted attachments must be PNG, JPEG, or UTF-8 plain text.",
        415,
    )


def validate_upload(
    content: object,
    media_type: object,
    display_name: object,
    attachment_id: object = None,
) -> ValidatedUpload:
    normalized_type, charset = _parse_media_type(media_type)
    if not isinstance(content, bytes):
        raise ConversationError("invalid_attachment", "The attachment body must be raw bytes.", 422)
    maximum = maximum_upload_bytes(media_type)
    if not content:
        raise ConversationError("invalid_attachment", "The pasted attachment is empty.", 422)
    if len(content) > maximum:
        raise ConversationError(
            "attachment_too_large",
            f"The pasted attachment exceeds the {maximum // (1024 * 1024)} MiB limit.",
            413,
        )

    if normalized_type in _IMAGE_TYPES:
        kind, suffix = _IMAGE_TYPES[normalized_type]
        if charset is not None:
            raise ConversationError("invalid_attachment", "Image content types cannot specify a charset.", 415)
        if normalized_type == "image/png" and not (
            content.startswith(b"\x89PNG\r\n\x1a\n") and b"IHDR" in content[:32]
        ):
            raise ConversationError("invalid_attachment", "The attachment is not a valid PNG image.", 422)
        if normalized_type == "image/jpeg" and not (
            content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
        ):
            raise ConversationError("invalid_attachment", "The attachment is not a valid JPEG image.", 422)
        default_name = f"Pasted image{suffix}"
    elif normalized_type == "text/plain":
        kind, suffix = "pasted_text", ".txt"
        if charset not in {None, "utf-8", "utf8", "us-ascii"}:
            raise ConversationError("invalid_attachment", "Pasted text must use UTF-8.", 415)
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConversationError("invalid_attachment", "Pasted text must be valid UTF-8.", 422) from exc
        if "\x00" in decoded:
            raise ConversationError("invalid_attachment", "Pasted text cannot contain NUL bytes.", 422)
        default_name = "Pasted text.txt"
    else:
        raise ConversationError(
            "unsupported_attachment",
            "Pasted attachments must be PNG, JPEG, or UTF-8 plain text.",
            415,
        )

    return ValidatedUpload(
        attachment_id=_attachment_id(attachment_id),
        kind=kind,
        display_name=_display_name(display_name, default_name),
        media_type=normalized_type,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        suffix=suffix,
    )


def _attachment_id(value: object) -> str:
    if value is None:
        return uuid4().hex
    if not isinstance(value, str) or _CLIENT_ATTACHMENT_ID.fullmatch(value) is None:
        raise ConversationError(
            "invalid_attachment",
            "The attachment id must be exactly 32 lowercase hexadecimal characters.",
            422,
        )
    return value


def write_upload(root: Path, upload: ValidatedUpload) -> str:
    base = attachment_base(root)
    destination = base / f"{upload.attachment_id}{upload.suffix}"
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix="upload-", suffix=".tmp", dir=base)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(upload.content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination.relative_to(Path(root).resolve()).as_posix()


def attachment_base(root: Path) -> Path:
    resolved_root = Path(root).expanduser().resolve()
    base = (resolved_root / ATTACHMENT_DIRECTORY).resolve()
    base.relative_to(resolved_root)
    base.mkdir(parents=True, exist_ok=True)
    return base


def cleanup_orphaned_attachment_files(
    root: Path,
    referenced_relative_paths: Iterable[object],
    *,
    now: float | None = None,
) -> int:
    """Remove a bounded set of old managed files that have no database record."""

    resolved_root = Path(root).expanduser().resolve()
    base = attachment_base(resolved_root)
    referenced_names: set[str] = set()
    for relative_path in referenced_relative_paths:
        if not isinstance(relative_path, str) or not relative_path:
            continue
        candidate = (resolved_root / Path(relative_path)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            continue
        referenced_names.add(candidate.name)

    cutoff = (time.time() if now is None else now) - ORPHAN_CLEANUP_MINIMUM_AGE_SECONDS
    removed = 0
    try:
        entries = base.iterdir()
        for index, path in enumerate(entries):
            if index >= ORPHAN_CLEANUP_SCAN_LIMIT:
                break
            if path.name in referenced_names or not (
                _MANAGED_FILE_NAME.fullmatch(path.name)
                or _TEMPORARY_FILE_NAME.fullmatch(path.name)
            ):
                continue
            try:
                if path.lstat().st_mtime > cutoff:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def resolve_attachment_path(root: Path, attachment: Mapping[str, Any]) -> Path:
    relative_path = attachment.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path:
        raise ConversationError("attachment_unavailable", "The attachment path is unavailable.", 409)
    resolved_root = Path(root).expanduser().resolve()
    base = attachment_base(resolved_root)
    candidate = (resolved_root / Path(relative_path)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ConversationError("attachment_unavailable", "The attachment path is outside managed storage.", 409) from exc
    if not candidate.is_file():
        raise ConversationError("attachment_unavailable", "The attachment file is unavailable.", 404)
    expected_size = attachment.get("byte_size")
    if not isinstance(expected_size, int) or candidate.stat().st_size != expected_size:
        raise ConversationError("attachment_changed", "The managed attachment size changed unexpectedly.", 409)
    expected_hash = attachment.get("sha256")
    if not isinstance(expected_hash, str) or _file_sha256(candidate) != expected_hash:
        raise ConversationError("attachment_changed", "The managed attachment content changed unexpectedly.", 409)
    return candidate


def public_attachment(attachment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: attachment.get(key)
        for key in (
            "attachment_id",
            "kind",
            "display_name",
            "media_type",
            "byte_size",
            "state",
            "created_at",
            "updated_at",
        )
    }


def _parse_media_type(value: object) -> tuple[str, str | None]:
    if not isinstance(value, str) or not value.strip():
        raise ConversationError("unsupported_attachment", "An attachment content type is required.", 415)
    parts = [part.strip() for part in value.split(";")]
    normalized = parts[0].lower()
    charset: str | None = None
    for parameter in parts[1:]:
        if not parameter:
            continue
        name, separator, parameter_value = parameter.partition("=")
        if separator != "=" or name.strip().lower() != "charset" or charset is not None:
            raise ConversationError("unsupported_attachment", "The attachment content type is invalid.", 415)
        charset = parameter_value.strip().strip('"').lower()
    return normalized, charset


def _display_name(value: object, default: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ConversationError("invalid_attachment", "The attachment name is invalid.", 422)
    # A display name is never used as a path. Removing browser-supplied path
    # components also keeps older clipboard implementations tidy.
    clean = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not clean
        or len(clean) > 180
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        raise ConversationError("invalid_attachment", "The attachment name is invalid.", 422)
    return clean


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
