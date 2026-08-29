from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rightmemory.conversations.jsonrpc import JsonRpcRemoteError
from rightmemory.conversations.models import ConversationError
from rightmemory.conversations.service import ConversationRuntimeRegistry, ConversationService


class _FakePursuitStore:
    def __init__(self, items: list[dict[str, Any]], root_key: str):
        self.items = items
        self.root_key = root_key

    def snapshot(self) -> dict[str, Any]:
        return {"items": [dict(item) for item in self.items], "root_key": self.root_key}


def _catalog_model(
    model_id: str,
    *,
    display_name: str,
    efforts: tuple[str, ...],
    default_effort: str,
    is_default: bool = False,
    hidden: bool = False,
    provider_model: str | None = None,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "model": provider_model or model_id,
        "displayName": display_name,
        "hidden": hidden,
        "supportedReasoningEfforts": [
            {
                "reasoningEffort": effort,
                "description": f"{effort} description",
            }
            for effort in efforts
        ],
        "defaultReasoningEffort": default_effort,
        "isDefault": is_default,
    }


class _FakeAdapter:
    def __init__(
        self,
        host: dict[str, Any],
        *,
        on_notification,
        on_server_request,
        on_disconnect,
        local_cwd: Path,
    ):
        self.host = host
        self.local_cwd = Path(local_cwd)
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.on_disconnect = on_disconnect
        self.epoch = f"epoch-{id(self)}"
        self.calls: list[tuple[Any, ...]] = []
        self.responses: list[tuple[Any, dict[str, Any], str | None]] = []
        self.thread_count = 0
        self.turn_count = 0
        self.unmaterialized_threads: set[str] = set()
        self.closed = False
        self.model_pages: dict[str | None, dict[str, Any]] = {
            None: {
                "data": [
                    _catalog_model(
                        "gpt-default",
                        display_name="Default model",
                        efforts=("low", "medium"),
                        default_effort="low",
                        is_default=True,
                    ),
                    _catalog_model(
                        "gpt-deep",
                        display_name="Deep model",
                        efforts=("medium", "high"),
                        default_effort="medium",
                    ),
                ],
                "nextCursor": None,
            }
        }
        self.config: dict[str, Any] = {
            "model": None,
            "model_reasoning_effort": None,
        }
        self.config_error: BaseException | None = None

    def connect(self) -> dict[str, Any]:
        self.calls.append(("connect",))
        return {
            "userAgent": "fake-codex/1",
            "codexHome": "/fake/codex",
            "platformFamily": "test",
            "platformOs": "test-os",
        }

    def close(self) -> None:
        self.closed = True

    def start_thread(self, cwd: str, **optional: Any) -> dict[str, Any]:
        self.thread_count += 1
        thread = {
            "id": f"thread-{self.thread_count}",
            "name": f"Thread {self.thread_count}",
            "status": {"type": "idle"},
        }
        self.calls.append(("start_thread", cwd, optional))
        self.unmaterialized_threads.add(thread["id"])
        return {"thread": thread}

    def list_models(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        include_hidden: bool | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("list_models", cursor, limit, include_hidden))
        return self.model_pages[cursor]

    def read_config(
        self,
        *,
        cwd: str | None = None,
        include_layers: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("read_config", cwd, include_layers))
        if self.config_error is not None:
            raise self.config_error
        return {"config": dict(self.config), "origins": {}}

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("resume_thread", thread_id))
        if thread_id in self.unmaterialized_threads:
            raise JsonRpcRemoteError(
                -32600, f"no rollout found for thread id {thread_id}"
            )
        return {"thread": {"id": thread_id, "status": {"type": "idle"}}}

    def archive_thread(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(("archive_thread", thread_id))
        return {}

    def start_turn(self, thread_id: str, text: str, **optional: Any) -> dict[str, Any]:
        self.turn_count += 1
        turn = {"id": f"turn-{self.turn_count}", "status": "inProgress"}
        self.calls.append(("start_turn", thread_id, text, optional))
        self.unmaterialized_threads.discard(thread_id)
        self.emit_notification(
            "turn/started", {"threadId": thread_id, "turn": turn}
        )
        return {"turn": turn}

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.calls.append(("interrupt_turn", thread_id, turn_id))
        return {}

    def respond_server_request(
        self,
        request_id: str | int,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        epoch: str | None = None,
    ) -> None:
        if error is not None:
            raise AssertionError("tests only expect successful provider responses")
        self.responses.append((request_id, result or {}, epoch))

    def emit_notification(self, method: str, params: dict[str, Any]) -> None:
        self.on_notification(SimpleNamespace(epoch=self.epoch, method=method, params=params))

    def emit_request(self, request_id: int, method: str, params: dict[str, Any]) -> None:
        self.on_server_request(
            SimpleNamespace(
                epoch=self.epoch,
                request_id=request_id,
                method=method,
                params=params,
            )
        )

    def disconnect(self, error: BaseException | None = None) -> None:
        self.on_disconnect(SimpleNamespace(epoch=self.epoch, error=error))


class _FakeAdapterFactory:
    def __init__(self):
        self.instances: list[_FakeAdapter] = []

    def __call__(self, host: dict[str, Any], **kwargs: Any) -> _FakeAdapter:
        adapter = _FakeAdapter(host, **kwargs)
        self.instances.append(adapter)
        return adapter


class ConversationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.items = [
            {"id": "alpha", "title": "**Alpha**", "body": "Alpha body"},
            {"id": "beta", "title": "Beta", "body": "Beta body"},
        ]
        self.pursuit_stores: dict[str, _FakePursuitStore] = {}
        self.adapters = _FakeAdapterFactory()

        def pursuit_factory(root: Path) -> _FakePursuitStore:
            key = str(root.resolve())
            return self.pursuit_stores.setdefault(
                key, _FakePursuitStore([dict(item) for item in self.items], f"root:{key}")
            )

        self.registry = ConversationRuntimeRegistry(
            self.adapters,
            pursuit_store_factory=pursuit_factory,
        )
        self.service = self.registry.service(self.root)

    def tearDown(self):
        self.registry.close()
        self.temporary.cleanup()

    def _create(self, pursuit_id: str = "alpha") -> dict[str, Any]:
        return self.service.create_conversation(pursuit_id)["conversation"]

    @property
    def adapter(self) -> _FakeAdapter:
        return self.adapters.instances[-1]

    def test_pursuit_is_validated_and_plain_title_is_captured(self):
        with self.assertRaises(ConversationError) as caught:
            self.service.create_conversation("missing")
        self.assertEqual(caught.exception.code, "pursuit_not_found")
        self.assertEqual(self.adapters.instances, [])

        conversation = self._create()
        self.assertEqual(conversation["pursuit_title_snapshot"], "Alpha")
        self.assertEqual(conversation["pursuit_id"], "alpha")
        self.assertEqual(conversation["host_id"], "local")
        self.assertEqual(conversation["project_id"], "local-root")
        self.assertEqual(self.adapter.calls[1][0], "start_thread")
        self.assertEqual(Path(self.adapter.calls[1][1]), self.root)
        self.assertEqual(
            self.service.workspace()["root_key"],
            self.pursuit_stores[str(self.root)].root_key,
        )

    def test_first_message_uses_resident_fresh_thread_and_keeps_event_order(self):
        conversation = self._create()
        result = self.service.send_message(conversation["conversation_id"], "Hello Codex")
        self.assertEqual(result["turn"]["id"], "turn-1")
        self.assertEqual(result["conversation"]["active_turn_id"], "turn-1")
        self.assertEqual(result["conversation"]["status"], "running")
        calls = [call[0] for call in self.adapter.calls]
        self.assertNotIn("resume_thread", calls)
        self.assertIn("start_turn", calls)
        events = self.service.detail(conversation["conversation_id"])["events"]
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds[:3], ["thread.started", "user.message", "turn.started"])
        self.assertEqual(events[1]["payload"]["text"], "Hello Codex")

    def test_model_catalog_normalizes_pages_and_uses_effective_config_defaults(self):
        self.service.probe_host("local")
        hidden = _catalog_model(
            "hidden-picker-id",
            display_name="Hidden",
            efforts=("low",),
            default_effort="low",
            hidden=True,
            provider_model="hidden-wire-model",
        )
        default = _catalog_model(
            "default-picker-id",
            display_name="Default",
            efforts=("low", "medium"),
            default_effort="low",
            is_default=True,
            provider_model="default-wire-model",
        )
        configured = _catalog_model(
            "configured-picker-id",
            display_name="Configured",
            efforts=("medium", "high"),
            default_effort="medium",
            provider_model="configured-wire-model",
        )
        self.adapter.model_pages = {
            None: {
                "data": [hidden, {"id": "picker-only"}, default],
                "nextCursor": "next",
            },
            "next": {"data": [configured], "nextCursor": None},
        }
        self.adapter.config = {
            "model": "configured-picker-id",
            "model_reasoning_effort": "high",
        }

        catalog = self.service.model_catalog("local")

        self.assertEqual(catalog["host_id"], "local")
        self.assertEqual(
            [model["id"] for model in catalog["models"]],
            ["default-wire-model", "configured-wire-model"],
        )
        self.assertEqual(catalog["default_model"], "configured-wire-model")
        self.assertEqual(catalog["default_reasoning_effort"], "high")
        self.assertEqual(
            catalog["models"][1]["supported_reasoning_efforts"],
            [
                {"reasoning_effort": "medium", "description": "medium description"},
                {"reasoning_effort": "high", "description": "high description"},
            ],
        )
        self.assertIn(("list_models", None, None, False), self.adapter.calls)
        self.assertIn(("list_models", "next", None, False), self.adapter.calls)

    def test_model_catalog_falls_back_when_config_read_is_unsupported(self):
        self.service.probe_host("local")
        self.adapter.config_error = JsonRpcRemoteError(-32601, "method not found")

        catalog = self.service.model_catalog("local")

        self.assertEqual(catalog["default_model"], "gpt-default")
        self.assertEqual(catalog["default_reasoning_effort"], "low")

    def test_model_settings_persist_and_override_the_immediately_following_turn(self):
        created = self.service.create_conversation(
            "alpha",
            "local",
            "local-root",
            "gpt-deep",
            "high",
        )["conversation"]
        self.assertEqual(created["model"], "gpt-deep")
        self.assertEqual(created["reasoning_effort"], "high")
        start_thread = next(
            call for call in self.adapter.calls if call[0] == "start_thread"
        )
        self.assertEqual(start_thread[2], {"model": "gpt-deep"})

        first = self.service.send_message(created["conversation_id"], "First")
        first_start = [
            call for call in self.adapter.calls if call[0] == "start_turn"
        ][-1]
        self.assertEqual(
            first_start[3],
            {"model": "gpt-deep", "reasoning_effort": "high"},
        )

        updated = self.service.update_settings(
            created["conversation_id"], "gpt-default", "medium"
        )["conversation"]
        self.assertEqual(updated["model"], "gpt-default")
        self.assertEqual(updated["reasoning_effort"], "medium")
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": first["turn"]["id"], "status": "completed"},
            },
        )
        self.service.send_message(created["conversation_id"], "Second")
        second_start = [
            call for call in self.adapter.calls if call[0] == "start_turn"
        ][-1]
        self.assertEqual(
            second_start[3],
            {"model": "gpt-default", "reasoning_effort": "medium"},
        )

    def test_model_settings_reject_unavailable_model_and_effort_pairs(self):
        conversation = self._create()

        with self.assertRaises(ConversationError) as invalid_model:
            self.service.update_settings(
                conversation["conversation_id"], "not-listed", "low"
            )
        with self.assertRaises(ConversationError) as invalid_effort:
            self.service.update_settings(
                conversation["conversation_id"], "gpt-default", "high"
            )

        self.assertEqual(invalid_model.exception.code, "invalid_model")
        self.assertEqual(
            invalid_effort.exception.code, "invalid_reasoning_effort"
        )
        stored = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertIsNone(stored["model"])
        self.assertIsNone(stored["reasoning_effort"])

    def test_new_adapter_epoch_resumes_persisted_thread_before_next_turn(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "First")
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": conversation["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        old_adapter = self.adapter
        old_adapter.disconnect(RuntimeError("connection changed"))
        self.service.probe_host("local")
        self.adapter.turn_count = 1

        result = self.service.send_message(conversation["conversation_id"], "Second")

        self.assertIsNot(self.adapter, old_adapter)
        calls = [call[0] for call in self.adapter.calls]
        self.assertLess(calls.index("resume_thread"), calls.index("start_turn"))
        self.assertEqual(result["conversation"]["status"], "running")

    def test_new_epoch_replaces_an_unmaterialized_thread_before_first_send(self):
        conversation = self.service.create_conversation(
            "alpha", "local", "local-root", "gpt-deep", "high"
        )["conversation"]
        old_thread_id = conversation["thread_id"]
        old_adapter = self.adapter
        old_adapter.disconnect(RuntimeError("connection changed"))
        self.service.probe_host("local")
        self.adapter.thread_count = 1
        self.adapter.unmaterialized_threads.add(old_thread_id)

        result = self.service.send_message(conversation["conversation_id"], "First")

        self.assertIsNot(self.adapter, old_adapter)
        replacement_thread_id = result["conversation"]["thread_id"]
        self.assertEqual(replacement_thread_id, "thread-2")
        self.assertNotEqual(replacement_thread_id, old_thread_id)
        self.assertIsNone(self.service.store.find_conversation("local", old_thread_id))
        calls = self.adapter.calls
        self.assertIn(("resume_thread", old_thread_id), calls)
        replacement_start = next(
            call for call in calls if call[0] == "start_thread"
        )
        self.assertEqual(replacement_start[2], {"model": "gpt-deep"})
        self.assertEqual(
            [call[:3] for call in calls if call[0] == "start_turn"],
            [("start_turn", replacement_thread_id, "First")],
        )
        self.assertEqual(
            next(call for call in calls if call[0] == "start_turn")[3],
            {"model": "gpt-deep", "reasoning_effort": "high"},
        )
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["thread.started", "thread.replaced", "user.message", "turn.started"],
        )
        self.assertEqual(
            sum(event["kind"] == "user.message" for event in events), 1
        )
        self.assertEqual(
            events[1]["payload"]["previous_thread_id"], old_thread_id
        )
        self.assertEqual(
            events[1]["payload"]["thread"]["id"], replacement_thread_id
        )

    def test_missing_rollout_is_not_recovered_after_turn_evidence(self):
        conversation = self._create()
        old_thread_id = conversation["thread_id"]
        self.service.store.append_event(
            kind="turn.started",
            payload={"turn": {"id": "accepted-turn"}},
            conversation_id=conversation["conversation_id"],
            turn_id="accepted-turn",
        )
        self.adapter.disconnect(RuntimeError("connection changed"))
        self.service.probe_host("local")
        self.adapter.unmaterialized_threads.add(old_thread_id)

        with self.assertRaises(ConversationError) as caught:
            self.service.send_message(conversation["conversation_id"], "Unsafe")

        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertNotIn("start_thread", [call[0] for call in self.adapter.calls])
        persisted = self.service.store.get_conversation(conversation["conversation_id"])
        assert persisted is not None
        self.assertEqual(persisted["thread_id"], old_thread_id)
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(events[-1]["kind"], "protocol.error")
        self.assertEqual(events[-1]["payload"]["operation"], "thread/resume")
        self.assertNotIn("user.message", [event["kind"] for event in events])

    def test_create_fails_if_connection_epoch_changes_before_residency_mark(self):
        original = self.service._mark_thread_resident
        self.service._mark_thread_resident = lambda *args: False
        try:
            with self.assertRaises(ConversationError) as caught:
                self._create()
        finally:
            self.service._mark_thread_resident = original

        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(self.service.store.list_conversations(), [])
        self.assertEqual(self.service.store.read_events(), [])

    def test_resume_failure_is_labeled_and_does_not_persist_unsent_message(self):
        conversation = self._create()
        self.adapter.disconnect(RuntimeError("connection changed"))
        self.service.probe_host("local")

        def failing_resume(thread_id: str) -> dict[str, Any]:
            self.adapter.calls.append(("resume_thread", thread_id))
            raise RuntimeError("resume failed")

        self.adapter.resume_thread = failing_resume
        with self.assertRaises(ConversationError) as caught:
            self.service.send_message(conversation["conversation_id"], "Not sent")

        self.assertEqual(caught.exception.code, "provider_unavailable")
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["thread.started", "protocol.error"],
        )
        self.assertEqual(events[-1]["payload"]["operation"], "thread/resume")
        self.assertNotIn("start_turn", [call[0] for call in self.adapter.calls])

    def test_completion_callback_before_turn_start_returns_is_not_overwritten(self):
        conversation = self._create()
        original_start_turn = self.adapter.start_turn

        def completing_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            result = original_start_turn(thread_id, text, **optional)
            turn = result["turn"]
            self.adapter.emit_notification(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {"id": turn["id"], "status": "completed"},
                },
            )
            return result

        self.adapter.start_turn = completing_start_turn
        result = self.service.send_message(conversation["conversation_id"], "Quick work")
        self.assertEqual(result["conversation"]["status"], "completed")
        self.assertIsNone(result["conversation"]["active_turn_id"])
        persisted = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(persisted["status"], "completed")
        self.assertIsNone(persisted["active_turn_id"])

    def test_terminal_error_before_turn_start_returns_is_not_ignored(self):
        conversation = self._create()

        def failing_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            self.adapter.turn_count += 1
            turn = {"id": f"turn-{self.adapter.turn_count}", "status": "inProgress"}
            self.adapter.calls.append(("start_turn", thread_id, text, optional))
            self.adapter.emit_notification(
                "error",
                {
                    "threadId": thread_id,
                    "turnId": turn["id"],
                    "message": "The turn failed before its start response.",
                    "willRetry": False,
                },
            )
            return {"turn": turn}

        self.adapter.start_turn = failing_start_turn
        result = self.service.send_message(conversation["conversation_id"], "Fail quickly")

        self.assertEqual(result["conversation"]["status"], "failed")
        self.assertIsNone(result["conversation"]["active_turn_id"])
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["thread.started", "user.message", "protocol.error"],
        )
        self.assertEqual(events[-1]["turn_id"], "turn-1")

    def test_disconnect_after_turn_start_response_cannot_restore_running_state(self):
        conversation = self._create()
        disconnected_adapter = self.adapter
        original_start_turn = disconnected_adapter.start_turn

        def disconnecting_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            result = original_start_turn(thread_id, text, **optional)
            disconnected_adapter.disconnect(RuntimeError("lost after response"))
            return result

        disconnected_adapter.start_turn = disconnecting_start_turn
        result = self.service.send_message(conversation["conversation_id"], "Work")
        self.assertEqual(result["conversation"]["status"], "unknown")
        self.assertEqual(result["conversation"]["active_turn_id"], "turn-1")
        persisted = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(persisted["status"], "unknown")
        self.assertEqual(persisted["active_turn_id"], "turn-1")

    def test_concurrent_double_send_starts_exactly_one_turn(self):
        conversation = self._create()
        original_start_turn = self.adapter.start_turn
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        call_count = 0
        count_lock = threading.Lock()

        def blocking_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            nonlocal call_count
            with count_lock:
                call_count += 1
                current = call_count
            (first_entered if current == 1 else second_entered).set()
            if not release.wait(timeout=3):
                raise AssertionError("test did not release turn/start")
            return original_start_turn(thread_id, text, **optional)

        self.adapter.start_turn = blocking_start_turn
        results: list[dict[str, Any]] = []
        errors: list[ConversationError] = []

        def send(text: str) -> None:
            try:
                results.append(self.service.send_message(conversation["conversation_id"], text))
            except ConversationError as exc:
                errors.append(exc)

        first = threading.Thread(target=send, args=("First",))
        second = threading.Thread(target=send, args=("Second",))
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        interleaved = second_entered.wait(timeout=0.25)
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(interleaved, "a second turn/start entered before the first send finished")
        self.assertEqual(call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual([error.code for error in errors], ["conversation_busy"])
        messages = [
            event["payload"]["text"]
            for event in self.service.detail(conversation["conversation_id"])["events"]
            if event["kind"] == "user.message"
        ]
        self.assertEqual(messages, ["First"])

    def test_send_and_archive_do_not_interleave(self):
        conversation = self._create()
        original_start_turn = self.adapter.start_turn
        original_archive_thread = self.adapter.archive_thread
        send_entered = threading.Event()
        archive_attempted = threading.Event()
        archive_called = threading.Event()
        release_send = threading.Event()
        errors: list[BaseException] = []

        def blocking_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            send_entered.set()
            if not release_send.wait(timeout=3):
                raise AssertionError("test did not release turn/start")
            return original_start_turn(thread_id, text, **optional)

        def observed_archive_thread(thread_id: str) -> dict[str, Any]:
            archive_called.set()
            return original_archive_thread(thread_id)

        self.adapter.start_turn = blocking_start_turn
        self.adapter.archive_thread = observed_archive_thread

        def send() -> None:
            try:
                self.service.send_message(conversation["conversation_id"], "Work")
            except BaseException as exc:
                errors.append(exc)

        def archive() -> None:
            archive_attempted.set()
            try:
                self.service.archive(conversation["conversation_id"])
            except BaseException as exc:
                errors.append(exc)

        sending = threading.Thread(target=send)
        archiving = threading.Thread(target=archive)
        sending.start()
        self.assertTrue(send_entered.wait(timeout=1))
        archiving.start()
        self.assertTrue(archive_attempted.wait(timeout=1))
        interleaved = archive_called.wait(timeout=0.25)
        release_send.set()
        sending.join(timeout=2)
        archiving.join(timeout=2)

        self.assertFalse(interleaved, "archive reached the provider during turn/start")
        self.assertFalse(sending.is_alive())
        self.assertFalse(archiving.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(archive_called.is_set())
        final = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(final["lifecycle"], "archived")
        self.assertEqual(final["status"], "idle")
        self.assertIsNone(final["active_turn_id"])

    def test_send_and_interrupt_are_ordered_around_the_accepted_turn(self):
        conversation = self._create()
        original_start_turn = self.adapter.start_turn
        original_interrupt_turn = self.adapter.interrupt_turn
        send_entered = threading.Event()
        interrupt_attempted = threading.Event()
        interrupt_called = threading.Event()
        release_send = threading.Event()
        errors: list[BaseException] = []

        def blocking_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            send_entered.set()
            if not release_send.wait(timeout=3):
                raise AssertionError("test did not release turn/start")
            return original_start_turn(thread_id, text, **optional)

        def observed_interrupt_turn(thread_id: str, turn_id: str) -> dict[str, Any]:
            interrupt_called.set()
            return original_interrupt_turn(thread_id, turn_id)

        self.adapter.start_turn = blocking_start_turn
        self.adapter.interrupt_turn = observed_interrupt_turn

        def send() -> None:
            try:
                self.service.send_message(conversation["conversation_id"], "Work")
            except BaseException as exc:
                errors.append(exc)

        def interrupt() -> None:
            interrupt_attempted.set()
            try:
                self.service.interrupt(conversation["conversation_id"])
            except BaseException as exc:
                errors.append(exc)

        sending = threading.Thread(target=send)
        interrupting = threading.Thread(target=interrupt)
        sending.start()
        self.assertTrue(send_entered.wait(timeout=1))
        interrupting.start()
        self.assertTrue(interrupt_attempted.wait(timeout=1))
        interleaved = interrupt_called.wait(timeout=0.25)
        release_send.set()
        sending.join(timeout=2)
        interrupting.join(timeout=2)

        self.assertFalse(interleaved, "interrupt reached the provider before turn/start returned")
        self.assertFalse(sending.is_alive())
        self.assertFalse(interrupting.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(interrupt_called.is_set())
        interrupt_calls = [call for call in self.adapter.calls if call[0] == "interrupt_turn"]
        self.assertEqual(interrupt_calls, [("interrupt_turn", conversation["thread_id"], "turn-1")])
        final = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(final["status"], "interrupted")
        self.assertIsNone(final["active_turn_id"])

    def test_different_conversations_can_start_turns_in_parallel(self):
        first_conversation = self._create()
        second_conversation = self._create("beta")
        original_start_turn = self.adapter.start_turn
        first_entered = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        errors: list[BaseException] = []

        def selectively_blocking_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            if thread_id == first_conversation["thread_id"]:
                first_entered.set()
                if not release_first.wait(timeout=3):
                    raise AssertionError("test did not release first turn/start")
            return original_start_turn(thread_id, text, **optional)

        self.adapter.start_turn = selectively_blocking_start_turn

        def send_first() -> None:
            try:
                self.service.send_message(first_conversation["conversation_id"], "First")
            except BaseException as exc:
                errors.append(exc)

        def send_second() -> None:
            try:
                self.service.send_message(second_conversation["conversation_id"], "Second")
            except BaseException as exc:
                errors.append(exc)
            finally:
                second_finished.set()

        first = threading.Thread(target=send_first)
        second = threading.Thread(target=send_second)
        first.start()
        self.assertTrue(first_entered.wait(timeout=1))
        second.start()
        ran_in_parallel = second_finished.wait(timeout=1)
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertTrue(ran_in_parallel, "a different conversation was blocked by the first send")
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_move_waits_for_an_in_flight_send(self):
        conversation = self._create()
        original_start_turn = self.adapter.start_turn
        send_entered = threading.Event()
        move_attempted = threading.Event()
        move_finished = threading.Event()
        release_send = threading.Event()
        errors: list[BaseException] = []

        def blocking_start_turn(
            thread_id: str, text: str, **optional: Any
        ) -> dict[str, Any]:
            send_entered.set()
            if not release_send.wait(timeout=3):
                raise AssertionError("test did not release turn/start")
            return original_start_turn(thread_id, text, **optional)

        self.adapter.start_turn = blocking_start_turn

        def send() -> None:
            try:
                self.service.send_message(conversation["conversation_id"], "Work")
            except BaseException as exc:
                errors.append(exc)

        def move() -> None:
            move_attempted.set()
            try:
                self.service.move(conversation["conversation_id"], "beta")
            except BaseException as exc:
                errors.append(exc)
            finally:
                move_finished.set()

        sending = threading.Thread(target=send)
        moving = threading.Thread(target=move)
        sending.start()
        self.assertTrue(send_entered.wait(timeout=1))
        moving.start()
        self.assertTrue(move_attempted.wait(timeout=1))
        interleaved = move_finished.wait(timeout=0.25)
        pursuit_while_sending = self.service.detail(conversation["conversation_id"])[
            "conversation"
        ]["pursuit_id"]
        release_send.set()
        sending.join(timeout=2)
        moving.join(timeout=2)

        self.assertFalse(interleaved, "move finished while turn/start was still in flight")
        self.assertEqual(pursuit_while_sending, "alpha")
        self.assertFalse(sending.is_alive())
        self.assertFalse(moving.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            self.service.detail(conversation["conversation_id"])["conversation"]["pursuit_id"],
            "beta",
        )

    def test_conversation_request_response_waits_for_archive(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.emit_request(
            52,
            "item/commandExecution/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "command-1",
                "command": "echo safe",
            },
        )
        pending = self.service.workspace()["pending_requests"][0]
        original_archive_thread = self.adapter.archive_thread
        original_respond = self.adapter.respond_server_request
        archive_entered = threading.Event()
        response_attempted = threading.Event()
        response_called = threading.Event()
        release_archive = threading.Event()
        errors: list[BaseException] = []

        def blocking_archive_thread(thread_id: str) -> dict[str, Any]:
            archive_entered.set()
            if not release_archive.wait(timeout=3):
                raise AssertionError("test did not release thread/archive")
            return original_archive_thread(thread_id)

        def observed_response(
            request_id: str | int,
            *,
            result: dict[str, Any] | None = None,
            error: dict[str, Any] | None = None,
            epoch: str | None = None,
        ) -> None:
            response_called.set()
            original_respond(
                request_id,
                result=result,
                error=error,
                epoch=epoch,
            )

        self.adapter.archive_thread = blocking_archive_thread
        self.adapter.respond_server_request = observed_response

        def archive() -> None:
            try:
                self.service.archive(conversation["conversation_id"])
            except BaseException as exc:
                errors.append(exc)

        def respond() -> None:
            response_attempted.set()
            try:
                self.service.respond_request(
                    pending["request_key"],
                    "accept",
                    None,
                    conversation["conversation_id"],
                )
            except BaseException as exc:
                errors.append(exc)

        archiving = threading.Thread(target=archive)
        responding = threading.Thread(target=respond)
        archiving.start()
        self.assertTrue(archive_entered.wait(timeout=1))
        responding.start()
        self.assertTrue(response_attempted.wait(timeout=1))
        interleaved = response_called.wait(timeout=0.25)
        release_archive.set()
        archiving.join(timeout=2)
        responding.join(timeout=2)

        self.assertFalse(interleaved, "request response reached the provider during archive")
        self.assertFalse(archiving.is_alive())
        self.assertFalse(responding.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConversationError)
        self.assertEqual(errors[0].code, "stale_request")
        self.assertFalse(response_called.is_set())
        final = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(final["lifecycle"], "archived")
        self.assertEqual(final["status"], "idle")

    def test_notifications_reconcile_deltas_completed_items_and_turn_state(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Work")
        thread_id = conversation["thread_id"]
        self.adapter.emit_notification(
            "item/agentMessage/delta",
            {
                "threadId": thread_id,
                "turnId": "turn-1",
                "itemId": "item-1",
                "delta": "partial",
            },
        )
        completed_item = {"id": "item-1", "type": "agentMessage", "text": "partial complete"}
        self.adapter.emit_notification(
            "item/completed",
            {"threadId": thread_id, "turnId": "turn-1", "item": completed_item},
        )
        self.adapter.emit_notification(
            "future/progress",
            {"threadId": thread_id, "turnId": "turn-1", "detail": "kept"},
        )
        self.adapter.emit_notification(
            "thread/name/updated",
            {"threadId": thread_id, "threadName": "A useful Codex title"},
        )
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        detail = self.service.detail(conversation["conversation_id"])
        self.assertEqual(detail["conversation"]["status"], "completed")
        self.assertEqual(detail["conversation"]["thread_title"], "A useful Codex title")
        self.assertIsNone(detail["conversation"]["active_turn_id"])
        by_kind = {event["kind"]: event for event in detail["events"]}
        self.assertEqual(by_kind["item.completed"]["payload"]["item"], completed_item)
        self.assertEqual(by_kind["protocol.notification"]["payload"]["method"], "future/progress")
        self.assertEqual(by_kind["thread.name"]["payload"]["threadName"], "A useful Codex title")

    def test_delayed_terminal_notification_for_old_turn_records_without_overwrite(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "First")
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": conversation["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        self.service.send_message(conversation["conversation_id"], "Second")

        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": conversation["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        detail = self.service.detail(conversation["conversation_id"])
        self.assertEqual(detail["conversation"]["status"], "running")
        self.assertEqual(detail["conversation"]["active_turn_id"], "turn-2")
        delayed = [
            event
            for event in detail["events"]
            if event["kind"] == "turn.completed" and event["turn_id"] == "turn-1"
        ]
        self.assertEqual(len(delayed), 2)

    def test_command_approval_response_is_epoch_bound_and_normalized(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run it")
        self.adapter.emit_request(
            41,
            "item/commandExecution/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "command-1",
                "command": "echo safe",
            },
        )
        workspace = self.service.workspace()
        self.assertEqual(len(workspace["pending_requests"]), 1)
        pending = workspace["pending_requests"][0]
        self.assertEqual(pending["connection_epoch"], self.adapter.epoch)
        self.assertEqual(
            self.service.detail(conversation["conversation_id"])["conversation"]["status"],
            "waiting_approval",
        )

        result = self.service.respond_request(
            pending["request_key"], "accept", None, conversation["conversation_id"]
        )
        self.assertEqual(result["request"]["state"], "resolved")
        self.assertEqual(self.adapter.responses, [(41, {"decision": "accept"}, self.adapter.epoch)])
        kinds = [event["kind"] for event in self.service.detail(conversation["conversation_id"])["events"]]
        self.assertIn("server_request", kinds)
        self.assertIn("server_request_resolved", kinds)

    def test_interrupt_stales_old_turn_approval_before_next_turn(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "First turn")
        self.adapter.emit_request(
            81,
            "item/commandExecution/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "command-1",
                "command": "echo old",
            },
        )
        old_request = self.service.workspace()["pending_requests"][0]
        self.service.interrupt(conversation["conversation_id"])
        self.assertEqual(
            self.service.store.get_pending_request_by_key(old_request["request_key"])["state"],
            "stale",
        )

        next_turn = self.service.send_message(conversation["conversation_id"], "Second turn")
        self.assertEqual(next_turn["conversation"]["active_turn_id"], "turn-2")
        with self.assertRaises(ConversationError) as caught:
            self.service.respond_request(
                old_request["request_key"],
                "accept",
                None,
                conversation["conversation_id"],
            )
        self.assertEqual(caught.exception.code, "stale_request")
        self.assertEqual(self.adapter.responses, [])

    def test_nested_turn_identity_is_persisted_for_staling_and_validation(self):
        interrupted = self._create()
        self.service.send_message(interrupted["conversation_id"], "First")
        self.adapter.emit_request(
            86,
            "item/commandExecution/requestApproval",
            {
                "threadId": interrupted["thread_id"],
                "turn": {"id": "turn-1"},
                "itemId": "nested-command",
                "command": "echo nested",
            },
        )
        nested = self.service.workspace()["pending_requests"][0]
        self.assertEqual(nested["payload"]["turnId"], "turn-1")
        self.service.interrupt(interrupted["conversation_id"])
        self.assertEqual(
            self.service.store.get_pending_request_by_key(nested["request_key"])["state"],
            "stale",
        )

        mismatched = self._create("beta")
        self.service.send_message(mismatched["conversation_id"], "Second")
        self.adapter.emit_request(
            87,
            "item/fileChange/requestApproval",
            {
                "threadId": mismatched["thread_id"],
                "turn": {"id": "turn-2"},
                "itemId": "nested-patch",
            },
        )
        pending = self.service.workspace()["pending_requests"][0]
        self.assertEqual(pending["payload"]["turnId"], "turn-2")
        self.service.store.update_conversation(
            mismatched["conversation_id"],
            status="running",
            active_turn_id="turn-newer",
            touch_activity=True,
        )
        with self.assertRaises(ConversationError) as caught:
            self.service.respond_request(
                pending["request_key"], "accept", None, mismatched["conversation_id"]
            )
        self.assertEqual(caught.exception.code, "stale_request")
        self.assertEqual(
            self.service.store.get_pending_request_by_key(pending["request_key"])["state"],
            "stale",
        )

    def test_completion_stales_pending_requests_for_that_turn(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.emit_request(
            82,
            "item/fileChange/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "patch-1",
            },
        )
        pending = self.service.workspace()["pending_requests"][0]
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": conversation["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        self.assertEqual(
            self.service.store.get_pending_request_by_key(pending["request_key"])["state"],
            "stale",
        )
        self.assertEqual(self.service.workspace()["pending_requests"], [])

    def test_response_rejects_noncurrent_turn_and_archived_conversation(self):
        old_turn = self._create()
        self.service.send_message(old_turn["conversation_id"], "Run")
        self.adapter.emit_request(
            84,
            "item/commandExecution/requestApproval",
            {
                "threadId": old_turn["thread_id"],
                "turnId": "turn-1",
                "itemId": "command-old",
                "command": "echo old",
            },
        )
        old_pending = self.service.workspace()["pending_requests"][0]
        self.service.store.update_conversation(
            old_turn["conversation_id"],
            status="running",
            active_turn_id="turn-new",
            touch_activity=True,
        )
        with self.assertRaises(ConversationError) as old_error:
            self.service.respond_request(
                old_pending["request_key"], "accept", None, old_turn["conversation_id"]
            )
        self.assertEqual(old_error.exception.code, "stale_request")
        self.assertEqual(
            self.service.store.get_pending_request_by_key(old_pending["request_key"])["state"],
            "stale",
        )

        archived = self._create("beta")
        self.service.send_message(archived["conversation_id"], "Run")
        self.adapter.emit_request(
            85,
            "item/fileChange/requestApproval",
            {
                "threadId": archived["thread_id"],
                "turnId": "turn-2",
                "itemId": "patch-archived",
            },
        )
        archived_pending = self.service.workspace()["pending_requests"][0]
        self.service.store.archive_conversation(archived["conversation_id"])
        with self.assertRaises(ConversationError) as archived_error:
            self.service.respond_request(
                archived_pending["request_key"],
                "accept",
                None,
                archived["conversation_id"],
            )
        self.assertEqual(archived_error.exception.code, "stale_request")
        self.assertEqual(
            self.service.store.get_pending_request_by_key(archived_pending["request_key"])["state"],
            "stale",
        )
        self.assertEqual(self.adapter.responses, [])

    def test_request_queued_before_interrupt_is_rejected_after_turn_stops(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        original_interrupt = self.adapter.interrupt_turn
        interrupt_entered = threading.Event()
        release_interrupt = threading.Event()
        request_attempted = threading.Event()
        request_errors: list[BaseException] = []

        def blocking_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
            interrupt_entered.set()
            if not release_interrupt.wait(timeout=3):
                raise AssertionError("test did not release interrupt")
            return original_interrupt(thread_id, turn_id)

        self.adapter.interrupt_turn = blocking_interrupt

        interrupting = threading.Thread(
            target=lambda: self.service.interrupt(conversation["conversation_id"])
        )

        def deliver_request() -> None:
            request_attempted.set()
            try:
                self.adapter.emit_request(
                    83,
                    "item/commandExecution/requestApproval",
                    {
                        "threadId": conversation["thread_id"],
                        "turnId": "turn-1",
                        "itemId": "command-late",
                        "command": "echo late",
                    },
                )
            except BaseException as exc:
                request_errors.append(exc)

        delivering = threading.Thread(target=deliver_request)
        interrupting.start()
        self.assertTrue(interrupt_entered.wait(timeout=1))
        delivering.start()
        self.assertTrue(request_attempted.wait(timeout=1))
        release_interrupt.set()
        interrupting.join(timeout=2)
        delivering.join(timeout=2)

        self.assertFalse(interrupting.is_alive())
        self.assertFalse(delivering.is_alive())
        self.assertEqual(len(request_errors), 1)
        self.assertIsInstance(request_errors[0], ConversationError)
        self.assertEqual(request_errors[0].code, "stale_request")
        self.assertEqual(self.service.store.list_pending_requests(state=None), [])

    def test_tool_user_input_is_mapped_to_protocol_answer_shape(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Ask")
        self.adapter.emit_request(
            "question-rpc",
            "item/tool/requestUserInput",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "question-item",
                "isBlocking": True,
                "questions": [
                    {"id": "choice", "header": "Choice", "question": "Which one?"}
                ],
            },
        )
        pending = self.service.workspace()["pending_requests"][0]
        self.service.respond_request(
            pending["request_key"],
            response={"choice": {"answers": ["First"]}},
            expected_conversation_id=conversation["conversation_id"],
        )
        self.assertEqual(
            self.adapter.responses[-1][1],
            {"answers": {"choice": {"answers": ["First"]}}},
        )

    def test_disconnect_marks_requests_stale_and_never_replays_rpc_id(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.emit_request(
            9,
            "item/fileChange/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "patch-1",
            },
        )
        request_key = self.service.workspace()["pending_requests"][0]["request_key"]
        self.adapter.disconnect(RuntimeError("lost transport"))
        pending = self.service.store.get_pending_request_by_key(request_key)
        self.assertEqual(pending["state"], "stale")
        with self.assertRaises(ConversationError) as caught:
            self.service.respond_request(
                request_key, "accept", None, conversation["conversation_id"]
            )
        self.assertEqual(caught.exception.code, "stale_request")
        self.assertEqual(self.adapter.responses, [])
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertIn("server_request_stale", [event["kind"] for event in events])
        self.assertEqual(
            self.service.detail(conversation["conversation_id"])["conversation"]["status"],
            "unknown",
        )

    def test_reconcile_clears_only_provider_confirmed_inactive_turn_state(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        old_adapter = self.adapter
        old_adapter.disconnect(RuntimeError("connection lost"))
        uncertain = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(uncertain["status"], "unknown")
        self.assertEqual(uncertain["active_turn_id"], "turn-1")

        reconciled = self.service.reconcile(conversation["conversation_id"])
        self.assertTrue(reconciled["resolved"])
        self.assertEqual(reconciled["conversation"]["status"], "idle")
        self.assertIsNone(reconciled["conversation"]["active_turn_id"])
        self.assertIsNot(self.adapter, old_adapter)
        self.assertIn("resume_thread", [call[0] for call in self.adapter.calls])

        next_turn = self.service.send_message(conversation["conversation_id"], "Continue")
        self.assertEqual(next_turn["conversation"]["status"], "running")
        kinds = [
            event["kind"]
            for event in self.service.detail(conversation["conversation_id"])["events"]
        ]
        self.assertIn("thread.reconciled", kinds)

    def test_reconcile_replaces_legacy_wedged_zero_turn_thread(self):
        conversation = self._create()
        old_thread_id = conversation["thread_id"]
        self.service.store.append_event(
            kind="user.message",
            payload={"text": "Legacy unsent message"},
            conversation_id=conversation["conversation_id"],
        )
        self.service.store.append_event(
            kind="protocol.error",
            payload={
                "operation": "turn/start",
                "message": f"no rollout found for thread id {old_thread_id}",
            },
            conversation_id=conversation["conversation_id"],
        )
        self.service.store.update_conversation(
            conversation["conversation_id"], status="unknown", touch_activity=True
        )
        old_adapter = self.adapter
        old_adapter.disconnect(RuntimeError("connection changed"))
        self.service.probe_host("local")
        self.adapter.thread_count = 1
        self.adapter.unmaterialized_threads.add(old_thread_id)

        result = self.service.reconcile(conversation["conversation_id"])

        self.assertTrue(result["resolved"])
        self.assertEqual(result["conversation"]["status"], "idle")
        self.assertIsNone(result["conversation"]["active_turn_id"])
        self.assertEqual(result["conversation"]["thread_id"], "thread-2")
        self.assertEqual(result["thread"]["id"], "thread-2")
        self.assertIsNone(self.service.store.find_conversation("local", old_thread_id))
        self.assertIn(("resume_thread", old_thread_id), self.adapter.calls)
        self.assertNotIn("start_turn", [call[0] for call in self.adapter.calls])
        events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["thread.started", "user.message", "protocol.error", "thread.replaced"],
        )
        self.assertEqual(events[1]["payload"]["text"], "Legacy unsent message")

        calls_after_recovery = list(self.adapter.calls)
        repeated = self.service.reconcile(conversation["conversation_id"])
        self.assertTrue(repeated["resolved"])
        self.assertEqual(repeated["conversation"]["thread_id"], "thread-2")
        self.assertEqual(repeated["thread"]["id"], "thread-2")
        self.assertEqual(self.adapter.calls, calls_after_recovery)
        repeated_events = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(
            sum(event["kind"] == "thread.replaced" for event in repeated_events),
            1,
        )

    def test_reconcile_preserves_turn_fence_for_unknown_provider_state(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.disconnect(RuntimeError("connection lost"))
        self.service.probe_host("local")

        def unknown_resume(thread_id: str) -> dict[str, Any]:
            return {"thread": {"id": thread_id, "status": {"type": "futureStatus"}}}

        self.adapter.resume_thread = unknown_resume
        reconciled = self.service.reconcile(conversation["conversation_id"])
        self.assertFalse(reconciled["resolved"])
        self.assertEqual(reconciled["conversation"]["status"], "unknown")
        self.assertEqual(reconciled["conversation"]["active_turn_id"], "turn-1")
        with self.assertRaises(ConversationError) as caught:
            self.service.send_message(conversation["conversation_id"], "Unsafe duplicate")
        self.assertEqual(caught.exception.code, "conversation_busy")

    def test_reconcile_does_not_overwrite_newer_completion_callback(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.disconnect(RuntimeError("connection lost"))
        self.service.probe_host("local")

        def completing_resume(thread_id: str) -> dict[str, Any]:
            self.adapter.emit_notification(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            )
            return {
                "thread": {
                    "id": thread_id,
                    "status": {"type": "active"},
                    "turns": [{"id": "turn-1", "status": "inProgress"}],
                }
            }

        self.adapter.resume_thread = completing_resume
        reconciled = self.service.reconcile(conversation["conversation_id"])
        self.assertEqual(reconciled["conversation"]["status"], "completed")
        self.assertIsNone(reconciled["conversation"]["active_turn_id"])

    def test_disconnect_after_resume_response_keeps_reconcile_unresolved(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        self.adapter.disconnect(RuntimeError("first connection lost"))
        self.service.probe_host("local")
        disconnected_adapter = self.adapter
        original_resume = disconnected_adapter.resume_thread

        def disconnecting_resume(thread_id: str) -> dict[str, Any]:
            result = original_resume(thread_id)
            disconnected_adapter.disconnect(RuntimeError("lost after resume response"))
            return result

        disconnected_adapter.resume_thread = disconnecting_resume
        reconciled = self.service.reconcile(conversation["conversation_id"])
        self.assertFalse(reconciled["resolved"])
        self.assertEqual(reconciled["conversation"]["status"], "unknown")
        self.assertEqual(reconciled["conversation"]["active_turn_id"], "turn-1")

    def test_server_response_write_failure_fences_connection_and_active_work(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        for request_id, item_id in ((61, "command-1"), (62, "command-2")):
            self.adapter.emit_request(
                request_id,
                "item/commandExecution/requestApproval",
                {
                    "threadId": conversation["thread_id"],
                    "turnId": "turn-1",
                    "itemId": item_id,
                    "command": "echo safe",
                },
            )
        first, second = self.service.workspace()["pending_requests"]
        failed_adapter = self.adapter

        def fail_response(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("write outcome unknown")

        failed_adapter.respond_server_request = fail_response
        with self.assertRaises(ConversationError) as caught:
            self.service.respond_request(
                first["request_key"],
                "accept",
                None,
                conversation["conversation_id"],
            )
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(
            self.service.store.get_pending_request_by_key(first["request_key"])["state"],
            "resolved",
        )
        self.assertEqual(
            self.service.store.get_pending_request_by_key(second["request_key"])["state"],
            "stale",
        )
        current = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(current["active_turn_id"], "turn-1")
        self.assertTrue(failed_adapter.closed)
        self.assertIsNone(self.service._existing_adapter("local"))
        kinds = [
            event["kind"]
            for event in self.service.detail(conversation["conversation_id"])["events"]
        ]
        self.assertIn("server_request_stale", kinds)
        self.assertIn("server_response_failed", kinds)

    def test_failed_response_from_replaced_epoch_does_not_poison_new_work(self):
        first = self._create()
        second = self._create("beta")
        self.service.send_message(first["conversation_id"], "First")
        self.service.send_message(second["conversation_id"], "Second")
        self.adapter.emit_request(
            63,
            "item/commandExecution/requestApproval",
            {
                "threadId": first["thread_id"],
                "turnId": "turn-1",
                "itemId": "command-1",
                "command": "echo safe",
            },
        )
        pending = self.service.workspace()["pending_requests"][0]
        failed_adapter = self.adapter
        replacement: dict[str, _FakeAdapter] = {}

        def replace_then_fail(*args: Any, **kwargs: Any) -> None:
            host = self.service.store.get_host("local")
            assert host is not None
            current = self.adapters(
                host,
                local_cwd=self.root,
                on_notification=lambda message: self.service._on_notification("local", message),
                on_server_request=lambda message: self.service._on_server_request("local", message),
                on_disconnect=lambda message: self.service._on_disconnect("local", message),
            )
            current.connect()
            with self.service._adapter_lock:
                self.service._adapters["local"] = current
            replacement["adapter"] = current
            raise RuntimeError("old epoch write failed")

        failed_adapter.respond_server_request = replace_then_fail
        with self.assertRaises(ConversationError):
            self.service.respond_request(
                pending["request_key"], "accept", None, first["conversation_id"]
            )
        second_state = self.service.detail(second["conversation_id"])["conversation"]
        self.assertEqual(second_state["status"], "running")
        self.assertEqual(second_state["active_turn_id"], "turn-2")
        self.assertIs(self.service._existing_adapter("local"), replacement["adapter"])

    def test_post_persistence_request_failure_leaves_no_answerable_request(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        original_append = self.service._append_event

        def fail_server_request_event(
            kind: str,
            payload: dict[str, Any],
            *,
            conversation_id: str | None = None,
            turn_id: str | None = None,
        ) -> dict[str, Any]:
            if kind == "server_request":
                raise RuntimeError("event persistence failed")
            return original_append(
                kind,
                payload,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )

        self.service._append_event = fail_server_request_event
        with self.assertRaisesRegex(RuntimeError, "event persistence failed"):
            self.adapter.emit_request(
                72,
                "item/fileChange/requestApproval",
                {
                    "threadId": conversation["thread_id"],
                    "turnId": "turn-1",
                    "itemId": "patch-1",
                },
            )
        requests = self.service.store.list_pending_requests(state=None)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["state"], "resolved")
        current = self.service.detail(conversation["conversation_id"])["conversation"]
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["active_turn_id"], "turn-1")

    def test_server_request_persistence_failure_reaches_protocol_dispatcher(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")

        def fail_persistence(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("database write failed")

        self.service.store.create_pending_request = fail_persistence
        with self.assertRaisesRegex(RuntimeError, "database write failed"):
            self.adapter.emit_request(
                71,
                "item/fileChange/requestApproval",
                {
                    "threadId": conversation["thread_id"],
                    "turnId": "turn-1",
                    "itemId": "patch-1",
                },
            )

    def test_disconnect_leaves_inactive_conversation_statuses_unchanged(self):
        completed = self._create()
        self.service.send_message(completed["conversation_id"], "Finish")
        self.adapter.emit_notification(
            "turn/completed",
            {
                "threadId": completed["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        idle = self._create("beta")
        self.adapter.disconnect(RuntimeError("normal process shutdown"))
        self.assertEqual(
            self.service.detail(completed["conversation_id"])["conversation"]["status"],
            "completed",
        )
        self.assertEqual(
            self.service.detail(idle["conversation_id"])["conversation"]["status"],
            "idle",
        )

    def test_controlled_close_persists_unknown_until_reconciled_after_reopen(self):
        conversation = self._create()
        self.service.send_message(conversation["conversation_id"], "Run")
        old_adapter = self.adapter
        self.registry.close_root(self.root)

        persisted = self.service.store.get_conversation(conversation["conversation_id"])
        assert persisted is not None
        self.assertEqual(persisted["status"], "unknown")
        self.assertEqual(persisted["active_turn_id"], "turn-1")
        self.assertTrue(old_adapter.closed)

        self.service = self.registry.service(self.root)
        reconciled = self.service.reconcile(conversation["conversation_id"])
        self.assertEqual(reconciled["conversation"]["status"], "idle")
        self.assertIsNone(reconciled["conversation"]["active_turn_id"])

    def test_unclean_reopen_marks_persisted_busy_statuses_unknown(self):
        conversations = [
            self._create("alpha" if index % 2 == 0 else "beta")
            for index in range(4)
        ]
        statuses = ["starting", "running", "waiting_approval", "waiting_input"]
        for index, (conversation, status) in enumerate(
            zip(conversations, statuses), start=1
        ):
            self.service.store.update_conversation(
                conversation["conversation_id"],
                status=status,
                active_turn_id=f"crash-turn-{index}",
                touch_activity=True,
            )

        reopened = ConversationService(
            self.root,
            adapter_factory=self.adapters,
            pursuit_store_factory=lambda root: self.pursuit_stores[str(root.resolve())],
        )
        try:
            for index, conversation in enumerate(conversations, start=1):
                recovered = reopened.store.get_conversation(conversation["conversation_id"])
                assert recovered is not None
                self.assertEqual(recovered["status"], "unknown")
                self.assertEqual(recovered["active_turn_id"], f"crash-turn-{index}")
        finally:
            reopened.close()

    def test_late_callbacks_from_old_epoch_cannot_change_reconnected_state(self):
        conversation = self._create()
        old_adapter = self.adapter
        old_adapter.disconnect(RuntimeError("first connection ended"))
        self.service.probe_host("local")
        current_adapter = self.adapter
        self.assertIsNot(current_adapter, old_adapter)
        self.service.send_message(conversation["conversation_id"], "New connection turn")

        old_adapter.emit_notification(
            "thread/name/updated",
            {"threadId": conversation["thread_id"], "threadName": "Stale title"},
        )
        old_adapter.emit_notification(
            "turn/completed",
            {
                "threadId": conversation["thread_id"],
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        old_adapter.emit_request(
            77,
            "item/fileChange/requestApproval",
            {
                "threadId": conversation["thread_id"],
                "turnId": "turn-1",
                "itemId": "old-patch",
            },
        )
        old_adapter.disconnect(RuntimeError("late old disconnect"))

        detail = self.service.detail(conversation["conversation_id"])
        self.assertEqual(detail["conversation"]["status"], "running")
        self.assertEqual(detail["conversation"]["thread_title"], "Thread 1")
        self.assertEqual(detail["pending_requests"], [])
        self.assertIsNone(self.service.store.get_host("local")["last_error"])
        self.assertIs(self.service._existing_adapter("local"), current_adapter)

    def test_default_detail_returns_latest_bounded_history(self):
        conversation = self._create()
        for index in range(510):
            self.service.store.append_event(
                kind="test.event",
                payload={"index": index},
                conversation_id=conversation["conversation_id"],
            )
        latest = self.service.detail(conversation["conversation_id"])["events"]
        self.assertEqual(len(latest), 500)
        self.assertEqual(latest[0]["payload"]["index"], 10)
        from_start = self.service.detail(conversation["conversation_id"], 0)["events"]
        self.assertEqual(from_start[0]["kind"], "thread.started")

    def test_explicit_detail_page_cursor_stops_at_last_delivered_event(self):
        conversation = self._create()
        delivered = [
            {
                "event_id": event_id,
                "conversation_id": conversation["conversation_id"],
                "turn_id": None,
                "kind": "test.event",
                "payload": {"index": event_id},
                "created_at": "2026-08-29T00:00:00+00:00",
            }
            for event_id in range(100, 1100)
        ]
        self.service.store.latest_event_id = lambda: 5000
        self.service.store.read_events = lambda **kwargs: delivered
        detail = self.service.detail(conversation["conversation_id"], 0)
        self.assertEqual(len(detail["events"]), 1000)
        self.assertEqual(detail["cursor"], 1099)

    def test_workspace_cursor_precedes_events_that_race_with_snapshot(self):
        conversation = self._create()
        cursor_before = self.service.store.latest_event_id()
        original_list = self.service.store.list_conversations
        injected: dict[str, Any] = {}

        def injecting_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if not injected:
                injected["event"] = self.service.store.append_event(
                    kind="test.concurrent",
                    payload={"source": "workspace"},
                    conversation_id=conversation["conversation_id"],
                )
            return original_list(*args, **kwargs)

        self.service.store.list_conversations = injecting_list
        snapshot = self.service.workspace()
        self.assertEqual(snapshot["cursor"], cursor_before)
        self.assertGreater(injected["event"]["event_id"], snapshot["cursor"])

    def test_detail_cursor_precedes_events_that_race_with_snapshot(self):
        conversation = self._create()
        cursor_before = self.service.store.latest_event_id()
        original_get = self.service.store.get_conversation
        injected: dict[str, Any] = {}

        def injecting_get(conversation_id: str) -> dict[str, Any] | None:
            if not injected:
                injected["event"] = self.service.store.append_event(
                    kind="test.concurrent",
                    payload={"source": "detail"},
                    conversation_id=conversation_id,
                )
            return original_get(conversation_id)

        self.service.store.get_conversation = injecting_get
        detail = self.service.detail(conversation["conversation_id"])
        self.assertGreater(injected["event"]["event_id"], cursor_before)
        self.assertEqual(detail["cursor"], injected["event"]["event_id"])
        self.assertIn(
            injected["event"]["event_id"],
            [event["event_id"] for event in detail["events"]],
        )

    def test_registry_keeps_roots_and_event_streams_isolated(self):
        with tempfile.TemporaryDirectory() as second:
            second_root = Path(second).resolve()
            second_service = self.registry.service(second_root)
            first = self._create()
            self.assertEqual(len(self.service.workspace()["conversations"]), 1)
            self.assertEqual(second_service.workspace()["conversations"], [])
            self.assertEqual(second_service.store.read_events(), [])

            cursor = self.service.store.latest_event_id()
            stream = self.service.stream_events(
                after_event_id=cursor, heartbeat_seconds=0.01
            )
            self.assertIsNone(next(stream))
            stopped = threading.Event()

            def consume() -> None:
                try:
                    next(stream)
                except StopIteration:
                    stopped.set()

            thread = threading.Thread(target=consume)
            thread.start()
            self.registry.invalidate_root_session(self.root)
            thread.join(timeout=1)
            self.assertTrue(stopped.is_set())
            self.assertEqual(first["pursuit_id"], "alpha")

    def test_deleted_pursuit_does_not_delete_or_reclassify_conversation(self):
        conversation = self._create()
        self.pursuit_stores[str(self.root)].items.clear()
        detail = self.service.detail(conversation["conversation_id"])
        self.assertFalse(detail["conversation"]["pursuit_available"])
        self.assertEqual(detail["conversation"]["lifecycle"], "active")
        self.assertEqual(detail["conversation"]["pursuit_title_snapshot"], "Alpha")

    def test_move_changes_only_primary_pursuit_attachment(self):
        conversation = self._create()
        moved = self.service.move(conversation["conversation_id"], "beta")["conversation"]
        self.assertEqual(moved["pursuit_id"], "beta")
        self.assertEqual(moved["pursuit_title_snapshot"], "Beta")
        self.assertEqual(moved["host_id"], conversation["host_id"])
        self.assertEqual(moved["project_id"], conversation["project_id"])
        self.assertEqual(moved["thread_id"], conversation["thread_id"])

    def test_host_and_project_admission_keeps_remote_path_out_of_shell(self):
        with self.assertRaises(ConversationError) as caught:
            self.service.add_host("Remote", "remote", "custom codex")
        self.assertEqual(caught.exception.code, "invalid_host")
        host = self.service.add_host("Remote", "remote")["host"]
        with self.assertRaises(ConversationError):
            self.service.add_project(host["host_id"], "Bad", r"C:\not-posix")
        project = self.service.add_project(host["host_id"], "Repo", "/srv/repo")["project"]
        self.assertEqual(project["cwd"], "/srv/repo")


if __name__ == "__main__":
    unittest.main()
