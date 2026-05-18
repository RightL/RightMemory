import tempfile
import unittest
from pathlib import Path

from rightmemory.agent_cli import (
    build_claude_command,
    build_codex_command,
    parse_claude_output,
    parse_codex_output,
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


if __name__ == "__main__":
    unittest.main()
