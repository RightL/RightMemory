from __future__ import annotations

import json
import io
import os
import re
import shutil
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .hub.client import HubClient, HubClientError
from .shared_view_models import (
    PROVIDER_VIEWS_DIR,
    SharedViewConnection,
    load_connections,
    load_shared_view_credential,
    validate_heading_id,
)

FILE_RECIPE_KEYS = {
    "version",
    "view_id",
    "kind",
    "title",
    "approved",
    "intent",
    "render",
    "include_headings",
    "include_nodes",
    "include_files",
    "exclude_ids",
    "publish",
}
FILE_RECIPE_REQUIRED_KEYS = {
    "version",
    "view_id",
    "kind",
    "title",
    "approved",
    "intent",
    "render",
    "include_headings",
    "include_nodes",
    "include_files",
    "exclude_ids",
}
FILE_RECIPE_ARRAY_KEYS = {"include_headings", "include_nodes", "include_files", "exclude_ids"}
FILE_RECIPE_PUBLISH_KEYS = {"enabled", "hub_url", "credential_id"}


HEADING_ID_RE = re.compile(r"^(#{1,4})\s+.*?\{(?:F#|S#|MF#|MQ#|#)([A-Za-z0-9_.-]+)\}")
NODE_ID_RE = re.compile(r"^\s*-\s+`([^`]+)`")
MANAGED_EXAMPLE_START = "<!-- rightmemory:example:start -->"
MANAGED_EXAMPLE_END = "<!-- rightmemory:example:end -->"


@dataclass(frozen=True)
class FileViewRecipe:
    view_id: str
    title: str
    intent: str
    include_headings: tuple[str, ...] = ()
    include_nodes: tuple[str, ...] = ()
    include_files: tuple[str, ...] = ()
    exclude_ids: tuple[str, ...] = ()
    approved: bool = False
    publish_hub_url: str | None = None
    publish_credential_id: str | None = None


@dataclass(frozen=True)
class FileViewPullResult:
    heading_id: str
    status: str
    message: str


@dataclass(frozen=True)
class FileViewPublishResult:
    view_id: str
    status: str
    message: str


def write_file_view_recipe(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    include_headings: list[str] | tuple[str, ...] = (),
    include_nodes: list[str] | tuple[str, ...] = (),
    include_files: list[str] | tuple[str, ...] = (),
    exclude_ids: list[str] | tuple[str, ...] = (),
    approved: bool = False,
    publish_hub_url: str | None = None,
    publish_credential_id: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    recipe = FileViewRecipe(
        view_id=validate_heading_id(view_id),
        title=_required_text(title, "title"),
        intent=_required_text(intent, "intent"),
        include_headings=tuple(validate_heading_id(item) for item in include_headings),
        include_nodes=tuple(validate_heading_id(item) for item in include_nodes),
        include_files=tuple(_validate_memory_source_file(item) for item in include_files),
        exclude_ids=tuple(validate_heading_id(item) for item in exclude_ids),
        approved=bool(approved),
        publish_hub_url=_optional_text(publish_hub_url),
        publish_credential_id=validate_heading_id(publish_credential_id) if publish_credential_id else None,
    )
    view_dir = _view_dir(root, recipe.view_id)
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_text(view_dir / ".gitignore", "dist/\n")
    _write_text(view_dir / "view.md", f"# {recipe.title}\n\n{recipe.intent}\n")
    _write_text(view_dir / "recipe.toml", _render_recipe_toml(recipe))
    return f"wrote file view recipe {recipe.view_id}"


def load_file_view_recipe(memory_root: Path, view_id: str) -> FileViewRecipe:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    path = _view_dir(root, clean_view_id) / "recipe.toml"
    if not path.is_file():
        raise FileNotFoundError(f"file view recipe not found: shared_views/{clean_view_id}/recipe.toml")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("kind") != "file":
        raise ValueError(f"shared_views/{clean_view_id}/recipe.toml is not a file view recipe")
    publish = data.get("publish", {})
    if publish is None:
        publish = {}
    if not isinstance(publish, dict):
        raise ValueError("file view recipe [publish] must be a TOML table")
    return FileViewRecipe(
        view_id=validate_heading_id(str(data.get("view_id", clean_view_id))),
        title=str(data.get("title") or clean_view_id),
        intent=str(data.get("intent") or ""),
        include_headings=tuple(validate_heading_id(str(item)) for item in data.get("include_headings", []) if isinstance(item, str)),
        include_nodes=tuple(validate_heading_id(str(item)) for item in data.get("include_nodes", []) if isinstance(item, str)),
        include_files=tuple(_validate_memory_source_file(item) for item in data.get("include_files", []) if isinstance(item, str)),
        exclude_ids=tuple(validate_heading_id(str(item)) for item in data.get("exclude_ids", []) if isinstance(item, str)),
        approved=bool(data.get("approved", False)),
        publish_hub_url=str(publish.get("hub_url")).strip() if publish.get("hub_url") else None,
        publish_credential_id=validate_heading_id(str(publish.get("credential_id"))) if publish.get("credential_id") else None,
    )


def validate_file_view_recipe_source(
    memory_root: Path,
    view_id: str,
    *,
    require_selection: bool = False,
    require_publish: bool = False,
) -> FileViewRecipe:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    path = _view_dir(root, clean_view_id) / "recipe.toml"
    if not path.is_file():
        raise FileNotFoundError(f"file view recipe not found: shared_views/{clean_view_id}/recipe.toml")
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    errors = _file_view_recipe_schema_errors(data, require_publish=require_publish)
    if errors:
        raise ValueError("invalid file view recipe:\n" + "\n".join(f"- {error}" for error in errors))

    recipe = load_file_view_recipe(root, clean_view_id)
    selected = recipe.include_headings or recipe.include_nodes or recipe.include_files
    if require_selection and not selected:
        raise ValueError(
            "invalid file view recipe:\n"
            "- file view recipe must include at least one heading, node, or memory file"
        )
    return recipe


def load_all_file_view_recipes(memory_root: Path) -> list[FileViewRecipe]:
    root = Path(memory_root).expanduser()
    views_root = root / PROVIDER_VIEWS_DIR
    if not views_root.is_dir():
        return []
    recipes: list[FileViewRecipe] = []
    for recipe_path in sorted(views_root.glob("*/recipe.toml")):
        recipes.append(load_file_view_recipe(root, recipe_path.parent.name))
    return recipes


def render_file_view(memory_root: Path, view_id: str) -> str:
    root = Path(memory_root).expanduser()
    recipe = load_file_view_recipe(root, view_id)
    rendered = _render_selected_memory(root, recipe)
    view_dir = _view_dir(root, recipe.view_id)
    temp = view_dir / f".dist.tmp-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    _write_text(temp / "MEMORY.md", rendered)
    _write_text(temp / "manifest.toml", f'version = 1\nview_id = "{recipe.view_id}"\n')
    final = view_dir / "dist"
    if final.exists():
        shutil.rmtree(final)
    temp.rename(final)
    return f"rendered file view {recipe.view_id}"


def export_file_view_package(memory_root: Path, view_id: str, target_path: Path) -> str:
    root = Path(memory_root).expanduser()
    recipe = load_file_view_recipe(root, view_id)
    render_file_view(root, recipe.view_id)
    source = _view_dir(root, recipe.view_id)
    target = Path(target_path).expanduser()
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"file view package target is not a directory: {target}")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copy2(source / "view.md", target / "view.md")
    shutil.copy2(source / "recipe.toml", target / "recipe.toml")
    shutil.copytree(source / "dist", target / "dist")
    _write_text(
        target / "rightmemory-shared-view.toml",
        "\n".join(
            [
                "version = 1",
                f'view_id = "{recipe.view_id}"',
                'kind = "file"',
                f'ref = "rightmemory://mf/{recipe.view_id}"',
                f"title = {_toml_string(recipe.title)}",
                f"description = {_toml_string(recipe.intent)}",
                "",
            ]
        ),
    )
    return f"exported file view {recipe.view_id} to {target}"


def approve_file_view(memory_root: Path, view_id: str) -> str:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    _write_text(_view_dir(root, recipe.view_id) / "recipe.toml", _render_recipe_toml(_replace_recipe(recipe, approved=True)))
    return f"approved file view {recipe.view_id}"


def invite_file_view(
    memory_root: Path,
    view_id: str,
    *,
    hub_url: str | None = None,
    credential_id: str | None = None,
    label: str | None = None,
    expires_at: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    if not recipe.approved:
        raise ValueError(f"file view is not approved: {recipe.view_id}")
    resolved_hub_url = _optional_text(hub_url) or recipe.publish_hub_url
    if not resolved_hub_url:
        raise ValueError("file view invite requires a hub URL")
    resolved_credential_id = validate_heading_id(credential_id) if credential_id else recipe.publish_credential_id
    if not resolved_credential_id:
        raise ValueError("file view invite requires a credential id")
    credential = load_shared_view_credential(root, resolved_credential_id)
    with TemporaryDirectory() as tempdir:
        package = Path(tempdir) / recipe.view_id
        export_file_view_package(root, recipe.view_id, package)
        client = HubClient(resolved_hub_url, credential["token"])
        client.publish_package(recipe.view_id, package)
        invitation = client.create_invitation(recipe.view_id, label=label, expires_at=expires_at)
    invitation_url = invitation.get("invitation_url")
    if not isinstance(invitation_url, str) or not invitation_url:
        raise ValueError("hub did not return an invitation_url")
    return f"invited file view {recipe.view_id}\ninvitation_url\t{invitation_url}"


def pull_file_view(memory_root: Path, heading_id: str) -> FileViewPullResult:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    connection = load_connections(root).get(clean_heading_id)
    if connection is None or connection.view_type != "file":
        return FileViewPullResult(clean_heading_id, "unavailable", "file view connection not found")
    try:
        archive = _download_file_view_archive(root, connection)
        _replace_import_from_zip(root, clean_heading_id, archive)
        return FileViewPullResult(clean_heading_id, "pulled", "file view pulled")
    except (KeyError, ValueError, OSError, HubClientError, zipfile.BadZipFile) as exc:
        if _import_exists(root, clean_heading_id):
            return FileViewPullResult(clean_heading_id, "stale", f"using stale file view import: {exc}")
        return FileViewPullResult(clean_heading_id, "unavailable", f"file view unavailable: {exc}")


def pull_all_file_views(memory_root: Path) -> list[FileViewPullResult]:
    root = Path(memory_root).expanduser()
    return [
        pull_file_view(root, connection.heading_id)
        for connection in load_connections(root).values()
        if connection.view_type == "file"
    ]


def publish_approved_file_views(memory_root: Path) -> list[FileViewPublishResult]:
    root = Path(memory_root).expanduser()
    results: list[FileViewPublishResult] = []
    for recipe in load_all_file_view_recipes(root):
        if not recipe.approved:
            continue
        try:
            validate_file_view_recipe_source(root, recipe.view_id, require_selection=True)
        except (FileNotFoundError, ValueError) as exc:
            results.append(FileViewPublishResult(recipe.view_id, "failed", str(exc)))
            continue
        if not recipe.publish_hub_url or not recipe.publish_credential_id:
            results.append(FileViewPublishResult(recipe.view_id, "skipped", "approved recipe has no publish target"))
            continue
        try:
            with TemporaryDirectory() as tempdir:
                package = Path(tempdir) / recipe.view_id
                export_file_view_package(root, recipe.view_id, package)
                credential = load_shared_view_credential(root, recipe.publish_credential_id)
                client = HubClient(recipe.publish_hub_url, credential["token"])
                client.publish_package(recipe.view_id, package)
            results.append(FileViewPublishResult(recipe.view_id, "published", "file view published"))
        except (KeyError, ValueError, OSError, HubClientError) as exc:
            results.append(FileViewPublishResult(recipe.view_id, "failed", str(exc)))
    return results


def record_file_view_publish_results(
    memory_root: Path,
    results: list[FileViewPublishResult],
    *,
    trigger: str = "manual",
) -> None:
    if not results:
        return
    root = Path(memory_root).expanduser()
    path = root / ".runtime" / "shared_views" / "publish-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for result in results:
            handle.write(
                json.dumps(
                    {
                        "created_at": created_at,
                        "view_id": result.view_id,
                        "status": result.status,
                        "message": result.message,
                        "trigger": trigger,
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def list_file_view_publish_events(memory_root: Path, *, limit: int = 50) -> list[dict[str, object]]:
    root = Path(memory_root).expanduser()
    path = root / ".runtime" / "shared_views" / "publish-events.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    records.reverse()
    return records[: max(0, int(limit))]


def _download_file_view_archive(root: Path, connection: SharedViewConnection) -> bytes:
    target = connection.target
    if target.kind != "http-file":
        raise ValueError("file view connection does not have an HTTP file target")
    if not target.base_url or not target.credential_id:
        raise ValueError("HTTP file target is missing base_url or credential_id")
    credential = load_shared_view_credential(root, target.credential_id)
    client = HubClient(target.base_url, credential["token"])
    return client.download_package(target.view_id or connection.heading_id)


def _replace_import_from_zip(root: Path, heading_id: str, archive_bytes: bytes) -> None:
    imports_root = root / ".runtime" / "shared_views" / "imports"
    imports_root.mkdir(parents=True, exist_ok=True)
    final = imports_root / heading_id
    temp = imports_root / f".{heading_id}.tmp-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            required = {"view.md", "recipe.toml", "rightmemory-shared-view.toml", "dist/MEMORY.md", "dist/manifest.toml"}
            missing = sorted(required - set(names))
            if missing:
                raise ValueError(f"file view package missing required files: {', '.join(missing)}")
            for name in names:
                relative = _validate_package_relative_path(name)
                if relative.endswith("/"):
                    continue
                target = temp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        if final.exists():
            shutil.rmtree(final)
        temp.rename(final)
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp)
        raise

def _import_exists(root: Path, heading_id: str) -> bool:
    return (root / ".runtime" / "shared_views" / "imports" / heading_id / "dist" / "MEMORY.md").is_file()


def _render_selected_memory(root: Path, recipe: FileViewRecipe) -> str:
    sections = [f"# {recipe.title} Shared View", "", recipe.intent, "", "## Published Context", ""]
    excluded = set(recipe.exclude_ids)
    for relative in recipe.include_files:
        path = root / relative
        if path.is_file():
            sections.extend([f"### {relative}", "", path.read_text(encoding="utf-8").rstrip(), ""])
    sources = sorted(root.glob("MEMORY*.md"))
    for source in sources:
        lines = source.read_text(encoding="utf-8").splitlines()
        sections.extend(_selected_lines_from_source(lines, recipe, excluded))
    rendered = "\n".join(line for line in sections).rstrip() + "\n"
    return rendered


def _selected_lines_from_source(lines: list[str], recipe: FileViewRecipe, excluded: set[str]) -> list[str]:
    output: list[str] = []
    heading_depth: int | None = None
    excluded_subtree_depth: int | None = None
    include_subtree = False
    in_managed_example = False
    for line in lines:
        if MANAGED_EXAMPLE_START in line:
            in_managed_example = MANAGED_EXAMPLE_END not in line
            continue
        if in_managed_example:
            if MANAGED_EXAMPLE_END in line:
                in_managed_example = False
            continue
        heading_match = HEADING_ID_RE.match(line)
        if heading_match:
            depth = len(heading_match.group(1))
            item_id = heading_match.group(2)
            if excluded_subtree_depth is not None:
                if depth > excluded_subtree_depth:
                    continue
                excluded_subtree_depth = None
            if heading_depth is not None and depth <= heading_depth:
                include_subtree = False
                heading_depth = None
            if item_id in excluded:
                excluded_subtree_depth = depth
                continue
            if item_id in recipe.include_headings and item_id not in excluded:
                include_subtree = True
                heading_depth = depth
                output.append(line)
                continue
        elif excluded_subtree_depth is not None:
            continue
        node_match = NODE_ID_RE.match(line)
        if node_match and node_match.group(1) in excluded:
            continue
        if node_match and node_match.group(1) in recipe.include_nodes:
            output.append(line)
            continue
        if include_subtree:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    return output


def _render_recipe_toml(recipe: FileViewRecipe) -> str:
    lines = [
        "version = 1",
        f'view_id = "{recipe.view_id}"',
        'kind = "file"',
        f"title = {_toml_string(recipe.title)}",
        f"approved = {str(recipe.approved).lower()}",
        f"intent = {_toml_string(recipe.intent)}",
        'render = "expanded-heading-subtrees"',
        "",
        f"include_headings = {_toml_array(recipe.include_headings)}",
        f"include_nodes = {_toml_array(recipe.include_nodes)}",
        f"include_files = {_toml_array(recipe.include_files)}",
        f"exclude_ids = {_toml_array(recipe.exclude_ids)}",
    ]
    if recipe.publish_hub_url or recipe.publish_credential_id:
        lines.extend(
            [
                "",
                "[publish]",
                "enabled = true",
                f"hub_url = {_toml_string(recipe.publish_hub_url or '')}",
                f"credential_id = {_toml_string(recipe.publish_credential_id or '')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _file_view_recipe_schema_errors(data: dict[str, object], *, require_publish: bool) -> list[str]:
    errors: list[str] = []
    keys = set(data)
    unknown = sorted(keys - FILE_RECIPE_KEYS)
    if unknown:
        errors.append(
            "unsupported field(s): "
            + ", ".join(unknown)
            + "; use include_headings, include_nodes, include_files, and exclude_ids"
        )
    missing = sorted(FILE_RECIPE_REQUIRED_KEYS - keys)
    if missing:
        errors.append("missing required field(s): " + ", ".join(missing))
    if data.get("kind") != "file":
        errors.append('kind must be "file"')
    for key in sorted(FILE_RECIPE_ARRAY_KEYS):
        value = data.get(key)
        if not isinstance(value, list):
            errors.append(f"{key} must be a TOML array")
            continue
        if any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{key} entries must be non-empty strings")
    publish = data.get("publish")
    if publish is None:
        if require_publish:
            errors.append("missing required [publish] table")
        return errors
    if not isinstance(publish, dict):
        errors.append("[publish] must be a TOML table")
        return errors
    unknown_publish = sorted(set(publish) - FILE_RECIPE_PUBLISH_KEYS)
    if unknown_publish:
        errors.append("[publish] has unsupported field(s): " + ", ".join(unknown_publish))
    if require_publish:
        for key in ("hub_url", "credential_id"):
            value = publish.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"[publish].{key} must be a non-empty string")
    return errors


def _replace_recipe(recipe: FileViewRecipe, *, approved: bool) -> FileViewRecipe:
    return FileViewRecipe(
        view_id=recipe.view_id,
        title=recipe.title,
        intent=recipe.intent,
        include_headings=recipe.include_headings,
        include_nodes=recipe.include_nodes,
        include_files=recipe.include_files,
        exclude_ids=recipe.exclude_ids,
        approved=approved,
        publish_hub_url=recipe.publish_hub_url,
        publish_credential_id=recipe.publish_credential_id,
    )


def _validate_package_relative_path(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    if "\\" in relative_path or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"package path traversal entry: {relative_path}")
    if not relative_path or relative_path == ".":
        raise ValueError("package path must not be empty")
    return path.as_posix()


def _view_dir(root: Path, view_id: str) -> Path:
    return root / PROVIDER_VIEWS_DIR / validate_heading_id(view_id)


def _validate_memory_source_file(value: str) -> str:
    path = Path(value)
    text = path.as_posix()
    if path.is_absolute() or ".." in path.parts or not re.fullmatch(r"MEMORY(?:_[A-Za-z0-9_.-]+)?\.md", text):
        raise ValueError(f"file view include_files entry must be a memory file: {value}")
    return text


def _required_text(value: str, label: str) -> str:
    clean = str(value).strip()
    if not clean:
        raise ValueError(f"file view {label} must not be empty")
    return clean


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
