from __future__ import annotations

import io
import os
import shutil
import stat
import tomllib
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .graph import GraphManifest, build_mf_manifest
from .session import _fsync_directory
from .shared_view_models import validate_heading_id


PACKAGE_VERSION = 2
RECIPE_VERSION = 1
DOCUMENT_KIND = "rightmemory-memory"
REQUIRED_PACKAGE_FILES = {
    "view.md",
    "recipe.toml",
    "rightmemory-shared-view.toml",
    "dist/MEMORY.md",
    "dist/manifest.toml",
}
PACKAGE_METADATA_FILES = {
    "view.md",
    "recipe.toml",
    "rightmemory-shared-view.toml",
}
DIST_REQUIRED_FILES = {"MEMORY.md", "manifest.toml"}
MAX_ARCHIVE_ENTRIES = 512
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024


class FileViewPackageError(ValueError):
    """A file-view package is unsafe or does not implement the MF v2 contract."""


@dataclass(frozen=True)
class ValidatedFileViewPackage:
    root: Path
    view_id: str
    namespace: str
    manifest: GraphManifest


def validate_mf_dist(
    dist_root: Path,
    *,
    expected_view_id: str,
    namespace_id: str | None = None,
) -> GraphManifest:
    """Validate the exact provider or consumer dist candidate."""
    root = Path(dist_root)
    clean_view_id = validate_heading_id(expected_view_id)
    clean_namespace = validate_heading_id(namespace_id or clean_view_id)
    files = _regular_tree_files(root, allow_directories=False)
    names = {relative.as_posix() for relative, _path in files}
    missing = sorted(DIST_REQUIRED_FILES - names)
    if missing:
        raise FileViewPackageError(f"MF dist is missing required files: {', '.join(missing)}")
    for name in sorted(names):
        if not _allowed_dist_file(name):
            raise FileViewPackageError(f"unexpected MF dist file: {name}")

    metadata = _load_toml(root / "manifest.toml", "dist/manifest.toml")
    _require_exact_keys(
        metadata,
        {"version", "view_id", "document_kind"},
        "dist/manifest.toml",
    )
    _require_version(metadata, PACKAGE_VERSION, "dist/manifest.toml")
    _require_matching_view_id(metadata, clean_view_id, "dist/manifest.toml")
    if metadata.get("document_kind") != DOCUMENT_KIND:
        raise FileViewPackageError(
            f'dist/manifest.toml document_kind must be "{DOCUMENT_KIND}"'
        )

    manifest = build_mf_manifest(root, clean_namespace)
    if manifest.errors:
        raise FileViewPackageError(
            "invalid MF Memory document:\n"
            + "\n".join(f"- {message}" for message in manifest.errors)
        )

    represented = {
        path.relative_to(root.resolve()).as_posix()
        for path in (*manifest.graph_files, *manifest.non_graph_files)
    }
    actual_resources = names - {"manifest.toml"}
    unexpected_resources = sorted(actual_resources - represented)
    if unexpected_resources:
        raise FileViewPackageError(
            "unreferenced MF resource file(s): " + ", ".join(unexpected_resources)
        )
    return manifest


def validate_file_view_package(
    package_root: Path,
    *,
    expected_view_id: str,
    namespace_id: str | None = None,
) -> ValidatedFileViewPackage:
    """Validate package metadata and the exact nested dist candidate."""
    root = Path(package_root)
    clean_view_id = validate_heading_id(expected_view_id)
    clean_namespace = validate_heading_id(namespace_id or clean_view_id)
    files = _regular_tree_files(root, allow_directories=True)
    names = {relative.as_posix() for relative, _path in files}
    missing = sorted(REQUIRED_PACKAGE_FILES - names)
    if missing:
        raise FileViewPackageError(
            f"file view package is missing required files: {', '.join(missing)}"
        )
    for name in sorted(names):
        if name in PACKAGE_METADATA_FILES:
            continue
        if not name.startswith("dist/") or not _allowed_dist_file(name.removeprefix("dist/")):
            raise FileViewPackageError(f"unexpected file view package file: {name}")

    recipe = _load_toml(root / "recipe.toml", "recipe.toml")
    _require_version(recipe, RECIPE_VERSION, "recipe.toml")
    _require_matching_view_id(recipe, clean_view_id, "recipe.toml")
    if recipe.get("kind") != "file":
        raise FileViewPackageError('recipe.toml kind must be "file"')

    package_metadata = _load_toml(
        root / "rightmemory-shared-view.toml",
        "rightmemory-shared-view.toml",
    )
    allowed_metadata = {"version", "view_id", "kind", "ref", "title", "description"}
    unknown_metadata = sorted(set(package_metadata) - allowed_metadata)
    if unknown_metadata:
        raise FileViewPackageError(
            "rightmemory-shared-view.toml has unsupported field(s): "
            + ", ".join(unknown_metadata)
        )
    _require_version(package_metadata, PACKAGE_VERSION, "rightmemory-shared-view.toml")
    _require_matching_view_id(package_metadata, clean_view_id, "rightmemory-shared-view.toml")
    if package_metadata.get("kind") != "file":
        raise FileViewPackageError('rightmemory-shared-view.toml kind must be "file"')

    manifest = validate_mf_dist(
        root / "dist",
        expected_view_id=clean_view_id,
        namespace_id=clean_namespace,
    )
    return ValidatedFileViewPackage(root.resolve(), clean_view_id, clean_namespace, manifest)


def valid_file_view_package(
    package_root: Path,
    *,
    expected_view_id: str,
    namespace_id: str | None = None,
) -> bool:
    try:
        validate_file_view_package(
            package_root,
            expected_view_id=expected_view_id,
            namespace_id=namespace_id,
        )
    except (FileNotFoundError, OSError, FileViewPackageError, tomllib.TOMLDecodeError):
        return False
    return True


def extract_package_archive(archive_bytes: bytes, target_root: Path) -> None:
    """Safely extract an HTTP package without laundering archive entry types."""
    target = Path(target_root)
    if target.exists():
        raise FileViewPackageError(f"package extraction target already exists: {target}")
    target.mkdir(parents=True)
    seen: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise FileViewPackageError(
                    f"file view package has more than {MAX_ARCHIVE_ENTRIES} archive entries"
                )
            for info in entries:
                relative = validate_package_relative_path(info.filename)
                folded = relative.casefold().rstrip("/")
                if folded in seen:
                    raise FileViewPackageError(
                        f"duplicate or case-colliding package archive entry: {info.filename}"
                    )
                seen.add(folded)
                _validate_zip_entry_type(info)
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise FileViewPackageError(
                        f"file view package exceeds {MAX_ARCHIVE_BYTES} uncompressed bytes"
                    )
                if info.is_dir():
                    if relative.rstrip("/") != "dist":
                        raise FileViewPackageError(
                            f"unexpected package archive directory: {info.filename}"
                        )
                    (target / "dist").mkdir(exist_ok=True)
                    continue
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def promote_directory_candidate(candidate: Path, final: Path) -> None:
    """Promote a validated sibling candidate while retaining the old tree on errors."""
    source = Path(candidate)
    destination = Path(final)
    if source.parent != destination.parent:
        raise ValueError("directory candidate and destination must be siblings")
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"directory candidate is not a regular directory: {source}")
    backup = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
    moved_previous = False
    try:
        if _path_exists(destination):
            os.replace(destination, backup)
            moved_previous = True
        os.replace(source, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        if moved_previous and not _path_exists(destination) and _path_exists(backup):
            os.replace(backup, destination)
            _fsync_directory(destination.parent)
        raise
    finally:
        if _path_exists(backup) and _path_exists(destination):
            _remove_path(backup)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def validate_package_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise FileViewPackageError("package path must not be empty")
    if "\\" in relative_path or "\x00" in relative_path:
        raise FileViewPackageError(f"unsafe package path: {relative_path!r}")
    raw_parts = relative_path.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise FileViewPackageError(f"unsafe package path: {relative_path!r}")
    path = PurePosixPath(relative_path)
    if path.is_absolute():
        raise FileViewPackageError(f"unsafe package path: {relative_path!r}")
    return path.as_posix() + ("/" if relative_path.endswith("/") else "")


def _regular_tree_files(root: Path, *, allow_directories: bool) -> tuple[tuple[Path, Path], ...]:
    if root.is_symlink() or not root.is_dir():
        raise FileViewPackageError(f"file view package path must be a regular directory: {root}")
    resolved_root = root.resolve(strict=True)
    entries: list[tuple[Path, Path]] = []
    casefolded: dict[str, str] = {}
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            path = current_path / directory
            relative = path.relative_to(root)
            if path.is_symlink():
                raise FileViewPackageError(f"package entry must not be a symlink: {relative.as_posix()}")
            if current_path != root or directory != "dist":
                if not allow_directories:
                    raise FileViewPackageError(f"unexpected MF dist directory: {relative.as_posix()}")
                raise FileViewPackageError(f"unexpected file view package directory: {relative.as_posix()}")
        for filename in files:
            path = current_path / filename
            relative = path.relative_to(root)
            text = relative.as_posix()
            folded = text.casefold()
            previous = casefolded.get(folded)
            if previous is not None and previous != text:
                raise FileViewPackageError(
                    f"case-colliding package paths: {previous}, {text}"
                )
            casefolded[folded] = text
            if path.is_symlink():
                raise FileViewPackageError(f"package entry must not be a symlink: {text}")
            if not path.is_file():
                raise FileViewPackageError(f"package entry must be a regular file: {text}")
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise FileViewPackageError(f"package entry escapes its root: {text}") from exc
            entries.append((relative, path))
    return tuple(sorted(entries, key=lambda item: item[0].as_posix()))


def _allowed_dist_file(name: str) -> bool:
    if "/" in name or name in {"", ".", ".."}:
        return False
    if name in DIST_REQUIRED_FILES:
        return True
    if not name.endswith(".md"):
        return False
    if name.startswith("MEMORY_SKILL_"):
        item_id = name.removeprefix("MEMORY_SKILL_").removesuffix(".md")
    elif name.startswith("MEMORY_"):
        item_id = name.removeprefix("MEMORY_").removesuffix(".md")
    else:
        return False
    try:
        validate_heading_id(item_id)
    except ValueError:
        return False
    return True


def _load_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise FileViewPackageError(f"invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise FileViewPackageError(f"{label} must contain a TOML table")
    return data


def _require_exact_keys(data: dict[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(data))
    unknown = sorted(set(data) - expected)
    if missing:
        raise FileViewPackageError(f"{label} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise FileViewPackageError(f"{label} has unsupported field(s): {', '.join(unknown)}")


def _require_version(data: dict[str, Any], expected: int, label: str) -> None:
    version = data.get("version")
    if isinstance(version, bool) or version != expected:
        raise FileViewPackageError(f"{label} version must be {expected}")


def _require_matching_view_id(data: dict[str, Any], expected: str, label: str) -> None:
    raw = data.get("view_id")
    if not isinstance(raw, str):
        raise FileViewPackageError(f"{label} view_id must be a string")
    try:
        actual = validate_heading_id(raw)
    except ValueError as exc:
        raise FileViewPackageError(f"{label} has an invalid view_id") from exc
    if actual != expected:
        raise FileViewPackageError(
            f"{label} view_id {actual!r} does not match expected view_id {expected!r}"
        )


def _validate_zip_entry_type(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & 0x1:
        raise FileViewPackageError(f"encrypted package archive entry is not supported: {info.filename}")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if info.is_dir():
        if kind not in {0, stat.S_IFDIR}:
            raise FileViewPackageError(f"invalid package archive directory: {info.filename}")
        return
    if kind not in {0, stat.S_IFREG}:
        raise FileViewPackageError(f"package archive entry must be a regular file: {info.filename}")
