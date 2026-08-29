from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .transport import SubprocessTransport


RpcId = int | str


class JsonRpcError(RuntimeError):
    """Base error for the App Server JSONL connection."""


class JsonRpcConnectionError(JsonRpcError):
    """The child process or its stdio connection stopped working."""


class JsonRpcConnectionClosed(JsonRpcConnectionError):
    """The connection was closed before an operation completed."""


class JsonRpcProtocolError(JsonRpcError):
    """The peer emitted a malformed JSON-RPC message."""


class JsonRpcTimeoutError(JsonRpcError):
    """A request did not receive a response before its deadline."""


class JsonRpcRemoteError(JsonRpcError):
    def __init__(self, code: int | str | None, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True, slots=True)
class RpcNotification:
    method: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RpcServerRequest:
    request_id: RpcId
    method: str
    params: dict[str, Any]


NotificationHandler = Callable[[RpcNotification], None]
ServerRequestHandler = Callable[[RpcServerRequest], None]
DisconnectHandler = Callable[[BaseException | None], None]


class _BoundedTextBuffer:
    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError("stderr limit must be positive")
        self._limit = limit
        self._value = ""
        self._lock = threading.Lock()

    def append(self, value: str) -> None:
        with self._lock:
            combined = self._value + value
            self._value = combined[-self._limit :]

    def get(self) -> str:
        with self._lock:
            return self._value


class JsonRpcConnection:
    """Persistent, thread-based JSON-RPC connection over JSON Lines stdio."""

    def __init__(
        self,
        transport: SubprocessTransport,
        *,
        on_notification: NotificationHandler | None = None,
        on_server_request: ServerRequestHandler | None = None,
        on_disconnect: DisconnectHandler | None = None,
        request_timeout: float = 30.0,
        stderr_limit: int = 16_384,
        dispatch_queue_limit: int = 2_048,
        client_name: str = "rightmemory",
        client_version: str = "0.1.0",
    ):
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        if not client_name.strip() or not client_version.strip():
            raise ValueError("client name and version must be non-empty")
        if (
            isinstance(dispatch_queue_limit, bool)
            or not isinstance(dispatch_queue_limit, int)
            or dispatch_queue_limit < 2
        ):
            raise ValueError("dispatch queue limit must be at least two")
        self._transport = transport
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_disconnect = on_disconnect
        self._request_timeout = request_timeout
        self._client_name = client_name
        self._client_version = client_version
        self._stderr = _BoundedTextBuffer(stderr_limit)

        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._dispatch_queue: queue.Queue[tuple[str, Any]] = queue.Queue(
            maxsize=dispatch_queue_limit
        )

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._next_request_id = 1
        self._epoch = ""
        self._started = False
        self._closing = False
        self._disconnect_published = False

    @property
    def epoch(self) -> str:
        with self._state_lock:
            return self._epoch

    @property
    def connected(self) -> bool:
        with self._state_lock:
            process = self._process
            return self._started and not self._closing and process is not None and process.poll() is None

    @property
    def stderr_tail(self) -> str:
        return self._stderr.get()

    def start(self) -> dict[str, Any]:
        with self._state_lock:
            if self._started:
                raise JsonRpcConnectionError("JSON-RPC connection was already started")
            self._process = self._transport.spawn()
            process = self._process
            if process.stdin is None or process.stdout is None or process.stderr is None:
                self._process = None
                _terminate_process(process)
                raise JsonRpcConnectionError("App Server did not expose all stdio pipes")
            self._epoch = uuid.uuid4().hex
            self._started = True
            self._closing = False
            self._disconnect_published = False

        self._dispatch_thread = threading.Thread(
            target=self._dispatch_main,
            name="rightmemory-app-server-dispatch",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader_main,
            name="rightmemory-app-server-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_main,
            name="rightmemory-app-server-stderr",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._reader_thread.start()
        self._stderr_thread.start()

        try:
            result = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self._client_name,
                        "version": self._client_version,
                    }
                },
            )
            if result is None:
                normalized: dict[str, Any] = {}
            elif isinstance(result, dict):
                normalized = result
            else:
                raise JsonRpcProtocolError("initialize result was not an object")
            self.notify("initialized")
            return normalized
        except BaseException:
            self.close()
            raise

    def request_future(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> Future[Any]:
        method = _method(method)
        request_id = self._allocate_request_id()
        future: Future[Any] = Future()
        with self._pending_lock:
            self._pending[request_id] = future
        try:
            self._write_message(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
        except BaseException as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            future.set_exception(exc)
            self._disconnect(_connection_error("failed to write App Server request", exc))
        return future

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        future = self.request_future(method, params)
        wait_for = self._request_timeout if timeout is None else timeout
        if wait_for <= 0:
            raise ValueError("request timeout must be positive")
        try:
            return future.result(timeout=wait_for)
        except FutureTimeoutError as exc:
            request_id = self._remove_pending_future(future)
            future.cancel()
            detail = f"App Server request {method!r} timed out after {wait_for:g} seconds"
            if request_id is not None:
                detail += f" (request {request_id})"
            raise JsonRpcTimeoutError(detail) from exc

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": _method(method)}
        if params is not None:
            message["params"] = dict(params)
        self._write_message(message)

    def respond(
        self,
        request_id: RpcId,
        *,
        result: Any = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        request_id = _rpc_id(request_id)
        message: dict[str, Any] = {"id": request_id}
        if error is None:
            message["result"] = result
        else:
            message["error"] = dict(error)
        self._write_message(message)

    def close(self) -> None:
        self._disconnect(None)
        current = threading.current_thread()
        for thread in (self._reader_thread, self._stderr_thread, self._dispatch_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=1.0)

    def _allocate_request_id(self) -> int:
        with self._state_lock:
            if not self._started or self._closing or self._process is None:
                raise JsonRpcConnectionClosed("App Server connection is not open")
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    def _write_message(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self._state_lock:
                process = self._process
                if not self._started or self._closing or process is None or process.stdin is None:
                    raise JsonRpcConnectionClosed("App Server connection is not open")
                stdin = process.stdin
            try:
                stdin.write(encoded)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise _connection_error("App Server stdin failed", exc) from exc

    def _reader_main(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        error: BaseException | None = None
        try:
            for raw_line in process.stdout:
                if not raw_line.strip():
                    continue
                self._handle_line(raw_line)
            with self._state_lock:
                closing = self._closing
            if not closing:
                detail = self.stderr_tail.strip()
                message = "App Server stdout closed"
                if detail:
                    message += f": {detail}"
                error = JsonRpcConnectionError(message)
        except BaseException as exc:
            error = exc if isinstance(exc, JsonRpcError) else _connection_error(
                "App Server stdout reader failed", exc
            )
        finally:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
            if error is not None:
                self._disconnect(error)

    def _stderr_main(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            for raw_line in process.stderr:
                self._stderr.append(raw_line)
        except (OSError, ValueError) as exc:
            self._stderr.append(f"stderr reader failed: {type(exc).__name__}: {exc}\n")
        finally:
            try:
                process.stderr.close()
            except (OSError, ValueError):
                pass

    def _handle_line(self, raw_line: str) -> None:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise JsonRpcProtocolError(f"invalid App Server JSON: {exc.msg}") from exc
        if not isinstance(message, dict):
            raise JsonRpcProtocolError("App Server message was not a JSON object")

        method = message.get("method")
        has_id = "id" in message
        if isinstance(method, str) and method:
            params = message.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise JsonRpcProtocolError(f"App Server {method!r} params were not an object")
            if has_id:
                event = RpcServerRequest(_rpc_id(message["id"]), method, params)
                self._queue_dispatch("request", event)
            else:
                self._queue_dispatch("notification", RpcNotification(method, params))
            return

        if not has_id:
            raise JsonRpcProtocolError("App Server message had neither method nor id")
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return
        with self._pending_lock:
            future = self._pending.pop(request_id, None)
        if future is None or future.cancelled():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(_remote_error(error))
            return
        if "result" not in message:
            future.set_exception(JsonRpcProtocolError("App Server response had no result or error"))
            return
        future.set_result(message["result"])

    def _dispatch_main(self) -> None:
        while True:
            kind, payload = self._dispatch_queue.get()
            if kind == "stop":
                return
            try:
                if kind == "notification" and self._on_notification is not None:
                    self._on_notification(payload)
                elif kind == "request" and self._on_server_request is not None:
                    self._on_server_request(payload)
                elif kind == "disconnect" and self._on_disconnect is not None:
                    self._on_disconnect(payload)
            except BaseException as exc:
                if kind == "request":
                    try:
                        self.respond(
                            payload.request_id,
                            error={
                                "code": -32603,
                                "message": f"RightMemory request handler failed: {type(exc).__name__}",
                            },
                        )
                    except JsonRpcError:
                        pass

    def _disconnect(self, error: BaseException | None) -> None:
        with self._state_lock:
            if self._closing:
                return
            if not self._started and self._process is None:
                return
            self._closing = True
            process = self._process

        failure = error or JsonRpcConnectionClosed("App Server connection closed")
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(failure)

        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            _terminate_process(process)

        with self._state_lock:
            self._started = False
            if not self._disconnect_published:
                self._disconnect_published = True
                publish = True
            else:
                publish = False
        if publish:
            self._queue_terminal_events(error)

    def _queue_dispatch(self, kind: str, payload: Any) -> None:
        try:
            self._dispatch_queue.put_nowait((kind, payload))
        except queue.Full as exc:
            raise JsonRpcConnectionError(
                "App Server callback queue overflowed; connection closed to bound memory use"
            ) from exc

    def _queue_terminal_events(self, error: BaseException | None) -> None:
        # On overflow, pending callback work is no longer a trustworthy projection of
        # the stream. Drop it and reserve the bounded queue for terminal delivery.
        while True:
            try:
                self._dispatch_queue.get_nowait()
            except queue.Empty:
                break
        self._dispatch_queue.put_nowait(("disconnect", error))
        self._dispatch_queue.put_nowait(("stop", None))

    def _remove_pending_future(self, target: Future[Any]) -> int | None:
        with self._pending_lock:
            for request_id, future in self._pending.items():
                if future is target:
                    self._pending.pop(request_id)
                    return request_id
        return None


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _method(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("JSON-RPC method must be a non-empty string")
    return value.strip()


def _rpc_id(value: object) -> RpcId:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise JsonRpcProtocolError("JSON-RPC id must be a string or integer")
    if isinstance(value, str) and not value:
        raise JsonRpcProtocolError("JSON-RPC string id must not be empty")
    return value


def _remote_error(value: object) -> JsonRpcRemoteError:
    if not isinstance(value, dict):
        return JsonRpcRemoteError(None, f"App Server error: {value}")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(message, str) or not message.strip():
        message = "App Server request failed"
    return JsonRpcRemoteError(code if isinstance(code, (int, str)) else None, message, value.get("data"))


def _connection_error(prefix: str, exc: BaseException) -> JsonRpcConnectionError:
    return JsonRpcConnectionError(f"{prefix}: {type(exc).__name__}: {exc}")
