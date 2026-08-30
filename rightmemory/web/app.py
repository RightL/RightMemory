from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from ..conversations.service import ConversationRuntimeRegistry
from .conversation_routes import add_conversation_routes
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
from .process import MANAGED_WEB_ENV, clear_web_process_files, consume_web_stop_request, register_web_process
from .service import WebStudioService, resolve_allowed_memory_root
from ..pursuit_store import PursuitStoreError
from ..shared_view_questions import question_response_payload, verify_question_view_token


def create_web_app(
    memory_root: Path,
    *,
    operator_token: str | None = None,
    conversation_registry: ConversationRuntimeRegistry | None = None,
) -> FastAPI:
    root = Path(memory_root).expanduser().resolve()
    ensure_web_auth_files(root, operator_token=operator_token)
    app = FastAPI(title="RightMemory Web Studio")
    static_root = Path(__file__).parent / "static"
    owns_conversation_registry = conversation_registry is None
    conversation_registry = conversation_registry or ConversationRuntimeRegistry()
    app.state.conversation_registry = conversation_registry
    if owns_conversation_registry:
        app.router.add_event_handler("shutdown", conversation_registry.close)

    async def current_session(request: Request):
        return require_session(root, request)

    def service_for_active_root(active_root: str | Path) -> WebStudioService:
        resolved = resolve_allowed_memory_root(root, active_root)
        return WebStudioService(resolved, allowed_root=root)

    async def current_service(session=Depends(current_session)):
        return service_for_active_root(session.active_root)

    conversation_stream_lifecycle = add_conversation_routes(
        app,
        configured_root=root,
        registry=conversation_registry,
    )

    @app.get("/")
    async def index():
        return _static_file_response(static_root, "index.html", allowed={"index.html"})

    @app.get("/static/{asset_name}")
    async def static_asset(asset_name: str):
        return _static_file_response(
            static_root, asset_name,
            allowed={"app.js", "styles.css", "pursuit-map.js", "pursuit-map.css", "pursuit-map.LICENSE.txt"},
        )

    @app.get("/api/session")
    async def session(request: Request):
        existing = read_session(root, request)
        if existing is None:
            return WebStudioService(root).session_data(authenticated=False)
        return service_for_active_root(existing.active_root).session_data(
            authenticated=True,
            csrf_token=existing.csrf_token,
        )

    @app.post("/api/login")
    async def login(response: Response, payload: dict[str, str] = Body(...)):
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
    async def logout(request: Request, response: Response, _session=Depends(current_session)):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        revoke_session(root, _session.session_id)
        active_root = _best_effort_logout_root(root, _session.active_root)
        if active_root is not None:
            conversation_stream_lifecycle.invalidate(
                active_root, _session.session_id
            )
            try:
                conversation_service = conversation_registry.service(active_root)
                await run_in_threadpool(
                    conversation_service.close_side_chats_for_session,
                    _session.session_id,
                )
            except Exception:
                # Revocation remains authoritative; startup purging is the
                # recovery path if provider or storage cleanup is unavailable.
                pass
            try:
                conversation_registry.invalidate_root_session(active_root)
            except Exception:
                # Stream cleanup must not undo a successful session revocation.
                pass
        clear_session_cookie(response)
        return ok_response("logged out")

    @app.get("/api/overview")
    async def overview(service=Depends(current_service)):
        return ok_response("overview loaded", service.overview())

    @app.get("/api/status")
    async def status_api(service=Depends(current_service)):
        return ok_response("status loaded", service.status())

    async def pursuit_response(service, message, action, *args):
        try:
            data = await run_in_threadpool(action, *args)
        except PursuitStoreError as exc:
            detail = error_detail(str(exc))
            detail.update(code=exc.code, diagnostics=list(exc.diagnostics))
            if exc.status in {409, 422}:
                try:
                    detail["snapshot"] = await run_in_threadpool(service.pursuit_map)
                except (OSError, ValueError, RuntimeError):
                    # A concurrent filesystem failure must not hide the original conflict.
                    pass
            raise HTTPException(status_code=exc.status, detail=detail) from exc
        except ValueError as exc:
            detail = error_detail(str(exc))
            detail.update(code="invalid_request", diagnostics=[])
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc
        return ok_response(message, data)

    @app.get("/api/pursuit-map")
    async def pursuit_map(service=Depends(current_service)):
        return await pursuit_response(service, "pursuit map loaded", service.pursuit_map)

    @app.post("/api/pursuit-map/operations")
    async def apply_pursuit_operation(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        return await pursuit_response(
            service, "pursuit map updated", service.apply_pursuit_operation, payload, session.session_id,
        )

    @app.post("/api/pursuit-map/undo")
    async def undo_pursuit_operation(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        return await pursuit_response(
            service, "pursuit map operation undone", service.undo_pursuit_operation, payload, session.session_id,
        )

    @app.post("/api/pursuit-map/redo")
    async def redo_pursuit_operation(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        return await pursuit_response(
            service, "pursuit map operation redone", service.redo_pursuit_operation, payload, session.session_id,
        )

    @app.get("/api/settings")
    async def settings_api(service=Depends(current_service)):
        return ok_response("settings loaded", service.settings())

    @app.get("/api/memory/files")
    async def memory_files(service=Depends(current_service)):
        return ok_response("memory files loaded", service.memory_files())

    @app.get("/api/memory/files/{file_id}")
    async def memory_file(file_id: str, service=Depends(current_service)):
        data = service.memory_file(file_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("memory file not found"))
        return ok_response("memory file loaded", data)

    @app.get("/api/insights")
    async def insights(service=Depends(current_service)):
        return ok_response("insights loaded", service.insights())

    @app.get("/api/insights/{insight_id}")
    async def insight(insight_id: str, service=Depends(current_service)):
        data = service.insight(insight_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("insight not found"))
        return ok_response("insight loaded", data)

    @app.get("/api/logs")
    async def logs(service=Depends(current_service)):
        return ok_response("logs loaded", service.logs())

    @app.get("/api/logs/{log_id}")
    async def log(log_id: str, service=Depends(current_service)):
        data = service.log(log_id)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("log not found"))
        return ok_response("log loaded", data)

    @app.get("/api/share/views")
    async def shared_views(service=Depends(current_service)):
        return ok_response("shared views loaded", service.shared_views())

    @app.get("/api/share/relationships")
    async def share_relationships(service=Depends(current_service)):
        return ok_response("share relationships loaded", service.share_relationships())

    @app.post("/api/share/relationships")
    async def create_share_relationship(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.create_share_relationship(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not create share relationship", technical=str(exc)),
            ) from exc
        return ok_response("share relationship created", data)

    @app.post("/api/share/relationships/{share_id}/revise")
    async def revise_share_relationship(
        share_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.revise_share_relationship(share_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not revise share relationship", technical=str(exc)),
            ) from exc
        return ok_response("share relationship revised", data)

    @app.post("/api/share/relationships/{share_id}/publish")
    async def publish_share_relationship(
        share_id: str,
        request: Request,
        payload: dict[str, object] | None = Body(default=None),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.publish_share_relationship(share_id, payload or {})
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not publish share relationship", technical=str(exc)),
            ) from exc
        return ok_response("share relationship published", data)

    @app.post("/api/share/views/build-file")
    async def build_file_view(
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
    async def build_question_view(
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
    async def approve_view(
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

    @app.post("/api/share/views/{view_id}/invite")
    async def invite_file_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] | None = Body(default=None),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.invite_file_view(view_id, payload or {})
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not create file-view invitation", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/views/{view_id}/publish-question")
    async def publish_question_view(
        view_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            message = service.publish_question_view(view_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not publish question-view invitation", technical=str(exc)),
            ) from exc
        return ok_response(message)

    @app.post("/api/share/provider-inbox")
    async def provider_http_inbox(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.provider_http_inbox(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not load provider inbox", technical=str(exc)),
            ) from exc
        return ok_response("provider inbox loaded", data)

    @app.get("/api/share/publish-events")
    async def publish_events(service=Depends(current_service)):
        return ok_response("publish events loaded", service.publish_events())

    @app.get("/api/share/questions/{view_id}/ready")
    async def question_view_ready(view_id: str, request: Request):
        if not _verify_question_bearer(root, view_id, request):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail("login required"))
        return ok_response("shared view question ready", {"view_id": view_id, "status": "ready"})

    @app.post("/api/share/questions/{view_id}/ask")
    async def answer_question_view(
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
    async def save_credential(
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
    async def accept_invite(
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

    @app.post("/api/use/connections/pull-all")
    async def pull_all_connections(
        request: Request,
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.pull_all_connections()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not pull shared views", technical=str(exc)),
            ) from exc
        return ok_response("shared views pulled", data)

    @app.get("/api/use/connections/status-all")
    async def connection_statuses(service=Depends(current_service)):
        return ok_response("shared view statuses loaded", service.connection_statuses())

    @app.post("/api/use/connections/{heading_id}/pull")
    async def pull_connection(
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
    async def connection_status(heading_id: str, service=Depends(current_service)):
        return ok_response("shared view status loaded", service.connection_status(heading_id))

    @app.post("/api/use/connections/{heading_id}/ask")
    async def ask_connection(
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
    async def note_connection(
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
    async def connection_notes(heading_id: str, service=Depends(current_service)):
        return ok_response("shared-view notes loaded", service.notes(heading_id))

    @app.get("/api/activity")
    async def activity(service=Depends(current_service)):
        return ok_response("activity loaded", service.activity())

    @app.post("/api/active-root")
    async def active_root(
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
        conversation_stream_lifecycle.invalidate(
            service.memory_root, session.session_id
        )
        try:
            conversation_service = conversation_registry.service(service.memory_root)
            await run_in_threadpool(
                conversation_service.close_side_chats_for_session,
                session.session_id,
            )
        except Exception:
            # The root switch succeeds even if temporary-provider cleanup must
            # fall back to the next runtime's startup purge.
            pass
        conversation_registry.invalidate_root_session(service.memory_root)
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


def _best_effort_logout_root(configured_root: Path, session_root: str) -> Path | None:
    base = Path(configured_root).expanduser().resolve()
    try:
        candidate = Path(session_root).expanduser().resolve()
        candidate.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _static_file_response(static_root: Path, asset_name: str, *, allowed: set[str]) -> Response:
    if asset_name not in allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("static asset not found"))
    path = static_root / asset_name
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail("static asset not found"))
    media_types = {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }
    return Response(content=path.read_bytes(), media_type=media_types.get(path.suffix, "application/octet-stream"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rightmemory.web.app")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--memory-root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    root = args.memory_root.resolve()
    server = uvicorn.Server(uvicorn.Config(create_web_app(root), host=args.host, port=args.port))
    managed = os.environ.get(MANAGED_WEB_ENV) == "1"
    if managed:
        register_web_process(root, os.getpid())

    def monitor_stop_request() -> None:
        ready = False
        while not server.should_exit:
            if consume_web_stop_request(root, os.getpid()):
                server.should_exit = True
                return
            if managed and server.started and not ready:
                register_web_process(root, os.getpid(), ready=True)
                ready = True
            time.sleep(0.1)

    monitor = threading.Thread(target=monitor_stop_request, name="rightmemory-web-stop", daemon=True)
    monitor.start()
    try:
        server.run()
    finally:
        server.should_exit = True
        monitor.join(timeout=1)
        clear_web_process_files(root, os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
