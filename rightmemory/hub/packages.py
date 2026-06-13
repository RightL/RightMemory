from __future__ import annotations

import os
import re
import secrets
import shutil
import tomllib
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from .models import HubPackageManifest, HubStoredPackage


REQUIRED_PACKAGE_FILES = (
    "view.md",
    "export.toml",
    "rightmemory-shared-view.toml",
    "dist/MEMORY.md",
)
DEFAULT_MAX_PACKAGE_BYTES = 10 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class PackageValidationError(ValueError):
    pass


def load_package_manifest(
    package_root: Path,
    *,
    expected_view_id: str | None = None,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
) -> HubPackageManifest:
    source_root = Path(package_root).expanduser()
    entries = _package_file_entries(source_root)
    files = tuple(relative for relative, _path in entries)
    file_set = set(files)
    for required in REQUIRED_PACKAGE_FILES:
        if required not in file_set:
            raise PackageValidationError(f"missing required package file: {required}")

    size_bytes = 0
    package_digest = sha256()
    for relative, path in entries:
        content = path.read_bytes()
        size_bytes += len(content)
        if max_package_bytes is not None and size_bytes > max_package_bytes:
            raise PackageValidationError(
                f"shared view package exceeds package size limit of {max_package_bytes} bytes"
            )
        package_digest.update(relative.encode("utf-8"))
        package_digest.update(b"\0")
        package_digest.update(content)
        package_digest.update(b"\0")

    export_metadata = _load_toml(source_root / "export.toml", "export.toml")
    invitation_metadata = _load_toml(source_root / "rightmemory-shared-view.toml", "rightmemory-shared-view.toml")
    view_id = _package_view_id(export_metadata, invitation_metadata)
    if expected_view_id is not None:
        clean_expected = _validate_hub_id(expected_view_id, "publish target view_id")
        if view_id != clean_expected:
            raise PackageValidationError(
                f"package view_id {view_id!r} does not match publish target {clean_expected!r}"
            )

    return HubPackageManifest(
        source_root=source_root.resolve(),
        view_id=view_id,
        title=_metadata_string(export_metadata, "title")
        or _metadata_string(invitation_metadata, "title")
        or view_id,
        ref=_metadata_string(export_metadata, "ref")
        or _metadata_string(invitation_metadata, "ref")
        or f"rightmemory://view/{view_id}",
        description=_metadata_string(export_metadata, "description")
        or _metadata_string(invitation_metadata, "description"),
        maintainer=_metadata_string(export_metadata, "maintainer")
        or _metadata_string(invitation_metadata, "maintainer"),
        files=files,
        size_bytes=size_bytes,
        package_hash=package_digest.hexdigest(),
        export_metadata=export_metadata,
        invitation_metadata=invitation_metadata,
    )


def copy_package_version(
    package_root: Path,
    storage_root: Path,
    *,
    view_id: str,
    version_id: str,
    max_package_bytes: int = DEFAULT_MAX_PACKAGE_BYTES,
) -> HubStoredPackage:
    clean_view_id = _validate_hub_id(view_id, "view_id")
    clean_version_id = _validate_hub_id(version_id, "version_id")
    manifest = load_package_manifest(
        package_root,
        expected_view_id=clean_view_id,
        max_package_bytes=max_package_bytes,
    )
    entries = _package_file_entries(Path(package_root).expanduser())

    versions_root = Path(storage_root).expanduser() / "views" / clean_view_id / "versions"
    final_path = versions_root / clean_version_id
    if final_path.exists():
        raise PackageValidationError(f"view version already exists: {clean_view_id}/{clean_version_id}")
    versions_root.mkdir(parents=True, exist_ok=True)
    temp_path = versions_root / f".{clean_version_id}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    if temp_path.exists():
        shutil.rmtree(temp_path)
    temp_path.mkdir()
    try:
        for relative, path in entries:
            target = temp_path / validate_package_relative_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=True)
        temp_path.rename(final_path)
    except BaseException:
        if temp_path.exists():
            shutil.rmtree(temp_path)
        raise

    return HubStoredPackage(
        path=final_path,
        version_id=clean_version_id,
        manifest=replace(manifest, source_root=final_path),
    )


def validate_package_relative_path(relative_path: str | Path | PurePosixPath) -> str:
    text = relative_path.as_posix() if isinstance(relative_path, PurePosixPath) else Path(relative_path).as_posix()
    path = PurePosixPath(text)
    if "\\" in text or path.is_absolute() or ".." in path.parts:
        raise PackageValidationError(f"package path traversal entry: {text}")
    if not text or text == ".":
        raise PackageValidationError("package path must not be empty")
    return path.as_posix()


def _package_file_entries(source_root: Path) -> tuple[tuple[str, Path], ...]:
    if not source_root.is_dir():
        raise PackageValidationError(f"shared view package is not a directory: {source_root}")
    resolved_root = source_root.resolve()
    entries: list[tuple[str, Path]] = []
    for path in sorted(source_root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        relative = validate_package_relative_path(path.relative_to(source_root))
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                raise PackageValidationError(f"broken package symlink: {relative}") from exc
            if resolved_root not in (target, *target.parents):
                raise PackageValidationError(f"symlink escapes package root: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackageValidationError(f"package entry is not a regular file: {relative}")
        entries.append((relative, path))
    return tuple(entries)


def _load_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PackageValidationError(f"invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackageValidationError(f"{label} must contain a TOML table")
    return data


def _package_view_id(export_metadata: dict[str, Any], invitation_metadata: dict[str, Any]) -> str:
    export_view_id = _metadata_string(export_metadata, "view_id")
    invitation_view_id = _metadata_string(invitation_metadata, "view_id")
    if not export_view_id and not invitation_view_id:
        raise PackageValidationError("package metadata must include view_id")
    if export_view_id and invitation_view_id and export_view_id != invitation_view_id:
        raise PackageValidationError("package view_id does not match invitation view_id")
    return _validate_hub_id(export_view_id or invitation_view_id or "", "view_id")


def _metadata_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PackageValidationError(f"package metadata field {key} must be a string")
    value = value.strip()
    return value or None


def _validate_hub_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackageValidationError(f"{label} must be a non-empty string")
    clean = value.strip()
    path = PurePosixPath(clean)
    if "\\" in clean or "/" in clean or path.is_absolute() or ".." in path.parts:
        raise PackageValidationError(f"{label} contains path traversal: {value!r}")
    if clean in {".", ".."} or not _ID_RE.fullmatch(clean):
        raise PackageValidationError(f"{label} contains invalid characters: {value!r}")
    return clean
