from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .jsonrpc import (
    JsonRpcConnection,
    JsonRpcProtocolError,
    RpcId,
    RpcNotification,
    RpcServerRequest,
)
from .transport import SubprocessTransport, transport_for_host


@dataclass(frozen=True, slots=True)
class AppServerNotification:
    epoch: str
    method: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AppServerRequest:
    epoch: str
    request_id: RpcId
    method: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AppServerDisconnect:
    epoch: str
    error: BaseException | None


NotificationHandler = Callable[[AppServerNotification], None]
ServerRequestHandler = Callable[[AppServerRequest], None]
DisconnectHandler = Callable[[AppServerDisconnect], None]


class StaleConnectionEpochError(RuntimeError):
    """A browser response belongs to an App Server process that is no longer active."""


class CodexAppServer:
    """Typed facade for the stable Codex App Server thread and turn methods."""

    def __init__(
        self,
        transport: SubprocessTransport,
        *,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
        request_timeout: float = 30.0,
        stderr_limit: int = 16_384,
    ):
        self._handler_lock = threading.Lock()
        self._notification_handler = on_notification
        self._server_request_handler = on_server_request
        self._disconnect_handler = on_disconnect
        self._connection = JsonRpcConnection(
            transport,
            on_notification=self._handle_notification,
            on_server_request=self._handle_server_request,
            on_disconnect=self._handle_disconnect,
            request_timeout=request_timeout,
            stderr_limit=stderr_limit,
        )

    @property
    def epoch(self) -> str:
        return self._connection.epoch

    @property
    def connected(self) -> bool:
        return self._connection.connected

    @property
    def stderr_tail(self) -> str:
        return self._connection.stderr_tail

    def set_handlers(
        self,
        *,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        with self._handler_lock:
            self._notification_handler = on_notification
            self._server_request_handler = on_server_request
            self._disconnect_handler = on_disconnect

    def connect(self) -> dict[str, Any]:
        return self._connection.start()

    def close(self) -> None:
        self._connection.close()

    def start_thread(
        self,
        cwd: str | os.PathLike[str],
        *,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        ephemeral: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": os.fspath(cwd)}
        _put_optional_string(params, "model", model)
        _put_optional_string(params, "approvalPolicy", approval_policy)
        _put_optional_string(params, "sandbox", sandbox)
        if ephemeral is not None:
            if not isinstance(ephemeral, bool):
                raise ValueError("ephemeral must be a boolean")
            params["ephemeral"] = ephemeral
        return self._request_object("thread/start", params)

    def list_models(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        include_hidden: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        _put_optional_string(params, "cursor", cursor)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("model list limit must be a positive integer")
            params["limit"] = limit
        if include_hidden is not None:
            if not isinstance(include_hidden, bool):
                raise ValueError("include_hidden must be a boolean")
            params["includeHidden"] = include_hidden
        return self._request_object("model/list", params)

    def read_config(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        include_layers: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(include_layers, bool):
            raise ValueError("include_layers must be a boolean")
        params: dict[str, Any] = {"includeLayers": include_layers}
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        return self._request_object("config/read", params)

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request_object("thread/resume", {"threadId": _identifier(thread_id, "thread")})

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        if not isinstance(include_turns, bool):
            raise ValueError("include_turns must be a boolean")
        return self._request_object(
            "thread/read",
            {"threadId": _identifier(thread_id, "thread"), "includeTurns": include_turns},
        )

    def list_threads(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        cwd: str | os.PathLike[str] | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        _put_optional_string(params, "cursor", cursor)
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("thread list limit must be a positive integer")
            params["limit"] = limit
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        if archived is not None:
            if not isinstance(archived, bool):
                raise ValueError("archived must be a boolean")
            params["archived"] = archived
        return self._request_object("thread/list", params)

    def archive_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request_object("thread/archive", {"threadId": _identifier(thread_id, "thread")})

    def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("turn text must be a non-empty string")
        params: dict[str, Any] = {
            "threadId": _identifier(thread_id, "thread"),
            "input": [{"type": "text", "text": text}],
        }
        _put_optional_string(params, "model", model)
        _put_optional_string(params, "effort", reasoning_effort)
        _put_optional_string(params, "approvalPolicy", approval_policy)
        return self._request_object("turn/start", params)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self._request_object(
            "turn/interrupt",
            {
                "threadId": _identifier(thread_id, "thread"),
                "turnId": _identifier(turn_id, "turn"),
            },
        )

    def respond_server_request(
        self,
        request_id: RpcId,
        *,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
        epoch: str | None = None,
    ) -> None:
        current_epoch = self.epoch
        if epoch is not None and epoch != current_epoch:
            raise StaleConnectionEpochError(
                "App Server request belongs to a stale connection and cannot be replayed"
            )
        self._connection.respond(request_id, result=result, error=error)

    def _request_object(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        result = self._connection.request(method, params)
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise JsonRpcProtocolError(f"{method} result was not an object")
        return result

    def _handle_notification(self, notification: RpcNotification) -> None:
        event = AppServerNotification(self.epoch, notification.method, notification.params)
        with self._handler_lock:
            handler = self._notification_handler
        if handler is not None:
            handler(event)

    def _handle_server_request(self, request: RpcServerRequest) -> None:
        event = AppServerRequest(self.epoch, request.request_id, request.method, request.params)
        with self._handler_lock:
            handler = self._server_request_handler
        if handler is not None:
            handler(event)

    def _handle_disconnect(self, error: BaseException | None) -> None:
        event = AppServerDisconnect(self.epoch, error)
        with self._handler_lock:
            handler = self._disconnect_handler
        if handler is not None:
            handler(event)


def create_app_server(
    host: Any,
    *,
    local_cwd: str | os.PathLike[str] | None = None,
    on_notification: NotificationHandler | None = None,
    on_server_request: ServerRequestHandler | None = None,
    on_disconnect: DisconnectHandler | None = None,
    request_timeout: float = 30.0,
    environment: Mapping[str, str] | None = None,
) -> CodexAppServer:
    return CodexAppServer(
        transport_for_host(host, local_cwd=local_cwd, environment=environment),
        on_notification=on_notification,
        on_server_request=on_server_request,
        on_disconnect=on_disconnect,
        request_timeout=request_timeout,
    )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} id must be a non-empty string")
    return value.strip()


def _put_optional_string(target: dict[str, Any], key: str, value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    target[key] = value.strip()
