from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rightmemory.conversations.app_server import (
    AppServerDisconnect,
    AppServerNotification,
    AppServerRequest,
    CodexAppServer,
    StaleConnectionEpochError,
    create_app_server,
)
from rightmemory.conversations.jsonrpc import (
    JsonRpcConnection,
    JsonRpcConnectionClosed,
    JsonRpcConnectionError,
)
from rightmemory.conversations.transport import (
    REMOTE_CODEX_APP_SERVER_COMMAND,
    SubprocessTransport,
    TransportConfigurationError,
    build_local_transport,
    build_ssh_transport,
    resolve_codex_binary,
    transport_for_host,
    validate_ssh_alias,
)


FAKE_SERVER = Path(__file__).with_name("fake_codex_app_server.py")


def _fake_transport(*, env: dict[str, str] | None = None) -> SubprocessTransport:
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    if env:
        child_env.update(env)
    return SubprocessTransport((sys.executable, str(FAKE_SERVER)), env=child_env)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class JsonRpcConnectionTests(unittest.TestCase):
    def test_initializes_and_supports_concurrent_out_of_order_requests(self):
        notifications = []
        initialized = threading.Event()

        def on_notification(notification):
            notifications.append(notification)
            if notification.method == "fake/initialized":
                initialized.set()

        connection = JsonRpcConnection(
            _fake_transport(),
            on_notification=on_notification,
            request_timeout=2,
        )
        self.addCleanup(connection.close)

        result = connection.start()
        slow = connection.request_future("test/delay", {"value": "slow", "delay": 0.15})
        fast = connection.request_future("test/echo", {"value": "fast"})

        self.assertEqual(result["serverInfo"]["name"], "fake-codex")
        self.assertEqual(
            result["received"],
            {"clientInfo": {"name": "rightmemory", "version": "0.1.0"}},
        )
        self.assertEqual(fast.result(timeout=1), {"value": "fast"})
        self.assertEqual(slow.result(timeout=1), {"value": "slow", "delay": 0.15})
        self.assertTrue(initialized.wait(1))
        self.assertEqual(notifications[0].params, {})
        self.assertTrue(connection.epoch)
        self.assertTrue(connection.connected)

    def test_close_fails_pending_futures_and_reports_disconnect(self):
        disconnects = []
        disconnected = threading.Event()

        def on_disconnect(error):
            disconnects.append(error)
            disconnected.set()

        connection = JsonRpcConnection(
            _fake_transport(),
            on_disconnect=on_disconnect,
            request_timeout=2,
        )
        connection.start()
        pending = connection.request_future("test/delay", {"delay": 5})
        connection.close()

        with self.assertRaises(JsonRpcConnectionClosed):
            pending.result(timeout=1)
        self.assertTrue(disconnected.wait(1))
        self.assertEqual(disconnects, [None])
        self.assertFalse(connection.connected)

    def test_unexpected_child_exit_fails_pending_request(self):
        connection = JsonRpcConnection(_fake_transport(), request_timeout=2)
        self.addCleanup(connection.close)
        connection.start()

        with self.assertRaises(JsonRpcConnectionError):
            connection.request("test/crash")

    def test_stderr_tail_is_bounded(self):
        connection = JsonRpcConnection(_fake_transport(), request_timeout=2, stderr_limit=128)
        self.addCleanup(connection.close)
        connection.start()
        connection.request("test/stderr", {"size": 1000})
        _wait_until(lambda: len(connection.stderr_tail) == 128)

        self.assertEqual(connection.stderr_tail, "x" * 127 + "\n")

    def test_callback_queue_overflow_closes_instead_of_growing_unbounded(self):
        release_handler = threading.Event()

        def blocking_handler(_notification):
            release_handler.wait(2)

        connection = JsonRpcConnection(
            _fake_transport(),
            on_notification=blocking_handler,
            request_timeout=2,
            dispatch_queue_limit=2,
        )
        connection.start()
        try:
            with self.assertRaisesRegex(JsonRpcConnectionError, "queue overflowed"):
                connection.request("test/flood", {"count": 20})
        finally:
            release_handler.set()
            connection.close()


class CodexAppServerTests(unittest.TestCase):
    def test_stable_thread_and_turn_methods_use_expected_shapes(self):
        server = CodexAppServer(_fake_transport(), request_timeout=2)
        self.addCleanup(server.close)
        server.connect()

        started = server.start_thread("/workspace")
        resumed = server.resume_thread("thread-1")
        read = server.read_thread("thread-1")
        listed = server.list_threads(cursor="next", limit=20, cwd="/workspace", archived=False)
        archived = server.archive_thread("thread-1")
        turn = server.start_turn("thread-1", "hello")
        interrupted = server.interrupt_turn("thread-1", "turn-1")

        self.assertEqual(started["received"], {"cwd": "/workspace"})
        self.assertEqual(resumed["received"], {"threadId": "thread-1"})
        self.assertEqual(
            read["received"],
            {"threadId": "thread-1", "includeTurns": True},
        )
        self.assertEqual(
            listed["received"],
            {"cursor": "next", "limit": 20, "cwd": "/workspace", "archived": False},
        )
        self.assertEqual(archived["received"], {"threadId": "thread-1"})
        self.assertEqual(
            turn["received"],
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
            },
        )
        self.assertEqual(
            interrupted["received"],
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

    def test_forwards_notifications_and_server_requests_with_epoch(self):
        notifications: list[AppServerNotification] = []
        requests: list[AppServerRequest] = []
        disconnects: list[AppServerDisconnect] = []
        response_seen = threading.Event()
        server_holder = {}

        def on_notification(notification: AppServerNotification) -> None:
            notifications.append(notification)
            if notification.method == "fake/serverResponse":
                response_seen.set()

        def on_request(request: AppServerRequest) -> None:
            requests.append(request)
            server_holder["server"].respond_server_request(
                request.request_id,
                result={"decision": "accept"},
                epoch=request.epoch,
            )

        server = CodexAppServer(
            _fake_transport(),
            on_notification=on_notification,
            on_server_request=on_request,
            on_disconnect=disconnects.append,
            request_timeout=2,
        )
        server_holder["server"] = server
        server.connect()
        epoch = server.epoch
        server.start_turn("thread-1", "run it")

        self.assertTrue(response_seen.wait(1))
        self.assertEqual(requests[0].epoch, epoch)
        self.assertEqual(requests[0].request_id, "approval-1")
        self.assertEqual(requests[0].method, "item/commandExecution/requestApproval")
        response = next(item for item in notifications if item.method == "fake/serverResponse")
        self.assertEqual(response.params["result"], {"decision": "accept"})
        self.assertTrue(all(item.epoch == epoch for item in notifications))
        with self.assertRaises(StaleConnectionEpochError):
            server.respond_server_request("approval-1", result={}, epoch="old-epoch")

        server.close()
        _wait_until(lambda: bool(disconnects))
        self.assertEqual(disconnects[0].epoch, epoch)
        self.assertIsNone(disconnects[0].error)


class ConversationTransportTests(unittest.TestCase):
    def test_local_transport_keeps_executable_and_arguments_separate(self):
        transport = build_local_transport(sys.executable, cwd=FAKE_SERVER.parent)
        self.assertEqual(
            transport.argv,
            (sys.executable, "app-server", "--listen", "stdio://"),
        )

        fake_process = object()
        with patch("rightmemory.conversations.transport.subprocess.Popen", return_value=fake_process) as popen:
            self.assertIs(transport.spawn(), fake_process)
        self.assertEqual(popen.call_args.args[0], list(transport.argv))
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(FAKE_SERVER.parent.resolve()))

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_bundled_executable_is_preferred_to_windows_command_shim(self):
        def which(name, path=None):
            return "C:/npm/codex.CMD" if name == "codex" else None

        with (
            patch("rightmemory.conversations.transport.shutil.which", side_effect=which),
            patch(
                "rightmemory.conversations.transport._bundled_codex_binary",
                return_value="C:/bundle/codex.exe",
            ),
        ):
            resolved = resolve_codex_binary(environment={"Path": "C:/npm"})

        self.assertEqual(resolved, "C:/bundle/codex.exe")

    @unittest.skipUnless(os.name == "nt", "Windows command shim behavior")
    def test_command_shim_fallback_remains_an_argv_with_shell_disabled(self):
        def which(name, path=None):
            return "C:/npm/codex.CMD" if name == "codex" else None

        with (
            patch("rightmemory.conversations.transport.shutil.which", side_effect=which),
            patch("rightmemory.conversations.transport._bundled_codex_binary", return_value=None),
        ):
            transport = build_local_transport(environment={"Path": "C:/npm"})

        fake_process = object()
        with patch("rightmemory.conversations.transport.subprocess.Popen", return_value=fake_process) as popen:
            transport.spawn()
        self.assertEqual(transport.argv[0], "C:/npm/codex.CMD")
        self.assertEqual(popen.call_args.args[0][0], "C:/npm/codex.CMD")
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_ssh_transport_uses_fixed_safe_argv_without_project_cwd(self):
        transport = build_ssh_transport("build-box", ssh_binary=sys.executable)

        self.assertEqual(transport.argv[0], sys.executable)
        self.assertIn("BatchMode=yes", transport.argv)
        self.assertIn("ConnectTimeout=10", transport.argv)
        self.assertIn("ClearAllForwardings=yes", transport.argv)
        self.assertIn("PermitLocalCommand=no", transport.argv)
        self.assertIn("StrictHostKeyChecking=yes", transport.argv)
        separator = transport.argv.index("--")
        self.assertEqual(
            transport.argv[separator + 1 :],
            ("build-box", REMOTE_CODEX_APP_SERVER_COMMAND),
        )
        self.assertEqual(REMOTE_CODEX_APP_SERVER_COMMAND, "codex app-server --listen stdio://")
        self.assertIsNone(transport.cwd)

    def test_ssh_alias_validation_rejects_option_and_shell_like_values(self):
        self.assertEqual(validate_ssh_alias("dev-box_2.example"), "dev-box_2.example")
        for invalid in (
            "",
            "-oProxyCommand=bad",
            "two words",
            "host\nnext",
            "user@host",
            "host;command",
            "a" * 129,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(TransportConfigurationError):
                    validate_ssh_alias(invalid)

    def test_host_factory_selects_local_and_ssh_transports(self):
        local = SimpleNamespace(kind="local", codex_command_override=sys.executable)
        ssh = SimpleNamespace(kind="ssh", ssh_alias="build-box", codex_command_override=None)
        with patch(
            "rightmemory.conversations.transport.resolve_ssh_binary",
            return_value=sys.executable,
        ):
            local_transport = transport_for_host(local, local_cwd=FAKE_SERVER.parent)
            ssh_transport = transport_for_host(ssh)
            app_server = create_app_server(local, local_cwd=FAKE_SERVER.parent)

        self.assertEqual(local_transport.argv[0], sys.executable)
        self.assertEqual(ssh_transport.argv[-2], "build-box")
        self.assertIsInstance(app_server, CodexAppServer)
        app_server.close()


if __name__ == "__main__":
    unittest.main()
