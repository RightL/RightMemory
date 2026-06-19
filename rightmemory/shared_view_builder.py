from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from .config import load_config
from .memory_git import current_active_memory_commit
from .runtime import RightMemoryRuntime
from .session import MemoryWriteLock
from .shared_view_files import (
    FILE_VIEW_RENDER_EXTRACTIVE,
    FileViewRecipe,
    publish_file_view_package,
    render_file_view,
    validate_file_view_recipe_source,
    write_file_view_recipe_from_recipe,
)
from .shared_view_models import validate_heading_id
from .shared_view_questions import validate_question_view_source


def run_file_view_builder(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    hub_url: str,
    credential_id: str,
) -> str:
    clean_view_id = validate_heading_id(view_id)
    message = "\n".join(
        [
            "<shared_view_build>",
            "kind: file",
            f"view_id: {clean_view_id}",
            f"title: {title.strip()}",
            f"intent: {intent.strip()}",
            f"publish_hub_url: {hub_url.strip()}",
            f"publish_credential_id: {credential_id.strip()}",
            "</shared_view_build>",
        ]
    )
    output = _run_builder(memory_root, clean_view_id, message)
    _require_artifact(memory_root, clean_view_id, "view.md")
    _require_artifact(memory_root, clean_view_id, "recipe.toml")
    recipe = validate_file_view_recipe_source(
        memory_root,
        clean_view_id,
        require_selection=False,
        require_publish=True,
    )
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE:
        validate_file_view_recipe_source(memory_root, clean_view_id, require_selection=True, require_publish=True)
        render_file_view(memory_root, clean_view_id)
    else:
        _require_nonempty_file_view_context(memory_root, clean_view_id)
    _require_nonempty_file_view_context(memory_root, clean_view_id)
    return output


def file_view_refresh_due(memory_root: Path, view_id: str, *, force: bool = False) -> bool:
    if force:
        return True
    recipe = validate_file_view_recipe_source(memory_root, view_id, require_selection=False)
    if recipe.semantic_refresh_days <= 0:
        return False
    current_commit = current_active_memory_commit(memory_root)
    if current_commit == recipe.last_semantic_refresh_memory_commit:
        return False
    if not recipe.last_semantic_refresh_at:
        return True
    refreshed_at = datetime.fromisoformat(recipe.last_semantic_refresh_at)
    return datetime.now(UTC) - refreshed_at >= timedelta(days=recipe.semantic_refresh_days)


def refresh_file_view(memory_root: Path, view_id: str, *, force: bool = False, publish: bool = False) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    old_recipe = validate_file_view_recipe_source(root, clean_view_id, require_selection=False)
    if not file_view_refresh_due(root, clean_view_id, force=force):
        return f"file view {clean_view_id} semantic refresh not due"
    view_dir = root / "shared_views" / clean_view_id
    with TemporaryDirectory() as tempdir:
        backup_dir = Path(tempdir) / clean_view_id
        if view_dir.exists():
            shutil.copytree(view_dir, backup_dir)
        try:
            output = _run_builder(root, clean_view_id, _refresh_message(old_recipe))
            with MemoryWriteLock(root):
                new_recipe = validate_file_view_recipe_source(root, clean_view_id, require_selection=False)
                refreshed = replace(
                    new_recipe,
                    approved=old_recipe.approved,
                    publish_hub_url=old_recipe.publish_hub_url,
                    publish_credential_id=old_recipe.publish_credential_id,
                    last_semantic_refresh_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    last_semantic_refresh_memory_commit=current_active_memory_commit(root),
                )
                write_file_view_recipe_from_recipe(root, refreshed)
                if refreshed.render == FILE_VIEW_RENDER_EXTRACTIVE:
                    validate_file_view_recipe_source(root, clean_view_id, require_selection=True)
                    render_file_view(root, clean_view_id)
                _require_nonempty_file_view_context(root, clean_view_id)
                _commit_refresh_if_changed(root, clean_view_id)
            if publish and refreshed.approved and refreshed.publish_hub_url and refreshed.publish_credential_id:
                publish_file_view_package(
                    root,
                    clean_view_id,
                    hub_url=refreshed.publish_hub_url,
                    credential_id=refreshed.publish_credential_id,
                )
            return f"refreshed file view {clean_view_id}\n{output}"
        except BaseException:
            with MemoryWriteLock(root):
                if backup_dir.exists():
                    if view_dir.exists():
                        shutil.rmtree(view_dir)
                    shutil.copytree(backup_dir, view_dir)
            raise


def run_question_view_builder(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
) -> str:
    clean_view_id = validate_heading_id(view_id)
    message = "\n".join(
        [
            "<shared_view_build>",
            "kind: question",
            f"view_id: {clean_view_id}",
            f"title: {title.strip()}",
            f"intent: {intent.strip()}",
            "</shared_view_build>",
        ]
    )
    output = _run_builder(memory_root, clean_view_id, message)
    _require_artifact(memory_root, clean_view_id, "view.md")
    _require_artifact(memory_root, clean_view_id, "retriever.md")
    _require_artifact(memory_root, clean_view_id, "question.toml")
    validate_question_view_source(memory_root, clean_view_id)
    return output


def _run_builder(memory_root: Path, view_id: str, message: str) -> str:
    root = Path(memory_root).expanduser()
    config = load_config("shared-view-builder", memory_root=root)
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(f"shared-view-builder-{view_id}", message)
    finally:
        runtime.cleanup()


def _require_artifact(memory_root: Path, view_id: str, relative: str) -> None:
    root = Path(memory_root).expanduser()
    path = root / "shared_views" / view_id / relative
    if not path.is_file():
        raise RuntimeError(f"shared-view builder did not create required artifact: {path.relative_to(root)}")


def _require_nonempty_file_view_context(memory_root: Path, view_id: str) -> None:
    root = Path(memory_root).expanduser()
    path = root / "shared_views" / view_id / "dist" / "MEMORY.md"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    marker = "## Published Context"
    if marker not in text or not text.split(marker, 1)[1].strip():
        raise RuntimeError(
            "shared-view builder rendered an empty file view; "
            "call the file-view compiler tool with content that produces Published Context"
        )


def _refresh_message(recipe: FileViewRecipe) -> str:
    lines = [
        "<shared_view_refresh>",
        "kind: file",
        f"view_id: {recipe.view_id}",
        f"title: {recipe.title}",
        f"intent: {recipe.intent}",
        f"previous_render: {recipe.render}",
    ]
    if recipe.publish_hub_url:
        lines.append(f"publish_hub_url: {recipe.publish_hub_url}")
    if recipe.publish_credential_id:
        lines.append(f"publish_credential_id: {recipe.publish_credential_id}")
    lines.append("</shared_view_refresh>")
    return "\n".join(lines)


def _commit_refresh_if_changed(root: Path, view_id: str) -> None:
    paths = [
        f"shared_views/{view_id}/.gitignore",
        f"shared_views/{view_id}/recipe.toml",
        f"shared_views/{view_id}/view.md",
    ]
    _run_git(root, "add", *paths)
    diff = _run_git(root, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return
    _run_git(root, "commit", "-m", f"shared-view: refresh {view_id}")


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result
