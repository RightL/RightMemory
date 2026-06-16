from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request, Response, status

from .packages import PackageValidationError, validate_package_relative_path
from .store import HubStore


MAX_ZIP_ENTRIES = 2048
ZIP_ENTRY_OVERHEAD_BYTES = 256


def create_hub_app(hub_root: Path) -> FastAPI:
    store = HubStore(Path(hub_root).expanduser())
    app = FastAPI(title="RightMemory Shared View Hub")

    @app.get("/health")
    def health() -> dict[str, Any]:
        initialized = store.db_path.is_file() and store.config_path.is_file()
        return {
            "status": "ok" if initialized else "uninitialized",
            "initialized": initialized,
            "storage": store.storage_root.is_dir(),
        }

    @app.post("/api/views/{view_id}/versions", status_code=status.HTTP_201_CREATED)
    async def publish_version(view_id: str, request: Request) -> dict[str, Any]:
        actor = _require_token(store, request, action="publish")
        _require_actor_provider(actor.provider_id)
        _ensure_view_provider_scope(store, view_id, actor.provider_id)
        config = store.load_config()
        package_root, cleanup = await _request_package_root(request, max_package_bytes=config.max_package_bytes)
        try:
            stored = store.store_package_version(
                package_root,
                view_id=view_id,
                provider_id=actor.provider_id,
                created_by_token_id=actor.token_id,
            )
        except PackageValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        finally:
            cleanup()
        return {
            "view_id": stored.manifest.view_id,
            "provider_id": actor.provider_id,
            "version_id": stored.version_id,
            "current_version_id": stored.version_id,
            "package_hash": stored.manifest.package_hash,
            "title": stored.manifest.title,
        }

    @app.post("/api/views/{view_id}/invitations", status_code=status.HTTP_201_CREATED)
    def create_invitation(
        view_id: str,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        actor = _require_token(store, request, action="publish")
        _require_actor_provider(actor.provider_id)
        _ensure_view_provider_scope(store, view_id, actor.provider_id)
        try:
            invitation = store.create_invitation(
                view_id,
                actor_id=actor.token_id,
                label=_optional_payload_str(payload or {}, "label"),
                expires_at=_optional_payload_str(payload or {}, "expires_at"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        config = store.load_config()
        invitation_url = f"{config.public_base_url.rstrip('/')}/i/{invitation['raw_token']}"
        return {
            "invitation_id": invitation["invitation_id"],
            "token_id": invitation["token_id"],
            "view_id": invitation["view_id"],
            "invitation_url": invitation_url,
            "expires_at": invitation["expires_at"],
        }

    @app.get("/i/{token}")
    def invitation_landing(token: str) -> dict[str, Any]:
        invitation = store.describe_invitation(token)
        if invitation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
        return {
            "view_id": invitation["view_id"],
            "title": invitation["title"],
            "description": invitation["description"],
            "api": {
                "view": f"/api/invitations/{token}/view",
                "accept": f"/api/invitations/{token}/accept",
            },
        }

    @app.get("/api/invitations/{token}/view")
    def describe_invitation(token: str) -> dict[str, Any]:
        invitation = store.describe_invitation(token)
        if invitation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
        return invitation

    @app.post("/api/invitations/{token}/accept", status_code=status.HTTP_201_CREATED)
    def accept_invitation(
        token: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        accepted = store.accept_invitation(
            token,
            consumer_label=_optional_payload_str(payload or {}, "consumer_label"),
        )
        if accepted is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
        return {
            "connection_id": accepted["connection_id"],
            "token_id": accepted["token_id"],
            "connection_token": accepted["connection_token"],
            "view_id": accepted["view_id"],
            "consumer_label": accepted["consumer_label"],
        }

    @app.get("/api/views/{view_id}/package")
    def download_package(view_id: str, request: Request) -> Response:
        _require_connection_actor(store, request, view_id)
        current = store.get_current_view_version(view_id)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
        with tempfile.TemporaryDirectory() as tempdir:
            archive_path = Path(tempdir) / "package.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(Path(current["path"]).rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.relative_to(current["path"]).as_posix())
            return Response(content=archive_path.read_bytes(), media_type="application/zip")

    @app.post("/api/views/{view_id}/interactions", status_code=status.HTTP_201_CREATED)
    def post_interaction(
        view_id: str,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        actor = _require_connection_actor(store, request, view_id)
        interaction_payload = _interaction_payload(payload or {})
        try:
            interaction = store.record_interaction(view_id, actor=actor, payload=interaction_payload)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="token is not scoped to view") from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return {
            "status": "recorded",
            "interaction_id": interaction["interaction_id"],
            "view_id": interaction["view_id"],
            "connection_id": interaction["connection_id"],
        }

    @app.get("/api/providers/{provider_id}/inbox")
    def provider_inbox(provider_id: str, request: Request) -> dict[str, Any]:
        _require_provider_or_admin(store, request, provider_id)
        return {
            "provider_id": provider_id,
            "interactions": store.list_provider_inbox(provider_id),
        }

    return app


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return ""


def _require_token(store: HubStore, request: Request, *, action: str):
    try:
        return store.require_token(_bearer_token(request), action=action)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _require_connection_actor(store: HubStore, request: Request, view_id: str):
    token = _bearer_token(request)
    try:
        return store.require_token(token, action="connect", view_id=view_id)
    except PermissionError as exc:
        if store.verify_token(token, action="connect"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="token is not scoped to view") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _require_provider_or_admin(store: HubStore, request: Request, provider_id: str):
    token = _bearer_token(request)
    try:
        return store.require_token_any(
            token,
            candidates=[
                {"action": "admin", "provider_id": None, "view_id": None},
                {"action": "publish", "provider_id": provider_id, "view_id": None},
            ],
        )
    except PermissionError as exc:
        if store.verify_token(token, action="admin") or store.verify_token(token, action="publish"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="token cannot read provider inbox") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _require_actor_provider(provider_id: str | None) -> None:
    if provider_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="publisher token has no provider scope")


def _ensure_view_provider_scope(store: HubStore, view_id: str, provider_id: str) -> None:
    view = store.get_view(view_id)
    if view is not None and view["provider_id"] not in {None, provider_id}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="token cannot manage this view")


async def _request_package_root(request: Request, *, max_package_bytes: int) -> tuple[Path, Callable[[], None]]:
    content_type = request.headers.get("content-type", "")
    if "application/zip" not in content_type:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="publish body must be application/zip")
    body = await _read_limited_body(request, max_bytes=max_package_bytes)
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="publish body is required")
    tempdir = tempfile.TemporaryDirectory()
    package_root = Path(tempdir.name)
    try:
        extracted = _extract_zip_package(body, package_root, max_uncompressed_bytes=max_package_bytes)
    except HTTPException:
        tempdir.cleanup()
        raise
    except (OSError, zipfile.BadZipFile, PackageValidationError) as exc:
        tempdir.cleanup()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return extracted, tempdir.cleanup


async def _read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="package upload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_zip_package(body: bytes, target_root: Path, *, max_uncompressed_bytes: int) -> Path:
    archive_path = target_root / "package.zip"
    archive_path.write_bytes(body)
    extract_root = target_root / "package"
    extract_root.mkdir()
    total_cost = 0
    entry_count = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            entry_count += 1
            total_cost += ZIP_ENTRY_OVERHEAD_BYTES
            if entry_count > MAX_ZIP_ENTRIES or total_cost > max_uncompressed_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="package archive has too many entries",
                )
            relative = validate_package_relative_path(info.filename)
            if info.is_dir():
                (extract_root / relative).mkdir(parents=True, exist_ok=True)
                continue
            target = extract_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    total_cost += len(chunk)
                    if total_cost > max_uncompressed_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            detail="package exceeds configured maximum size",
                        )
                    destination.write(chunk)
    archive_path.unlink()
    extracted = target_root / "extracted"
    extract_root.rename(extracted)
    return extracted


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{key} must be a non-empty string")
    return value.strip()


def _optional_payload_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{key} must be a string")
    value = value.strip()
    return value or None


def _payload_limit(value: Any) -> int:
    if value is None:
        return 12
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be an integer")
    return max(1, min(value, 25))


def _interaction_payload(payload: dict[str, Any]) -> dict[str, Any]:
    message = _required_payload_str(payload, "message")
    clean: dict[str, Any] = {
        "actor": _optional_payload_str(payload, "actor") or "consumer",
        "message": message,
    }
    task_context = _optional_payload_str(payload, "task_context")
    if task_context:
        clean["task_context"] = task_context
    for key, value in payload.items():
        if key not in clean and key not in {"actor", "message", "task_context"}:
            clean[key] = value
    return clean


def _noop_cleanup() -> None:
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rightmemory.hub.app")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--hub-root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    uvicorn.run(create_hub_app(args.hub_root), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
