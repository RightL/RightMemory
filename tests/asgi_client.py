from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


class CookieJar:
    def __init__(self) -> None:
        self._cookies: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self._cookies[str(name)] = str(value)

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._cookies.get(str(name), default)

    def update_from_header(self, value: str) -> None:
        cookie = SimpleCookie()
        cookie.load(value)
        for name, morsel in cookie.items():
            self._cookies[name] = morsel.value

    def header_value(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self._cookies.items())


class Headers:
    def __init__(self, raw_headers: list[tuple[bytes, bytes]]) -> None:
        self._items: dict[str, list[str]] = {}
        for key, value in raw_headers:
            self._items.setdefault(key.decode("latin-1").lower(), []).append(value.decode("latin-1"))

    def __getitem__(self, key: str) -> str:
        values = self._items[key.lower()]
        return ", ".join(values)

    def __contains__(self, key: str) -> bool:
        return key.lower() in self._items

    def __iter__(self):
        return iter(self._items)

    def get(self, key: str, default: str | None = None) -> str | None:
        values = self._items.get(key.lower())
        if not values:
            return default
        return ", ".join(values)

    def get_list(self, key: str) -> list[str]:
        return list(self._items.get(key.lower(), []))


class ASGIResponse:
    def __init__(self, *, status_code: int, headers: list[tuple[bytes, bytes]], content: bytes) -> None:
        self.status_code = status_code
        self.headers = Headers(headers)
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.text)


class ASGITestClient:
    def __init__(self, app, *, request_timeout_seconds: float = 5) -> None:
        self.app = app
        self.cookies = CookieJar()
        self.request_timeout_seconds = request_timeout_seconds

    def get(self, path: str, *, headers: dict[str, str] | None = None) -> ASGIResponse:
        return self.request("GET", path, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: object | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ASGIResponse:
        return self.request("POST", path, json=json, content=content, headers=headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> ASGIResponse:
        response = asyncio.run(self._request(method, path, json_body=json, content=content, headers=headers or {}))
        for set_cookie in response.headers.get_list("set-cookie"):
            self.cookies.update_from_header(set_cookie)
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None,
        content: bytes | str | None,
        headers: dict[str, str],
    ) -> ASGIResponse:
        parsed = urlsplit(path)
        request_path = parsed.path or "/"
        request_headers = {key.lower(): value for key, value in headers.items()}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("content-type", "application/json")
        elif isinstance(content, str):
            body = content.encode("utf-8")
        elif content is None:
            body = b""
        else:
            body = content
        cookie_header = self.cookies.header_value()
        if cookie_header and "cookie" not in request_headers:
            request_headers["cookie"] = cookie_header
        request_headers.setdefault("host", parsed.netloc or "testserver")
        raw_headers = [(key.encode("latin-1"), value.encode("latin-1")) for key, value in request_headers.items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": parsed.scheme or "http",
            "path": request_path,
            "raw_path": request_path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": (parsed.netloc or "testserver", 80),
            "state": {},
        }
        response_complete = asyncio.Event()
        request_sent = False
        status_code = 500
        response_headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code, response_headers
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete.set()

        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(response_complete.wait(), timeout=self.request_timeout_seconds)
        except TimeoutError as exc:
            if task.done():
                await task
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise TimeoutError("ASGI app did not send a complete response") from exc
        if task.done():
            await task
        else:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return ASGIResponse(status_code=status_code, headers=response_headers, content=b"".join(body_parts))
