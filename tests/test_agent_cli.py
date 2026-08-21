import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from openai_codex import ApprovalMode, Sandbox, TransportClosedError
from openai_codex.types import ReasoningEffort

from rightmemory.doctor import (
    DoctorCheck,
    _check_first_provider_calls,
    _check_resume_provider_thread,
    _provider_runtime,
    format_doctor_report,
    run_agent_cli_doctor,
)
from rightmemory.agent_cli import (
    CliAgentExecutor,
    NO_SESSION_RIGHTMEMORY_SESSION_ID,
    build_claude_command,
    parse_claude_output,
    _stable_claude_session_id,
)
from rightmemory.codex_sdk import CodexSdkRunResult, CodexSdkRunner, CodexSdkTiming
from rightmemory.config import AgentCliConfig, RuntimeConfig, SyncConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.provider_threads import ProviderThreadStore


EMPTY_RETRIEVE_SELECTION_JSON = '{"ids": [], "sources": [], "recent_candidates": []}'


class ProviderSessionStoreTests(unittest.TestCase):
    def test_save_load_and_lookup_provider_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ProviderSessionStore(root, "retrieve")
            record = ProviderSessionRecord(
                provider="codex",
                provider_session_id="thread-1",
                role="retrieve",
                rightmemory_session_id="agent-1",
                created_at="2026-05-18T00:00:00+00:00",
                updated_at="2026-05-18T00:01:00+00:00",
            )

            store.save(record)
            loaded = store.load("agent-1")
            is_internal = ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-1")
            wrong_provider = ProviderSessionStore.is_internal_provider_session(root, "claude", "thread-1")
            missing = ProviderSessionStore.is_internal_provider_session(root, "codex", "missing")

        self.assertEqual(loaded, record)
        self.assertTrue(is_internal)
        self.assertFalse(wrong_provider)
        self.assertFalse(missing)

    def test_lookup_ignores_corrupt_registry_records(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            corrupt = root / ".runtime" / "agent_cli_sessions" / "retrieve" / "broken.json"
            corrupt.parent.mkdir(parents=True)
            corrupt.write_text("{bad json", encoding="utf-8")

            result = ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-1")

        self.assertFalse(result)


class AgentCliCommandTests(unittest.TestCase):
    def test_build_claude_first_command_uses_session_id(self):
        config = AgentCliConfig(provider="claude", model="sonnet")
        session_id = "123e4567-e89b-12d3-a456-426614174000"

        command = build_claude_command("retrieve", config, "prompt", session_id, False)

        self.assertEqual(
            command,
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--model",
                "sonnet",
                "--permission-mode",
                "plan",
                "--session-id",
                session_id,
                "prompt",
            ],
        )

    def test_build_claude_uses_plan_permission_for_reviewer(self):
        session_id = "123e4567-e89b-12d3-a456-426614174000"

        command = build_claude_command(
            "reviewer",
            AgentCliConfig(provider="claude"),
            "prompt",
            session_id,
            False,
        )

        self.assertIn("--permission-mode", command)
        self.assertIn("plan", command)

    def test_build_claude_resume_command_uses_auto_permission_for_write_role(self):
        session_id = "123e4567-e89b-12d3-a456-426614174000"

        command = build_claude_command(
            "update",
            AgentCliConfig(provider="claude"),
            "prompt",
            session_id,
            True,
        )

        self.assertEqual(
            command,
            [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--permission-mode",
                "auto",
                "--resume",
                session_id,
                "prompt",
            ],
        )

    def test_build_claude_fork_resumes_source_and_requests_new_session(self):
        session_id = "123e4567-e89b-12d3-a456-426614174000"
        child_session_id = "123e4567-e89b-12d3-a456-426614174001"

        command = build_claude_command(
            "retrieve",
            AgentCliConfig(provider="claude"),
            "prompt",
            session_id,
            True,
            fork=True,
            fork_provider_session_id=child_session_id,
        )

        self.assertEqual(
            command[-6:-1],
            [
                "--resume",
                session_id,
                "--fork-session",
                "--session-id",
                child_session_id,
            ],
        )

    def test_build_claude_fork_requires_resume(self):
        with self.assertRaises(ValueError) as caught:
            build_claude_command(
                "retrieve",
                AgentCliConfig(provider="claude"),
                "prompt",
                "123e4567-e89b-12d3-a456-426614174000",
                False,
                fork=True,
            )

        self.assertIn("requires resume", str(caught.exception))

    def test_build_claude_fork_requires_child_session_id(self):
        with self.assertRaises(ValueError) as caught:
            build_claude_command(
                "retrieve",
                AgentCliConfig(provider="claude"),
                "prompt",
                "123e4567-e89b-12d3-a456-426614174000",
                True,
                fork=True,
            )

        self.assertIn("child provider session id", str(caught.exception))

    def test_build_claude_uses_auto_permission_for_shared_view_builder(self):
        session_id = "123e4567-e89b-12d3-a456-426614174000"

        command = build_claude_command(
            "shared-view-builder",
            AgentCliConfig(provider="claude"),
            "prompt",
            session_id,
            False,
        )

        self.assertIn("--permission-mode", command)
        self.assertIn("auto", command)

    def test_build_claude_command_rejects_non_uuid_session_id(self):
        with self.assertRaises(ValueError) as caught:
            build_claude_command("retrieve", AgentCliConfig(provider="claude"), "prompt", "uuid-1", False)

        self.assertIn("UUID", str(caught.exception))

    def test_build_claude_command_rejects_reasoning_effort(self):
        with self.assertRaises(ValueError) as caught:
            build_claude_command(
                "retrieve",
                AgentCliConfig(provider="claude", reasoning_effort="high"),
                "prompt",
                "123e4567-e89b-12d3-a456-426614174000",
                False,
            )

        self.assertIn("only supported for Codex", str(caught.exception))


class AgentCliParserTests(unittest.TestCase):
    def test_parse_claude_json_output(self):
        parsed = parse_claude_output(
            '{"type":"result","session_id":"123e4567-e89b-12d3-a456-426614174000","result":"done"}'
        )

        self.assertEqual(parsed.provider_session_id, "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(parsed.text, "done")

    def test_parse_claude_requires_session_id(self):
        with self.assertRaises(RuntimeError) as caught:
            parse_claude_output('{"type":"result","result":"done"}')

        self.assertIn("session_id", str(caught.exception))

    def test_parse_claude_requires_result(self):
        with self.assertRaises(RuntimeError) as caught:
            parse_claude_output('{"type":"result","session_id":"123e4567-e89b-12d3-a456-426614174000"}')

        self.assertIn("result", str(caught.exception))


class FakeCodexRunner:
    def __init__(
        self,
        *,
        outputs=None,
        turn_error: Exception | None = None,
        fork_error: Exception | None = None,
    ):
        self.outputs = list(outputs or ["done"])
        self.turn_error = turn_error
        self.fork_error = fork_error
        self.calls = []
        self.fork_calls = []
        self.close_calls = 0
        self._new_threads = 0
        self._cleanup_claims = set()

    def claim_opportunistic_cleanup(self, memory_root: Path) -> bool:
        key = str(memory_root.resolve())
        if key in self._cleanup_claims:
            return False
        self._cleanup_claims.add(key)
        return True

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        thread_id = kwargs["provider_session_id"]
        if thread_id is None:
            self._new_threads += 1
            thread_id = f"thread-{self._new_threads}"
            callback = kwargs.get("on_thread_started")
            if callback is not None:
                callback(thread_id)
        timing = CodexSdkTiming(
            client_start_ms=200.0,
            thread_open_ms=25.0,
            turn_ms=500.0,
            server_duration_ms=450,
            total_ms=725.0,
            usage={"total": {"inputTokens": 10, "outputTokens": 2}},
        )
        timing_callback = kwargs.get("on_timing")
        if timing_callback is not None:
            timing_callback(timing)
        if self.turn_error is not None:
            raise self.turn_error
        output_index = min(len(self.calls) + len(self.fork_calls) - 1, len(self.outputs) - 1)
        return CodexSdkRunResult(thread_id, self.outputs[output_index], timing)

    def run_forked_turn(self, **kwargs):
        self.fork_calls.append(kwargs)
        self._new_threads += 1
        thread_id = f"thread-{self._new_threads}"
        callback = kwargs.get("on_thread_started")
        if callback is not None:
            callback(thread_id)
        timing = CodexSdkTiming(
            client_start_ms=0.0,
            thread_open_ms=25.0,
            turn_ms=500.0,
            server_duration_ms=450,
            total_ms=525.0,
            usage={"total": {"inputTokens": 4, "outputTokens": 2}},
        )
        timing_callback = kwargs.get("on_timing")
        if timing_callback is not None:
            timing_callback(timing)
        if self.fork_error is not None:
            raise self.fork_error
        output_index = min(len(self.calls) + len(self.fork_calls) - 1, len(self.outputs) - 1)
        return CodexSdkRunResult(thread_id, self.outputs[output_index], timing)

    def close(self):
        self.close_calls += 1


class FakeSdkThread:
    def __init__(self, thread_id, *, result=None, error=None):
        self.id = thread_id
        self.result = result or SimpleNamespace(
            final_response="done",
            duration_ms=450,
            usage={"total": {"inputTokens": 10}},
        )
        self.error = error
        self.run_calls = []

    def run(self, prompt, **kwargs):
        self.run_calls.append((prompt, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeSdkCodex:
    def __init__(self, threads):
        self.threads = list(threads)
        self.start_calls = []
        self.resume_calls = []
        self.close_calls = 0

    def thread_start(self, **kwargs):
        self.start_calls.append(kwargs)
        return self.threads.pop(0)

    def thread_resume(self, thread_id, **kwargs):
        self.resume_calls.append((thread_id, kwargs))
        return self.threads.pop(0)

    def close(self):
        self.close_calls += 1


class CodexSdkRunnerTests(unittest.TestCase):
    def test_lazily_starts_sdk_and_runs_with_explicit_safety_and_model_settings(self):
        thread = FakeSdkThread("thread-1")
        codex = FakeSdkCodex([thread])
        configs = []
        clock = iter([0.0, 0.1, 0.3, 0.4, 0.5, 0.6, 1.6, 1.7]).__next__
        runner = CodexSdkRunner(
            codex_factory=lambda config: configs.append(config) or codex,
            clock=clock,
        )
        started = []

        self.assertEqual(configs, [])
        result = runner.run_turn(
            prompt="hello",
            provider_session_id=None,
            cwd=Path("/memory/root"),
            model="gpt-5.6-luna",
            reasoning_effort="high",
            sandbox="read-only",
            on_thread_started=started.append,
        )

        self.assertEqual(len(configs), 1)
        self.assertEqual(started, ["thread-1"])
        self.assertEqual(result.provider_session_id, "thread-1")
        self.assertEqual(result.text, "done")
        self.assertEqual(result.timing.client_start_ms, 200.0)
        self.assertEqual(result.timing.thread_open_ms, 100.0)
        self.assertEqual(result.timing.turn_ms, 1000.0)
        self.assertEqual(result.timing.server_duration_ms, 450)
        self.assertEqual(result.timing.usage, {"total": {"inputTokens": 10}})
        self.assertEqual(
            codex.start_calls,
            [
                {
                    "approval_mode": ApprovalMode.deny_all,
                    "cwd": str(Path("/memory/root")),
                    "model": "gpt-5.6-luna",
                    "sandbox": Sandbox.read_only,
                }
            ],
        )
        prompt, run_options = thread.run_calls[0]
        self.assertEqual(prompt, "hello")
        self.assertEqual(run_options["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(run_options["effort"], ReasoningEffort.high)
        self.assertEqual(run_options["sandbox"], Sandbox.read_only)

    def test_resumes_exact_thread_with_workspace_write(self):
        thread = FakeSdkThread("thread-1")
        codex = FakeSdkCodex([thread])
        runner = CodexSdkRunner(codex_factory=lambda _config: codex)

        result = runner.run_turn(
            prompt="again",
            provider_session_id="thread-1",
            cwd=Path("/memory/root"),
            model=None,
            reasoning_effort=None,
            sandbox="workspace-write",
        )

        self.assertEqual(result.provider_session_id, "thread-1")
        self.assertEqual(codex.resume_calls[0][0], "thread-1")
        self.assertEqual(codex.resume_calls[0][1]["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(codex.resume_calls[0][1]["sandbox"], Sandbox.workspace_write)
        self.assertEqual(thread.run_calls[0][1]["sandbox"], Sandbox.workspace_write)

    def test_transport_failure_invalidates_client_without_retrying_ambiguous_turn(self):
        first_thread = FakeSdkThread("thread-1", error=TransportClosedError("closed"))
        second_thread = FakeSdkThread("thread-2")
        clients = [FakeSdkCodex([first_thread]), FakeSdkCodex([second_thread])]
        factory_calls = []

        def factory(_config):
            client = clients[len(factory_calls)]
            factory_calls.append(client)
            return client

        runner = CodexSdkRunner(codex_factory=factory)
        arguments = {
            "prompt": "hello",
            "provider_session_id": None,
            "cwd": Path("/memory/root"),
            "model": None,
            "reasoning_effort": None,
            "sandbox": "read-only",
        }

        with self.assertRaises(TransportClosedError):
            runner.run_turn(**arguments)
        result = runner.run_turn(**arguments)

        self.assertEqual(len(factory_calls), 2)
        self.assertEqual(len(first_thread.run_calls), 1)
        self.assertEqual(factory_calls[0].close_calls, 1)
        self.assertEqual(result.provider_session_id, "thread-2")

    def test_rejects_invalid_effort_before_starting_sdk(self):
        factory = Mock()
        runner = CodexSdkRunner(codex_factory=factory)

        with self.assertRaises(ValueError):
            runner.run_turn(
                prompt="hello",
                provider_session_id=None,
                cwd=Path("/memory/root"),
                model=None,
                reasoning_effort="extreme",
                sandbox="read-only",
            )

        factory.assert_not_called()

    def test_close_is_idempotent_and_cleanup_claim_is_once_per_root(self):
        codex = FakeSdkCodex([FakeSdkThread("thread-1")])
        runner = CodexSdkRunner(codex_factory=lambda _config: codex)
        root = Path("/memory/root")
        runner.run_turn(
            prompt="hello",
            provider_session_id=None,
            cwd=root,
            model=None,
            reasoning_effort=None,
            sandbox="read-only",
        )

        self.assertTrue(runner.claim_opportunistic_cleanup(root))
        self.assertFalse(runner.claim_opportunistic_cleanup(root))
        runner.close()
        runner.close()

        self.assertEqual(codex.close_calls, 1)

    def test_cleanup_claim_renews_after_five_minutes(self):
        now = [100.0]
        runner = CodexSdkRunner(clock=lambda: now[0])
        root = Path("/memory/root")

        self.assertTrue(runner.claim_opportunistic_cleanup(root))
        self.assertFalse(runner.claim_opportunistic_cleanup(root))
        now[0] += 299.0
        self.assertFalse(runner.claim_opportunistic_cleanup(root))
        now[0] += 1.0
        self.assertTrue(runner.claim_opportunistic_cleanup(root))


class CliAgentExecutorTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("rightmemory.agent_cli.prepare_command", side_effect=lambda command: list(command))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opportunistic_cleanup_runs_only_for_top_level_state_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            overlay = root / "overlay"
            with patch("rightmemory.agent_cli.AgentCliThreadCleanup") as cleanup:
                cleanup.return_value.has_expired_codex_threads.return_value = True
                cleanup.return_value.run.return_value.errors = ()
                CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))
                CliAgentExecutor(
                    root,
                    "retrieve",
                    AgentCliConfig(provider="codex"),
                    state_root=overlay,
                )

        cleanup.assert_called_once_with(root)
        cleanup.return_value.run.assert_called_once_with()

    def test_shared_codex_runner_claims_opportunistic_cleanup_once_per_root(self):
        runner = FakeCodexRunner()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.agent_cli.AgentCliThreadCleanup") as cleanup:
                cleanup.return_value.has_expired_codex_threads.return_value = False
                CliAgentExecutor(
                    root,
                    "retrieve",
                    AgentCliConfig(provider="codex"),
                    codex_runner=runner,
                )
                CliAgentExecutor(
                    root,
                    "update",
                    AgentCliConfig(provider="codex"),
                    codex_runner=runner,
                )

        cleanup.assert_called_once_with(root)

    def test_executor_closes_owned_runner_once_but_never_closes_borrowed_runner(self):
        owned = FakeCodexRunner()
        borrowed = FakeCodexRunner()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.agent_cli.CodexSdkRunner", return_value=owned):
                owner = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))
            borrower = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=borrowed,
            )

            owner.cleanup()
            owner.cleanup()
            borrower.cleanup()

        self.assertEqual(owned.close_calls, 1)
        self.assertEqual(borrowed.close_calls, 0)

    def test_sdk_turn_emits_one_provider_timing_summary(self):
        runner = FakeCodexRunner()
        events = []
        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(
                Path(tempdir),
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
                trace_event=lambda event, **fields: events.append((event, fields)),
            )
            executor.run_session_turn("agent-1", "hello")

        self.assertEqual(len(events), 1)
        event, fields = events[0]
        self.assertEqual(event, "provider_timing")
        self.assertEqual(fields["transport"], "codex-sdk")
        self.assertFalse(fields["resumed"])
        self.assertEqual(fields["server_duration_ms"], 450)
        self.assertEqual(fields["outcome"], "success")
        self.assertIn("usage", fields)

    def test_failed_sdk_turn_emits_one_error_timing_summary(self):
        runner = FakeCodexRunner(turn_error=RuntimeError("failed"))
        events = []
        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(
                Path(tempdir),
                "update",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
                trace_event=lambda event, **fields: events.append((event, fields)),
            )
            with self.assertRaises(RuntimeError):
                executor.run_session_turn("agent-1", "hello")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]["outcome"], "error")
        self.assertEqual(events[0][1]["error_type"], "RuntimeError")

    def test_run_turn_is_one_shot_and_records_thread_ownership(self):
        runner = FakeCodexRunner(outputs=["done 中文"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )
            result = executor.run_turn("hello")

            record = ProviderSessionStore(root, "retrieve").load(NO_SESSION_RIGHTMEMORY_SESSION_ID)
            ownership = ProviderThreadStore(root).load("codex", "thread-1")
            is_internal = ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-1")

        self.assertEqual(result, "done 中文")
        self.assertIsNone(record)
        self.assertEqual(ownership.policy, "one-shot")
        self.assertEqual(ownership.rightmemory_session_id, NO_SESSION_RIGHTMEMORY_SESSION_ID)
        self.assertTrue(is_internal)

    def test_run_turn_records_provider_session_under_state_root(self):
        runner = FakeCodexRunner()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            state_root = root / "state"
            memory_root.mkdir()
            executor = CliAgentExecutor(
                memory_root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                state_root=state_root,
                codex_runner=runner,
            )
            result = executor.run_session_turn("agent-1", "hello")

            state_record = ProviderSessionStore(state_root, "retrieve").load("agent-1")
            memory_record = ProviderSessionStore(memory_root, "retrieve").load("agent-1")

        self.assertEqual(result, "done")
        self.assertEqual(runner.calls[0]["cwd"], memory_root)
        self.assertIsNotNone(state_record)
        self.assertEqual(state_record.provider_session_id, "thread-1")
        self.assertIsNone(memory_record)

    def test_retrieve_stateless_turn_does_not_save_provider_session(self):
        runner = FakeCodexRunner(outputs=["reply"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )
            result = executor.run_stateless_turn("snapshot\n\n# Query\n\nfind root")

            self.assertEqual(result, "reply")
            self.assertFalse((root / ".runtime" / "agent_cli_sessions" / "retrieve").exists())

    def test_run_turn_starts_fresh_thread_in_second_executor(self):
        runner = FakeCodexRunner(outputs=["first", "second"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            ).run_turn("hello")
            second = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            ).run_turn("again")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertIsNone(runner.calls[0]["provider_session_id"])
        self.assertIsNone(runner.calls[1]["provider_session_id"])

    def test_codex_session_turn_saves_and_resumes_provider_session(self):
        runner = FakeCodexRunner(outputs=["first", "second"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex", model="gpt-5"),
                codex_runner=runner,
            )
            first = executor.run_session_turn("agent-1", "hello")
            second = executor.run_session_turn("agent-1", "again")

            record = ProviderSessionStore(root, "retrieve").load("agent-1")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertIsNotNone(record)
        self.assertEqual(record.provider_session_id, "thread-1")
        self.assertIsNone(runner.calls[0]["provider_session_id"])
        self.assertEqual(runner.calls[1]["provider_session_id"], "thread-1")
        self.assertEqual(runner.calls[0]["model"], "gpt-5")
        self.assertEqual(runner.calls[0]["sandbox"], "read-only")

    def test_codex_new_retrieve_forks_reusable_prefix_then_resumes_child(self):
        runner = FakeCodexRunner(outputs=[EMPTY_RETRIEVE_SELECTION_JSON, "first", "second"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex", model="gpt-5"),
                codex_runner=runner,
            )

            first = executor.run_session_turn(
                "agent-1",
                "first query",
                prefix_context="stable snapshot",
            )
            second = executor.run_session_turn("agent-1", "follow-up")

            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")
            base = ProviderThreadStore(root).load("codex", "thread-1")
            child = ProviderThreadStore(root).load("codex", "thread-2")
            prefix_records = list(
                (root / ".runtime" / "agent_cli_prefixes" / "codex").glob("*.json")
            )

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertEqual(len(runner.fork_calls), 1)
        self.assertEqual(runner.fork_calls[0]["source_provider_session_id"], "thread-1")
        self.assertEqual(runner.calls[1]["provider_session_id"], "thread-2")
        self.assertEqual(mapping.provider_session_id, "thread-2")
        self.assertEqual(base.policy, "fork-base")
        self.assertEqual(child.forked_from_provider_session_id, "thread-1")
        self.assertEqual(len(prefix_records), 1)

    def test_codex_distinct_retrieve_sessions_reuse_one_prefix_base(self):
        runner = FakeCodexRunner(outputs=[EMPTY_RETRIEVE_SELECTION_JSON, "first", "second"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )

            first = executor.run_session_turn(
                "agent-1",
                "first query",
                prefix_context="stable snapshot",
            )
            second = executor.run_session_turn(
                "agent-2",
                "second query",
                prefix_context="stable snapshot",
            )

            first_mapping = ProviderSessionStore(root, "retrieve").load("agent-1")
            second_mapping = ProviderSessionStore(root, "retrieve").load("agent-2")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.fork_calls), 2)
        self.assertTrue(
            all(
                call["source_provider_session_id"] == "thread-1"
                for call in runner.fork_calls
            )
        )
        self.assertEqual(first_mapping.provider_session_id, "thread-2")
        self.assertEqual(second_mapping.provider_session_id, "thread-3")

    def test_invalid_prefix_bootstrap_falls_back_to_complete_new_thread(self):
        runner = FakeCodexRunner(outputs=["invalid bootstrap", "direct"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )

            with patch("rightmemory.agent_cli.print") as warning:
                result = executor.run_session_turn(
                    "agent-1",
                    "query",
                    prefix_context="stable snapshot",
                )

            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")
            prefix_records = list(
                (root / ".runtime" / "agent_cli_prefixes" / "codex").glob("*.json")
            )

        self.assertEqual(result, "direct")
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.fork_calls, [])
        self.assertEqual(mapping.provider_session_id, "thread-2")
        self.assertEqual(prefix_records, [])
        warning.assert_called_once()

    def test_codex_fork_failure_retires_base_and_rebuilds_before_next_fork(self):
        runner = FakeCodexRunner(
            outputs=[
                EMPTY_RETRIEVE_SELECTION_JSON,
                "unused fork output",
                "direct",
                EMPTY_RETRIEVE_SELECTION_JSON,
                "second",
            ],
            fork_error=RuntimeError("fork unavailable"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )

            with patch("rightmemory.agent_cli.print") as warning:
                first = executor.run_session_turn(
                    "agent-1",
                    "first query",
                    prefix_context="stable snapshot",
                )

            retired_base = ProviderThreadStore(root).load("codex", "thread-1")
            failed_child = ProviderThreadStore(root).load("codex", "thread-2")
            prefix_records_after_failure = list(
                (root / ".runtime" / "agent_cli_prefixes" / "codex").glob("*.json")
            )

            runner.fork_error = None
            second = executor.run_session_turn(
                "agent-2",
                "second query",
                prefix_context="stable snapshot",
            )

        self.assertEqual(first, "direct")
        self.assertEqual(second, "second")
        self.assertEqual(retired_base.status, "delete-pending")
        self.assertEqual(failed_child.forked_from_provider_session_id, "thread-1")
        self.assertIsNone(failed_child.last_successful_activity_at)
        self.assertEqual(prefix_records_after_failure, [])
        self.assertEqual(len(runner.fork_calls), 2)
        self.assertEqual(runner.fork_calls[0]["source_provider_session_id"], "thread-1")
        self.assertEqual(runner.fork_calls[1]["source_provider_session_id"], "thread-4")
        warning.assert_called_once()

    def test_claude_first_turn_uses_stable_uuid_then_resumes(self):
        calls = []
        expected_session_id = _stable_claude_session_id("retrieve", "agent-1")

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'{{"type":"result","session_id":"{expected_session_id}","result":"done {len(calls)}"}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(Path(tempdir), "retrieve", AgentCliConfig(provider="claude"))

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = executor.run_session_turn("agent-1", "remember")
                second = executor.run_session_turn("agent-1", "more")

        self.assertEqual(first, "done 1")
        self.assertEqual(second, "done 2")
        self.assertIn("--session-id", calls[0])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], expected_session_id)
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], expected_session_id)

    def test_claude_new_retrieve_forks_reusable_prefix_then_resumes_child(self):
        calls = []
        base_session_id = "123e4567-e89b-12d3-a456-426614174100"
        child_session_id = "123e4567-e89b-12d3-a456-426614174200"
        replies = [
            (base_session_id, EMPTY_RETRIEVE_SELECTION_JSON),
            (child_session_id, "first"),
            (child_session_id, "second"),
        ]

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            session_id, result = replies[len(calls) - 1]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"type": "result", "session_id": session_id, "result": result}
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="claude"))
            with (
                patch(
                    "rightmemory.agent_cli.uuid4",
                    side_effect=[UUID(base_session_id), UUID(child_session_id)],
                ),
                patch("rightmemory.agent_cli.subprocess.run", fake_run),
            ):
                first = executor.run_session_turn(
                    "agent-1",
                    "first query",
                    prefix_context="stable snapshot",
                )
                second = executor.run_session_turn("agent-1", "follow-up")

            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")
            child = ProviderThreadStore(root).load("claude", child_session_id)

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertIn("--session-id", calls[0])
        self.assertIn("--fork-session", calls[1])
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], base_session_id)
        self.assertEqual(calls[1][calls[1].index("--session-id") + 1], child_session_id)
        self.assertNotIn("--fork-session", calls[2])
        self.assertEqual(calls[2][calls[2].index("--resume") + 1], child_session_id)
        self.assertEqual(mapping.provider_session_id, child_session_id)
        self.assertEqual(child.forked_from_provider_session_id, base_session_id)

    def test_claude_fork_failure_falls_back_with_preowned_child(self):
        calls = []
        base_session_id = "123e4567-e89b-12d3-a456-426614174100"
        child_session_id = "123e4567-e89b-12d3-a456-426614174200"
        direct_session_id = _stable_claude_session_id("retrieve", "agent-1")

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {
                            "type": "result",
                            "session_id": base_session_id,
                            "result": EMPTY_RETRIEVE_SELECTION_JSON,
                        }
                    ),
                    stderr="",
                )
            if len(calls) == 2:
                return subprocess.CompletedProcess(
                    command,
                    7,
                    stdout="",
                    stderr="connection lost after fork",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "session_id": direct_session_id,
                        "result": "direct",
                    }
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="claude"))
            with (
                patch(
                    "rightmemory.agent_cli.uuid4",
                    side_effect=[UUID(base_session_id), UUID(child_session_id)],
                ),
                patch("rightmemory.agent_cli.subprocess.run", fake_run),
                patch("rightmemory.agent_cli.print") as warning,
            ):
                result = executor.run_session_turn(
                    "agent-1",
                    "query",
                    prefix_context="stable snapshot",
                )

            child = ProviderThreadStore(root).load("claude", child_session_id)
            base = ProviderThreadStore(root).load("claude", base_session_id)
            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")
            prefix_records = list(
                (root / ".runtime" / "agent_cli_prefixes" / "claude").glob("*.json")
            )

        self.assertEqual(result, "direct")
        self.assertEqual(calls[1][calls[1].index("--session-id") + 1], child_session_id)
        self.assertEqual(child.forked_from_provider_session_id, base_session_id)
        self.assertIsNone(child.last_successful_activity_at)
        self.assertEqual(base.status, "delete-pending")
        self.assertEqual(mapping.provider_session_id, direct_session_id)
        self.assertEqual(prefix_records, [])
        warning.assert_called_once()

    def test_claude_fork_accepts_and_owns_returned_child_id_mismatch(self):
        calls = []
        base_session_id = "123e4567-e89b-12d3-a456-426614174100"
        requested_child_session_id = "123e4567-e89b-12d3-a456-426614174200"
        returned_child_session_id = "123e4567-e89b-12d3-a456-426614174300"
        replies = [
            (base_session_id, EMPTY_RETRIEVE_SELECTION_JSON),
            (returned_child_session_id, "result"),
        ]

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            session_id, result = replies[len(calls) - 1]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"type": "result", "session_id": session_id, "result": result}
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="claude"))
            with (
                patch(
                    "rightmemory.agent_cli.uuid4",
                    side_effect=[
                        UUID(base_session_id),
                        UUID(requested_child_session_id),
                    ],
                ),
                patch("rightmemory.agent_cli.subprocess.run", fake_run),
            ):
                result = executor.run_session_turn(
                    "agent-1",
                    "query",
                    prefix_context="stable snapshot",
                )

            requested = ProviderThreadStore(root).load(
                "claude",
                requested_child_session_id,
            )
            returned = ProviderThreadStore(root).load(
                "claude",
                returned_child_session_id,
            )
            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")

        self.assertEqual(result, "result")
        self.assertEqual(requested.status, "delete-pending")
        self.assertEqual(returned.forked_from_provider_session_id, base_session_id)
        self.assertIsNotNone(returned.last_successful_activity_at)
        self.assertEqual(mapping.provider_session_id, returned_child_session_id)

    def test_fresh_provider_session_uses_new_claude_uuid(self):
        calls = []
        stable_session_id = _stable_claude_session_id("update", "agent-1")
        fresh_session_id = "123e4567-e89b-12d3-a456-426614174999"

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'{{"type":"result","session_id":"{fresh_session_id}","result":"done"}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(
                Path(tempdir),
                "update",
                AgentCliConfig(provider="claude"),
                fresh_provider_session=True,
            )

            with (
                patch("rightmemory.agent_cli.uuid4", return_value=UUID(fresh_session_id)),
                patch("rightmemory.agent_cli.subprocess.run", fake_run),
            ):
                result = executor.run_session_turn("agent-1", "remember")

        self.assertEqual(result, "done")
        self.assertIn("--session-id", calls[0])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], fresh_session_id)
        self.assertNotEqual(fresh_session_id, stable_session_id)

    def test_non_retrieve_session_turns_are_one_shot_for_claude(self):
        calls = []
        session_ids = [
            "123e4567-e89b-12d3-a456-426614174001",
            "123e4567-e89b-12d3-a456-426614174002",
        ]

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            session_id = command[command.index("--session-id") + 1]
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'{{"type":"result","session_id":"{session_id}","result":"done"}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(Path(tempdir), "update", AgentCliConfig(provider="claude"))
            with (
                patch("rightmemory.agent_cli.uuid4", side_effect=[UUID(value) for value in session_ids]),
                patch("rightmemory.agent_cli.subprocess.run", fake_run),
            ):
                executor.run_session_turn("agent-1", "first")
                executor.run_session_turn("agent-1", "second")

            mapping = ProviderSessionStore(Path(tempdir), "update").load("agent-1")
            records = ProviderThreadStore(Path(tempdir)).scan("claude").records

        self.assertIsNone(mapping)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.policy == "one-shot" for record in records))
        self.assertTrue(all("--resume" not in command for command in calls))

    def test_process_local_turn_resumes_only_within_executor(self):
        runner = FakeCodexRunner(outputs=["done", "done", "done"])
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = CliAgentExecutor(
                root,
                "reviewer",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )
            first.run_process_turn("one")
            first.run_process_turn("two")
            CliAgentExecutor(
                root,
                "reviewer",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            ).run_process_turn("three")

            records = ProviderThreadStore(root).scan("codex").records

        self.assertIsNone(runner.calls[0]["provider_session_id"])
        self.assertEqual(runner.calls[1]["provider_session_id"], "thread-1")
        self.assertIsNone(runner.calls[2]["provider_session_id"])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.policy == "process-local" for record in records))

    def test_unregistered_legacy_retrieve_mapping_is_not_resumed(self):
        runner = FakeCodexRunner()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ProviderSessionStore(root, "retrieve").save(
                ProviderSessionRecord(
                    provider="codex",
                    provider_session_id="legacy-thread",
                    role="retrieve",
                    rightmemory_session_id="agent-1",
                    created_at="2026-07-17T00:00:00+00:00",
                    updated_at="2026-07-17T00:00:00+00:00",
                )
            )
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )
            executor.run_session_turn("agent-1", "hello")

            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")

        self.assertIsNone(runner.calls[0]["provider_session_id"])
        self.assertEqual(mapping.provider_session_id, "thread-1")

    def test_expired_retrieve_mapping_is_not_resumed_when_cleanup_itself_fails(self):
        runner = FakeCodexRunner()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ProviderThreadStore(root).record_created(
                provider="codex",
                provider_session_id="expired-thread",
                role="retrieve",
                rightmemory_session_id="agent-1",
                policy="persistent",
                created_at="2020-01-01T00:00:00+00:00",
            )
            ProviderSessionStore(root, "retrieve").save(
                ProviderSessionRecord(
                    provider="codex",
                    provider_session_id="expired-thread",
                    role="retrieve",
                    rightmemory_session_id="agent-1",
                    created_at="2020-01-01T00:00:00+00:00",
                    updated_at="2020-01-01T00:00:00+00:00",
                )
            )
            with patch("rightmemory.agent_cli.AgentCliThreadCleanup") as cleanup:
                cleanup.return_value.has_expired_codex_threads.return_value = True
                cleanup.return_value.run.side_effect = OSError("cleanup unavailable")
                executor = CliAgentExecutor(
                    root,
                    "retrieve",
                    AgentCliConfig(provider="codex"),
                    codex_runner=runner,
                )
            executor.run_session_turn("agent-1", "hello")

        self.assertIsNone(runner.calls[0]["provider_session_id"])

    def test_failed_codex_turn_registers_ownership_before_turn_failure(self):
        runner = FakeCodexRunner(turn_error=RuntimeError("failed later"))
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "update",
                AgentCliConfig(provider="codex"),
                codex_runner=runner,
            )
            with self.assertRaises(RuntimeError):
                executor.run_session_turn("agent-1", "hello")

            ownership = ProviderThreadStore(root).load("codex", "thread-1")

        self.assertEqual(ownership.policy, "one-shot")
        self.assertIsNone(ownership.last_successful_activity_at)

    def test_claude_cli_failure_includes_stdout_and_stderr(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            return subprocess.CompletedProcess(command, 7, stdout="partial output", stderr="bad credentials")

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(Path(tempdir), "retrieve", AgentCliConfig(provider="claude"))
            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                with self.assertRaises(RuntimeError) as caught:
                    executor.run_session_turn("agent-1", "hello")

        message = str(caught.exception)
        self.assertIn("Claude CLI exited with status 7", message)
        self.assertIn("stderr: bad credentials", message)
        self.assertIn("stdout: partial output", message)


class AgentCliDoctorTests(unittest.TestCase):
    def test_doctor_resolves_codex_sdk_and_its_bundled_runtime(self):
        binary = Path("C:/bundled/codex.exe")
        with (
            patch("codex_cli_bin.bundled_codex_path", return_value=binary),
            patch("openai_codex.__version__", "0.147.0"),
        ):
            resolved = _provider_runtime("codex")

        self.assertEqual(resolved, f"codex-sdk-0.147.0:{binary}")

    def test_doctor_does_not_fall_back_to_global_codex_when_bundled_runtime_is_missing(self):
        with patch(
            "codex_cli_bin.bundled_codex_path",
            side_effect=FileNotFoundError("bundled runtime missing"),
        ):
            with self.assertRaises(FileNotFoundError):
                _provider_runtime("codex")

    def test_format_doctor_report(self):
        report = format_doctor_report(
            [
                DoctorCheck("first", True, "fine"),
                DoctorCheck("second", False, "bad"),
            ]
        )

        self.assertEqual(report, "[ok] first - fine\n[fail] second - bad")

    def test_doctor_reports_config_failure_without_provider_calls(self):
        def fake_load_config(role: str, memory_root=None):
            if role == "retrieve":
                return RuntimeConfig(role=role, model_id="openai/test")
            return _doctor_config(role)

        with patch("rightmemory.doctor.load_config", side_effect=fake_load_config):
            checks = run_agent_cli_doctor()

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "role configs")
        self.assertFalse(checks[0].ok)
        self.assertIn("retrieve", checks[0].detail)

    def test_first_provider_call_accepts_retrieve_no_match_result(self):
        checks = []

        with patch("rightmemory.doctor._runtime_turn", return_value="No strong match."):
            _check_first_provider_calls(checks, {"retrieve": _doctor_config("retrieve")}, "nonce")

        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].ok)

    def test_resume_check_verifies_provider_thread_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = _doctor_config("retrieve", root)
            checks = []

            def fake_runtime_turn(runtime_config, session_id, message):
                ProviderSessionStore(root, "retrieve").save(
                    ProviderSessionRecord(
                        provider="codex",
                        provider_session_id="thread-1",
                        role="retrieve",
                        rightmemory_session_id=session_id,
                        created_at="2026-08-17T00:00:00+00:00",
                        updated_at="2026-08-17T00:01:00+00:00",
                    )
                )
                return "No strong match."

            with patch("rightmemory.doctor._runtime_turn", side_effect=fake_runtime_turn):
                _check_resume_provider_thread(checks, config, "nonce")

        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].ok)


def _doctor_config(role: str, memory_root=None) -> RuntimeConfig:
    root = Path(memory_root) if memory_root is not None else Path(f"/real/{role}")
    return RuntimeConfig(
        role=role,
        runtime_mode="cli-agent",
        agent_cli=AgentCliConfig(provider="codex", model=f"model-{role}"),
        memory_root=root,
        sync=SyncConfig(memory_root=root, enabled=True),
    )


if __name__ == "__main__":
    unittest.main()
