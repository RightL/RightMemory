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
    revoke_session,
    set_session_cookie,
    verify_operator_token,
)
from .models import error_detail, ok_response
from .service import WebStudioService, resolve_allowed_memory_root
from ..shared_view_questions import question_response_payload, verify_question_view_token


def create_web_app(memory_root: Path, *, operator_token: str | None = None) -> FastAPI:
    root = Path(memory_root).expanduser().resolve()
    ensure_web_auth_files(root, operator_token=operator_token)
    app = FastAPI(title="RightMemory Web Studio")
    static_root = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    def current_session(request: Request):
        return require_session(root, request)

    def service_for_active_root(active_root: str | Path) -> WebStudioService:
        resolved = resolve_allowed_memory_root(root, active_root)
        return WebStudioService(resolved, allowed_root=root)

    def current_service(session=Depends(current_session)):
        return service_for_active_root(session.active_root)

    @app.get("/")
    def index():
        return FileResponse(static_root / "index.html")

    @app.get("/api/session")
    def session(request: Request):
        existing = read_session(root, request)
        if existing is None:
            return WebStudioService(root).session_data(authenticated=False)
        return service_for_active_root(existing.active_root).session_data(
            authenticated=True,
            csrf_token=existing.csrf_token,
        )

    @app.post("/api/login")
    def login(response: Response, payload: dict[str, str] = Body(...)):
        token = payload.get("token", "")
        if not verify_operator_token(root, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=error_detail("invalid operator token"),
            )
        cookie, session_info = create_session_cookie(root, active_root=root)
        set_session_cookie(response, cookie)
        return ok_response("logged in", {"csrf_token": session_info.csrf_token})

    @app.post("/api/logout")
    def logout(request: Request, response: Response, _session=Depends(current_session)):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        revoke_session(root, _session.session_id)
        clear_session_cookie(response)
        return ok_response("logged out")

    @app.get("/api/overview")
    def overview(service=Depends(current_service)):
        return ok_response("overview loaded", service.overview())

    @app.get("/api/status")
    def status_api(service=Depends(current_service)):
        return ok_response("status loaded", service.status())

    @app.get("/api/settings")
    def settings_api(service=Depends(current_service)):
        return ok_response("settings loaded", service.settings())

    @app.get("/api/memory/files")
    def memory_files(service=Depends(current_service)):
        return ok_response("memory files loaded", service.memory_files())

    @app.get("/api/memory/files/{file_id}")
    def memory_file(file_id: str, service=Depends(current_service)):
        data = service.memory_file(file_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("memory file not found"))
        return ok_response("memory file loaded", data)

    @app.get("/api/insights")
    def insights(service=Depends(current_service)):
        return ok_response("insights loaded", service.insights())

    @app.get("/api/insights/{insight_id}")
    def insight(insight_id: str, service=Depends(current_service)):
        data = service.insight(insight_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("insight not found"))
        return ok_response("insight loaded", data)

    @app.get("/api/logs")
    def logs(service=Depends(current_service)):
        return ok_response("logs loaded", service.logs())

    @app.get("/api/logs/{log_id}")
    def log(log_id: str, service=Depends(current_service)):
        data = service.log(log_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("log not found"))
        return ok_response("log loaded", data)

    @app.get("/api/share/views")
    def shared_views(service=Depends(current_service)):
        return ok_response("shared views loaded", service.shared_views())

    @app.post("/api/share/views/build-file")
    def build_file_view(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.build_file_view(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not build file view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/build-question")
    def build_question_view(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.build_question_view(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not build question view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/{view_id}/approve")
    def approve_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.approve_view(view_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not approve shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/questions/{view_id}/ask")
    def answer_question_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
    ):
        session = read_session(root, request)
        if session is not None:
            require_csrf(root, request, request.headers.get("x-csrf-token"))
            service = service_for_active_root(session.active_root)
        elif _verify_question_bearer(root, view_id, request):
            service = WebStudioService(root, allowed_root=root)
        else:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail("login required"))
        try:
            text = service.answer_question_view(view_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not answer shared-view question", technical=str(exc)),
            ) from exc
        payload = question_response_payload(text)
        message = "shared view question answered" if payload["status"] == "answered" else "shared view question unavailable"
        return ok_response(message, payload)

    @app.post("/api/share/credentials")
    def save_credential(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.save_credential(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not save shared-view credential", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/use/accept-invite")
    def accept_invite(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.accept_invite(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not accept shared-view invitation", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/use/connections/{heading_id}/pull")
    def pull_connection(
        heading_id: str,
        request: Request,
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.pull_connection(heading_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not pull shared view", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.get("/api/use/connections/{heading_id}/status")
    def connection_status(heading_id: str, service=Depends(current_service)):
        return ok_response("shared view status loaded", service.connection_status(heading_id))

    @app.post("/api/use/connections/{heading_id}/ask")
    def ask_connection(
        heading_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            text = service.ask_connection(heading_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not ask shared view", technical=str(exc)),
            ) from exc
        payload = question_response_payload(text)
        message = "shared view question answered" if payload["status"] == "answered" else "shared view question unavailable"
        return ok_response(message, payload)

    @app.post("/api/use/connections/{heading_id}/note")
    def note_connection(
        heading_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.note_connection(heading_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not record shared-view note", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.get("/api/use/connections/{heading_id}/notes")
    def connection_notes(heading_id: str, service=Depends(current_service)):
        return ok_response("shared-view notes loaded", service.notes(heading_id))

    @app.get("/api/activity")
    def activity(service=Depends(current_service)):
        return ok_response("activity loaded", service.activity())

    @app.post("/api/active-root")
    def active_root(
        request: Request,
        response: Response,
        payload: dict[str, str] = Body(...),
        session=Depends(current_session),
    ):
        x_csrf_token = request.headers.get("x-csrf-token")
        require_csrf(root, request, x_csrf_token)
        service = service_for_active_root(session.active_root)
        try:
            data = service.set_active_root(Path(payload.get("root", "")))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("invalid active root", technical=str(exc)),
            ) from exc
        cookie, updated_session = create_session_cookie(root, active_root=data["active_root"], session_id=session.session_id)
        set_session_cookie(response, cookie)
        data["csrf_token"] = updated_session.csrf_token
        return ok_response("active root updated", data)

    return app


def _verify_question_bearer(root: Path, view_id: str, request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    return bool(
        separator
        and scheme.lower() == "bearer"
        and verify_question_view_token(root, view_id, token.strip())
    )


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
