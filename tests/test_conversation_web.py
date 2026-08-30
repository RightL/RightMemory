from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
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
        self.session_ids: list[str] = []

    def read_events(self, *, after_event_id=0, limit=200):
        self.calls.append((after_event_id, limit))
        return [event for event in self.events if event["event_id"] > after_event_id][:limit]

    def read_events_for_session(self, owner_session_id, *, after_event_id=0, limit=200):
        self.session_ids.append(owner_session_id)
        return self.read_events(after_event_id=after_event_id, limit=limit)


class _FakeConversationService:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[object, ...]] = []
        self.store = _FakeEventStore()
        self.workspace_started: threading.Event | None = None
        self.workspace_release: threading.Event | None = None
        self.side_chat_cleanup_sessions: list[str] = []
        self.side_chat_cleanup_event = threading.Event()

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

    def model_catalog(self, host_id):
        self.calls.append(("models", host_id))
        return {
            "host_id": host_id,
            "models": [
                {
                    "id": "gpt-example",
                    "display_name": "Example",
                    "default_reasoning_effort": "medium",
                    "supported_reasoning_efforts": [
                        {
                            "reasoning_effort": "medium",
                            "description": "Balanced",
                        }
                    ],
                    "is_default": True,
                }
            ],
            "default_model": "gpt-example",
            "default_reasoning_effort": "medium",
        }

    def detail(self, conversation_id, after_event_id=0, owner_session_id=None):
        self.calls.append(("detail", conversation_id, after_event_id, owner_session_id))
        if conversation_id == "missing":
            raise ConversationError("conversation_not_found", "Conversation not found.", 404)
        return {"conversation": {"conversation_id": conversation_id}, "events": [], "pending_requests": []}

    def earlier_history(self, conversation_id, before_event_id, owner_session_id=None):
        self.calls.append(
            ("earlier_history", conversation_id, before_event_id, owner_session_id)
        )
        return {
            "conversation_id": conversation_id,
            "events": [
                {
                    "event_id": before_event_id - 1,
                    "conversation_id": conversation_id,
                    "kind": "test.event",
                    "payload": {},
                }
            ],
            "has_earlier_events": False,
        }

    def create_conversation(
        self,
        pursuit_id,
        host_id,
        project_id,
        model=None,
        reasoning_effort=None,
    ):
        self.calls.append(
            ("create", pursuit_id, host_id, project_id, model, reasoning_effort)
        )
        return {
            "conversation": {
                "conversation_id": "conversation-1",
                "pursuit_id": pursuit_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        }

    def create_side_chat(self, parent_conversation_id, owner_session_id):
        self.calls.append(("create_side_chat", parent_conversation_id, owner_session_id))
        return {
            "conversation": {
                "conversation_id": "side-chat-1",
                "kind": "side_chat",
                "parent_conversation_id": parent_conversation_id,
            }
        }

    def close_side_chat(self, conversation_id, owner_session_id=None):
        self.calls.append(("close_side_chat", conversation_id, owner_session_id))
        return {"conversation_id": conversation_id}

    def close_side_chats_for_session(self, owner_session_id):
        self.side_chat_cleanup_sessions.append(owner_session_id)
        self.side_chat_cleanup_event.set()
        return {"conversation_ids": []}

    def acknowledge_read(self, conversation_id, owner_session_id=None, event_id=None):
        self.calls.append(("read", conversation_id, owner_session_id, event_id))
        return {
            "conversation": {
                "conversation_id": conversation_id,
                "last_final_event_id": 8,
                "last_read_event_id": 8,
            }
        }

    def update_settings(
        self, conversation_id, model, reasoning_effort, owner_session_id=None
    ):
        self.calls.append(
            ("settings", conversation_id, model, reasoning_effort, owner_session_id)
        )
        return {
            "conversation": {
                "conversation_id": conversation_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        }

    def send_message(
        self, conversation_id, text, attachment_ids=None, owner_session_id=None
    ):
        self.calls.append(
            ("message", conversation_id, text, attachment_ids, owner_session_id)
        )
        return {"conversation_id": conversation_id}

    def upload_attachment(
        self,
        conversation_id,
        content,
        media_type,
        display_name=None,
        owner_session_id=None,
        attachment_id=None,
    ):
        self.calls.append(
            (
                "upload",
                conversation_id,
                content,
                media_type,
                display_name,
                owner_session_id,
                attachment_id,
            )
        )
        return {
            "attachment": {
                "attachment_id": "attachment-1",
                "kind": "pasted_text",
                "display_name": "notes.txt",
                "media_type": "text/plain",
                "byte_size": len(content),
                "state": "staged",
            }
        }

    def delete_staged_attachment(
        self, conversation_id, attachment_id, owner_session_id=None
    ):
        self.calls.append(
            ("delete_attachment", conversation_id, attachment_id, owner_session_id)
        )
        return {"attachment_id": attachment_id}

    def interrupt(self, conversation_id, owner_session_id=None):
        self.calls.append(("interrupt", conversation_id, owner_session_id))
        return {"conversation_id": conversation_id}

    def reconcile(self, conversation_id, owner_session_id=None):
        self.calls.append(("reconcile", conversation_id, owner_session_id))
        return {
            "conversation": {"conversation_id": conversation_id, "status": "idle"},
            "thread": {"id": "thread-1", "status": {"type": "idle"}},
            "resolved": True,
        }

    def archive(self, conversation_id, owner_session_id=None):
        self.calls.append(("archive", conversation_id, owner_session_id))
        return {"conversation_id": conversation_id}

    def move(self, conversation_id, pursuit_id, owner_session_id=None):
        self.calls.append(("move", conversation_id, pursuit_id, owner_session_id))
        return {"conversation_id": conversation_id}

    def respond_request(
        self,
        request_key,
        decision,
        response,
        expected_conversation_id,
        owner_session_id=None,
    ):
        self.calls.append(
            (
                "respond",
                request_key,
                decision,
                response,
                expected_conversation_id,
                owner_session_id,
            )
        )
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
        self.session_id = _session_id(self.client.cookies.get("rightmemory_session"))

    @property
    def service(self):
        return self.registry.service(self.root)

    def post(self, path, payload=None):
        return self.client.post(
            path,
            json={} if payload is None else payload,
            headers={"x-csrf-token": self.csrf},
        )

    async def release_view(self, view_id: str, page_id: str):
        return await self.client._request(
            "POST",
            "/api/conversation-session/release",
            json_body={"view_id": view_id, "page_id": page_id},
            content=None,
            headers={
                "cookie": self.client.cookies.header_value(),
                "x-csrf-token": self.csrf,
            },
        )

    def test_workspace_requires_login_and_is_root_scoped(self):
        anonymous = ASGITestClient(self.app)
        denied = anonymous.get("/api/conversation-workspace")
        response = self.client.get("/api/conversation-workspace")

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["projects"][0]["cwd"], str(self.root))

    def test_earlier_history_route_passes_the_authenticated_session_scope(self):
        response = self.client.get(
            "/api/conversations/conversation-1/history?before_event_id=501"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["events"][0]["event_id"], 500)
        self.assertFalse(response.json()["data"]["has_earlier_events"])
        self.assertIn(
            ("earlier_history", "conversation-1", 501, self.session_id),
            self.service.calls,
        )

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
        self.assertIn(
            ("message", "conversation-1", "Continue.", None, self.session_id),
            self.service.calls,
        )
        self.assertIn(
            ("reconcile", "conversation-1", self.session_id), self.service.calls
        )

    def test_side_chat_create_close_and_read_routes_require_csrf(self):
        denied_create = self.client.post(
            "/api/conversations/conversation-1/side-chats", json={}
        )
        created = self.post("/api/conversations/conversation-1/side-chats")
        denied_close = self.client.request(
            "DELETE", "/api/side-chats/side-chat-1"
        )
        closed = self.client.request(
            "DELETE",
            "/api/side-chats/side-chat-1",
            headers={"x-csrf-token": self.csrf},
        )
        marked_read = self.post(
            "/api/conversations/conversation-1/read", {"event_id": 8}
        )

        self.assertEqual(denied_create.status_code, 403)
        self.assertEqual(denied_close.status_code, 403)
        self.assertEqual(
            created.json()["data"],
            {
                "conversation": {
                    "conversation_id": "side-chat-1",
                    "kind": "side_chat",
                    "parent_conversation_id": "conversation-1",
                }
            },
        )
        self.assertEqual(
            closed.json()["data"], {"conversation_id": "side-chat-1"}
        )
        self.assertEqual(
            marked_read.json()["data"]["conversation"]["last_read_event_id"], 8
        )
        self.assertIn(
            ("create_side_chat", "conversation-1", self.session_id),
            self.service.calls,
        )
        self.assertIn(
            ("close_side_chat", "side-chat-1", self.session_id),
            self.service.calls,
        )
        self.assertIn(
            ("read", "conversation-1", self.session_id, 8), self.service.calls
        )

    def test_raw_attachment_upload_and_delete_require_csrf(self):
        denied = self.client.post(
            "/api/conversations/conversation-1/attachments",
            content=b"pasted context",
            headers={"content-type": "text/plain"},
        )
        uploaded = self.client.post(
            "/api/conversations/conversation-1/attachments",
            content=b"pasted context",
            headers={
                "content-type": "text/plain; charset=utf-8",
                "x-filename": "pasted%20notes.txt",
                "x-attachment-id": "0123456789abcdef0123456789abcdef",
                "x-csrf-token": self.csrf,
            },
        )
        deleted = self.client.request(
            "DELETE",
            "/api/conversations/conversation-1/attachments/attachment-1",
            headers={"x-csrf-token": self.csrf},
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(uploaded.status_code, 200)
        self.assertEqual(uploaded.json()["data"]["attachment"]["state"], "staged")
        self.assertEqual(deleted.status_code, 200)
        self.assertIn(
            (
                "upload",
                "conversation-1",
                b"pasted context",
                "text/plain; charset=utf-8",
                "pasted%20notes.txt",
                self.session_id,
                "0123456789abcdef0123456789abcdef",
            ),
            self.service.calls,
        )
        self.assertIn(
            (
                "delete_attachment",
                "conversation-1",
                "attachment-1",
                self.session_id,
            ),
            self.service.calls,
        )

    def test_model_catalog_creation_and_settings_routes_preserve_exact_shapes(self):
        catalog = self.client.get("/api/conversation-models?host_id=local")
        created = self.post(
            "/api/pursuit-conversations",
            {
                "pursuit_id": "build",
                "host_id": "local",
                "project_id": "local-root",
                "model": "gpt-example",
                "reasoning_effort": "medium",
            },
        )
        settings = self.post(
            "/api/conversations/conversation-1/settings",
            {"model": "gpt-example", "reasoning_effort": "medium"},
        )

        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(
            set(catalog.json()["data"]),
            {"host_id", "models", "default_model", "default_reasoning_effort"},
        )
        self.assertEqual(catalog.json()["data"]["models"][0]["id"], "gpt-example")
        self.assertEqual(created.json()["data"]["conversation"]["model"], "gpt-example")
        self.assertEqual(
            settings.json()["data"]["conversation"]["reasoning_effort"],
            "medium",
        )
        self.assertIn(("models", "local"), self.service.calls)
        self.assertIn(
            (
                "create",
                "build",
                "local",
                "local-root",
                "gpt-example",
                "medium",
            ),
            self.service.calls,
        )
        self.assertIn(
            (
                "settings",
                "conversation-1",
                "gpt-example",
                "medium",
                self.session_id,
            ),
            self.service.calls,
        )

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
        self.assertIn(
            (
                "respond",
                "request-1",
                "accept",
                None,
                "conversation-1",
                self.session_id,
            ),
            self.service.calls,
        )

    def test_domain_errors_keep_stable_code_and_status(self):
        response = self.client.get("/api/conversations/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "conversation_not_found")

    def test_event_stream_sends_snapshot_then_cursor_event(self):
        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=7&view_id=view-one&page_id=page-one",
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
        self.assertIn(self.session_id, self.service.store.session_ids)

    def test_event_stream_disconnect_never_cleans_side_chats(self):
        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("event: snapshot")
            await stream.close()
            await asyncio.sleep(0.12)
            return self.service.side_chat_cleanup_event.is_set()

        self.assertFalse(asyncio.run(scenario()))
        self.assertEqual(self.service.side_chat_cleanup_sessions, [])

    def test_explicit_view_release_cleans_after_disconnected_stream(self):
        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("event: snapshot")
            await stream.close()
            release = await self.release_view("view-one", "page-one")
            cleaned = await asyncio.to_thread(
                self.service.side_chat_cleanup_event.wait, 0.75
            )
            return release, cleaned

        with patch(
            "rightmemory.web.conversation_routes._SIDE_CHAT_RELEASE_GRACE_SECONDS",
            0.05,
        ):
            release, cleaned = asyncio.run(scenario())

        self.assertEqual(release.status_code, 200)
        self.assertTrue(release.json()["data"]["released"])
        self.assertTrue(cleaned)
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

    def test_event_stream_reconnect_cancels_pending_side_chat_cleanup(self):
        async def scenario():
            first = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-old",
                cookie=self.client.cookies.header_value(),
            )
            await first.start()
            await first.wait_for_text("event: snapshot")
            released_old = await self.release_view("view-one", "page-old")
            await first.close()

            reconnected = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-new",
                cookie=self.client.cookies.header_value(),
            )
            await reconnected.start()
            await reconnected.wait_for_text("event: snapshot")
            stale_release = await self.release_view("view-one", "page-old")
            await asyncio.sleep(0.18)
            cleaned_while_connected = self.service.side_chat_cleanup_event.is_set()
            await reconnected.close()
            await asyncio.sleep(0.18)
            cleaned_after_disconnect = self.service.side_chat_cleanup_event.is_set()
            released_new = await self.release_view("view-one", "page-new")
            cleaned_after_release = await asyncio.to_thread(
                self.service.side_chat_cleanup_event.wait, 0.75
            )
            return (
                released_old,
                stale_release,
                released_new,
                cleaned_while_connected,
                cleaned_after_disconnect,
                cleaned_after_release,
            )

        with patch(
            "rightmemory.web.conversation_routes._SIDE_CHAT_RELEASE_GRACE_SECONDS",
            0.12,
        ):
            result = asyncio.run(scenario())

        released_old, stale_release, released_new, connected, disconnected, released = result
        self.assertTrue(released_old.json()["data"]["released"])
        self.assertTrue(stale_release.json()["data"]["released"])
        self.assertTrue(released_new.json()["data"]["released"])
        self.assertFalse(connected)
        self.assertFalse(disconnected)
        self.assertTrue(released)
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

    def test_reconnect_after_release_does_not_revive_page_instance(self):
        async def scenario():
            first = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            await first.start()
            await first.wait_for_text("event: snapshot")
            released = await self.release_view("view-one", "page-one")
            await first.close()

            reconnected = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            await reconnected.start()
            await reconnected.wait_for_text("event: snapshot")
            await asyncio.sleep(0.18)
            cleaned_while_connected = self.service.side_chat_cleanup_event.is_set()
            await reconnected.close()
            cleaned_after_disconnect = await asyncio.to_thread(
                self.service.side_chat_cleanup_event.wait, 0.75
            )
            return released, cleaned_while_connected, cleaned_after_disconnect

        with patch(
            "rightmemory.web.conversation_routes._SIDE_CHAT_RELEASE_GRACE_SECONDS",
            0.12,
        ):
            released, while_connected, after_disconnect = asyncio.run(scenario())

        self.assertTrue(released.json()["data"]["released"])
        self.assertFalse(while_connected)
        self.assertTrue(after_disconnect)
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

    def test_release_before_first_stream_is_remembered(self):
        async def scenario():
            released = await self.release_view("view-one", "page-one")
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            await stream.start()
            await stream.wait_for_text("event: snapshot")
            await asyncio.sleep(0.18)
            cleaned_while_connected = self.service.side_chat_cleanup_event.is_set()
            await stream.close()
            cleaned_after_disconnect = await asyncio.to_thread(
                self.service.side_chat_cleanup_event.wait, 0.75
            )
            return released, cleaned_while_connected, cleaned_after_disconnect

        with patch(
            "rightmemory.web.conversation_routes._SIDE_CHAT_RELEASE_GRACE_SECONDS",
            0.12,
        ):
            released, while_connected, after_disconnect = asyncio.run(scenario())

        self.assertTrue(released.json()["data"]["released"])
        self.assertFalse(while_connected)
        self.assertTrue(after_disconnect)
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

    def test_duplicated_tab_pages_must_all_release_before_session_cleanup(self):
        async def scenario():
            first = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
                cookie=self.client.cookies.header_value(),
            )
            second = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-two",
                cookie=self.client.cookies.header_value(),
            )
            await first.start()
            await first.wait_for_text("event: snapshot")
            await second.start()
            await second.wait_for_text("event: snapshot")
            await self.release_view("view-one", "page-one")
            await first.close()
            await asyncio.sleep(0.12)
            cleaned_with_second_open = self.service.side_chat_cleanup_event.is_set()
            await second.close()
            await asyncio.sleep(0.12)
            cleaned_after_both_closed = self.service.side_chat_cleanup_event.is_set()
            await self.release_view("view-one", "page-two")
            cleaned_after_both_released = await asyncio.to_thread(
                self.service.side_chat_cleanup_event.wait, 0.75
            )
            return (
                cleaned_with_second_open,
                cleaned_after_both_closed,
                cleaned_after_both_released,
            )

        with patch(
            "rightmemory.web.conversation_routes._SIDE_CHAT_RELEASE_GRACE_SECONDS",
            0.05,
        ):
            open_second, both_closed, both_released = asyncio.run(scenario())

        self.assertFalse(open_second)
        self.assertFalse(both_closed)
        self.assertTrue(both_released)
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

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
                "/api/conversation-events?after_event_id=6&view_id=view-one&page_id=page-one",
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
                "/api/conversation-events?after_event_id=5&view_id=view-one&page_id=page-one",
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
                "/api/conversation-events?view_id=view-one&page_id=page-one",
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
                "/api/conversation-events?view_id=view-one&page_id=page-one",
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
                "/api/conversation-events?view_id=view-one&page_id=page-one",
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
        self.assertEqual(self.service.side_chat_cleanup_sessions, [self.session_id])

    def test_revoked_session_ends_existing_event_stream(self):
        cookie = self.client.cookies.get("rightmemory_session")
        session_id = _session_id(cookie)

        async def scenario():
            stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
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

    def test_logout_invalidates_only_its_own_session_streams(self):
        other_client = ASGITestClient(self.app)
        other_login = other_client.post(
            "/api/login", json={"token": "test-operator"}
        )
        other_session_id = _session_id(
            other_client.cookies.get("rightmemory_session")
        )
        self.assertNotEqual(other_session_id, self.session_id)

        async def scenario():
            other_stream = _ASGIStream(
                self.app,
                "/api/conversation-events?after_event_id=8&view_id=view-two&page_id=page-two",
                cookie=other_client.cookies.header_value(),
            )
            try:
                await other_stream.start()
                await other_stream.wait_for_text("event: snapshot")
                logout = await self.client._request(
                    "POST",
                    "/api/logout",
                    json_body={},
                    content=None,
                    headers={
                        "cookie": self.client.cookies.header_value(),
                        "x-csrf-token": self.csrf,
                    },
                )
                await asyncio.sleep(0.35)
                return (
                    logout,
                    not other_stream.complete.is_set(),
                    list(self.service.side_chat_cleanup_sessions),
                )
            finally:
                if other_stream._task is not None and not other_stream._task.done():
                    await other_stream.close()

        logout, other_stayed_open, cleanup_sessions = asyncio.run(scenario())

        self.assertEqual(logout.status_code, 200)
        self.assertTrue(other_stayed_open)
        self.assertEqual(cleanup_sessions, [self.session_id])

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
                "/api/conversation-events?after_event_id=8&view_id=view-one&page_id=page-one",
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
