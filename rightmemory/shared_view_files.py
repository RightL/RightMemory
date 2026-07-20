from __future__ import annotations

import json
import os
import shutil
import tomllib
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, mkdtemp

from .graph import BlockKey, DocumentBlock, GraphManifest, build_graph_manifest
from .hub.client import HubClient, HubClientError
from .shared_view_models import (
    PROVIDER_VIEWS_DIR,
    SharedViewConnection,
    load_connections,
    load_shared_view_credential,
    validate_heading_id,
)
from .session import _ensure_durable_directory, _fsync_directory
from .shared_view_package import (
    DOCUMENT_KIND,
    PACKAGE_VERSION,
    extract_package_archive,
    promote_directory_candidate,
    valid_file_view_package,
    validate_file_view_package,
    validate_mf_dist,
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
    "semantic_refresh_days",
    "last_semantic_refresh_at",
    "last_semantic_refresh_memory_commit",
}
FILE_RECIPE_REQUIRED_KEYS = {
    "version",
    "view_id",
    "kind",
    "title",
    "approved",
    "intent",
    "render",
    "semantic_refresh_days",
    "last_semantic_refresh_at",
    "last_semantic_refresh_memory_commit",
}
FILE_RECIPE_ARRAY_KEYS = {"include_headings", "include_nodes", "include_files", "exclude_ids"}
FILE_RECIPE_PUBLISH_KEYS = {"enabled", "hub_url", "credential_id"}
FILE_VIEW_RENDER_EXTRACTIVE = "extractive"
FILE_VIEW_RENDER_GENERATIVE = "generative"
FILE_VIEW_RENDER_VALUES = {FILE_VIEW_RENDER_EXTRACTIVE, FILE_VIEW_RENDER_GENERATIVE}
DEFAULT_SEMANTIC_REFRESH_DAYS = 7


MANAGED_EXAMPLE_START = "<!-- rightmemory:example:start -->"
MANAGED_EXAMPLE_END = "<!-- rightmemory:example:end -->"


@dataclass(frozen=True)
class FileViewRecipe:
    view_id: str
    title: str
    intent: str
    render: str = FILE_VIEW_RENDER_EXTRACTIVE
    include_headings: tuple[str, ...] = ()
    include_nodes: tuple[str, ...] = ()
    include_files: tuple[str, ...] = ()
    exclude_ids: tuple[str, ...] = ()
    approved: bool = False
    publish_hub_url: str | None = None
    publish_credential_id: str | None = None
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS
    last_semantic_refresh_at: str = ""
    last_semantic_refresh_memory_commit: str = ""


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


def write_extractive_file_view_recipe(
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
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS,
    last_semantic_refresh_at: str = "",
    last_semantic_refresh_memory_commit: str = "",
) -> str:
    root = Path(memory_root).expanduser()
    recipe = FileViewRecipe(
        view_id=validate_heading_id(view_id),
        title=_required_text(title, "title"),
        intent=_required_text(intent, "intent"),
        render=FILE_VIEW_RENDER_EXTRACTIVE,
        include_headings=tuple(validate_heading_id(item) for item in include_headings),
        include_nodes=tuple(validate_heading_id(item) for item in include_nodes),
        include_files=tuple(_validate_graph_source_file(root, item) for item in include_files),
        exclude_ids=tuple(validate_heading_id(item) for item in exclude_ids),
        approved=bool(approved),
        publish_hub_url=_optional_text(publish_hub_url),
        publish_credential_id=validate_heading_id(publish_credential_id) if publish_credential_id else None,
        semantic_refresh_days=_validate_refresh_days(semantic_refresh_days),
        last_semantic_refresh_at=str(last_semantic_refresh_at),
        last_semantic_refresh_memory_commit=str(last_semantic_refresh_memory_commit),
    )
    _write_file_view_source(root, recipe)
    return f"wrote extractive file view recipe {recipe.view_id}"


def _write_file_view_source(root: Path, recipe: FileViewRecipe) -> None:
    view_dir = _view_dir(root, recipe.view_id)
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_file_view_source_files(view_dir, recipe)


def _write_file_view_source_files(view_dir: Path, recipe: FileViewRecipe) -> None:
    _write_text(view_dir / ".gitignore", "dist/\n")
    _write_text(view_dir / "view.md", f"# {recipe.title}\n\n{recipe.intent}\n")
    _write_text(view_dir / "recipe.toml", _render_recipe_toml(recipe))


def write_file_view_source_from_recipe(memory_root: Path, recipe: FileViewRecipe) -> str:
    root = Path(memory_root).expanduser()
    _write_file_view_source(root, recipe)
    return f"wrote file view recipe {recipe.view_id}"


def write_generative_file_view(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    memory_document: str,
    approved: bool = False,
    publish_hub_url: str | None = None,
    publish_credential_id: str | None = None,
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS,
    last_semantic_refresh_at: str = "",
    last_semantic_refresh_memory_commit: str = "",
) -> str:
    body = _required_text(memory_document, "memory_document")
    root = Path(memory_root).expanduser()
    recipe = FileViewRecipe(
        view_id=validate_heading_id(view_id),
        title=_required_text(title, "title"),
        intent=_required_text(intent, "intent"),
        render=FILE_VIEW_RENDER_GENERATIVE,
        approved=bool(approved),
        publish_hub_url=_optional_text(publish_hub_url),
        publish_credential_id=validate_heading_id(publish_credential_id) if publish_credential_id else None,
        semantic_refresh_days=_validate_refresh_days(semantic_refresh_days),
        last_semantic_refresh_at=str(last_semantic_refresh_at),
        last_semantic_refresh_memory_commit=str(last_semantic_refresh_memory_commit),
    )
    _write_generated_file_view(root, recipe, body)
    return f"wrote generative file view {recipe.view_id}"


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
    render = str(data.get("render") or "").strip()
    return FileViewRecipe(
        view_id=validate_heading_id(str(data.get("view_id", clean_view_id))),
        title=str(data.get("title") or clean_view_id),
        intent=str(data.get("intent") or ""),
        render=render,
        include_headings=tuple(validate_heading_id(str(item)) for item in data.get("include_headings", []) if isinstance(item, str)),
        include_nodes=tuple(validate_heading_id(str(item)) for item in data.get("include_nodes", []) if isinstance(item, str)),
        include_files=tuple(
            _validate_graph_source_file(root, item)
            for item in data.get("include_files", [])
            if isinstance(item, str)
        ),
        exclude_ids=tuple(validate_heading_id(str(item)) for item in data.get("exclude_ids", []) if isinstance(item, str)),
        approved=bool(data.get("approved", False)),
        publish_hub_url=str(publish.get("hub_url")).strip() if publish.get("hub_url") else None,
        publish_credential_id=validate_heading_id(str(publish.get("credential_id"))) if publish.get("credential_id") else None,
        semantic_refresh_days=_validate_refresh_days(data.get("semantic_refresh_days", DEFAULT_SEMANTIC_REFRESH_DAYS)),
        last_semantic_refresh_at=str(data.get("last_semantic_refresh_at") or ""),
        last_semantic_refresh_memory_commit=str(data.get("last_semantic_refresh_memory_commit") or ""),
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
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE and require_selection and not selected:
        raise ValueError(
            "invalid file view recipe:\n"
            "- extractive file view recipe must include at least one heading, node, or memory file"
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
    if recipe.render == FILE_VIEW_RENDER_GENERATIVE:
        _require_generated_file_view_output(root, recipe)
        return f"generated file view {recipe.view_id} already exists"
    rendered = _render_selected_memory(root, recipe)
    view_dir = _view_dir(root, recipe.view_id)
    view_dir.mkdir(parents=True, exist_ok=True)
    temp = Path(mkdtemp(prefix=".dist.candidate-", dir=view_dir))
    for relative, text in rendered.items():
        _write_text(temp / relative, text)
    _write_text(temp / "manifest.toml", _render_dist_manifest(recipe.view_id))
    final = view_dir / "dist"
    try:
        validate_mf_dist(temp, expected_view_id=recipe.view_id)
        promote_directory_candidate(temp, final)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return f"rendered file view {recipe.view_id}"


def export_file_view_package(memory_root: Path, view_id: str, target_path: Path) -> str:
    root = Path(memory_root).expanduser()
    recipe = load_file_view_recipe(root, view_id)
    render_file_view(root, recipe.view_id)
    source = _view_dir(root, recipe.view_id)
    target = Path(target_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.is_dir():
        raise ValueError(f"file view package target is not a directory: {target}")
    candidate = Path(mkdtemp(prefix=f".{target.name}.candidate-", dir=target.parent))
    try:
        shutil.copy2(source / "view.md", candidate / "view.md")
        shutil.copy2(source / "recipe.toml", candidate / "recipe.toml")
        shutil.copytree(source / "dist", candidate / "dist", symlinks=True)
        _write_text(
            candidate / "rightmemory-shared-view.toml",
            "\n".join(
                [
                    f"version = {PACKAGE_VERSION}",
                    f'view_id = "{recipe.view_id}"',
                    'kind = "file"',
                    f'ref = "rightmemory://mf/{recipe.view_id}"',
                    f"title = {_toml_string(recipe.title)}",
                    f"description = {_toml_string(recipe.intent)}",
                    "",
                ]
            ),
        )
        validate_file_view_package(candidate, expected_view_id=recipe.view_id)
        promote_directory_candidate(candidate, target)
    finally:
        shutil.rmtree(candidate, ignore_errors=True)
    return f"exported file view {recipe.view_id} to {target}"


def approve_file_view(memory_root: Path, view_id: str) -> str:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    if recipe.render == FILE_VIEW_RENDER_GENERATIVE:
        _require_generated_file_view_output(root, recipe)
    else:
        render_file_view(root, recipe.view_id)
    validate_mf_dist(
        _view_dir(root, recipe.view_id) / "dist",
        expected_view_id=recipe.view_id,
    )
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
    client = HubClient(resolved_hub_url, credential["token"])
    _publish_file_view_package_with_client(root, recipe.view_id, client=client)
    invitation = client.create_invitation(
        recipe.view_id,
        label=label,
        expires_at=expires_at,
    )
    invitation_url = invitation.get("invitation_url")
    if not isinstance(invitation_url, str) or not invitation_url:
        raise ValueError("hub did not return an invitation_url")
    return f"invited file view {recipe.view_id}\ninvitation_url\t{invitation_url}"


def publish_file_view_package(
    memory_root: Path,
    view_id: str,
    *,
    hub_url: str,
    credential_id: str,
) -> dict[str, object]:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    if not recipe.approved:
        raise ValueError(f"file view is not approved: {recipe.view_id}")
    clean_hub_url = _required_text(hub_url, "hub_url")
    clean_credential_id = validate_heading_id(credential_id)
    credential = load_shared_view_credential(root, clean_credential_id)
    return _publish_file_view_package_with_client(
        root,
        recipe.view_id,
        client=HubClient(clean_hub_url, credential["token"]),
    )


def _publish_file_view_package_with_client(
    memory_root: Path,
    view_id: str,
    *,
    client: HubClient,
) -> dict[str, object]:
    root = Path(memory_root).expanduser()
    recipe = validate_file_view_recipe_source(root, view_id, require_selection=True)
    if not recipe.approved:
        raise ValueError(f"file view is not approved: {recipe.view_id}")
    with TemporaryDirectory() as tempdir:
        package = Path(tempdir) / recipe.view_id
        export_file_view_package(root, recipe.view_id, package)
        response = client.publish_package(recipe.view_id, package)
    if not isinstance(response, dict):
        raise ValueError("hub did not return a publish response")
    return response


def pull_file_view(memory_root: Path, heading_id: str) -> FileViewPullResult:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    connection = load_connections(root).get(clean_heading_id)
    if connection is None or connection.view_type != "file":
        return FileViewPullResult(clean_heading_id, "unavailable", "file view connection not found")
    if connection.target.kind == "git-file":
        try:
            from .git_share_transport import pull_git_file_view

            pull_git_file_view(root, connection.target, clean_heading_id)
            return FileViewPullResult(clean_heading_id, "pulled", "Git file view pulled")
        except (ValueError, OSError, RuntimeError) as exc:
            if _import_exists(
                root,
                clean_heading_id,
                expected_view_id=connection.target.view_id or clean_heading_id,
            ):
                return FileViewPullResult(clean_heading_id, "stale", f"using stale file view import: {exc}")
            return FileViewPullResult(clean_heading_id, "unavailable", f"file view unavailable: {exc}")
    try:
        archive = _download_file_view_archive(root, connection)
        _replace_import_from_zip(
            root,
            clean_heading_id,
            connection.target.view_id or clean_heading_id,
            archive,
        )
        return FileViewPullResult(clean_heading_id, "pulled", "file view pulled")
    except (KeyError, ValueError, OSError, HubClientError, zipfile.BadZipFile) as exc:
        if _import_exists(
            root,
            clean_heading_id,
            expected_view_id=connection.target.view_id or clean_heading_id,
        ):
            return FileViewPullResult(clean_heading_id, "stale", f"using stale file view import: {exc}")
        return FileViewPullResult(clean_heading_id, "unavailable", f"file view unavailable: {exc}")


def pull_all_file_views(memory_root: Path) -> list[FileViewPullResult]:
    root = Path(memory_root).expanduser()
    return [
        pull_file_view(root, connection.heading_id)
        for connection in load_connections(root).values()
        if connection.view_type == "file"
    ]


def publish_approved_file_views(
    memory_root: Path,
    *,
    operation_id: str | None = None,
    credential_root: Path | None = None,
) -> list[FileViewPublishResult]:
    root = Path(memory_root).expanduser()
    credentials = root if credential_root is None else Path(credential_root).expanduser()
    with TemporaryDirectory(prefix="rightmemory-publish-") as tempdir:
        outbox = Path(tempdir) / "outbox"
        prepare_file_view_publish_outbox(root, outbox)
        return publish_file_view_outbox(
            outbox,
            credential_root=credentials,
            operation_id=operation_id,
        )


def prepare_file_view_publish_outbox(memory_root: Path, outbox_root: Path) -> None:
    """Freeze every approved file-view package before any network request."""
    root = Path(memory_root).expanduser()
    outbox = Path(outbox_root).expanduser()
    if outbox.exists():
        _load_publish_outbox(outbox)
        return

    _ensure_durable_directory(outbox.parent)
    staging = outbox.with_name(f".{outbox.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    staging.mkdir()
    entries: list[dict[str, str]] = []
    try:
        packages = staging / "packages"
        packages.mkdir()
        for recipe in load_all_file_view_recipes(root):
            if not recipe.approved:
                continue
            try:
                validate_file_view_recipe_source(root, recipe.view_id, require_selection=True)
            except (FileNotFoundError, ValueError) as exc:
                entries.append(
                    {
                        "view_id": recipe.view_id,
                        "status": "failed",
                        "message": str(exc),
                    }
                )
                continue
            if not recipe.publish_hub_url or not recipe.publish_credential_id:
                entries.append(
                    {
                        "view_id": recipe.view_id,
                        "status": "skipped",
                        "message": "approved recipe has no publish target",
                    }
                )
                continue
            package = packages / recipe.view_id
            try:
                export_file_view_package(root, recipe.view_id, package)
            except (FileNotFoundError, ValueError, OSError) as exc:
                shutil.rmtree(package, ignore_errors=True)
                entries.append(
                    {
                        "view_id": recipe.view_id,
                        "status": "failed",
                        "message": str(exc),
                    }
                )
                continue
            entries.append(
                {
                    "view_id": recipe.view_id,
                    "status": "ready",
                    "message": "file view package prepared",
                    "hub_url": recipe.publish_hub_url,
                    "credential_id": recipe.publish_credential_id,
                }
            )
        (staging / "manifest.json").write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _fsync_publish_outbox(staging)
        try:
            os.replace(staging, outbox)
        except OSError:
            if not outbox.exists():
                raise
            _load_publish_outbox(outbox)
        _fsync_directory(outbox.parent)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def publish_file_view_outbox(
    outbox_root: Path,
    *,
    credential_root: Path,
    operation_id: str | None = None,
) -> list[FileViewPublishResult]:
    outbox = Path(outbox_root).expanduser()
    entries = _load_publish_outbox(outbox)
    results: list[FileViewPublishResult] = []
    for entry in entries:
        view_id = entry["view_id"]
        status = entry["status"]
        if status != "ready":
            results.append(FileViewPublishResult(view_id, status, entry["message"]))
            continue
        try:
            credential = load_shared_view_credential(credential_root, entry["credential_id"])
            client = HubClient(entry["hub_url"], credential["token"])
            package = outbox / "packages" / view_id
            if operation_id is None:
                client.publish_package(view_id, package)
            else:
                client.publish_package(
                    view_id,
                    package,
                    idempotency_key=f"{operation_id}:{view_id}",
                )
            results.append(FileViewPublishResult(view_id, "published", "file view published"))
        except (KeyError, ValueError, OSError, HubClientError) as exc:
            results.append(FileViewPublishResult(view_id, "failed", str(exc)))
    return results


def _load_publish_outbox(outbox: Path) -> list[dict[str, str]]:
    data = json.loads((outbox / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"version", "entries"} or data["version"] != 1:
        raise ValueError(f"invalid file-view publish outbox: {outbox}")
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError(f"invalid file-view publish outbox entries: {outbox}")
    entries: list[dict[str, str]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError(f"invalid file-view publish outbox entry: {outbox}")
        status = raw.get("status")
        expected = {"view_id", "status", "message", "hub_url", "credential_id"} if status == "ready" else {
            "view_id",
            "status",
            "message",
        }
        if set(raw) != expected or status not in {"ready", "skipped", "failed"}:
            raise ValueError(f"invalid file-view publish outbox entry: {outbox}")
        if not all(isinstance(value, str) and value for value in raw.values()):
            raise ValueError(f"invalid file-view publish outbox value: {outbox}")
        view_id = validate_heading_id(raw["view_id"])
        if status == "ready" and not (outbox / "packages" / view_id).is_dir():
            raise ValueError(f"file-view publish outbox package is missing: {view_id}")
        entries.append(dict(raw))
    return entries


def _fsync_publish_outbox(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


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


def _replace_import_from_zip(
    root: Path,
    heading_id: str,
    expected_view_id: str,
    archive_bytes: bytes,
) -> None:
    imports_root = root / ".runtime" / "shared_views" / "imports"
    imports_root.mkdir(parents=True, exist_ok=True)
    final = imports_root / heading_id
    temp = imports_root / f".{heading_id}.candidate-{uuid.uuid4().hex}"
    try:
        extract_package_archive(archive_bytes, temp)
        validate_file_view_package(
            temp,
            expected_view_id=expected_view_id,
            namespace_id=heading_id,
        )
        promote_directory_candidate(temp, final)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise

def _import_exists(root: Path, heading_id: str, *, expected_view_id: str | None = None) -> bool:
    return valid_file_view_package(
        root / ".runtime" / "shared_views" / "imports" / heading_id,
        expected_view_id=expected_view_id or heading_id,
        namespace_id=heading_id,
    )


def _render_selected_memory(root: Path, recipe: FileViewRecipe) -> dict[str, str]:
    manifest = build_graph_manifest(root)
    if manifest.errors:
        raise ValueError(
            "cannot render file view from invalid RightMemory graph:\n"
            + "\n".join(f"- {message}" for message in manifest.errors)
        )
    projection = _FileViewProjection(manifest, recipe)
    rendered = projection.render()
    if not rendered.get("MEMORY.md", "").strip():
        raise ValueError("file view selection produced an empty Memory document")
    return rendered


def _write_generated_file_view(root: Path, recipe: FileViewRecipe, memory_document: str) -> None:
    views_dir = root / PROVIDER_VIEWS_DIR
    views_dir.mkdir(parents=True, exist_ok=True)
    candidate = Path(mkdtemp(prefix=f".{recipe.view_id}.candidate-", dir=views_dir))
    try:
        _write_file_view_source_files(candidate, recipe)
        dist = candidate / "dist"
        dist.mkdir()
        _write_text(dist / "MEMORY.md", memory_document.strip() + "\n")
        _write_text(dist / "manifest.toml", _render_dist_manifest(recipe.view_id))
        validate_mf_dist(dist, expected_view_id=recipe.view_id)
        promote_directory_candidate(candidate, _view_dir(root, recipe.view_id))
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def _require_generated_file_view_output(root: Path, recipe: FileViewRecipe) -> None:
    dist = _view_dir(root, recipe.view_id) / "dist"
    if not (dist / "MEMORY.md").is_file():
        raise ValueError(f"generative file view output is missing: shared_views/{recipe.view_id}/dist/MEMORY.md")
    validate_mf_dist(dist, expected_view_id=recipe.view_id)


class _FileViewProjection:
    def __init__(self, manifest: GraphManifest, recipe: FileViewRecipe):
        self.manifest = manifest
        self.recipe = recipe
        self.full_roots: set[BlockKey] = set()
        self.exact_nodes: set[BlockKey] = set()
        self.excluded_roots = {
            item.block_key
            for item_id in recipe.exclude_ids
            if (item := manifest.items.get(item_id)) is not None and item.block_key is not None
        }
        self.f_owner_by_path = {
            reference.path.resolve(): self.manifest.items[reference.id].block_key
            for reference in self.manifest.backing.values()
            if reference.kind == "F#" and reference.id in self.manifest.items
        }
        self.needed: set[BlockKey] = set()
        self._select_recipe_content()

    def render(self) -> dict[str, str]:
        documents: dict[str, list[str]] = {"MEMORY.md": []}
        f_backings = {
            reference.path.resolve(): reference
            for reference in self.manifest.backing.values()
            if reference.kind == "F#"
        }
        for document in sorted(
            self.manifest.documents.values(),
            key=lambda item: item.source_order,
        ):
            owner_key = self.f_owner_by_path.get(document.path.resolve())
            document_full = document.root_key in self.full_roots or bool(
                owner_key is not None and self._is_under_full(owner_key)
            )
            document_needed = document.root_key in self.needed or any(
                block.source_path == document.path and self._is_included(block.key)
                for block in self.manifest.blocks.values()
                if block.kind != "root"
            )
            if not document_full and not document_needed:
                continue
            text = self._render_block(
                document.root_key,
                inherited_full=document_full,
                force_include=True,
            ).strip()
            if not text:
                continue
            text = _strip_managed_examples(text).strip()
            if not text:
                continue
            reference = f_backings.get(document.path.resolve())
            if reference is None:
                documents["MEMORY.md"].append(text)
            else:
                documents[f"MEMORY_{reference.id}.md"] = [text]

        output = {
            relative: "\n\n".join(parts).rstrip() + "\n"
            for relative, parts in documents.items()
            if parts
        }
        for item in self.manifest.items.values():
            if item.block_key is None or not self._is_included(item.block_key):
                continue
            reference = self.manifest.backing.get(item.id)
            if reference is None or reference.kind not in {"M#", "S#"}:
                continue
            if reference.kind == "M#":
                relative = f"MEMORY_{reference.id}.md"
            else:
                relative = f"MEMORY_SKILL_{reference.id}.md"
            output[relative] = reference.path.read_text(encoding="utf-8")
        return output

    def _select_recipe_content(self) -> None:
        for item_id in self.recipe.include_headings:
            item = self.manifest.items.get(item_id)
            if (
                item is None
                or item.block_key is None
                or self.manifest.blocks[item.block_key].kind != "heading"
            ):
                raise ValueError(f"file view include_headings references unknown heading: {item_id}")
            self.full_roots.add(item.block_key)
        for item_id in self.recipe.include_nodes:
            item = self.manifest.items.get(item_id)
            if (
                item is None
                or item.block_key is None
                or self.manifest.blocks[item.block_key].kind != "node"
            ):
                raise ValueError(f"file view include_nodes references unknown node: {item_id}")
            self.exact_nodes.add(item.block_key)

        documents_by_relative = {
            document.relative_path: document
            for document in self.manifest.documents.values()
        }
        for relative in self.recipe.include_files:
            document = documents_by_relative.get(relative)
            if document is None:
                raise ValueError(
                    f"file view include_files entry must be a RightMemory graph file: {relative}"
                )
            self.full_roots.add(document.root_key)
            owner_key = self.f_owner_by_path.get(document.path.resolve())
            if owner_key is not None:
                self.full_roots.add(owner_key)

        selected = {*self.full_roots, *self.exact_nodes}
        for key in selected:
            current: BlockKey | None = key
            while current is not None:
                self.needed.add(current)
                block = self.manifest.blocks[current]
                current = block.logical_parent

    def _render_block(
        self,
        key: BlockKey,
        *,
        inherited_full: bool,
        force_include: bool = False,
    ) -> str:
        if self._is_excluded(key):
            return ""
        block = self.manifest.blocks[key]
        full = inherited_full or key in self.full_roots
        include = force_include or full or key in self.needed or key in self.exact_nodes
        if not include:
            return ""
        if block.kind == "node":
            return block.line if full or key in self.exact_nodes else ""

        pieces: list[str] = []
        if block.kind != "root":
            pieces.append(block.line)
        for part in block.physical_parts:
            if isinstance(part, tuple):
                rendered = self._render_block(part, inherited_full=full)
                if rendered:
                    pieces.append(rendered)
            elif full:
                pieces.append(part)
        return "\n".join(pieces).strip("\n")

    def _is_included(self, key: BlockKey) -> bool:
        if self._is_excluded(key):
            return False
        if key in self.exact_nodes or key in self.needed:
            return True
        return self._is_under_full(key)

    def _is_under_full(self, key: BlockKey) -> bool:
        current: BlockKey | None = key
        while current is not None:
            if current in self.full_roots:
                return True
            current = self.manifest.blocks[current].logical_parent
        return False

    def _is_excluded(self, key: BlockKey) -> bool:
        current: BlockKey | None = key
        while current is not None:
            if current in self.excluded_roots:
                return True
            current = self.manifest.blocks[current].logical_parent
        return False

def _strip_managed_examples(text: str) -> str:
    output: list[str] = []
    in_example = False
    for line in text.splitlines():
        if MANAGED_EXAMPLE_START in line:
            in_example = MANAGED_EXAMPLE_END not in line
            continue
        if in_example:
            if MANAGED_EXAMPLE_END in line:
                in_example = False
            continue
        output.append(line)
    return "\n".join(output)


def _render_recipe_toml(recipe: FileViewRecipe) -> str:
    lines = [
        "version = 1",
        f'view_id = "{recipe.view_id}"',
        'kind = "file"',
        f"title = {_toml_string(recipe.title)}",
        f"approved = {str(recipe.approved).lower()}",
        f"intent = {_toml_string(recipe.intent)}",
        f"render = {_toml_string(recipe.render)}",
        f"semantic_refresh_days = {recipe.semantic_refresh_days}",
        f"last_semantic_refresh_at = {_toml_string(recipe.last_semantic_refresh_at)}",
        f"last_semantic_refresh_memory_commit = {_toml_string(recipe.last_semantic_refresh_memory_commit)}",
    ]
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE:
        lines.extend(
            [
                "",
                f"include_headings = {_toml_array(recipe.include_headings)}",
                f"include_nodes = {_toml_array(recipe.include_nodes)}",
                f"include_files = {_toml_array(recipe.include_files)}",
                f"exclude_ids = {_toml_array(recipe.exclude_ids)}",
            ]
        )
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


def _render_dist_manifest(view_id: str) -> str:
    return (
        f"version = {PACKAGE_VERSION}\n"
        f"view_id = {_toml_string(view_id)}\n"
        f"document_kind = {_toml_string(DOCUMENT_KIND)}\n"
    )


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
    render = data.get("render")
    if render not in FILE_VIEW_RENDER_VALUES:
        errors.append('render must be "extractive" or "generative"')
    selection_keys = FILE_RECIPE_ARRAY_KEYS & keys
    if render == FILE_VIEW_RENDER_EXTRACTIVE:
        missing_selection = sorted(FILE_RECIPE_ARRAY_KEYS - keys)
        if missing_selection:
            errors.append("missing required extractive selection field(s): " + ", ".join(missing_selection))
        for key in sorted(FILE_RECIPE_ARRAY_KEYS):
            value = data.get(key)
            if not isinstance(value, list):
                errors.append(f"{key} must be a TOML array")
                continue
            if any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(f"{key} entries must be non-empty strings")
    elif render == FILE_VIEW_RENDER_GENERATIVE and selection_keys:
        for key in sorted(selection_keys):
            errors.append(f"generative file view recipe must not include selection field: {key}")
    refresh_days = data.get("semantic_refresh_days")
    if isinstance(refresh_days, bool) or not isinstance(refresh_days, int) or refresh_days < 0:
        errors.append("semantic_refresh_days must be a nonnegative integer")
    for key in ("last_semantic_refresh_at", "last_semantic_refresh_memory_commit"):
        if not isinstance(data.get(key), str):
            errors.append(f"{key} must be a string")
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
        render=recipe.render,
        include_headings=recipe.include_headings,
        include_nodes=recipe.include_nodes,
        include_files=recipe.include_files,
        exclude_ids=recipe.exclude_ids,
        approved=approved,
        publish_hub_url=recipe.publish_hub_url,
        publish_credential_id=recipe.publish_credential_id,
        semantic_refresh_days=recipe.semantic_refresh_days,
        last_semantic_refresh_at=recipe.last_semantic_refresh_at,
        last_semantic_refresh_memory_commit=recipe.last_semantic_refresh_memory_commit,
    )


def _view_dir(root: Path, view_id: str) -> Path:
    return root / PROVIDER_VIEWS_DIR / validate_heading_id(view_id)


def _validate_graph_source_file(root: Path, value: str) -> str:
    path = PurePosixPath(value)
    if "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"file view include_files entry must be a RightMemory graph file: {value}")
    text = path.as_posix()
    manifest = build_graph_manifest(root)
    graph_files = {graph_file.relative_to(manifest.root).as_posix() for graph_file in manifest.graph_files}
    if text not in graph_files:
        raise ValueError(f"file view include_files entry must be a RightMemory graph file: {value}")
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


def _validate_refresh_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("semantic_refresh_days must be a nonnegative integer")
    return value


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
