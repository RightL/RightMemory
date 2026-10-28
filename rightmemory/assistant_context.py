from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from .config import load_sync_config
from .corrections import AGENT_CORRECTION_SOURCE_PATHS
from .graph import GraphManifest, build_graph_manifest
from .learning_retrieval import format_learning_index, learning_sources
from .retrieve_context import current_memory_head
from .session import MemoryWriteLock, _ensure_runtime_gitignore
from .shared_view_files import pull_all_file_views
from .shared_view_models import load_connections
from .shared_view_package import valid_file_view_package
from .sync import SyncManager, SyncResult


def prepare_retrieve_context(
    memory_root: Path,
    *,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Prepare native file access for an assistant-owned Retrieve task.

    The active root remains live. Only the source index and shared role
    instructions are materialized for this invocation; no model, worker, or
    delivery-history state is created.
    """
    root = Path(memory_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {root}")
    sync_config = load_sync_config(root)
    try:
        sync_result = SyncManager(sync_config).refresh_for_retrieve()
    except Exception as exc:
        sync_result = SyncResult(
            "error", f"retrieve sync refresh failed: {type(exc).__name__}: {exc}"
        )
    with MemoryWriteLock(root):
        file_views = pull_all_file_views(root)
        manifest = build_graph_manifest(root)
        mf_imports = _mf_import_directories(root, manifest)
        memory_commit = current_memory_head(root)
        source_files = set(manifest.files)
        source_files.update(
            root / filename
            for filename in AGENT_CORRECTION_SOURCE_PATHS.values()
            if (root / filename).is_file() and not (root / filename).is_symlink()
        )
        learning = learning_sources(root)
        learning_index = format_learning_index(root)
    if instructions is None:
        from .assistant_prompt import build_assistant_retrieve_instructions

        instructions = build_assistant_retrieve_instructions(root)

    runtime_root = root / ".runtime"
    _ensure_runtime_gitignore(runtime_root)
    output_root = runtime_root / "assistant" / "retrieve"
    output_root.mkdir(parents=True, exist_ok=True)
    prepared_root = Path(mkdtemp(prefix="retrieve-", dir=output_root))
    instructions_file = prepared_root / "RETRIEVE.md"
    instructions_file.write_text(instructions, encoding="utf-8")
    learning_index_file = prepared_root / "LEARNING.md"
    learning_index_file.write_text(learning_index, encoding="utf-8")
    return {
        "memory_root": str(root),
        "memory_commit": memory_commit,
        "live_files": True,
        "instructions_file": str(instructions_file),
        "learning_index_file": str(learning_index_file),
        "learning_source_count": len(learning),
        "learning_root": str(root / "learning") if (root / "learning").is_dir() else None,
        "source_files": [str(path) for path in sorted(source_files)],
        "mf_imports": mf_imports,
        "pending_count": sum(source.kind == "interaction evidence" for source in learning.values()),
        "sync": asdict(sync_result),
        "file_views": [asdict(result) for result in file_views],
    }


def _mf_import_directories(root: Path, manifest: GraphManifest) -> dict[str, str]:
    connections = load_connections(root)
    imports: dict[str, str] = {}
    for heading_id, heading in sorted(manifest.headings.items()):
        if heading.anchor_kind != "MF#":
            continue
        package_root = root / ".runtime" / "shared_views" / "imports" / heading_id
        connection = connections.get(heading_id)
        view_id = (
            connection.target.view_id
            if connection is not None and connection.target.view_id
            else heading_id
        )
        if valid_file_view_package(
            package_root, expected_view_id=view_id, namespace_id=heading_id
        ):
            imports[heading_id] = str(package_root / "dist")
    return imports
