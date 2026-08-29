from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

import anyio.to_thread

from rightmemory.conversations import ConversationError
from rightmemory.web.app import create_web_app
from rightmemory.web.auth import read_session_cookie, revoke_session
from tests.asgi_client import ASGITestClient


class _FakeEventStore:
    def __init__(self):
        self.events = [
            {
                "event_id": 8,
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "kind": "agent_message_delta",
                "payload": {"delta": "hello"},
                "created_at": "2026-08-29T00:00:00+00:00",
            }
        ]
        self.calls: list[tuple[int, int]] = []

    def read_events(self, *, after_event_id=0, limit=200):
        self.calls.append((after_event_id, limit))
        return [event for event in self.events if event["event_id"] > after_event_id][:limit]


class _FakeConversationService:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[object, ...]] = []
        self.store = _FakeEventStore()
        self.workspace_started: threading.Event | None = None
        self.workspace_release: threading.Event | None = None

    def workspace(self):
        if self.workspace_started is not None:
            self.workspace_started.set()
        if self.workspace_release is not None and not self.workspace_release.wait(timeout=2.0):
            raise TimeoutError("test workspace was not released")
        return {
            "hosts": [{"host_id": "local", "kind": "local", "display_name": "Local"}],
            "projects": [{"project_id": "local-root", "host_id": "local", "cwd": str(self.root)}],
            "conversations": [],
            "pending_requests": [],
            "cursor": 7,
        }

    def list_for_pursuit(self, pursuit_id):
        self.calls.append(("list", pursuit_id))
        return {"conversations": []}

    def detail(self, conversation_id, after_event_id=0):
        self.calls.append(("detail", conversation_id, after_event_id))
        if conversation_id == "missing":
            raise ConversationError("conversation_not_found", "Conversation not found.", 404)
        return {"conversation": {"conversation_id": conversation_id}, "events": [], "pending_requests": []}

    def create_conversation(self, pursuit_id, host_id, project_id):
        self.calls.append(("create", pursuit_id, host_id, project_id))
        return {"conversation": {"conversation_id": "conversation-1", "pursuit_id": pursuit_id}}

    def send_message(self, conversation_id, text):
        self.calls.append(("message", conversation_id, text))
        return {"conversation_id": conversation_id}

    def interrupt(self, conversation_id):
        self.calls.append(("interrupt", conversation_id))
        return {"conversation_id": conversation_id}

    def reconcile(self, conversation_id):
        self.calls.append(("reconcile", conversation_id))
        return {
            "conversation": {"conversation_id": conversation_id, "status": "idle"},
            "thread": {"id": "thread-1", "status": {"type": "idle"}},
            "resolved": True,
        }

    def archive(self, conversation_id):
        self.calls.append(("archive", conversation_id))
        return {"conversation_id": conversation_id}

    def move(self, conversation_id, pursuit_id):
        self.calls.append(("move", conversation_id, pursuit_id))
        return {"conversation_id": conversation_id}

    def respond_request(self, request_key, decision, response, expected_conversation_id):
        self.calls.append(("respond", request_key, decision, response, expected_conversation_id))
        return {"request_key": request_key}

    def add_host(self, display_name, ssh_alias, command_override=None):
        self.calls.append(("host", display_name, ssh_alias, command_override))
        return {"host": {"host_id": "host-1", "kind": "ssh", "display_name": display_name}}

    def probe_host(self, host_id):
        self.calls.append(("probe", host_id))
        return {"host": {"host_id": host_id, "kind": "ssh", "display_name": "Remote"}}

    def add_project(self, host_id, label, cwd):
        self.calls.append(("project", host_id, label, cwd))
        return {"project": {"project_id": "project-1", "host_id": host_id, "label": label, "cwd": cwd}}


class _FakeRegistry:
    def __init__(self):
        self.services: dict[Path, _FakeConversationService] = {}
        self.invalidated: list[Path] = []
        self.on_invalidate = None

    def service(self, root):
        resolved = Path(root).resolve()
        return self.services.setdefault(resolved, _FakeConversationService(resolved))

    def invalidate_root_session(self, root):
        if self.on_invalidate is not None:
            self.on_invalidate()
        self.invalidated.append(Path(root).resolve())

    def close(self):
        return None


class _ASGIStream:
    def __init__(self, app, path: str, *, cookie: str, headers: dict[str, str] | None = None):
        self.app = app
        self.path = path
        self.cookie = cookie
        self.request_headers = dict(headers or {})
        self.status_code = 500
        self.headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []
        self.complete = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.changed = asyncio.Condition()
        self._task = None

    @property
    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8")

    async def start(self) -> None:
        parsed = urlsplit(self.path)
        request_path = parsed.path or "/"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": request_path,
            "raw_path": request_path.encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", self.cookie.encode("latin-1")),
                *[
                    (key.lower().encode("latin-1"), value.encode("latin-1"))
                    for key, value in self.request_headers.items()
                ],
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "state": {},
        }
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                self.status_code = int(message["status"])
                self.headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                async with self.changed:
                    self.chunks.append(message.get("body", b""))
                    self.changed.notify_all()
                if not message.get("more_body", False):
                    self.complete.set()

        self._task = asyncio.create_task(self.app(scope, receive, send))

    async def wait_for_text(self, value: str, *, timeout: float = 2.0) -> None:
        async def wait():
            async with self.changed:
                await self.changed.wait_for(lambda: value in self.text)

        await asyncio.wait_for(wait(), timeout=timeout)

    async def wait_until_complete(self, *, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self.complete.wait(), timeout=timeout)
        await asyncio.wait_for(self._task, timeout=timeout)

    async def close(self) -> None:
        self.disconnect.set()
        await asyncio.wait_for(self._task, timeout=2.0)


class ConversationWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        (self.root / "MEMORY.md").write_text("# Memory {#memory}\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n\n## Build {#build}\n", encoding="utf-8")
        self.registry = _FakeRegistry()
        self.app = create_web_app(
            self.root,
            operator_token="test-operator",
            conversation_registry=self.registry,
        )
        self.client = ASGITestClient(self.app)
        login = self.client.post("/api/login", json={"token": "test-operator"})
        self.csrf = login.json()["data"]["csrf_token"]

    @property
    def service(self):
        return self.registry.service(self.root)

    def post(self, path, payload=None):
        return self.client.post(
            path,
            json={} if payload is None else payload,
            headers={"x-csrf-token": self.csrf},
        )

    def test_workspace_requires_login_and_is_root_scoped(self):
        anonymous = ASGITestClient(self.app)
        denied = anonymous.get("/api/conversation-workspace")
        response = self.client.get("/api/conversation-workspace")

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["projects"][0]["cwd"], str(self.root))

    def test_create_and_message_mutations_require_csrf(self):
        missing = self.client.post(
            "/api/pursuit-conversations",
            json={"pursuit_id": "build", "host_id": "local", "project_id": "local-root"},
        )
        created = self.post(
            "/api/pursuit-conversations",
            {"pursuit_id": "build", "host_id": "local", "project_id": "local-root"},
        )
        sent = self.post("/api/conversations/conversation-1/messages", {"text": "Continue."})
        reconciled = self.post("/api/conversations/conversation-1/reconcile")

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["data"]["conversation"]["pursuit_id"], "build")
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(reconciled.status_code, 200)
        self.assertTrue(reconciled.json()["data"]["resolved"])
        self.assertIn(("message", "conversation-1", "Continue."), self.service.calls)
        self.assertIn(("reconcile", "conversation-1"), self.service.calls)

    def test_host_project_and_approval_routes_preserve_registered_identifiers(self):
        host = self.post("/api/conversation-hosts", {"kind": "ssh", "display_name": "Lab", "ssh_alias": "lab"})
        project = self.post(
            "/api/conversation-projects",
            {"host_id": "host-1", "label": "Repo", "cwd": "/srv/repo"},
        )
        response = self.post(
            "/api/conversations/conversation-1/server-requests/request-1/respond",
            {"decision": "accept"},
        )

        self.assertEqual(host.json()["data"]["host"]["host_id"], "host-1")
        self.assertEqual(project.json()["data"]["project"]["cwd"], "/srv/repo")
        self.assertEqual(response.status_code, 200)
        self.assertIn(("respond", "request-1", "accept", None, "conversation-1"), self.service.calls)

    def test_domain_errors_keep_stable_code_and_status(self):
        response = self.client.get("/api/conversations/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "conversation_not_found")

    def test_event_stream_sends_snapshot_then_cursor_event(self):
        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=7",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("id: 8\nevent: conversation")
            await stream.close()
            return stream

        response = asyncio.run(scenario())

        self.assertEqual(response.status_code, 200)
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in response.headers}
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertIn("id: 7\nevent: snapshot", response.text)
        self.assertIn("id: 8\nevent: conversation", response.text)
        self.assertIn((7, 500), self.service.store.calls)

    def test_query_cursor_replays_events_created_between_rest_and_sse(self):
        self.service.store.events = [
            {
                "event_id": 7,
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "kind": "agent_message_delta",
                "payload": {"delta": "REST-to-SSE gap"},
                "created_at": "2026-08-29T00:00:00+00:00",
            }
        ]

        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=6",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("id: 7\nevent: conversation")
            await stream.close()
            return stream

        stream = asyncio.run(scenario())

        self.assertIn("id: 6\nevent: snapshot", stream.text)
        self.assertIn("REST-to-SSE gap", stream.text)
        self.assertIn((6, 500), self.service.store.calls)

    def test_last_event_id_overrides_the_original_query_on_reconnect(self):
        self.service.store.events = [
            {
                "event_id": 6,
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "kind": "agent_message_delta",
                "payload": {"delta": "old query event"},
                "created_at": "2026-08-29T00:00:00+00:00",
            },
            {
                "event_id": 8,
                "conversation_id": "conversation-1",
                "turn_id": "turn-1",
                "kind": "agent_message_delta",
                "payload": {"delta": "native reconnect event"},
                "created_at": "2026-08-29T00:00:01+00:00",
            },
        ]

        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=5",
                cookie=self.client.cookies.header_value(),
                headers={"last-event-id": "7"},
            )
            await stream.start()
            await stream.wait_for_text("id: 8\nevent: conversation")
            await stream.close()
            return stream

        stream = asyncio.run(scenario())

        self.assertIn("id: 7\nevent: snapshot", stream.text)
        self.assertIn("native reconnect event", stream.text)
        self.assertNotIn("old query event", stream.text)
        self.assertIn((7, 500), self.service.store.calls)
        self.assertNotIn((5, 500), self.service.store.calls)

    def test_snapshot_id_resumes_an_event_written_after_snapshot_delivery(self):
        self.service.store.events.clear()

        async def scenario():
            first = _ASGIStream(
                self.app,
                "/api/conversation-events",
                cookie=self.client.cookies.header_value(),
            )
            await first.start()
            await first.wait_for_text("id: 7\nevent: snapshot")
            await first.close()

            self.service.store.events.append(
                {
                    "event_id": 8,
                    "conversation_id": "conversation-1",
                    "turn_id": "turn-1",
                    "kind": "agent_message_delta",
                    "payload": {"delta": "arrived after snapshot"},
                    "created_at": "2026-08-29T00:00:01+00:00",
                }
            )
            resumed = _ASGIStream(
                self.app,
                "/api/conversation-events",
                cookie=self.client.cookies.header_value(),
                headers={"last-event-id": "7"},
            )
            await resumed.start()
            await resumed.wait_for_text("id: 8\nevent: conversation")
            await resumed.close()
            return first, resumed

        first, resumed = asyncio.run(scenario())

        self.assertIn("id: 7\nevent: snapshot", first.text)
        self.assertIn("arrived after snapshot", resumed.text)

    def test_root_switch_during_workspace_snapshot_ends_the_old_stream(self):
        other = self.root / "other"
        other.mkdir()
        (other / "MEMORY.md").write_text("# Other {#other}\n", encoding="utf-8")
        started = threading.Event()
        release = threading.Event()
        self.service.workspace_started = started
        self.service.workspace_release = release
        cookie_header = self.client.cookies.header_value()

        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events",
                cookie=cookie_header,
            )
            try:
                await stream.start()
                entered = await asyncio.wait_for(asyncio.to_thread(started.wait, 1.0), timeout=1.5)
                self.assertTrue(entered)
                switched = await self.client._request(
                    "POST",
                    "/api/active-root",
                    json_body={"root": str(other)},
                    content=None,
                    headers={
                        "cookie": cookie_header,
                        "x-csrf-token": self.csrf,
                    },
                )
                release.set()
                await stream.wait_until_complete()
                return switched, stream
            finally:
                release.set()
                if stream._task is not None and not stream._task.done():
                    await stream.close()

        switched, stream = asyncio.run(scenario())

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(stream.text, "")
        self.assertIn(self.root, self.registry.invalidated)

    def test_revoked_session_ends_existing_event_stream(self):
        cookie = self.client.cookies.get("rightmemory_session")
        session_id = _session_id(cookie)

        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("event: snapshot")
            revoke_session(self.root, session_id)
            await stream.wait_until_complete()
            return stream

        response = asyncio.run(scenario())

        self.assertTrue(response.complete.is_set())
        self.assertNotIn("heartbeat", response.text)

    def test_idle_event_stream_does_not_hold_the_threadpool_token(self):
        self.service.store.events.clear()
        cookie_header = self.client.cookies.header_value()
        cookie = self.client.cookies.get("rightmemory_session")
        session_id = _session_id(cookie)

        async def scenario():
            limiter = anyio.to_thread.current_default_thread_limiter()
            previous_tokens = limiter.total_tokens
            limiter.total_tokens = 1
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8",
                cookie=cookie_header,
            )
            try:
                await stream.start()
                await stream.wait_for_text("event: snapshot")
                await asyncio.sleep(0.05)
                response = await asyncio.wait_for(
                    self.client._request(
                        "GET",
                        "/api/conversation-workspace",
                        json_body=None,
                        content=None,
                        headers={"cookie": cookie_header},
                    ),
                    timeout=0.75,
                )
                revoke_session(self.root, session_id)
                await stream.wait_until_complete()
                return response
            finally:
                limiter.total_tokens = previous_tokens
                if stream._task is not None and not stream._task.done():
                    await stream.close()

        response = asyncio.run(scenario())

        self.assertEqual(response.status_code, 200)

    def test_logout_revokes_before_stream_invalidation(self):
        cookie = self.client.cookies.get("rightmemory_session")
        observed: list[bool] = []
        self.registry.on_invalidate = lambda: observed.append(
            read_session_cookie(self.root, cookie) is None
        )

        response = self.post("/api/logout")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed, [True])

    def test_logout_revokes_and_clears_when_the_active_root_was_deleted(self):
        other = self.root / "other"
        other.mkdir()
        memory_file = other / "MEMORY.md"
        memory_file.write_text("# Other {#other}\n", encoding="utf-8")
        switched = self.post("/api/active-root", {"root": str(other)})
        self.assertEqual(switched.status_code, 200)
        self.csrf = switched.json()["data"]["csrf_token"]
        signed_cookie = self.client.cookies.get("rightmemory_session")
        deleted_root = other.resolve()
        memory_file.unlink()
        other.rmdir()

        response = self.post("/api/logout")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(read_session_cookie(self.root, signed_cookie))
        self.assertFalse(self.client.get("/api/session").json()["authenticated"])
        self.assertIn(deleted_root, self.registry.invalidated)

    def test_switching_active_root_invalidates_old_root_streams(self):
        other = self.root / "other"
        other.mkdir()
        (other / "MEMORY.md").write_text("# Other {#other}\n", encoding="utf-8")

        response = self.post("/api/active-root", {"root": str(other)})

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.root, self.registry.invalidated)
        workspace = self.client.get("/api/conversation-workspace")
        self.assertEqual(workspace.json()["data"]["projects"][0]["cwd"], str(other))


def _session_id(cookie: str | None) -> str:
    if cookie is None:
        raise AssertionError("missing session cookie")
    body = cookie.rsplit(".", 1)[0]
    padded = body + ("=" * (-len(body) % 4))
    payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    return payload["sid"]


if __name__ == "__main__":
    unittest.main()
