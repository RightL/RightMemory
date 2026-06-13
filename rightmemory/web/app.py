from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    clear_session_cookie,
    create_session_cookie,
    ensure_web_auth_files,
    read_session,
    require_csrf,
    require_session,
    set_session_cookie,
    verify_operator_token,
)
from .models import error_detail, ok_response
from .service import WebStudioService


def create_web_app(memory_root: Path, *, operator_token: str | None = None) -> FastAPI:
    root = Path(memory_root).expanduser()
    ensure_web_auth_files(root, operator_token=operator_token)
    service = WebStudioService(root)
    app = FastAPI(title="RightMemory Web Studio")
    static_root = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    def current_session(request: Request):
        return require_session(service.memory_root, request)

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/api/session")
    def session(request: Request):
        existing = read_session(service.memory_root, request)
        return service.session_data(
            authenticated=existing is not None,
            csrf_token=existing.csrf_token if existing else None,
        )

    @app.post("/api/login")
    def login(response: Response, payload: dict[str, str] = Body(...)):
        token = payload.get("token", "")
        if not verify_operator_token(service.memory_root, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=error_detail("invalid operator token"),
            )
        cookie, session_info = create_session_cookie(service.memory_root)
        set_session_cookie(response, cookie)
        return ok_response("logged in", {"csrf_token": session_info.csrf_token})

    @app.post("/api/logout")
    def logout(response: Response, _session=Depends(current_session)):
        clear_session_cookie(response)
        return ok_response("logged out")

    @app.get("/api/overview")
    def overview(_session=Depends(current_session)):
        return ok_response("overview loaded", service.overview())

    @app.get("/api/status")
    def status_api(_session=Depends(current_session)):
        return ok_response("status loaded", service.status())

    @app.get("/api/memory/files")
    def memory_files(_session=Depends(current_session)):
        return ok_response("memory files loaded", service.memory_files())

    @app.get("/api/memory/files/{file_id}")
    def memory_file(file_id: str, _session=Depends(current_session)):
        data = service.memory_file(file_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("memory file not found"))
        return ok_response("memory file loaded", data)

    @app.get("/api/insights")
    def insights(_session=Depends(current_session)):
        return ok_response("insights loaded", service.insights())

    @app.get("/api/insights/{insight_id}")
    def insight(insight_id: str, _session=Depends(current_session)):
        data = service.insight(insight_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("insight not found"))
        return ok_response("insight loaded", data)

    @app.get("/api/logs")
    def logs(_session=Depends(current_session)):
        return ok_response("logs loaded", service.logs())

    @app.get("/api/logs/{log_id}")
    def log(log_id: str, _session=Depends(current_session)):
        data = service.log(log_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("log not found"))
        return ok_response("log loaded", data)

    @app.get("/api/share/views")
    def shared_views(_session=Depends(current_session)):
        return ok_response("shared views loaded", service.shared_views())

    @app.post("/api/share/views")
    def define_view(
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.define_view(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not define shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/{view_id}/build")
    def build_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] | None = Body(default=None),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.build_view(view_id, payload or {})
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not build shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/{view_id}/export")
    def export_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.export_view(view_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not export shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/{view_id}/publish")
    def publish_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.publish_view(view_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not publish shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/use/accept-invite")
    def accept_invite(
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.accept_invite(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not accept shared-view invitation", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/use/connections/{heading_id}/retrieve")
    def retrieve_connection(
        heading_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            text = service.retrieve_connection(heading_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not retrieve shared view", technical=str(exc)),
            ) from exc
        return ok_response("shared view retrieved", {"text": text})

    @app.post("/api/use/connections/{heading_id}/note")
    def note_connection(
        heading_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        _session=Depends(current_session),
    ):
        require_csrf(service.memory_root, request, request.headers.get("x-csrf-token"))
        try:
            message = service.note_connection(heading_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not record shared-view note", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.get("/api/use/connections/{heading_id}/notes")
    def connection_notes(heading_id: str, _session=Depends(current_session)):
        return ok_response("shared-view notes loaded", service.notes(heading_id))

    @app.post("/api/active-root")
    def active_root(
        request: Request,
        payload: dict[str, str] = Body(...),
        _session=Depends(current_session),
    ):
        x_csrf_token = request.headers.get("x-csrf-token")
        require_csrf(service.memory_root, request, x_csrf_token)
        try:
            data = service.set_active_root(Path(payload.get("root", "")))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("invalid active root", technical=str(exc)),
            ) from exc
        return ok_response("active root updated", data)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rightmemory.web.app")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--memory-root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    uvicorn.run(create_web_app(args.memory_root), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
