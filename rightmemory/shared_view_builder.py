from __future__ import annotations

from pathlib import Path

from .config import load_config
from .runtime import RightMemoryRuntime
from .shared_view_models import validate_heading_id


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
    return output


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
