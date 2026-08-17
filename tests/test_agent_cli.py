import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from rightmemory.doctor import (
    DoctorCheck,
    _check_first_provider_calls,
    _check_resume_provider_thread,
    format_doctor_report,
    run_agent_cli_doctor,
)
from rightmemory.agent_cli import (
    CliAgentExecutor,
    NO_SESSION_RIGHTMEMORY_SESSION_ID,
    build_claude_command,
    build_codex_command,
    parse_claude_output,
    parse_codex_output,
    _run_cli,
    _stable_claude_session_id,
)
from rightmemory.config import AgentCliConfig, RuntimeConfig, SyncConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.provider_threads import ProviderThreadStore


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
    def test_run_cli_uses_binary_stdin_when_supplied(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, stdout=b"done", stderr=b"")

        with (
            patch("rightmemory.agent_cli.prepare_command", side_effect=lambda command: list(command)),
            patch("rightmemory.agent_cli.subprocess.run", side_effect=fake_run),
        ):
            output = _run_cli(["codex", "exec"], Path("/memory/root"), "Codex", stdin="input")

        self.assertEqual(output, "done")
        self.assertIsInstance(captured.get("input"), bytes)

    def test_build_codex_first_command_uses_memory_root_and_read_only_sandbox(self):
        config = AgentCliConfig(provider="codex", model="gpt-5")
        memory_root = Path("/memory/root")

        command = build_codex_command(memory_root, "retrieve", config, None)

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                str(memory_root),
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5",
            ],
        )

    def test_build_codex_first_command_uses_workspace_write_for_write_role(self):
        memory_root = Path("/memory/root")
        command = build_codex_command(
            memory_root,
            "update",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                str(memory_root),
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
            ],
        )

    def test_build_codex_command_applies_role_reasoning_effort(self):
        command = build_codex_command(
            Path("/memory/root"),
            "update",
            AgentCliConfig(provider="codex", model="gpt-5.6-sol", reasoning_effort="xhigh"),
            None,
        )

        self.assertEqual(
            command[command.index("--model") :],
            [
                "--model",
                "gpt-5.6-sol",
                "--config",
                "model_reasoning_effort=xhigh",
            ],
        )

    def test_build_codex_uses_workspace_write_for_pruner(self):
        command = build_codex_command(
            Path("/memory/root"),
            "pruner",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_build_codex_uses_workspace_write_for_insight(self):
        command = build_codex_command(
            Path("/memory/root"),
            "insight",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertIn("workspace-write", command)

    def test_build_codex_uses_workspace_write_for_shared_view_builder(self):
        command = build_codex_command(
            Path("/memory/root"),
            "shared-view-builder",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_build_codex_uses_read_only_for_historian(self):
        command = build_codex_command(
            Path("/memory/root"),
            "historian",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)

    def test_build_codex_uses_read_only_for_reviewer(self):
        command = build_codex_command(
            Path("/memory/root"),
            "reviewer",
            AgentCliConfig(provider="codex"),
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)

    def test_build_codex_resume_command_uses_provider_session_id(self):
        config = AgentCliConfig(provider="codex", model="gpt-5")
        memory_root = Path("/memory/root")

        command = build_codex_command(memory_root, "retrieve", config, "thread-1")

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                str(memory_root),
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5",
                "resume",
                "thread-1",
            ],
        )

    def test_build_codex_resume_command_uses_workspace_write_for_write_role(self):
        memory_root = Path("/memory/root")
        command = build_codex_command(
            memory_root,
            "update",
            AgentCliConfig(provider="codex"),
            "thread-1",
        )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                str(memory_root),
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "resume",
                "thread-1",
            ],
        )

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
    def test_parse_codex_jsonl_output(self):
        output = (
            '{"type":"thread.started","thread_id":"thread-1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
        )

        parsed = parse_codex_output(output)

        self.assertEqual(parsed.provider_session_id, "thread-1")
        self.assertEqual(parsed.text, "done")

    def test_parse_codex_requires_thread_id(self):
        with self.assertRaises(RuntimeError) as caught:
            parse_codex_output('{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n')

        self.assertIn("thread_id", str(caught.exception))

    def test_parse_codex_requires_final_agent_message(self):
        with self.assertRaises(RuntimeError) as caught:
            parse_codex_output('{"type":"thread.started","thread_id":"thread-1"}\n')

        self.assertIn("final agent message", str(caught.exception))

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

    def test_run_turn_is_one_shot_and_records_thread_ownership(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started","thread_id":"thread-chat"}\n'
                    '{"type":"item.completed","item":{"type":"agent_message","text":"done 中文"}}\n'
                ).encode("utf-8"),
                stderr=b"",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                result = executor.run_turn("hello")

            record = ProviderSessionStore(root, "retrieve").load(NO_SESSION_RIGHTMEMORY_SESSION_ID)
            ownership = ProviderThreadStore(root).load("codex", "thread-chat")
            is_internal = ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-chat")

        self.assertEqual(result, "done 中文")
        self.assertIsNone(record)
        self.assertEqual(ownership.policy, "one-shot")
        self.assertEqual(ownership.rightmemory_session_id, NO_SESSION_RIGHTMEMORY_SESSION_ID)
        self.assertTrue(is_internal)

    def test_run_turn_records_provider_session_under_state_root(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append((command, cwd))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started","thread_id":"thread-state"}\n'
                    '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                ),
                stderr="",
            )

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
            )

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                result = executor.run_session_turn("agent-1", "hello")

            state_record = ProviderSessionStore(state_root, "retrieve").load("agent-1")
            memory_record = ProviderSessionStore(memory_root, "retrieve").load("agent-1")

        self.assertEqual(result, "done")
        self.assertEqual(calls[0][1], str(memory_root))
        self.assertIsNotNone(state_record)
        self.assertEqual(state_record.provider_session_id, "thread-state")
        self.assertIsNone(memory_record)

    def test_retrieve_stateless_turn_does_not_save_provider_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(
                root,
                "retrieve",
                AgentCliConfig(provider="codex"),
            )

            with patch(
                "rightmemory.agent_cli._run_cli",
                return_value=(
                    '{"type":"thread.started","thread_id":"thread-1"}\n'
                    '{"item":{"type":"agent_message","text":"reply"}}\n'
                ),
            ):
                result = executor.run_stateless_turn("snapshot\n\n# Query\n\nfind root")

            self.assertEqual(result, "reply")
            self.assertFalse((root / ".runtime" / "agent_cli_sessions" / "retrieve").exists())

    def test_run_turn_starts_fresh_thread_in_second_executor(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            number = len(calls)
            text_out = "first" if number == 1 else "second"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f'{{"type":"thread.started","thread_id":"thread-chat-{number}"}}\n'
                    f'{{"type":"item.completed","item":{{"type":"agent_message","text":"{text_out}"}}}}\n'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex")).run_turn("hello")
                second = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex")).run_turn("again")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertNotIn("resume", calls[0])
        self.assertNotIn("resume", calls[1])

    def test_codex_session_turn_saves_and_resumes_provider_session(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            text_out = "first" if len(calls) == 1 else "second"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started","thread_id":"thread-1"}\n'
                    f'{{"type":"item.completed","item":{{"type":"agent_message","text":"{text_out}"}}}}\n'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex", model="gpt-5"))

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = executor.run_session_turn("agent-1", "hello")
                second = executor.run_session_turn("agent-1", "again")

            record = ProviderSessionStore(root, "retrieve").load("agent-1")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertIsNotNone(record)
        self.assertEqual(record.provider_session_id, "thread-1")
        self.assertNotIn("resume", calls[0])
        self.assertEqual(calls[1][-2:], ["resume", "thread-1"])

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
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            calls.append(command)
            if "resume" in command:
                thread_id = command[command.index("resume") + 1]
            else:
                thread_id = f"thread-{len([call for call in calls if 'resume' not in call])}"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f'{{"type":"thread.started","thread_id":"{thread_id}"}}\n'
                    '{"item":{"type":"agent_message","text":"done"}}\n'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = CliAgentExecutor(root, "reviewer", AgentCliConfig(provider="codex"))
                first.run_process_turn("one")
                first.run_process_turn("two")
                CliAgentExecutor(root, "reviewer", AgentCliConfig(provider="codex")).run_process_turn("three")

            records = ProviderThreadStore(root).scan("codex").records

        self.assertNotIn("resume", calls[0])
        self.assertEqual(calls[1][-2:], ["resume", "thread-1"])
        self.assertNotIn("resume", calls[2])
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.policy == "process-local" for record in records))

    def test_unregistered_legacy_retrieve_mapping_is_not_resumed(self):
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
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))
            with patch(
                "rightmemory.agent_cli._run_cli",
                return_value=(
                    '{"type":"thread.started","thread_id":"new-thread"}\n'
                    '{"item":{"type":"agent_message","text":"done"}}\n'
                ),
            ) as run:
                executor.run_session_turn("agent-1", "hello")

            mapping = ProviderSessionStore(root, "retrieve").load("agent-1")

        self.assertNotIn("resume", run.call_args.args[0])
        self.assertEqual(mapping.provider_session_id, "new-thread")

    def test_expired_retrieve_mapping_is_not_resumed_when_cleanup_itself_fails(self):
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
                executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))
            with patch(
                "rightmemory.agent_cli._run_cli",
                return_value=(
                    '{"type":"thread.started","thread_id":"new-thread"}\n'
                    '{"item":{"type":"agent_message","text":"done"}}\n'
                ),
            ) as run:
                executor.run_session_turn("agent-1", "hello")

        self.assertNotIn("resume", run.call_args.args[0])

    def test_failed_codex_command_registers_partial_thread_started_event(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            return subprocess.CompletedProcess(
                command,
                7,
                stdout='{"type":"thread.started","thread_id":"failed-thread"}\n',
                stderr="failed later",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "update", AgentCliConfig(provider="codex"))
            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                with self.assertRaises(RuntimeError):
                    executor.run_session_turn("agent-1", "hello")

            ownership = ProviderThreadStore(root).load("codex", "failed-thread")

        self.assertEqual(ownership.policy, "one-shot")
        self.assertIsNone(ownership.last_successful_activity_at)

    def test_cli_failure_includes_stdout_and_stderr(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None, input=None):
            return subprocess.CompletedProcess(command, 7, stdout="partial output", stderr="bad credentials")

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(Path(tempdir), "retrieve", AgentCliConfig(provider="codex"))
            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                with self.assertRaises(RuntimeError) as caught:
                    executor.run_session_turn("agent-1", "hello")

        message = str(caught.exception)
        self.assertIn("Codex CLI exited with status 7", message)
        self.assertIn("stderr: bad credentials", message)
        self.assertIn("stdout: partial output", message)


class AgentCliDoctorTests(unittest.TestCase):
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
