import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.agent_cli import (
    CliAgentExecutor,
    NO_SESSION_RIGHTMEMORY_SESSION_ID,
    build_claude_command,
    build_codex_command,
    parse_claude_output,
    parse_codex_output,
    _stable_claude_session_id,
)
from rightmemory.config import AgentCliConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore


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
    def test_build_codex_first_command_uses_memory_root_and_read_only_sandbox(self):
        config = AgentCliConfig(provider="codex", model="gpt-5")

        command = build_codex_command(Path("/memory/root"), "retrieve", config, "prompt", None)

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                "/memory/root",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5",
                "prompt",
            ],
        )

    def test_build_codex_first_command_uses_workspace_write_for_write_role(self):
        command = build_codex_command(
            Path("/memory/root"),
            "update",
            AgentCliConfig(provider="codex"),
            "prompt",
            None,
        )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                "/memory/root",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "prompt",
            ],
        )

    def test_build_codex_resume_command_uses_provider_session_id(self):
        config = AgentCliConfig(provider="codex", model="gpt-5")

        command = build_codex_command(Path("/memory/root"), "retrieve", config, "prompt", "thread-1")

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                "/memory/root",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                "gpt-5",
                "resume",
                "thread-1",
                "prompt",
            ],
        )

    def test_build_codex_resume_command_uses_workspace_write_for_write_role(self):
        command = build_codex_command(
            Path("/memory/root"),
            "update",
            AgentCliConfig(provider="codex"),
            "prompt",
            "thread-1",
        )

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--json",
                "--cd",
                "/memory/root",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "resume",
                "thread-1",
                "prompt",
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

    def test_build_claude_command_rejects_non_uuid_session_id(self):
        with self.assertRaises(ValueError) as caught:
            build_claude_command("retrieve", AgentCliConfig(provider="claude"), "prompt", "uuid-1", False)

        self.assertIn("UUID", str(caught.exception))


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
    def test_run_turn_saves_provider_session_record(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started","thread_id":"thread-chat"}\n'
                    '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            executor = CliAgentExecutor(root, "retrieve", AgentCliConfig(provider="codex"))

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                result = executor.run_turn("hello")

            record = ProviderSessionStore(root, "retrieve").load(NO_SESSION_RIGHTMEMORY_SESSION_ID)
            is_internal = ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-chat")

        self.assertEqual(result, "done")
        self.assertIsNotNone(record)
        self.assertEqual(record.provider_session_id, "thread-chat")
        self.assertEqual(record.rightmemory_session_id, NO_SESSION_RIGHTMEMORY_SESSION_ID)
        self.assertTrue(is_internal)

    def test_run_turn_resumes_saved_provider_session_in_second_executor(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
            calls.append(command)
            text_out = "first" if len(calls) == 1 else "second"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    '{"type":"thread.started","thread_id":"thread-chat"}\n'
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
        self.assertEqual(calls[1][-3:], ["resume", "thread-chat", calls[1][-1]])

    def test_codex_session_turn_saves_and_resumes_provider_session(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
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
        self.assertEqual(calls[1][-3:], ["resume", "thread-1", calls[1][-1]])
        self.assertIn("Caller message:\nhello", calls[0][-1])
        self.assertIn("You are RightMemory retrieve mode.", calls[0][-1])

    def test_claude_first_turn_uses_stable_uuid_then_resumes(self):
        calls = []
        expected_session_id = _stable_claude_session_id("update", "agent-1")

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'{{"type":"result","session_id":"{expected_session_id}","result":"done {len(calls)}"}}',
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tempdir:
            executor = CliAgentExecutor(Path(tempdir), "update", AgentCliConfig(provider="claude"))

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = executor.run_session_turn("agent-1", "remember")
                second = executor.run_session_turn("agent-1", "more")

        self.assertEqual(first, "done 1")
        self.assertEqual(second, "done 2")
        self.assertIn("--session-id", calls[0])
        self.assertEqual(calls[0][calls[0].index("--session-id") + 1], expected_session_id)
        self.assertIn("--resume", calls[1])
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], expected_session_id)

    def test_cli_failure_includes_stdout_and_stderr(self):
        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
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


if __name__ == "__main__":
    unittest.main()
