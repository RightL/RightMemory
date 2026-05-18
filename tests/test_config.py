import tempfile
import types
import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.agent_cli import NO_SESSION_RIGHTMEMORY_SESSION_ID
from rightmemory.config import AgentCliConfig, RuntimeConfig, load_config, load_review_config, load_sync_config
from rightmemory.prompt import build_cli_agent_instructions, build_instructions
from rightmemory.runtime import RightMemoryRuntime, build_model
from rightmemory.sync import SyncResult


class ConfigTests(unittest.TestCase):
    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_minimal_openai_compatible_config(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "hosted_vllm//models/example-chat-model"
            api_base = "http://127.0.0.1:8000/v1"
            api_key = "token"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("retrieve")

        self.assertEqual(config.role, "retrieve")
        self.assertEqual(config.model_id, "hosted_vllm//models/example-chat-model")
        self.assertEqual(config.api_base, "http://127.0.0.1:8000/v1")
        self.assertEqual(config.api_key, "token")
        self.assertEqual(config.model_kwargs, {})
        self.assertEqual(config.runtime_mode, "standalone")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_anthropic_compatible_config(self):
        config_path = self._write_config(
            """
            [dreamer.model]
            model_id = "anthropic/example-dreamer-model"
            api_base = "https://api.example.com/anthropic"
            api_key = "token"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("dreamer")

        self.assertEqual(config.role, "dreamer")
        self.assertEqual(config.model_id, "anthropic/example-dreamer-model")
        self.assertEqual(config.api_base, "https://api.example.com/anthropic")
        self.assertEqual(config.api_key, "token")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_default_provider_with_role_model(self):
        config_path = self._write_config(
            """
            [agent_cli]
            provider = "codex"

            [retrieve.agent_cli]
            model = "gpt-5"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("retrieve")

        self.assertEqual(config.role, "retrieve")
        self.assertIsNone(config.model_id)
        self.assertEqual(config.runtime_mode, "cli-agent")
        self.assertEqual(config.agent_cli, AgentCliConfig(provider="codex", model="gpt-5"))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_role_provider_override(self):
        config_path = self._write_config(
            """
            [agent_cli]
            provider = "codex"

            [dreamer.agent_cli]
            provider = "claude"
            model = "claude-opus-4"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("dreamer")

        self.assertEqual(config.runtime_mode, "cli-agent")
        self.assertEqual(config.agent_cli, AgentCliConfig(provider="claude", model="claude-opus-4"))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_missing_provider_error(self):
        config_path = self._write_config(
            """
            [retrieve.agent_cli]
            model = "gpt-5"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("[agent_cli].provider", str(caught.exception))
        self.assertIn("[retrieve.agent_cli].provider", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_rejects_role_model_and_agent_cli(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "openai/fast"

            [retrieve.agent_cli]
            provider = "codex"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("[retrieve] must not define both [retrieve.model] and [retrieve.agent_cli]", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_nested_model_kwargs(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "hosted_vllm//models/example-chat-model"

            [update.model.kwargs]
            extra_body = { chat_template_kwargs = { thinking = true, preserve_thinking = true } }
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("update")

        self.assertEqual(
            config.model_kwargs,
            {"extra_body": {"chat_template_kwargs": {"thinking": True, "preserve_thinking": True}}},
        )

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_missing_model_id(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            api_base = "http://127.0.0.1:8000/v1"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError):
                load_config("retrieve")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_rejects_global_model_config(self):
        config_path = self._write_config(
            """
            [model]
            model_id = "anthropic/claude-test"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError):
                load_config("retrieve")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_rejects_runtime_section(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "anthropic/claude-test"

            [runtime]
            mode = "retrieve"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError):
                load_config("retrieve")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_rejects_legacy_curator_section(self):
        config_path = self._write_config(
            """
            [curator.model]
            model_id = "openai/legacy"

            [retrieve.model]
            model_id = "openai/fast"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("unsupported top-level config key(s): curator", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_reviewer_config(self):
        config_path = self._write_config(
            """
            [reviewer.model]
            model_id = "openai/reviewer"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("reviewer")

        self.assertEqual(config.role, "reviewer")
        self.assertEqual(config.model_id, "openai/reviewer")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_debug_trace_config(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "openai/fast"

            [debug]
            trace = true
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("retrieve")

        self.assertTrue(config.debug_trace)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_rejects_unknown_debug_key(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "openai/fast"

            [debug]
            trace = true
            format = "jsonl"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("unsupported [debug] config key(s): format", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_review_config_sources(self):
        config_path = self._write_config(
            """
            [review]
            idle_seconds = 7200
            since_days = 14

            [[review.sources]]
            kind = "codex"
            path = "~/codex-history"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_review_config()

        self.assertEqual(config.idle_seconds, 7200)
        self.assertEqual(config.since_days, 14)
        self.assertEqual(len(config.sources), 1)
        self.assertEqual(config.sources[0].kind, "codex")
        self.assertEqual(config.sources[0].path, Path("~/codex-history").expanduser())

    def test_review_config_defaults_to_three_day_window(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            config_path = memory_root / "rightmemory.toml"

            with patch("rightmemory.config.MEMORY_ROOT", memory_root), patch("rightmemory.config.CONFIG_PATH", config_path):
                config = load_review_config()

        self.assertEqual(config.since_days, 3)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_review_config_allows_sync_section(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true

            [review]
            idle_seconds = 7200
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_review_config()

        self.assertEqual(config.idle_seconds, 7200)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_defaults_to_disabled(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_sync_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 24)
        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_enabled(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true
            stale_pull_after_hours = 12
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_sync_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 12)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_rejects_bool_stale_pull_after_hours(self):
        config_path = self._write_config(
            """
            [sync]
            stale_pull_after_hours = true
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_sync_config()

        self.assertIn("[sync].stale_pull_after_hours must be a positive integer", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_rejects_unknown_key(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true
            remote = "origin"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_sync_config()

        self.assertIn("unsupported [sync] config key(s): remote", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_load_config_allows_sync_section_and_reconciler_role(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true

            [sync-reconciler.model]
            model_id = "openai/reconciler"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("sync-reconciler")

        self.assertEqual(config.role, "sync-reconciler")
        self.assertEqual(config.model_id, "openai/reconciler")
        self.assertTrue(config.sync.enabled)

    def _write_config(self, content: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        with handle:
            handle.write(content)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def test_builds_openai_compatible_model(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="hosted_vllm//models/example-chat-model",
            api_base="http://127.0.0.1:8000/v1",
            api_key="token",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            model = build_model(config)

        self.assertEqual(model.model_name, "/models/example-chat-model")
        self.assertEqual(model.provider.kwargs, {"base_url": "http://127.0.0.1:8000/v1", "api_key": "token"})

    def test_builds_anthropic_model(self):
        config = RuntimeConfig(
            role="dreamer",
            model_id="anthropic/example-dreamer-model",
            api_base="https://api.example.com/anthropic",
            api_key="token",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            model = build_model(config)

        self.assertEqual(model.model_name, "example-dreamer-model")
        self.assertEqual(
            model.provider.kwargs,
            {"base_url": "https://api.example.com/anthropic", "api_key": "token"},
        )

    def test_cli_agent_runtime_uses_executor_without_pydantic_agent(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_session_turn.return_value = "cli reply"
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "cli reply")
        executor_class.assert_called_once_with(Path(self.tempdir.name), "retrieve", AgentCliConfig(provider="codex"))
        executor_class.return_value.run_session_turn.assert_called_once_with("agent-session", "remember one")

    def test_cli_agent_run_turn_uses_reserved_session_lock(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )
        events = []

        class FakeLockedSession:
            def __enter__(self):
                events.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock_exit")

        def locked(session_id):
            events.append(("locked", session_id))
            return FakeLockedSession()

        def run_session_turn(session_id, message):
            events.append(("agent", session_id, message))
            return "cli reply"

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_session_turn.side_effect = run_session_turn
            runtime = RightMemoryRuntime(config)
            runtime.sessions.locked = locked
            result = runtime.run_turn("remember one")

        self.assertEqual(result, "cli reply")
        self.assertEqual(
            events,
            [
                ("locked", NO_SESSION_RIGHTMEMORY_SESSION_ID),
                "lock_enter",
                ("agent", NO_SESSION_RIGHTMEMORY_SESSION_ID, "remember one"),
                "lock_exit",
            ],
        )

    def test_cli_agent_rejects_reserved_public_session_id(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with patch("rightmemory.runtime.CliAgentExecutor"):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(ValueError) as caught:
                runtime.run_session_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, "remember one")

        self.assertIn("reserved", str(caught.exception))

    def test_run_turn_preserves_message_history(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            model_kwargs={"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_turn("remember one")
            second = runtime.run_turn("what was that?")

        self.assertEqual(first, "reply 1")
        self.assertEqual(second, "reply 2")
        self.assertIsNone(runtime.agent.calls[0]["message_history"])
        self.assertEqual(runtime.agent.calls[1]["message_history"], ["message 1"])
        self.assertEqual(
            runtime.agent.calls[0]["model_settings"],
            {"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )
        self.assertEqual(runtime.agent.calls[0]["usage_limits"].request_limit, 100)

    def test_write_role_creates_memory_lock_and_gitignore(self):
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        self.assertTrue((Path(self.tempdir.name) / ".runtime" / "memory.lock").exists())
        self.assertEqual(
            (Path(self.tempdir.name) / ".gitignore").read_text(encoding="utf-8"),
            "*\n!MEMORY.md\n!MEMORY_*.md\n!dream_logs/\n!dream_logs/*.md\n",
        )

    def test_retrieve_role_does_not_create_memory_lock(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        self.assertFalse((Path(self.tempdir.name) / ".runtime" / "memory.lock").exists())

    def test_update_turn_runs_sync_preflight_without_exposing_context(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            manager_class.return_value.preflight.return_value = SyncResult("synced", "local memory is current")
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        message = runtime.agent.calls[0]["message"]
        self.assertEqual(message, "remember one")
        manager_class.return_value.preflight.assert_called_once()
        manager_class.return_value.push.assert_called_once()

    def test_update_sync_preflight_runs_while_write_lock_is_held(self):
        events = []

        class FakeLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock_exit")

        def preflight():
            events.append("preflight")
            return SyncResult("synced", "local memory is current")

        def push():
            events.append("push")
            return SyncResult("pushed", "local memory pushed")

        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.MemoryWriteLock", FakeLock),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            manager_class.return_value.preflight.side_effect = preflight
            manager_class.return_value.push.side_effect = push
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                events.append("model")

                class FakeResult:
                    output = "reply"

                    def all_messages_json(self):
                        return b'["message"]'

                return FakeResult()

            runtime.agent.run_sync = run_sync
            runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(events, ["lock_enter", "preflight", "model", "push", "lock_exit"])

    def test_dirty_preflight_runs_sync_reconciler_before_update_agent(self):
        repairs = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch.object(RightMemoryRuntime, "_run_sync_reconciler", lambda self, result: repairs.append(result.status)),
        ):
            manager_class.return_value.preflight.side_effect = [
                SyncResult("dirty", "local memory has uncommitted changes", ["MEMORY.md"]),
                SyncResult("synced", "local memory is current"),
            ]
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(repairs, ["dirty"])
        self.assertEqual(runtime.agent.calls[0]["message"], "remember one")

    def test_dirty_push_runs_sync_reconciler_after_update_agent(self):
        repairs = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch.object(RightMemoryRuntime, "_run_sync_reconciler", lambda self, result: repairs.append(result.status)),
        ):
            manager_class.return_value.preflight.return_value = SyncResult("synced", "local memory is current")
            manager_class.return_value.push.return_value = SyncResult(
                "dirty",
                "local memory has uncommitted changes",
                ["MEMORY.md"],
            )
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(runtime.agent.calls[0]["message"], "remember one")
        self.assertEqual(repairs, ["dirty"])

    def test_retrieve_turn_does_not_run_sync_preflight(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        manager_class.assert_not_called()

    def test_sync_reconciler_receives_sync_push_tool_when_sync_enabled(self):
        for role in ("dreamer", "reviewer", "sync-reconciler", "update"):
            with self.subTest(role=role):
                config = RuntimeConfig(
                    role=role,
                    model_id="openai/test",
                    memory_root=Path(self.tempdir.name),
                    sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
                )

                with patch.dict("sys.modules", self._fake_pydantic_modules()):
                    runtime = RightMemoryRuntime(config)

                tool_names = [tool.__name__ for tool in runtime.agent.tools]
                if role == "sync-reconciler":
                    self.assertIn("sync_push", tool_names)
                else:
                    self.assertNotIn("sync_push", tool_names)

    def test_retrieve_does_not_receive_sync_push_tool_when_sync_enabled(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = [tool.__name__ for tool in runtime.agent.tools]
        self.assertNotIn("sync_push", tool_names)

    def test_write_roles_do_not_receive_sync_push_tool_when_sync_disabled(self):
        for role in ("dreamer", "reviewer", "sync-reconciler", "update"):
            with self.subTest(role=role):
                config = RuntimeConfig(
                    role=role,
                    model_id="openai/test",
                    memory_root=Path(self.tempdir.name),
                    sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=False),
                )

                with patch.dict("sys.modules", self._fake_pydantic_modules()):
                    runtime = RightMemoryRuntime(config)

                tool_names = [tool.__name__ for tool in runtime.agent.tools]
                self.assertNotIn("sync_push", tool_names)

    def test_semantic_prompt_guidance_keeps_sync_work_out(self):
        instructions = build_instructions(Path("/memory"), "update")

        self.assertNotIn("Runtime sync context", instructions)
        self.assertNotIn("already performed sync preflight", instructions)
        self.assertNotIn("call `sync_push`", instructions)
        self.assertNotIn("dirty state", instructions)

        retrieve_instructions = build_instructions(Path("/memory"), "retrieve")
        self.assertIn("local memory", retrieve_instructions)
        self.assertIn("does not perform sync preflight by default", retrieve_instructions)

    def test_run_session_turn_preserves_message_history_on_disk(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            first_runtime = RightMemoryRuntime(config)
            first = first_runtime.run_session_turn("agent-session", "remember one")
            first_runtime.cleanup()

            second_runtime = RightMemoryRuntime(config)
            second = second_runtime.run_session_turn("agent-session", "what was that?")

        self.assertEqual(first, "reply 1")
        self.assertEqual(second, "reply 1")
        self.assertIsNone(first_runtime.agent.calls[0]["message_history"])
        self.assertEqual(second_runtime.agent.calls[0]["message_history"], ["message 1"])
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertEqual(json.loads(history_path.read_text(encoding="utf-8")), ["message 1"])
        gitignore_path = Path(self.tempdir.name) / ".runtime" / ".gitignore"
        self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "*\n")

    def test_debug_trace_writes_session_events_without_changing_history(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "reply 1")
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertEqual(json.loads(history_path.read_text(encoding="utf-8")), ["message 1"])
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            ["run_started", "history_loaded", "model_started", "model_finished", "history_saved", "run_finished"],
        )
        self.assertEqual(events[0]["message"], "remember one")
        self.assertEqual(events[0]["model_id"], "openai/test")
        self.assertEqual(events[3]["output"], "reply 1")

    def test_cli_agent_debug_trace_uses_cli_model_id(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex", model="gpt-5"),
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_session_turn.return_value = "cli reply"
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "cli reply")
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["run_started", "model_started", "model_finished", "run_finished"])
        self.assertEqual(events[0]["model_id"], "gpt-5")
        self.assertEqual(events[2]["output"], "cli reply")

    def test_debug_trace_records_tool_events(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            with runtime._debug_trace("agent-session"):
                runtime.agent.kwargs["tools"][0]("*.md")

        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["tool_started", "tool_finished"])
        self.assertEqual(events[0]["tool"], "glob")

    def test_debug_trace_records_failures_before_history_save(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )
        fake_modules = self._fake_pydantic_modules()
        fake_modules["pydantic_ai"].Agent = self._failing_agent()

        with patch.dict("sys.modules", fake_modules):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("agent-session", "remember one")

        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertFalse(history_path.exists())
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["event"], "run_failed")
        self.assertEqual(events[-1]["error_type"], "RuntimeError")

    def test_run_session_turn_rejects_path_session_id(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with self.assertRaises(ValueError):
            runtime.run_session_turn("../bad", "hello")

    def test_rejects_unsupported_model_kwargs(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", model_kwargs={"api_version": "2026-01-01"})

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with self.assertRaises(ValueError):
            runtime.run_turn("hello")

    def test_tools_raise_model_retry_for_recoverable_errors(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test")
        fake_modules = self._fake_pydantic_modules()

        with patch.dict("sys.modules", fake_modules):
            runtime = RightMemoryRuntime(config)
            tools = {tool.__name__: tool for tool in runtime.agent.kwargs["tools"]}

            with self.assertRaises(fake_modules["pydantic_ai"].ModelRetry) as caught:
                tools["glob"]("../*.md")

        self.assertIn("glob pattern must be relative", str(caught.exception))

    def test_runtime_exposes_commit_tools(self):
        config = RuntimeConfig(role="update", model_id="openai/test")

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.kwargs["tools"]}
        self.assertIn("read", tool_names)
        self.assertIn("grep", tool_names)
        self.assertIn("glob", tool_names)
        self.assertIn("read_command", tool_names)
        self.assertIn("edit_file", tool_names)
        self.assertIn("create_file", tool_names)
        self.assertIn("delete_file", tool_names)
        self.assertIn("rename_file", tool_names)
        self.assertIn("git_add", tool_names)
        self.assertIn("git_commit", tool_names)
        self.assertNotIn("git_discard", tool_names)
        self.assertNotIn("apply_patch", tool_names)

    def test_sync_reconciler_exposes_sync_repair_tools(self):
        config = RuntimeConfig(
            role="sync-reconciler",
            model_id="openai/test",
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.kwargs["tools"]}
        self.assertIn("git_discard", tool_names)
        self.assertIn("sync_push", tool_names)

    def test_retrieve_runtime_is_read_only(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test")

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.kwargs["tools"]}
        self.assertIn("read", tool_names)
        self.assertIn("grep", tool_names)
        self.assertIn("glob", tool_names)
        self.assertIn("read_command", tool_names)
        self.assertNotIn("search_files", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("create_file", tool_names)
        self.assertNotIn("delete_file", tool_names)
        self.assertNotIn("rename_file", tool_names)
        self.assertNotIn("apply_patch", tool_names)
        self.assertNotIn("git_add", tool_names)
        self.assertNotIn("git_discard", tool_names)
        self.assertNotIn("git_commit", tool_names)

    def _fake_pydantic_modules(self):
        class FakeModelRetry(Exception):
            pass

        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.model = kwargs["model"]
                self.tools = kwargs["tools"]

            def run_sync(self, message, message_history=None, model_settings=None, usage_limits=None):
                self.calls.append(
                    {
                        "message": message,
                        "message_history": message_history,
                        "model_settings": model_settings,
                        "usage_limits": usage_limits,
                    }
                )
                call_count = len(self.calls)

                class FakeResult:
                    output = f"reply {call_count}"

                    def all_messages(self):
                        return [f"message {call_count}"]

                    def all_messages_json(self):
                        return json.dumps(self.all_messages()).encode()

                return FakeResult()

        class FakeProvider:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeModel:
            def __init__(self, model_name, provider=None):
                self.model_name = model_name
                self.provider = provider

        class FakeModelMessagesTypeAdapter:
            @staticmethod
            def validate_json(data):
                return json.loads(data)

        class FakeUsageLimits:
            def __init__(self, request_limit=None):
                self.request_limit = request_limit

        return {
            "pydantic_ai": types.SimpleNamespace(
                Agent=FakeAgent,
                ModelRetry=FakeModelRetry,
                UsageLimits=FakeUsageLimits,
            ),
            "pydantic_ai.messages": types.SimpleNamespace(ModelMessagesTypeAdapter=FakeModelMessagesTypeAdapter),
            "pydantic_ai.models": types.SimpleNamespace(),
            "pydantic_ai.models.openai": types.SimpleNamespace(OpenAIChatModel=FakeModel),
            "pydantic_ai.providers": types.SimpleNamespace(),
            "pydantic_ai.providers.openai": types.SimpleNamespace(OpenAIProvider=FakeProvider),
            "pydantic_ai.models.anthropic": types.SimpleNamespace(AnthropicModel=FakeModel),
            "pydantic_ai.providers.anthropic": types.SimpleNamespace(AnthropicProvider=FakeProvider),
        }

    def _failing_agent(self):
        class FailingAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run_sync(self, message, message_history=None, model_settings=None, usage_limits=None):
                raise RuntimeError("model failed")

        return FailingAgent


class PromptTests(unittest.TestCase):
    def test_cli_agent_prompt_is_thin_and_embeds_role_prompt(self):
        prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), "update")

        self.assertIn("You are RightMemory update mode.", prompt)
        self.assertIn("configured memory root is /home/example/.rightmemory", prompt)
        self.assertIn("MEMORY.md", prompt)
        self.assertIn("MEMORY_*.md", prompt)
        self.assertIn("dream_logs/", prompt)
        self.assertIn("Follow the canonical role instructions below.", prompt)
        self.assertIn("Return a concise final reply.", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("Update Role", prompt)
        self.assertIn("candidate memory", prompt)
        self.assertNotIn("Command-selected behavior", prompt)
        self.assertNotIn("Standalone adaptation", prompt)
        self.assertNotIn("read_command", prompt)
        self.assertNotIn("edit_file(path, old_string, new_string", prompt)
        self.assertNotIn("create_file", prompt)
        self.assertNotIn("Pydantic AI", prompt)
        self.assertNotIn("provider tool", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_cli_agent_prompt_rejects_unknown_role(self):
        with self.assertRaises(ValueError) as caught:
            build_cli_agent_instructions(Path("/home/example/.rightmemory"), "curator")

        self.assertIn("role must be one of:", str(caught.exception))

    def test_cli_agent_reviewer_prompt_does_not_expose_standalone_tool_names(self):
        prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), "reviewer")

        self.assertIn("Reviewer Role", prompt)
        self.assertIn("graph sanity", prompt)
        self.assertNotIn("validate_memory", prompt)
        self.assertNotIn("git_discard", prompt)
        self.assertNotIn("sync_push", prompt)

    def test_cli_agent_sync_reconciler_prompt_does_not_expose_standalone_tool_names(self):
        prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), "sync-reconciler")

        self.assertIn("Sync Reconciler Role", prompt)
        self.assertIn("available validation", prompt)
        self.assertIn("runtime-provided sync", prompt)
        self.assertNotIn("validate_memory", prompt)
        self.assertNotIn("git_discard", prompt)
        self.assertNotIn("sync_push", prompt)

    def test_retrieve_prompt_has_role_prompt_and_retrieve_command_behavior(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "retrieve")

        self.assertIn("The only allowed root directory is /home/example/.rightmemory", prompt)
        self.assertIn("Stay within the command-selected retrieve role", prompt)
        self.assertNotIn("Do not blend retrieve, update, dreamer, or reviewer responsibilities.", prompt)
        self.assertIn("rightmemory retrieve", prompt)
        self.assertIn("read-only retrieval request", prompt)
        self.assertIn("read_command", prompt)
        self.assertIn("sed -n", prompt)
        self.assertIn("Retrieve Role", prompt)
        self.assertNotIn("Every dispatch must start", prompt)
        self.assertNotIn("[RETRIEVE]", prompt)
        self.assertNotIn("[UPDATE]", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("embedded schema above", prompt)
        self.assertNotIn("memory-curator", prompt)
        self.assertNotIn("memory-dreamer", prompt)
        self.assertNotIn("rightmemory-schema.md", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_update_prompt_has_role_prompt_and_update_command_behavior(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "update")

        self.assertIn("The only allowed root directory is /home/example/.rightmemory", prompt)
        self.assertIn("rightmemory update", prompt)
        self.assertIn("read-write memory update request", prompt)
        self.assertIn("Update Role", prompt)
        self.assertIn("candidate memory", prompt)
        self.assertIn("raw process logs", prompt)
        self.assertIn("compare each candidate with relevant existing memory", prompt)
        self.assertIn("delete obsolete memory", prompt)
        self.assertIn("mention the unresolved conflict in the final reply", prompt)
        self.assertIn("edit_file(path, old_string, new_string", prompt)
        self.assertIn("create_file", prompt)
        self.assertIn("read_command", prompt)
        self.assertIn("Choose the edit shape that makes memory clearer", prompt)
        self.assertIn("Place memory in a clear tree", prompt)
        self.assertNotIn("Codex-style patches", prompt)
        self.assertNotIn("small, reviewable edits", prompt)
        self.assertNotIn("Every dispatch must start", prompt)
        self.assertNotIn("[RETRIEVE]", prompt)
        self.assertNotIn("[UPDATE]", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("embedded schema above", prompt)
        self.assertNotIn("memory-curator", prompt)
        self.assertNotIn("memory-dreamer", prompt)
        self.assertNotIn("rightmemory-schema.md", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_dreamer_prompt_has_role_prompt(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "dreamer")

        self.assertIn("The only allowed root directory is /home/example/.rightmemory", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("embedded schema above", prompt)
        self.assertIn("Dreamer Role", prompt)
        self.assertNotIn("memory-curator", prompt)
        self.assertNotIn("rightmemory-schema.md", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_reviewer_prompt_has_role_prompt(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "reviewer")

        self.assertIn("The only allowed root directory is /home/example/.rightmemory", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("embedded schema above", prompt)
        self.assertIn("Reviewer Role", prompt)
        self.assertIn("Review Input", prompt)
        self.assertIn("Normalized session JSON", prompt)
        self.assertIn("What To Save Or Revise", prompt)
        self.assertIn("Implicit And Candidate Memory", prompt)
        self.assertIn("Memory Alignment", prompt)
        self.assertIn("possible memory, not as proof", prompt)
        self.assertIn("Candidate:", prompt)
        self.assertIn("Promote candidate memory", prompt)
        self.assertIn("one-session conflict evidence", prompt)
        self.assertIn("future agents again", prompt)
        self.assertIn("compare assistant responses with existing memory", prompt)
        self.assertIn("Avoid broad guesses", prompt)
        self.assertIn("Review the session as a whole", prompt)
        self.assertIn("validate_memory", prompt)
        self.assertIn("commit them", prompt)
        self.assertIn("Choose the edit shape that makes memory clearer", prompt)
        self.assertIn("Place memory in a clear tree", prompt)
        self.assertNotIn("Dispatch Contract", prompt)
        self.assertNotIn("Be more conservative than an explicit update request", prompt)
        self.assertNotIn("candidates for new memory", prompt)
        self.assertNotIn("promote it to normal memory", prompt)
        self.assertNotIn("too weak even for a candidate", prompt)
        self.assertNotIn("grounded rule with scope", prompt)
        self.assertNotIn("Do not save every mismatch", prompt)
        self.assertNotIn("memory-curator", prompt)
        self.assertNotIn("memory-dreamer", prompt)
        self.assertNotIn("rightmemory-schema.md", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_sync_reconciler_prompt_has_role_prompt(self):
        prompt = build_instructions(Path("/memory"), "sync-reconciler")

        self.assertIn("The only allowed root directory is /memory", prompt)
        self.assertIn("sync-reconciler", prompt)
        self.assertIn("sync watcher selected sync reconciliation behavior", prompt)
        self.assertIn("current sync repair context", prompt)
        self.assertIn("scheduled sync workflow supplies repair context", prompt)
        self.assertIn("Runtime sync context block", prompt)
        self.assertNotIn("the runtime already performed sync preflight", prompt)
        self.assertIn("dirty memory state", prompt)
        self.assertIn("push conflicts", prompt)
        self.assertIn("git_discard", prompt)
        self.assertIn("Preserve coherent durable memory", prompt)
        self.assertIn("commit", prompt)
        self.assertIn("sync_push", prompt)
        self.assertIn("call `sync_push` again", prompt)
        self.assertIn("Final replies should include repaired files", prompt)
        self.assertIn("Sync Reconciler Role", prompt)
        self.assertIn("RightMemory Schema", prompt)
        self.assertIn("embedded schema above", prompt)
        self.assertNotIn("{{MEMORY_ROOT}}", prompt)
        self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_prompt_assets_are_included_in_wheel(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(force_include["skills"], "rightmemory/skills")
        self.assertEqual(force_include["rightmemory/prompts"], "rightmemory/prompts")


def load_sync_config_for_test(memory_root: Path, enabled: bool):
    from rightmemory.config import SyncConfig

    return SyncConfig(memory_root=memory_root, enabled=enabled)


if __name__ == "__main__":
    unittest.main()
