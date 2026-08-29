from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from ..conversations import ConversationError
from ..conversations.service import ConversationRuntimeRegistry, ConversationService
from .auth import SESSION_COOKIE, WebSession, read_session_cookie, require_csrf, require_session
from .models import error_detail, ok_response
from .service import resolve_allowed_memory_root


def add_conversation_routes(
    app: FastAPI,
    *,
    configured_root: Path,
    registry: ConversationRuntimeRegistry,
) -> _StreamLifecycle:
    """Attach the Pursuit conversation workspace to one Web Studio app."""

    root = Path(configured_root).expanduser().resolve()
    stream_lifecycle = _StreamLifecycle()

    async def current_session(request: Request):
        return require_session(root, request)

    async def current_conversation_service(session=Depends(current_session)) -> ConversationService:
        active_root = resolve_allowed_memory_root(root, session.active_root)
        return registry.service(active_root)

    async def mutation(
        request: Request,
        action: Callable[..., dict[str, Any]],
        *args: object,
    ) -> dict[str, Any]:
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        return await service_response(action, *args)

    @app.get("/api/conversation-workspace")
    async def conversation_workspace(service=Depends(current_conversation_service)):
        data = await service_response(service.workspace)
        return ok_response("conversation workspace loaded", data)

    @app.get("/api/pursuit-conversations")
    async def pursuit_conversations(
        pursuit_id: str = Query(...),
        service=Depends(current_conversation_service),
    ):
        data = await service_response(service.list_for_pursuit, pursuit_id)
        return ok_response("Pursuit conversations loaded", data)

    @app.post("/api/pursuit-conversations")
    async def create_pursuit_conversation(
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.create_conversation,
            payload.get("pursuit_id"),
            payload.get("host_id"),
            payload.get("project_id"),
        )
        return ok_response("conversation created", data)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation_detail(
        conversation_id: str,
        after_event_id: int | None = Query(None, ge=0),
        service=Depends(current_conversation_service),
    ):
        data = await service_response(service.detail, conversation_id, after_event_id)
        return ok_response("conversation loaded", data)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_conversation_message(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.send_message, conversation_id, payload.get("text"))
        return ok_response("message sent", data)

    @app.post("/api/conversations/{conversation_id}/interrupt")
    async def interrupt_conversation(
        conversation_id: str,
        request: Request,
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.interrupt, conversation_id)
        return ok_response("conversation interrupted", data)

    @app.post("/api/conversations/{conversation_id}/reconcile")
    async def reconcile_conversation(
        conversation_id: str,
        request: Request,
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.reconcile, conversation_id)
        return ok_response("conversation status reconciled", data)

    @app.post("/api/conversations/{conversation_id}/archive")
    async def archive_conversation(
        conversation_id: str,
        request: Request,
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.archive, conversation_id)
        return ok_response("conversation archived", data)

    @app.post("/api/conversations/{conversation_id}/move")
    async def move_conversation(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.move, conversation_id, payload.get("pursuit_id"))
        return ok_response("conversation attachment moved", data)

    @app.post("/api/conversations/{conversation_id}/server-requests/{request_key}/respond")
    async def respond_to_server_request(
        conversation_id: str,
        request_key: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.respond_request,
            request_key,
            payload.get("decision"),
            payload.get("response"),
            conversation_id,
        )
        return ok_response("server request answered", data)

    @app.post("/api/conversation-hosts")
    async def create_conversation_host(
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        if payload.get("kind") not in {None, "ssh"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_conversation_error_detail("invalid_host", "Only named SSH hosts can be added."),
            )
        data = await mutation(
            request,
            service.add_host,
            payload.get("display_name"),
            payload.get("ssh_alias"),
            payload.get("codex_command_override"),
        )
        return ok_response("conversation host added", data)

    @app.post("/api/conversation-hosts/{host_id}/probe")
    async def probe_conversation_host(
        host_id: str,
        request: Request,
        service=Depends(current_conversation_service),
    ):
        data = await mutation(request, service.probe_host, host_id)
        return ok_response("conversation host checked", data)

    @app.post("/api/conversation-projects")
    async def create_conversation_project(
        request: Request,
        payload: dict[str, object] = Body(...),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.add_project,
            payload.get("host_id"),
            payload.get("label"),
            payload.get("cwd"),
        )
        return ok_response("conversation project added", data)

    @app.get("/api/conversation-events")
    async def conversation_events(
        request: Request,
        after_event_id: int | None = Query(None, ge=0),
        session=Depends(current_session),
    ):
        active_root = resolve_allowed_memory_root(root, session.active_root)
        service = registry.service(active_root)
        stream_generation = stream_lifecycle.generation(active_root)
        snapshot = await service_response(service.workspace)
        header_cursor = _event_cursor(request.headers.get("last-event-id"))
        # Native EventSource reconnects retain the URL but advance Last-Event-ID.
        cursor = header_cursor if header_cursor is not None else after_event_id
        if cursor is None:
            cursor = _workspace_cursor(snapshot)

        session_cookie = request.cookies.get(SESSION_COOKIE)

        async def stream() -> AsyncIterator[str]:
            current_session = await run_in_threadpool(read_session_cookie, root, session_cookie)
            if (
                not _same_session(current_session, session)
                or not stream_lifecycle.is_current(active_root, stream_generation)
            ):
                return
            yield _sse("snapshot", snapshot, event_id=cursor)
            event_cursor = cursor
            loop = asyncio.get_running_loop()
            heartbeat_at = loop.time() + _SSE_HEARTBEAT_SECONDS
            while stream_lifecycle.is_current(active_root, stream_generation):
                events = await run_in_threadpool(
                    service.store.read_events,
                    after_event_id=event_cursor,
                    limit=_SSE_EVENT_PAGE_SIZE,
                )
                current_session = await run_in_threadpool(read_session_cookie, root, session_cookie)
                if (
                    not _same_session(current_session, session)
                    or not stream_lifecycle.is_current(active_root, stream_generation)
                ):
                    return
                if events:
                    heartbeat_at = loop.time() + _SSE_HEARTBEAT_SECONDS
                    for event in events:
                        event_id = event.get("event_id")
                        if isinstance(event_id, int):
                            event_cursor = event_id
                        yield _sse(
                            "conversation",
                            event,
                            event_id=event_id if isinstance(event_id, int) else None,
                        )
                    continue

                now = loop.time()
                if now >= heartbeat_at:
                    yield ": heartbeat\n\n"
                    heartbeat_at = now + _SSE_HEARTBEAT_SECONDS
                await asyncio.sleep(min(_SSE_POLL_SECONDS, max(0.0, heartbeat_at - loop.time())))

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return stream_lifecycle


_SSE_EVENT_PAGE_SIZE = 500
_SSE_POLL_SECONDS = 0.25
_SSE_HEARTBEAT_SECONDS = 15.0


class _StreamLifecycle:
    """Track root-scoped stream invalidations without blocking the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}

    def generation(self, root: Path) -> int:
        key = _root_key(root)
        with self._lock:
            return self._generations.get(key, 0)

    def is_current(self, root: Path, generation: int) -> bool:
        key = _root_key(root)
        with self._lock:
            return self._generations.get(key, 0) == generation

    def invalidate(self, root: Path) -> None:
        key = _root_key(root)
        with self._lock:
            self._generations[key] = self._generations.get(key, 0) + 1


def _root_key(root: Path) -> str:
    return os.path.normcase(str(Path(root).expanduser().resolve()))


def _same_session(current: WebSession | None, expected: WebSession) -> bool:
    return current == expected


async def service_response(
    action: Callable[..., dict[str, Any]],
    *args: object,
) -> dict[str, Any]:
    try:
        return await run_in_threadpool(action, *args)
    except ConversationError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=_conversation_error_detail(exc.code, str(exc)),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_conversation_error_detail("invalid_request", str(exc)),
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=_conversation_error_detail("app_server_timeout", "Codex did not answer in time."),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_conversation_error_detail("app_server_unavailable", "Could not start or reach Codex."),
        ) from exc


def _conversation_error_detail(code: str, message: str) -> dict[str, Any]:
    detail = error_detail(message)
    detail.update(code=code, diagnostics=[])
    return detail


def _event_cursor(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        cursor = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_conversation_error_detail("invalid_cursor", "Last-Event-ID must be a non-negative integer."),
        ) from exc
    if cursor < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_conversation_error_detail("invalid_cursor", "Last-Event-ID must be a non-negative integer."),
        )
    return cursor


def _workspace_cursor(snapshot: dict[str, Any]) -> int:
    value = snapshot.get("cursor", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _sse(event: str, data: dict[str, Any], *, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend((f"event: {event}", "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")), ""))
    return "\n".join(lines) + "\n"
