import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from rightmemory.doctor import DoctorCheck, format_doctor_report, run_agent_cli_doctor
from rightmemory.agent_cli import (
    CliAgentExecutor,
    NO_SESSION_RIGHTMEMORY_SESSION_ID,
    build_claude_command,
    build_codex_command,
    parse_claude_output,
    parse_codex_output,
    _stable_claude_session_id,
)
from rightmemory.config import ROLES, AgentCliConfig, RuntimeConfig, SyncConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.semantic_upgrades import SemanticUpgradeContext, SemanticUpgradeNote


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

    def test_build_codex_uses_workspace_write_for_pruner(self):
        command = build_codex_command(
            Path("/memory/root"),
            "pruner",
            AgentCliConfig(provider="codex"),
            "prompt",
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_build_codex_uses_read_only_for_historian(self):
        command = build_codex_command(
            Path("/memory/root"),
            "historian",
            AgentCliConfig(provider="codex"),
            "prompt",
            None,
        )

        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)

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

    def test_cli_agent_executor_includes_semantic_upgrades_for_dreamer_prompt(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nReconsider older memory.",
                    source="example.md",
                )
            ],
            warnings=[],
        )
        prompts = []

        def fake_build_codex_command(memory_root, role, config, prompt, provider_session_id):
            prompts.append(prompt)
            return ["codex"]

        with tempfile.TemporaryDirectory() as tempdir:
            with (
                patch("rightmemory.agent_cli.build_codex_command", fake_build_codex_command),
                patch(
                    "rightmemory.agent_cli._run_cli",
                    return_value=(
                        '{"type":"thread.started","thread_id":"t1"}\n'
                        '{"item":{"type":"agent_message","text":"done"}}\n'
                    ),
                ),
            ):
                executor = CliAgentExecutor(
                    Path(tempdir),
                    "dreamer",
                    AgentCliConfig(provider="codex"),
                    semantic_upgrades=context,
                )
                result = executor.run_session_turn("dreamer-session", "run")

        self.assertEqual(result, "done")
        self.assertIn("example-note", prompts[0])
        self.assertIn("Reconsider older memory.", prompts[0])

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

    def test_run_turn_records_provider_session_under_state_root(self):
        calls = []

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
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

    def test_fresh_provider_session_uses_new_claude_uuid(self):
        calls = []
        stable_session_id = _stable_claude_session_id("update", "agent-1")
        fresh_session_id = "123e4567-e89b-12d3-a456-426614174999"

        def fake_run(command, cwd=None, capture_output=None, text=None, check=None):
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


class AgentCliDoctorTests(unittest.TestCase):
    def test_format_doctor_report(self):
        report = format_doctor_report(
            [
                DoctorCheck("first", True, "fine"),
                DoctorCheck("second", False, "bad"),
            ]
        )

        self.assertEqual(report, "[ok] first - fine\n[fail] second - bad")

    def test_doctor_runs_checks_against_temporary_memory_root(self):
        runtime_configs = []
        turn_calls = []
        history = {}

        class FakeRuntime:
            def __init__(self, config):
                runtime_configs.append(config)
                self.config = config

            def run_session_turn(self, session_id: str, message: str) -> str:
                turn_calls.append((self.config.role, self.config.agent_cli.provider, session_id, message))
                memory_root = self.config.memory_root
                if "Reply exactly `RM_FIRST_" in message:
                    return _token_after(message, "Reply exactly `")
                if "Remember this doctor token" in message:
                    token = _token_after(message, "token for the next check: `")
                    history[(self.config.role, session_id)] = token
                    return f"READY {token}"
                if "What doctor token" in message:
                    return history[(self.config.role, session_id)]
                if "DOCTOR_RETRIEVE_TOKEN" in message:
                    return _token_after((memory_root / "MEMORY.md").read_text(encoding="utf-8"), "DOCTOR_RETRIEVE_TOKEN: ")
                if "Append this exact line" in message:
                    line = _token_after(message, "Append this exact line to MEMORY.md: `")
                    with (memory_root / "MEMORY.md").open("a", encoding="utf-8") as handle:
                        handle.write(f"\n{line}\n")
                    return "WROTE"
                if "Run git status" in message:
                    commit_message = _token_after(message, "commit with message `")
                    subprocess.run(["git", "status", "--short"], cwd=memory_root, check=True, capture_output=True)
                    subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", commit_message],
                        cwd=memory_root,
                        check=True,
                        capture_output=True,
                    )
                    return "COMMITTED"
                if "outside the memory root" in message:
                    return "BOUNDARY_BLOCKED"
                raise AssertionError(f"unexpected doctor prompt: {message}")

            def cleanup(self):
                pass

        with (
            patch("rightmemory.doctor.load_config", side_effect=_doctor_config),
            patch("rightmemory.doctor.shutil.which", return_value="/usr/bin/codex"),
            patch("rightmemory.doctor.RightMemoryRuntime", FakeRuntime),
        ):
            checks = run_agent_cli_doctor()

        self.assertTrue(all(check.ok for check in checks), checks)
        self.assertEqual([check.name for check in checks], [
            "role configs",
            "provider CLI binaries",
            "temporary Git memory repo",
            "first provider call",
            "resume history",
            "retrieve reads memory",
            "write role edits memory",
            "write role commits memory",
            "write boundary",
        ])
        self.assertTrue(runtime_configs)
        self.assertTrue(all(config.memory_root.name == "memory" for config in runtime_configs))
        self.assertTrue(all(config.state_root == config.memory_root for config in runtime_configs))
        self.assertTrue(all(not config.sync.enabled for config in runtime_configs))
        first_call_roles = {role for role, provider, session_id, message in turn_calls if "Reply exactly `RM_FIRST_" in message}
        self.assertEqual(first_call_roles, ROLES)
        first_call_check = next(check for check in checks if check.name == "first provider call")
        for role in ROLES:
            self.assertIn(f"{role}:codex", first_call_check.detail)
        session_ids = [session_id for role, provider, session_id, message in turn_calls]
        self.assertTrue(all(session_id.startswith("doctor-") for session_id in session_ids))
        nonce_parts = {session_id.split("-", 2)[1] for session_id in session_ids}
        self.assertEqual(len(nonce_parts), 1)
        self.assertTrue(all(len(nonce) == 32 for nonce in nonce_parts))

    def test_doctor_reports_config_failure_without_provider_calls(self):
        def fake_load_config(role: str):
            if role == "retrieve":
                return RuntimeConfig(role=role, model_id="openai/test")
            return _doctor_config(role)

        with patch("rightmemory.doctor.load_config", side_effect=fake_load_config):
            checks = run_agent_cli_doctor()

        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].name, "role configs")
        self.assertFalse(checks[0].ok)
        self.assertIn("retrieve", checks[0].detail)

    def test_doctor_fails_boundary_if_sibling_file_is_created(self):
        history = {}

        class FakeRuntime:
            def __init__(self, config):
                self.config = config

            def run_session_turn(self, session_id: str, message: str) -> str:
                memory_root = self.config.memory_root
                if "Reply exactly `RM_FIRST_" in message:
                    return _token_after(message, "Reply exactly `")
                if "Remember this doctor token" in message:
                    token = _token_after(message, "token for the next check: `")
                    history[(self.config.role, session_id)] = token
                    return f"READY {token}"
                if "What doctor token" in message:
                    return history[(self.config.role, session_id)]
                if "DOCTOR_RETRIEVE_TOKEN" in message:
                    return _token_after((memory_root / "MEMORY.md").read_text(encoding="utf-8"), "DOCTOR_RETRIEVE_TOKEN: ")
                if "Append this exact line" in message:
                    line = _token_after(message, "Append this exact line to MEMORY.md: `")
                    with (memory_root / "MEMORY.md").open("a", encoding="utf-8") as handle:
                        handle.write(f"\n{line}\n")
                    return "WROTE"
                if "Run git status" in message:
                    commit_message = _token_after(message, "commit with message `")
                    subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", commit_message],
                        cwd=memory_root,
                        check=True,
                        capture_output=True,
                    )
                    return "COMMITTED"
                if "outside the memory root" in message:
                    target = Path(message.split("outside the memory root: ", 1)[1].splitlines()[0])
                    target.write_text("bad", encoding="utf-8")
                    return "CREATED"
                raise AssertionError(f"unexpected doctor prompt: {message}")

            def cleanup(self):
                pass

        with (
            patch("rightmemory.doctor.load_config", side_effect=_doctor_config),
            patch("rightmemory.doctor.shutil.which", return_value="/usr/bin/codex"),
            patch("rightmemory.doctor.RightMemoryRuntime", FakeRuntime),
        ):
            checks = run_agent_cli_doctor()

        boundary = checks[-1]
        self.assertEqual(boundary.name, "write boundary")
        self.assertFalse(boundary.ok)
        self.assertIn("outside file was created", boundary.detail)

    def test_doctor_fails_boundary_on_generic_runtime_exception(self):
        history = {}

        class FakeRuntime:
            def __init__(self, config):
                self.config = config

            def run_session_turn(self, session_id: str, message: str) -> str:
                memory_root = self.config.memory_root
                if "Reply exactly `RM_FIRST_" in message:
                    return _token_after(message, "Reply exactly `")
                if "Remember this doctor token" in message:
                    token = _token_after(message, "token for the next check: `")
                    history[(self.config.role, session_id)] = token
                    return f"READY {token}"
                if "What doctor token" in message:
                    return history[(self.config.role, session_id)]
                if "DOCTOR_RETRIEVE_TOKEN" in message:
                    return _token_after((memory_root / "MEMORY.md").read_text(encoding="utf-8"), "DOCTOR_RETRIEVE_TOKEN: ")
                if "Append this exact line" in message:
                    line = _token_after(message, "Append this exact line to MEMORY.md: `")
                    with (memory_root / "MEMORY.md").open("a", encoding="utf-8") as handle:
                        handle.write(f"\n{line}\n")
                    return "WROTE"
                if "Run git status" in message:
                    commit_message = _token_after(message, "commit with message `")
                    subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", commit_message],
                        cwd=memory_root,
                        check=True,
                        capture_output=True,
                    )
                    return "COMMITTED"
                if "outside the memory root" in message:
                    raise RuntimeError("provider crashed before making a tool call")
                raise AssertionError(f"unexpected doctor prompt: {message}")

            def cleanup(self):
                pass

        with (
            patch("rightmemory.doctor.load_config", side_effect=_doctor_config),
            patch("rightmemory.doctor.shutil.which", return_value="/usr/bin/codex"),
            patch("rightmemory.doctor.RightMemoryRuntime", FakeRuntime),
        ):
            checks = run_agent_cli_doctor()

        boundary = checks[-1]
        self.assertEqual(boundary.name, "write boundary")
        self.assertFalse(boundary.ok)
        self.assertIn("provider crashed", boundary.detail)

    def test_doctor_accepts_boundary_denial_exception_when_sibling_file_is_absent(self):
        history = {}

        class FakeRuntime:
            def __init__(self, config):
                self.config = config

            def run_session_turn(self, session_id: str, message: str) -> str:
                memory_root = self.config.memory_root
                if "Reply exactly `RM_FIRST_" in message:
                    return _token_after(message, "Reply exactly `")
                if "Remember this doctor token" in message:
                    token = _token_after(message, "token for the next check: `")
                    history[(self.config.role, session_id)] = token
                    return f"READY {token}"
                if "What doctor token" in message:
                    return history[(self.config.role, session_id)]
                if "DOCTOR_RETRIEVE_TOKEN" in message:
                    return _token_after((memory_root / "MEMORY.md").read_text(encoding="utf-8"), "DOCTOR_RETRIEVE_TOKEN: ")
                if "Append this exact line" in message:
                    line = _token_after(message, "Append this exact line to MEMORY.md: `")
                    with (memory_root / "MEMORY.md").open("a", encoding="utf-8") as handle:
                        handle.write(f"\n{line}\n")
                    return "WROTE"
                if "Run git status" in message:
                    commit_message = _token_after(message, "commit with message `")
                    subprocess.run(["git", "add", "MEMORY.md"], cwd=memory_root, check=True, capture_output=True)
                    subprocess.run(
                        ["git", "commit", "-m", commit_message],
                        cwd=memory_root,
                        check=True,
                        capture_output=True,
                    )
                    return "COMMITTED"
                if "outside the memory root" in message:
                    raise RuntimeError("sandbox denied write outside workspace")
                raise AssertionError(f"unexpected doctor prompt: {message}")

            def cleanup(self):
                pass

        with (
            patch("rightmemory.doctor.load_config", side_effect=_doctor_config),
            patch("rightmemory.doctor.shutil.which", return_value="/usr/bin/codex"),
            patch("rightmemory.doctor.RightMemoryRuntime", FakeRuntime),
        ):
            checks = run_agent_cli_doctor()

        boundary = checks[-1]
        self.assertEqual(boundary.name, "write boundary")
        self.assertTrue(boundary.ok)
        self.assertIn("outside write blocked", boundary.detail)


def _doctor_config(role: str) -> RuntimeConfig:
    return RuntimeConfig(
        role=role,
        runtime_mode="cli-agent",
        agent_cli=AgentCliConfig(provider="codex", model=f"model-{role}"),
        memory_root=Path(f"/real/{role}"),
        sync=SyncConfig(memory_root=Path(f"/real/{role}"), enabled=True),
    )


def _token_after(text: str, prefix: str) -> str:
    tail = text.split(prefix, 1)[1]
    for delimiter in ("`", "\n"):
        if delimiter in tail:
            return tail.split(delimiter, 1)[0].strip()
    return tail.strip()


if __name__ == "__main__":
    unittest.main()
