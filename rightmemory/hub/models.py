from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HubConfig:
    public_base_url: str = "http://127.0.0.1:8765"
    max_package_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class HubToken:
    token_id: str
    raw_token: str
    action: str
    provider_id: str | None = None
    view_id: str | None = None
    label: str | None = None
    created_at: str = ""


@dataclass(frozen=True)
class TokenActor:
    token_id: str
    action: str
    provider_id: str | None = None
    view_id: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    kind: str
    created_at: str
    actor_id: str | None = None
    provider_id: str | None = None
    view_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HubPackageManifest:
    source_root: Path
    view_id: str
    title: str
    ref: str
    files: tuple[str, ...]
    size_bytes: int
    package_hash: str
    description: str | None = None
    maintainer: str | None = None
    export_metadata: dict[str, Any] = field(default_factory=dict)
    invitation_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HubStoredPackage:
    path: Path
    version_id: str
    manifest: HubPackageManifest
