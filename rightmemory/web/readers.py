from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..watch import MANAGED_WATCH_TARGETS
from .auth import WEB_RUNTIME_DIR


MAX_PREVIEW_BYTES = 64 * 1024
MAX_PREVIEW_LINES = 600


@dataclass(frozen=True)
class WebArtifact:
    id: str
    label: str
    kind: str
    path: Path

    def summary(self, root: Path) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "path": _display_path(root, self.path),
            "exists": self.path.exists(),
        }


def list_memory_artifacts(memory_root: Path) -> list[WebArtifact]:
    root = Path(memory_root)
    artifacts: list[WebArtifact] = []
    for path in _active_memory_files(root):
        artifacts.append(_artifact(root, "memory", path))
    provider_root = root / "shared_views"
    artifacts.extend(_artifact(root, "shared-view-source", path) for path in _known_markdown_or_toml(provider_root))
    imports_root = root / ".runtime" / "shared_views" / "imports"
    artifacts.extend(_artifact(root, "shared-view-import", path) for path in _known_markdown_or_toml(imports_root))
    return _dedupe_artifacts(artifacts)


def list_insight_artifacts(memory_root: Path) -> list[WebArtifact]:
    root = Path(memory_root)
    insight_root = root / "insight_logs"
    paths = sorted(
        (path for path in insight_root.glob("*.md") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return [_artifact(root, "insight", path) for path in paths]


def list_log_artifacts(memory_root: Path) -> list[WebArtifact]:
    root = Path(memory_root)
    artifacts = [
        WebArtifact(
            id=f"watch:{name}",
            label=f"{name} watch",
            kind="watch-log",
            path=root / ".runtime" / "watch" / f"{name}.log",
        )
        for name in MANAGED_WATCH_TARGETS
    ]
    artifacts.append(
        WebArtifact(
            id="web:service",
            label="web service",
            kind="web-log",
            path=root / WEB_RUNTIME_DIR / "web.log",
        )
    )
    return artifacts


def resolve_artifact(artifacts: list[WebArtifact], artifact_id: str) -> WebArtifact | None:
    return next((artifact for artifact in artifacts if artifact.id == artifact_id), None)


def read_artifact_text(artifact: WebArtifact) -> str:
    if not artifact.path.is_file() or artifact.path.is_symlink():
        raise FileNotFoundError(str(artifact.path))
    with artifact.path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - MAX_PREVIEW_BYTES))
        text = handle.read(MAX_PREVIEW_BYTES).decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > MAX_PREVIEW_LINES:
        text = "\n".join(lines[-MAX_PREVIEW_LINES:])
    return text


def _active_memory_files(root: Path) -> list[Path]:
    files = []
    memory = root / "MEMORY.md"
    if memory.is_file() and not memory.is_symlink():
        files.append(memory)
    files.extend(
        sorted(
            path
            for path in root.glob("MEMORY_*.md")
            if path.is_file() and not path.is_symlink() and not path.name.startswith("MEMORY_SKILL_")
        )
    )
    return files


def _known_markdown_or_toml(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    paths = []
    for path in base.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix not in {".md", ".toml"}:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if base.resolve() in (resolved, *resolved.parents):
            paths.append(path)
    return sorted(paths)


def _artifact(root: Path, kind: str, path: Path) -> WebArtifact:
    relative = _display_path(root, path)
    digest = hashlib.sha256(f"{kind}:{relative}".encode("utf-8")).hexdigest()[:16]
    return WebArtifact(id=f"{kind}:{digest}", label=relative, kind=kind, path=path)


def _dedupe_artifacts(artifacts: list[WebArtifact]) -> list[WebArtifact]:
    seen = set()
    deduped = []
    for artifact in artifacts:
        key = (artifact.kind, artifact.label)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(artifact)
    return deduped


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path)
