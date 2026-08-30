from __future__ import annotations

import hashlib
import os
import re
import struct
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
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_COUNT = 4
MAX_TEXT_COUNT = 4
MAX_FILE_COUNT = 8
MAX_TOTAL_COUNT = 8
# Compressed byte limits do not bound decoder memory. These limits keep the
# accepted preview surface below decompression-bomb scale before a browser can
# render an uploaded PNG or JPEG.
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 12_500_000
ORPHAN_CLEANUP_MINIMUM_AGE_SECONDS = 24 * 60 * 60
ORPHAN_CLEANUP_SCAN_LIMIT = 512

_IMAGE_TYPES = {
    "image/png": ("image", ".png"),
    "image/jpeg": ("image", ".jpg"),
}
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+*'|~-]+/[a-z0-9!#$&^_.+*'|~-]+\Z")
_SAFE_EXTENSION = re.compile(r"[A-Za-z0-9]{1,16}\Z")
_MANAGED_FILE_NAME = re.compile(r"[0-9a-f]{32}\.[a-z0-9]{1,16}\Z")
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


def maximum_upload_bytes(
    media_type: object, attachment_kind: object = None
) -> int:
    normalized, _charset = _parse_media_type(media_type)
    requested_kind = _requested_attachment_kind(attachment_kind)
    if requested_kind == "file":
        return MAX_FILE_BYTES
    if normalized in _IMAGE_TYPES:
        return MAX_IMAGE_BYTES
    if normalized == "text/plain":
        return MAX_TEXT_BYTES
    return MAX_FILE_BYTES


def validate_upload(
    content: object,
    media_type: object,
    display_name: object,
    attachment_id: object = None,
    attachment_kind: object = None,
) -> ValidatedUpload:
    normalized_type, charset = _parse_media_type(media_type)
    requested_kind = _requested_attachment_kind(attachment_kind)
    if not isinstance(content, bytes):
        raise ConversationError("invalid_attachment", "The attachment body must be raw bytes.", 422)
    maximum = maximum_upload_bytes(media_type, requested_kind)
    if not content:
        raise ConversationError("invalid_attachment", "The attachment is empty.", 422)
    if len(content) > maximum:
        raise ConversationError(
            "attachment_too_large",
            f"The attachment exceeds the {maximum // (1024 * 1024)} MiB limit.",
            413,
        )

    if requested_kind == "file":
        kind = "file"
        default_name = "Attachment.bin"
        clean_display_name = _display_name(display_name, default_name)
        suffix = _managed_suffix(clean_display_name)
    elif normalized_type in _IMAGE_TYPES:
        kind, suffix = _IMAGE_TYPES[normalized_type]
        if charset is not None:
            raise ConversationError("invalid_attachment", "Image content types cannot specify a charset.", 415)
        dimensions = (
            _png_dimensions(content)
            if normalized_type == "image/png"
            else _jpeg_dimensions(content)
        )
        _validate_image_dimensions(*dimensions)
        default_name = f"Pasted image{suffix}"
        clean_display_name = _display_name(display_name, default_name)
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
        clean_display_name = _display_name(display_name, default_name)
    else:
        kind = "file"
        default_name = "Attachment.bin"
        clean_display_name = _display_name(display_name, default_name)
        suffix = _managed_suffix(clean_display_name)

    return ValidatedUpload(
        attachment_id=_attachment_id(attachment_id),
        kind=kind,
        display_name=clean_display_name,
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


def is_managed_attachment_name(value: object) -> bool:
    return isinstance(value, str) and _MANAGED_FILE_NAME.fullmatch(value) is not None


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
                is_managed_attachment_name(path.name)
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
    if candidate.parent != base or not is_managed_attachment_name(candidate.name):
        raise ConversationError("attachment_unavailable", "The attachment path is not a managed file.", 409)
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
    if _MEDIA_TYPE.fullmatch(normalized) is None:
        raise ConversationError("unsupported_attachment", "The attachment content type is invalid.", 415)
    charset: str | None = None
    for parameter in parts[1:]:
        if not parameter:
            continue
        name, separator, parameter_value = parameter.partition("=")
        if separator != "=" or name.strip().lower() != "charset" or charset is not None:
            raise ConversationError("unsupported_attachment", "The attachment content type is invalid.", 415)
        charset = parameter_value.strip().strip('"').lower()
    return normalized, charset


def _requested_attachment_kind(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value.strip().lower() != "file":
        raise ConversationError(
            "invalid_attachment",
            "The requested attachment kind is invalid.",
            422,
        )
    return "file"


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


def _managed_suffix(display_name: str) -> str:
    _stem, separator, extension = display_name.rpartition(".")
    if separator and _SAFE_EXTENSION.fullmatch(extension) is not None:
        return f".{extension.lower()}"
    return ".bin"


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if (
        len(content) < 24
        or not content.startswith(b"\x89PNG\r\n\x1a\n")
        or content[8:12] != b"\x00\x00\x00\r"
        or content[12:16] != b"IHDR"
    ):
        raise ConversationError(
            "invalid_attachment", "The attachment is not a valid PNG image.", 422
        )
    return struct.unpack(">II", content[16:24])


_JPEG_START_OF_FRAME_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 4 or not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise ConversationError(
            "invalid_attachment", "The attachment is not a valid JPEG image.", 422
        )
    offset = 2
    while offset < len(content):
        if content[offset] != 0xFF:
            break
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            break
        marker = content[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if offset + 2 > len(content):
            break
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(content):
            break
        if marker in _JPEG_START_OF_FRAME_MARKERS:
            if segment_length < 7:
                break
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            return width, height
        if marker == 0xDA:
            break
        offset += segment_length
    raise ConversationError(
        "invalid_attachment", "The attachment is not a valid JPEG image.", 422
    )


def _validate_image_dimensions(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise ConversationError(
            "invalid_attachment", "The attachment has invalid image dimensions.", 422
        )
    if (
        width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise ConversationError(
            "image_dimensions_too_large",
            "The image dimensions exceed the safe preview limit.",
            413,
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
