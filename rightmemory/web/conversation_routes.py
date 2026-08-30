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
from starlette.responses import FileResponse, StreamingResponse

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
    app.router.add_event_handler("shutdown", stream_lifecycle.close)

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

    @app.get("/api/conversation-models")
    async def conversation_models(
        host_id: str = Query(...),
        service=Depends(current_conversation_service),
    ):
        data = await service_response(service.model_catalog, host_id)
        return ok_response("conversation models loaded", data)

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
            payload.get("model"),
            payload.get("reasoning_effort"),
        )
        return ok_response("conversation created", data)

    @app.get("/api/conversations/{conversation_id}")
    async def conversation_detail(
        conversation_id: str,
        after_event_id: int | None = Query(None, ge=0),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await service_response(
            service.detail, conversation_id, after_event_id, session.session_id
        )
        return ok_response("conversation loaded", data)

    @app.get("/api/conversations/{conversation_id}/history")
    async def conversation_history(
        conversation_id: str,
        before_event_id: int = Query(..., ge=1),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await service_response(
            service.earlier_history,
            conversation_id,
            before_event_id,
            session.session_id,
        )
        return ok_response("earlier conversation history loaded", data)

    @app.post("/api/conversations/{parent_conversation_id}/side-chats")
    async def create_side_chat(
        parent_conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.create_side_chat,
            parent_conversation_id,
            session.session_id,
        )
        return ok_response("side chat created", data)

    @app.delete("/api/side-chats/{conversation_id}")
    async def close_side_chat(
        conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request, service.close_side_chat, conversation_id, session.session_id
        )
        return ok_response("side chat closed", data)

    @app.post("/api/conversations/{conversation_id}/read")
    async def acknowledge_conversation_read(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.acknowledge_read,
            conversation_id,
            session.session_id,
            payload.get("event_id"),
        )
        return ok_response("conversation marked read", data)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_conversation_message(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.send_message,
            conversation_id,
            payload.get("text"),
            payload.get("attachment_ids"),
            session.session_id,
        )
        return ok_response("message sent", data)

    @app.post("/api/conversations/{conversation_id}/attachments")
    async def upload_conversation_attachment(
        conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        content = await _bounded_request_body(request, _MAX_RAW_ATTACHMENT_BYTES)
        data = await service_response(
            service.upload_attachment,
            conversation_id,
            content,
            request.headers.get("content-type"),
            request.headers.get("x-filename"),
            session.session_id,
            request.headers.get("x-attachment-id"),
            request.headers.get("x-attachment-kind"),
        )
        return ok_response("attachment staged", data)

    @app.get("/api/conversations/{conversation_id}/attachments/{attachment_id}")
    async def read_conversation_attachment(
        conversation_id: str,
        attachment_id: str,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        metadata, path = await service_response(
            service.attachment_file,
            conversation_id,
            attachment_id,
            session.session_id,
        )
        return FileResponse(
            path,
            media_type=metadata["media_type"],
            filename=metadata["display_name"],
            content_disposition_type=(
                "attachment" if metadata.get("kind") == "file" else "inline"
            ),
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete("/api/conversations/{conversation_id}/attachments/{attachment_id}")
    async def delete_conversation_attachment(
        conversation_id: str,
        attachment_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.delete_staged_attachment,
            conversation_id,
            attachment_id,
            session.session_id,
        )
        return ok_response("attachment removed", data)

    @app.post("/api/conversations/{conversation_id}/settings")
    async def update_conversation_settings(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.update_settings,
            conversation_id,
            payload.get("model"),
            payload.get("reasoning_effort"),
            session.session_id,
        )
        return ok_response("conversation settings updated", data)

    @app.post("/api/conversations/{conversation_id}/interrupt")
    async def interrupt_conversation(
        conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request, service.interrupt, conversation_id, session.session_id
        )
        return ok_response("conversation interrupted", data)

    @app.post("/api/conversations/{conversation_id}/reconcile")
    async def reconcile_conversation(
        conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request, service.reconcile, conversation_id, session.session_id
        )
        return ok_response("conversation status reconciled", data)

    @app.post("/api/conversations/{conversation_id}/archive")
    async def archive_conversation(
        conversation_id: str,
        request: Request,
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request, service.archive, conversation_id, session.session_id
        )
        return ok_response("conversation archived", data)

    @app.post("/api/conversations/{conversation_id}/move")
    async def move_conversation(
        conversation_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.move,
            conversation_id,
            payload.get("pursuit_id"),
            session.session_id,
        )
        return ok_response("conversation attachment moved", data)

    @app.post("/api/conversations/{conversation_id}/server-requests/{request_key}/respond")
    async def respond_to_server_request(
        conversation_id: str,
        request_key: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        data = await mutation(
            request,
            service.respond_request,
            request_key,
            payload.get("decision"),
            payload.get("response"),
            conversation_id,
            session.session_id,
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
        view_id: str = Query(..., min_length=1, max_length=128),
        page_id: str = Query(..., min_length=1, max_length=128),
        session=Depends(current_session),
    ):
        active_root = resolve_allowed_memory_root(root, session.active_root)
        service = registry.service(active_root)
        stream_token, stream_generation = await run_in_threadpool(
            stream_lifecycle.open_stream,
            active_root,
            session.session_id,
            view_id,
            page_id,
        )

        def cleanup_side_chats() -> None:
            service.close_side_chats_for_session(session.session_id)

        try:
            snapshot = await service_response(service.workspace)
            header_cursor = _event_cursor(request.headers.get("last-event-id"))
            # Native EventSource reconnects retain the URL but advance Last-Event-ID.
            cursor = header_cursor if header_cursor is not None else after_event_id
            if cursor is None:
                cursor = _workspace_cursor(snapshot)
        except BaseException:
            stream_lifecycle.close_stream(
                active_root,
                session.session_id,
                stream_token,
                cleanup_side_chats,
            )
            raise

        session_cookie = request.cookies.get(SESSION_COOKIE)

        async def stream() -> AsyncIterator[str]:
            try:
                current_session = await run_in_threadpool(read_session_cookie, root, session_cookie)
                if (
                    not _same_session(current_session, session)
                    or not stream_lifecycle.is_current(
                        active_root, session.session_id, stream_generation
                    )
                ):
                    return
                yield _sse("snapshot", snapshot, event_id=cursor)
                event_cursor = cursor
                loop = asyncio.get_running_loop()
                heartbeat_at = loop.time() + _SSE_HEARTBEAT_SECONDS
                while stream_lifecycle.is_current(
                    active_root, session.session_id, stream_generation
                ):
                    events = await run_in_threadpool(
                        service.store.read_events_for_session,
                        session.session_id,
                        after_event_id=event_cursor,
                        limit=_SSE_EVENT_PAGE_SIZE,
                    )
                    current_session = await run_in_threadpool(read_session_cookie, root, session_cookie)
                    if (
                        not _same_session(current_session, session)
                        or not stream_lifecycle.is_current(
                            active_root, session.session_id, stream_generation
                        )
                    ):
                        return
                    if events:
                        heartbeat_at = loop.time() + _SSE_HEARTBEAT_SECONDS
                        for event in events:
                            if not stream_lifecycle.is_current(
                                active_root,
                                session.session_id,
                                stream_generation,
                            ):
                                return
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
            finally:
                stream_lifecycle.close_stream(
                    active_root,
                    session.session_id,
                    stream_token,
                    cleanup_side_chats,
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/conversation-session/release")
    async def release_conversation_view(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
        service=Depends(current_conversation_service),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        view_id = _required_view_identifier(payload.get("view_id"), "view_id")
        page_id = _required_view_identifier(payload.get("page_id"), "page_id")
        active_root = resolve_allowed_memory_root(root, session.active_root)

        def cleanup_side_chats() -> None:
            service.close_side_chats_for_session(session.session_id)

        released = stream_lifecycle.release_view(
            active_root,
            session.session_id,
            view_id,
            page_id,
            cleanup_side_chats,
        )
        return ok_response("conversation view released", {"released": released})

    return stream_lifecycle


_SSE_EVENT_PAGE_SIZE = 500
_SSE_POLL_SECONDS = 0.25
_SSE_HEARTBEAT_SECONDS = 15.0
_SIDE_CHAT_RELEASE_GRACE_SECONDS = 15.0
_MAX_RELEASED_VIEW_TOMBSTONES = 256
_MAX_RAW_ATTACHMENT_BYTES = 20 * 1024 * 1024
_VIEW_ACTIVE = "active"
_VIEW_DISCONNECTED = "disconnected"
_VIEW_RELEASED = "released"


class _StreamLifecycle:
    """Lease browser views across transient stream loss and page crashes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generations: dict[tuple[str, str], int] = {}
        self._streams: dict[
            tuple[str, str], dict[object, tuple[str, str]]
        ] = {}
        self._views: dict[
            tuple[str, str], dict[tuple[str, str], str]
        ] = {}
        self._cleanup_timers: dict[tuple[str, str], threading.Timer] = {}
        self._cleanup_tokens: dict[tuple[str, str], object] = {}
        self._cleanup_gates: dict[tuple[str, str], threading.Event] = {}

    def is_current(
        self, root: Path, session_id: str, generation: int
    ) -> bool:
        key = (_root_key(root), session_id)
        with self._lock:
            return self._generations.get(key, 0) == generation

    def invalidate(self, root: Path, session_id: str) -> None:
        key = (_root_key(root), session_id)
        with self._lock:
            self._generations[key] = self._generations.get(key, 0) + 1
            timer = self._cleanup_timers.pop(key, None)
            self._cleanup_tokens.pop(key, None)
            self._streams.pop(key, None)
            self._views.pop(key, None)
        if timer is not None:
            timer.cancel()

    def open_stream(
        self,
        root: Path,
        session_id: str,
        view_id: str,
        page_id: str,
    ) -> tuple[object, int]:
        """Register a stream and snapshot its invalidation generation atomically."""
        key = (_root_key(root), session_id)
        stream_token = object()
        while True:
            with self._lock:
                cleanup_gate = self._cleanup_gates.get(key)
                if cleanup_gate is None:
                    page_key = (view_id, page_id)
                    views = self._views.setdefault(key, {})
                    if views.get(page_key) != _VIEW_RELEASED:
                        views[page_key] = _VIEW_ACTIVE
                    self._streams.setdefault(key, {})[stream_token] = page_key
                    timer = self._cleanup_timers.pop(key, None)
                    self._cleanup_tokens.pop(key, None)
                    generation = self._generations.get(key, 0)
                    break
            # The route invokes this method in Starlette's worker pool, so a
            # slow provider cleanup blocks only this key's reconnect attempt.
            cleanup_gate.wait()
        if timer is not None:
            timer.cancel()
        return stream_token, generation

    def close_stream(
        self,
        root: Path,
        session_id: str,
        stream_token: object,
        cleanup: Callable[[], None],
    ) -> None:
        """Start a bounded abandonment lease after a page loses its last stream."""
        key = (_root_key(root), session_id)
        with self._lock:
            streams = self._streams.get(key)
            if streams is None or stream_token not in streams:
                return
            page_key = streams.pop(stream_token)
            if not streams:
                self._streams.pop(key, None)
            if page_key not in streams.values():
                views = self._views.get(key)
                if views is not None and views.get(page_key) == _VIEW_ACTIVE:
                    views[page_key] = _VIEW_DISCONNECTED
            timer = self._schedule_cleanup_locked(key, cleanup)
        if timer is not None:
            timer.start()

    def release_view(
        self,
        root: Path,
        session_id: str,
        view_id: str,
        page_id: str,
        cleanup: Callable[[], None],
    ) -> bool:
        """Record an exact page release even when it races ahead of SSE setup."""
        key = (_root_key(root), session_id)
        with self._lock:
            views = self._views.setdefault(key, {})
            page_key = (view_id, page_id)
            views[page_key] = _VIEW_RELEASED
            self._prune_released_views_locked(key, preserve=page_key)
            timer = self._schedule_cleanup_locked(key, cleanup)
        if timer is not None:
            timer.start()
        return True

    def _prune_released_views_locked(
        self,
        key: tuple[str, str],
        *,
        preserve: tuple[str, str],
    ) -> None:
        """Bound inactive release tombstones while retaining active page records."""
        views = self._views[key]
        active_pages = set(self._streams.get(key, {}).values())
        inactive_released = [
            page_key
            for page_key, view_state in views.items()
            if view_state == _VIEW_RELEASED and page_key not in active_pages
        ]
        excess = len(inactive_released) - _MAX_RELEASED_VIEW_TOMBSTONES
        if excess <= 0:
            return
        for page_key in inactive_released:
            if excess <= 0:
                break
            if page_key == preserve:
                continue
            views.pop(page_key, None)
            excess -= 1

    def _schedule_cleanup_locked(
        self,
        key: tuple[str, str],
        cleanup: Callable[[], None],
    ) -> threading.Timer | None:
        views = self._views.get(key)
        if (
            self._streams.get(key)
            or not views
            or any(view_state == _VIEW_ACTIVE for view_state in views.values())
        ):
            return None
        if key in self._cleanup_timers:
            # Repeated release requests are idempotent and must not extend the
            # already-running abandonment lease.
            return None
        cleanup_token = object()
        self._cleanup_tokens[key] = cleanup_token
        timer = threading.Timer(
            _SIDE_CHAT_RELEASE_GRACE_SECONDS,
            self._run_cleanup,
            args=(key, cleanup_token, cleanup),
        )
        timer.daemon = True
        self._cleanup_timers[key] = timer
        return timer

    def close(self) -> None:
        """Cancel grace timers when the web application shuts down."""
        with self._lock:
            timers = list(self._cleanup_timers.values())
            self._generations.clear()
            self._cleanup_timers.clear()
            self._cleanup_tokens.clear()
            self._streams.clear()
            self._views.clear()
        for timer in timers:
            timer.cancel()

    def _run_cleanup(
        self,
        key: tuple[str, str],
        cleanup_token: object,
        cleanup: Callable[[], None],
    ) -> None:
        cleanup_gate: threading.Event | None = None
        with self._lock:
            if (
                self._cleanup_tokens.get(key) is not cleanup_token
                or key in self._cleanup_gates
                or self._streams.get(key)
                or not self._views.get(key)
                or any(
                    view_state == _VIEW_ACTIVE
                    for view_state in self._views[key].values()
                )
            ):
                return
            self._cleanup_tokens.pop(key, None)
            self._cleanup_timers.pop(key, None)
            self._generations.pop(key, None)
            self._views.pop(key, None)
            cleanup_gate = threading.Event()
            self._cleanup_gates[key] = cleanup_gate
        try:
            cleanup()
        except Exception:
            # A later startup purge is the final recovery path if session-end
            # cleanup loses storage or provider access.
            return
        finally:
            with self._lock:
                if self._cleanup_gates.get(key) is cleanup_gate:
                    self._cleanup_gates.pop(key, None)
            if cleanup_gate is not None:
                cleanup_gate.set()


def _root_key(root: Path) -> str:
    return os.path.normcase(str(Path(root).expanduser().resolve()))


def _required_view_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_conversation_error_detail(
                "invalid_view_identifier", f"{field} must be a non-empty identifier."
            ),
        )
    return value


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


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_conversation_error_detail(
                    "invalid_attachment", "Content-Length must be an integer."
                ),
            ) from exc
        if declared < 0 or declared > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=_conversation_error_detail(
                    "attachment_too_large", "The attachment is too large."
                ),
            )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=_conversation_error_detail(
                    "attachment_too_large", "The attachment is too large."
                ),
            )
    return bytes(body)
