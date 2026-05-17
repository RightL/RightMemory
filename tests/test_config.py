import tempfile
import types
import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import RuntimeConfig, load_config, load_review_config
from rightmemory.prompt import build_instructions
from rightmemory.runtime import RightMemoryRuntime, build_model


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
        self.assertNotIn("apply_patch", tool_names)

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
        self.assertNotIn("git_commit", tool_names)

    def _fake_pydantic_modules(self):
        class FakeModelRetry(Exception):
            pass

        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.model = kwargs["model"]

            def run_sync(self, message, message_history=None, model_settings=None):
                self.calls.append(
                    {"message": message, "message_history": message_history, "model_settings": model_settings}
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

        return {
            "pydantic_ai": types.SimpleNamespace(Agent=FakeAgent, ModelRetry=FakeModelRetry),
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

            def run_sync(self, message, message_history=None, model_settings=None):
                raise RuntimeError("model failed")

        return FailingAgent


class PromptTests(unittest.TestCase):
    def test_retrieve_prompt_has_role_prompt_and_retrieve_command_behavior(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "retrieve")

        self.assertIn("The only allowed root directory is /home/example/.rightmemory", prompt)
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

    def test_prompt_assets_are_included_in_wheel(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(force_include["skills"], "rightmemory/skills")
        self.assertEqual(force_include["rightmemory/prompts"], "rightmemory/prompts")


if __name__ == "__main__":
    unittest.main()
