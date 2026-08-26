from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rightmemory.codex_sdk import CodexSdkRunner, _unsubscribe_thread


class FakeTransportClosedError(RuntimeError):
    pass


class FakeInvalidRequestError(RuntimeError):
    pass


class FakeThread:
    def __init__(self, thread_id, *, result=None, error=None, events=None):
        self.id = thread_id
        self.result = result or SimpleNamespace(
            final_response="forked response",
            duration_ms=321,
            usage={"total": {"inputTokens": 12, "outputTokens": 3}},
        )
        self.error = error
        self.events = events
        self.run_calls = []

    def run(self, prompt, **kwargs):
        self.run_calls.append((prompt, kwargs))
        if self.events is not None:
            self.events.append("run")
        if self.error is not None:
            raise self.error
        return self.result


class FakeCodex:
    def __init__(
        self,
        threads,
        *,
        events=None,
        unsubscribe_error=None,
        unarchive_error=None,
    ):
        self.threads = list(threads)
        self.events = events
        self.unsubscribe_error = unsubscribe_error
        self.unarchive_error = unarchive_error
        self.fork_calls = []
        self.unarchive_calls = []
        self.unsubscribe_calls = []
        self.close_calls = 0

    def thread_fork(self, source_thread_id, **kwargs):
        self.fork_calls.append((source_thread_id, kwargs))
        if self.events is not None:
            self.events.append("fork")
        return self.threads.pop(0)

    def thread_unarchive(self, thread_id):
        self.unarchive_calls.append(thread_id)
        if self.events is not None:
            self.events.append("unarchive")
        if self.unarchive_error is not None:
            raise self.unarchive_error

    def thread_unsubscribe(self, thread_id):
        self.unsubscribe_calls.append(thread_id)
        if self.events is not None:
            self.events.append("unsubscribe")
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error
        return "unsubscribed"

    def close(self):
        self.close_calls += 1
        if self.events is not None:
            self.events.append("close")


class FakeArchiveClient:
    def __init__(self, root, *, calls=None, events=None):
        self.root = Path(root)
        self.calls = calls
        self.events = events

    def archive_threads(self, thread_ids):
        ids = list(dict.fromkeys(thread_ids))
        if self.calls is not None:
            self.calls.append((self.root, ids))
        if self.events is not None:
            self.events.append("archive")
        return [SimpleNamespace(thread_id=thread_id, archived=True, error=None) for thread_id in ids]


def fake_archive_client_factory(root):
    return FakeArchiveClient(root)


def fake_sdk():
    return SimpleNamespace(
        approval_mode=SimpleNamespace(deny_all="deny-all"),
        codex=None,
        config=lambda **kwargs: kwargs,
        sandbox=SimpleNamespace(read_only="read-only", full_access="full-access"),
        transport_closed_error=FakeTransportClosedError,
        invalid_request_error=FakeInvalidRequestError,
        reasoning_effort=lambda value: f"effort:{value}",
        thread_unsubscribe_response=object,
    )


class CodexSdkForkTests(unittest.TestCase):
    def setUp(self):
        self.sdk_patcher = patch(
            "rightmemory.codex_sdk._load_codex_sdk",
            return_value=fake_sdk(),
        )
        self.sdk_patcher.start()
        self.addCleanup(self.sdk_patcher.stop)

    def test_unsubscribe_uses_current_sdk_raw_request_fallback(self):
        request = Mock(
            return_value=SimpleNamespace(
                status=SimpleNamespace(value="unsubscribed"),
            )
        )
        codex = SimpleNamespace(_client=SimpleNamespace(request=request))
        sdk = fake_sdk()

        _unsubscribe_thread(codex, "thread-1", sdk)

        request.assert_called_once_with(
            "thread/unsubscribe",
            {"threadId": "thread-1"},
            response_model=sdk.thread_unsubscribe_response,
        )

    def test_forks_source_and_returns_first_turn_output_and_timing(self):
        events = []
        archive_calls = []
        thread = FakeThread("child-thread", events=events)
        codex = FakeCodex([thread], events=events)
        configs = []
        clock = iter([0.0, 0.1, 0.3, 0.4, 0.5, 0.6, 1.6, 1.7, 1.9, 2.0]).__next__
        runner = CodexSdkRunner(
            codex_factory=lambda config: configs.append(config) or codex,
            archive_client_factory=lambda root: FakeArchiveClient(
                root,
                calls=archive_calls,
                events=events,
            ),
            clock=clock,
        )
        callbacks = []
        timings = []

        result = runner.run_forked_turn(
            prompt="current request",
            source_provider_session_id="seed-thread",
            cwd=Path("/memory/root"),
            model="gpt-5.6-sol",
            reasoning_effort="high",
            sandbox="read-only",
            on_thread_started=lambda thread_id: (
                events.append("callback"),
                callbacks.append(thread_id),
            ),
            on_timing=timings.append,
        )

        self.assertEqual(len(configs), 1)
        self.assertEqual(
            events,
            ["unarchive", "fork", "callback", "run", "unsubscribe", "close", "archive"],
        )
        self.assertEqual(callbacks, ["child-thread"])
        self.assertEqual(codex.unarchive_calls, ["seed-thread"])
        self.assertEqual(codex.unsubscribe_calls, ["child-thread"])
        self.assertEqual(codex.close_calls, 1)
        self.assertEqual(
            archive_calls,
            [(Path("/memory/root"), ["seed-thread", "child-thread"])],
        )
        self.assertEqual(result.provider_session_id, "child-thread")
        self.assertEqual(result.text, "forked response")
        self.assertEqual(result.timing.client_start_ms, 200.0)
        self.assertEqual(result.timing.thread_open_ms, 100.0)
        self.assertEqual(result.timing.turn_ms, 1000.0)
        self.assertEqual(result.timing.thread_release_ms, 200.0)
        self.assertEqual(result.timing.server_duration_ms, 321)
        self.assertEqual(
            result.timing.usage,
            {"total": {"inputTokens": 12, "outputTokens": 3}},
        )
        self.assertEqual(timings, [result.timing])
        self.assertEqual(
            codex.fork_calls,
            [
                (
                    "seed-thread",
                    {
                        "approval_mode": "deny-all",
                        "cwd": str(Path("/memory/root")),
                        "model": "gpt-5.6-sol",
                        "sandbox": "read-only",
                    },
                )
            ],
        )
        self.assertEqual(
            thread.run_calls,
            [
                (
                    "current request",
                    {
                        "approval_mode": "deny-all",
                        "cwd": str(Path("/memory/root")),
                        "effort": "effort:high",
                        "model": "gpt-5.6-sol",
                        "sandbox": "read-only",
                    },
                )
            ],
        )

    def test_rejects_empty_or_unchanged_fork_id_before_callback_and_turn(self):
        for returned_id in ("", "  ", "seed-thread"):
            with self.subTest(returned_id=returned_id):
                thread = FakeThread(returned_id)
                codex = FakeCodex([thread])
                runner = CodexSdkRunner(
                    codex_factory=lambda _config: codex,
                    archive_client_factory=fake_archive_client_factory,
                )
                callback = Mock()

                with self.assertRaises(RuntimeError):
                    runner.run_forked_turn(
                        prompt="current request",
                        source_provider_session_id="seed-thread",
                        cwd=Path("/memory/root"),
                        model=None,
                        reasoning_effort=None,
                        sandbox="read-only",
                        on_thread_started=callback,
                    )

                callback.assert_not_called()
                self.assertEqual(thread.run_calls, [])

    def test_records_child_before_failed_turn_and_uses_fresh_connection_next_time(self):
        events = []
        failed = FakeThread("child-failed", error=ValueError("turn failed"), events=events)
        succeeded = FakeThread("child-succeeded")
        failed_codex = FakeCodex([failed], events=events)
        succeeded_codex = FakeCodex([succeeded])
        factory = Mock(side_effect=[failed_codex, succeeded_codex])
        runner = CodexSdkRunner(
            codex_factory=factory,
            archive_client_factory=fake_archive_client_factory,
        )
        started = []
        timings = []

        with self.assertRaisesRegex(ValueError, "turn failed"):
            runner.run_forked_turn(
                prompt="first request",
                source_provider_session_id="seed-thread",
                cwd=Path("/memory/root"),
                model=None,
                reasoning_effort=None,
                sandbox="read-only",
                on_thread_started=lambda thread_id: (
                    events.append("callback"),
                    started.append(thread_id),
                ),
                on_timing=timings.append,
            )

        result = runner.run_forked_turn(
            prompt="second request",
            source_provider_session_id="seed-thread",
            cwd=Path("/memory/root"),
            model=None,
            reasoning_effort=None,
            sandbox="read-only",
            on_thread_started=started.append,
        )

        self.assertEqual(events[:4], ["unarchive", "fork", "callback", "run"])
        self.assertEqual(started, ["child-failed", "child-succeeded"])
        self.assertEqual(len(timings), 1)
        self.assertGreaterEqual(timings[0].turn_ms, 0.0)
        self.assertEqual(result.provider_session_id, "child-succeeded")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(failed_codex.close_calls, 1)
        self.assertEqual(succeeded_codex.close_calls, 1)

    def test_fork_accepts_source_that_is_already_unarchived(self):
        error = FakeInvalidRequestError(
            "JSON-RPC error -32600: no archived rollout found for thread id seed-thread"
        )
        codex = FakeCodex([FakeThread("child-thread")], unarchive_error=error)
        runner = CodexSdkRunner(
            codex_factory=lambda _config: codex,
            archive_client_factory=fake_archive_client_factory,
        )

        result = runner.run_forked_turn(
            prompt="current request",
            source_provider_session_id="seed-thread",
            cwd=Path("/memory/root"),
            model=None,
            reasoning_effort=None,
            sandbox="read-only",
        )

        self.assertEqual(result.provider_session_id, "child-thread")
        self.assertEqual(codex.close_calls, 1)

    def test_fork_propagates_other_unarchive_failures(self):
        codex = FakeCodex(
            [FakeThread("unused")],
            unarchive_error=FakeInvalidRequestError("permission denied"),
        )
        runner = CodexSdkRunner(
            codex_factory=lambda _config: codex,
            archive_client_factory=fake_archive_client_factory,
        )

        with self.assertRaisesRegex(FakeInvalidRequestError, "permission denied"):
            runner.run_forked_turn(
                prompt="current request",
                source_provider_session_id="seed-thread",
                cwd=Path("/memory/root"),
                model=None,
                reasoning_effort=None,
                sandbox="read-only",
            )

        self.assertEqual(codex.fork_calls, [])
        self.assertEqual(codex.close_calls, 1)

    def test_callback_failure_prevents_turn_but_leaves_recorded_child_owned(self):
        thread = FakeThread("child-thread")
        codex = FakeCodex([thread])
        runner = CodexSdkRunner(
            codex_factory=lambda _config: codex,
            archive_client_factory=fake_archive_client_factory,
        )
        started = []

        def record_then_fail(thread_id):
            started.append(thread_id)
            raise LookupError("record failed")

        with self.assertRaisesRegex(LookupError, "record failed"):
            runner.run_forked_turn(
                prompt="current request",
                source_provider_session_id="seed-thread",
                cwd=Path("/memory/root"),
                model=None,
                reasoning_effort=None,
                sandbox="read-only",
                on_thread_started=record_then_fail,
            )

        self.assertEqual(started, ["child-thread"])
        self.assertEqual(thread.run_calls, [])
        self.assertEqual(codex.unsubscribe_calls, ["child-thread"])
        self.assertEqual(codex.close_calls, 1)

    def test_turn_and_release_failures_preserve_turn_error_after_unsubscribing(self):
        thread = FakeThread("child-thread", error=ValueError("turn failed"))
        codex = FakeCodex(
            [thread],
            unsubscribe_error=RuntimeError("release failed"),
        )
        runner = CodexSdkRunner(
            codex_factory=lambda _config: codex,
            archive_client_factory=fake_archive_client_factory,
        )
        timings = []

        with (
            patch("rightmemory.codex_sdk.print") as warning,
            self.assertRaisesRegex(ValueError, "turn failed"),
        ):
            runner.run_forked_turn(
                prompt="current request",
                source_provider_session_id="seed-thread",
                cwd=Path("/memory/root"),
                model=None,
                reasoning_effort=None,
                sandbox="read-only",
                on_timing=timings.append,
            )

        self.assertEqual(codex.unsubscribe_calls, ["child-thread"])
        self.assertEqual(len(timings), 1)
        self.assertEqual(timings[0].thread_release_error_type, "RuntimeError")
        self.assertGreaterEqual(timings[0].thread_release_ms, 0.0)
        self.assertEqual(codex.close_calls, 1)
        warning.assert_called_once()

    def test_transport_failure_invalidates_connection_without_retrying_turn(self):
        failed = FakeThread(
            "child-failed",
            error=FakeTransportClosedError("transport closed"),
        )
        succeeded = FakeThread("child-succeeded")
        clients = [FakeCodex([failed]), FakeCodex([succeeded])]
        factory = Mock(side_effect=clients)
        runner = CodexSdkRunner(
            codex_factory=factory,
            archive_client_factory=fake_archive_client_factory,
        )
        arguments = {
            "prompt": "current request",
            "source_provider_session_id": "seed-thread",
            "cwd": Path("/memory/root"),
            "model": None,
            "reasoning_effort": None,
            "sandbox": "read-only",
        }

        with self.assertRaises(FakeTransportClosedError):
            runner.run_forked_turn(**arguments)
        result = runner.run_forked_turn(**arguments)

        self.assertEqual(len(failed.run_calls), 1)
        self.assertEqual(clients[0].close_calls, 1)
        self.assertEqual(clients[0].unsubscribe_calls, [])
        self.assertEqual(result.provider_session_id, "child-succeeded")
        self.assertEqual(clients[1].unsubscribe_calls, ["child-succeeded"])
        self.assertEqual(clients[1].close_calls, 1)
        self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
