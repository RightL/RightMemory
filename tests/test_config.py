import tempfile
import types
import json
import subprocess
import tomllib
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

from rightmemory.agent_cli import NO_SESSION_RIGHTMEMORY_SESSION_ID
from rightmemory.config import (
    AgentCliConfig,
    PrunerConfig,
    RuntimeConfig,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_insight_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
from rightmemory.isolated_write import IsolatedWriteResult, MainMemoryDirtyError
from rightmemory.prompt import build_cli_agent_instructions, build_instructions
from rightmemory.provider_sessions import ProviderSessionStore
from rightmemory.prune import PruneDueStatus
from rightmemory.runtime import RightMemoryRuntime, build_model
from rightmemory.retrieve_selection import RetrieveSelection
from rightmemory.semantic_upgrades import SemanticUpgradeContext, SemanticUpgradeNote
from rightmemory.shared_view_files import FileViewPublishResult
from rightmemory.sync import SyncResult


EMPTY_RETRIEVE_SELECTION_JSON = '{"ids": [], "sources": [], "recent_candidates": []}'


class ConfigTests(unittest.TestCase):
    def test_load_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [retrieve.model]
                model_id = "openai/project"
                """,
                encoding="utf-8",
            )

            config = load_config("retrieve", memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertEqual(config.state_root, root)
        self.assertEqual(config.model_id, "openai/project")

    def test_load_config_accepts_retrieve_output_safety_limit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [retrieve]
                max_output_chars = 12345

                [retrieve.model]
                model_id = "openai/project"
                """,
                encoding="utf-8",
            )

            config = load_config("retrieve", memory_root=root)

        self.assertEqual(config.retrieve_max_output_chars, 12345)

    def test_load_review_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [review]
                sources = []
                """,
                encoding="utf-8",
            )

            config = load_review_config(memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertEqual(config.sources, [])

    def test_load_sync_config_accepts_explicit_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "rightmemory.toml").write_text(
                """
                [sync]
                enabled = true
                stale_pull_after_hours = 8
                """,
                encoding="utf-8",
            )

            config = load_sync_config(memory_root=root)

        self.assertEqual(config.memory_root, root)
        self.assertTrue(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 8)

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
    def test_non_retrieve_role_inherits_missing_standalone_executor_from_update(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "openai/fast-retrieve"

            [update.model]
            model_id = "hosted_vllm//models/write-model"
            api_base = "http://127.0.0.1:8000/v1"
            api_key = "token"

            [update.model.kwargs]
            temperature = 0

            [pruner]
            generation_commits = 12
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("pruner")

        self.assertEqual(config.role, "pruner")
        self.assertEqual(config.runtime_mode, "standalone")
        self.assertEqual(config.model_id, "hosted_vllm//models/write-model")
        self.assertEqual(config.api_base, "http://127.0.0.1:8000/v1")
        self.assertEqual(config.api_key, "token")
        self.assertEqual(config.model_kwargs, {"temperature": 0})

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_non_retrieve_role_inherits_missing_agent_cli_executor_from_update(self):
        config_path = self._write_config(
            """
            [agent_cli]
            provider = "codex"

            [update.agent_cli]
            model = "gpt-5"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("sync-reconciler")

        self.assertEqual(config.role, "sync-reconciler")
        self.assertEqual(config.runtime_mode, "cli-agent")
        self.assertEqual(config.agent_cli, AgentCliConfig(provider="codex", model="gpt-5"))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_retrieve_does_not_inherit_writer_executor(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "openai/write-model"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("[agent_cli].provider", str(caught.exception))
        self.assertIn("[retrieve.agent_cli].provider", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_writer_executor_fallback_ignores_retrieve_model(self):
        config_path = self._write_config(
            """
            [retrieve.model]
            model_id = "openai/fast-retrieve"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("reviewer")

        self.assertIn("[agent_cli].provider", str(caught.exception))
        self.assertIn("[reviewer.agent_cli].provider", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_explicit_incomplete_role_model_does_not_inherit_fields(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "openai/write-model"

            [pruner.model]
            api_base = "http://127.0.0.1:8000/v1"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("pruner")

        self.assertIn("[model].model_id must be a non-empty string", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_missing_executor_fallback_prefers_update(self):
        config_path = self._write_config(
            """
            [dreamer.model]
            model_id = "openai/dreamer"

            [update.model]
            model_id = "openai/update"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("reviewer")

        self.assertEqual(config.model_id, "openai/update")

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
            batch_size = 4

            [[review.sources]]
            kind = "codex"
            path = "~/codex-history"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_review_config()

        self.assertEqual(config.idle_seconds, 7200)
        self.assertEqual(config.since_days, 14)
        self.assertEqual(config.batch_size, 4)
        self.assertEqual(len(config.sources), 1)
        self.assertEqual(config.sources[0].kind, "codex")
        self.assertEqual(config.sources[0].path, Path("~/codex-history").expanduser())

    def test_review_config_defaults_to_six_hour_idle_and_three_day_window(self):
        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            config_path = memory_root / "rightmemory.toml"

            with patch("rightmemory.config.MEMORY_ROOT", memory_root), patch("rightmemory.config.CONFIG_PATH", config_path):
                config = load_review_config()

        self.assertEqual(config.idle_seconds, 6 * 60 * 60)
        self.assertEqual(config.since_days, 3)
        self.assertEqual(config.batch_size, 3)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_review_config_rejects_invalid_batch_size(self):
        config_path = self._write_config(
            """
            [review]
            batch_size = 0
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_review_config()

        self.assertIn("[review].batch_size must be a positive integer", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_defaults(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_async_update_config()

        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))
        self.assertEqual(config.target_batch_candidates, 15)
        self.assertEqual(config.max_wait_seconds, 24 * 60 * 60)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_parses_custom_values(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "openai/update"

            [update.async]
            target_batch_candidates = 22
            max_wait_seconds = 7200
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            async_config = load_async_update_config()
            runtime_config = load_config("update")

        self.assertEqual(async_config.target_batch_candidates, 22)
        self.assertEqual(async_config.max_wait_seconds, 7200)
        self.assertEqual(runtime_config.model_id, "openai/update")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_rejects_invalid_values(self):
        cases = [
            ("target_batch_candidates = 0", "[update.async].target_batch_candidates must be a positive integer"),
            ("target_batch_candidates = true", "[update.async].target_batch_candidates must be a positive integer"),
            ("max_wait_seconds = -1", "[update.async].max_wait_seconds must be a positive integer"),
            ("max_wait_seconds = true", "[update.async].max_wait_seconds must be a positive integer"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                config_path = self._write_config(
                    f"""
                    [update.async]
                    {body}
                    """
                )

                with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
                    with self.assertRaises(ValueError) as caught:
                        load_async_update_config()

                self.assertIn(message, str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_rejects_unknown_key(self):
        config_path = self._write_config(
            """
            [update.async]
            target_batch_candidates = 15
            extra = 1
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_async_update_config()

        self.assertIn("unsupported [update.async] config key(s): extra", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_dreamer_watch_config_defaults(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_dreamer_watch_config()

        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))
        self.assertEqual(config.trigger_points, 50.0)
        self.assertEqual(config.update_candidate_points, 1.0)
        self.assertFalse(hasattr(config, "review_session_points"))
        self.assertEqual(config.check_interval_seconds, 3000)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_dreamer_watch_config_parses_custom_values(self):
        config_path = self._write_config(
            """
            [dreamer.model]
            model_id = "openai/dreamer"

            [dreamer.watch]
            trigger_points = 75.5
            update_candidate_points = 2
            review_session_points = 3.25
            check_interval_seconds = 120
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_dreamer_watch_config()
            runtime_config = load_config("dreamer")

        self.assertEqual(config.trigger_points, 75.5)
        self.assertEqual(config.update_candidate_points, 2)
        self.assertFalse(hasattr(config, "review_session_points"))
        self.assertEqual(config.check_interval_seconds, 120)
        self.assertEqual(runtime_config.model_id, "openai/dreamer")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_dreamer_watch_config_rejects_invalid_values(self):
        cases = [
            ("trigger_points = 0", "[dreamer.watch].trigger_points must be a positive number"),
            ("trigger_points = nan", "[dreamer.watch].trigger_points must be a positive number"),
            ("trigger_points = inf", "[dreamer.watch].trigger_points must be a positive number"),
            ("update_candidate_points = -1", "[dreamer.watch].update_candidate_points must be a positive number"),
            ("review_session_points = true", "[dreamer.watch].review_session_points must be a positive number"),
            ("review_session_points = -inf", "[dreamer.watch].review_session_points must be a positive number"),
            ("check_interval_seconds = 1.5", "[dreamer.watch].check_interval_seconds must be a positive integer"),
            ("unknown = 1", "unsupported [dreamer.watch] config key(s): unknown"),
        ]

        for watch_config, message in cases:
            with self.subTest(watch_config=watch_config):
                config_path = self._write_config(
                    f"""
                    [dreamer.watch]
                    {watch_config}
                    """
                )

                with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
                    with self.assertRaises(ValueError) as caught:
                        load_dreamer_watch_config()

                self.assertIn(message, str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_insight_watch_config_defaults(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_insight_watch_config()

        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))
        self.assertEqual(config.trigger_points, 150.0)
        self.assertEqual(config.update_candidate_points, 1.0)
        self.assertFalse(hasattr(config, "review_session_points"))
        self.assertEqual(config.check_interval_seconds, 3000)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_insight_watch_config_parses_custom_values(self):
        config_path = self._write_config(
            """
            [insight.model]
            model_id = "openai/insight"

            [insight.watch]
            trigger_points = 225
            update_candidate_points = 2.5
            review_session_points = 4
            check_interval_seconds = 600
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_insight_watch_config()
            runtime_config = load_config("insight")

        self.assertEqual(config.trigger_points, 225.0)
        self.assertEqual(config.update_candidate_points, 2.5)
        self.assertFalse(hasattr(config, "review_session_points"))
        self.assertEqual(config.check_interval_seconds, 600)
        self.assertEqual(runtime_config.model_id, "openai/insight")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_insight_watch_config_rejects_invalid_values(self):
        cases = [
            ("trigger_points = 0", "[insight.watch].trigger_points must be a positive number"),
            ("update_candidate_points = -1", "[insight.watch].update_candidate_points must be a positive number"),
            ("review_session_points = true", "[insight.watch].review_session_points must be a positive number"),
            ("check_interval_seconds = 1.5", "[insight.watch].check_interval_seconds must be a positive integer"),
            ("unknown = 1", "unsupported [insight.watch] config key(s): unknown"),
        ]

        for watch_config, message in cases:
            with self.subTest(watch_config=watch_config):
                config_path = self._write_config(
                    f"""
                    [insight.watch]
                    {watch_config}
                    """
                )

                with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
                    with self.assertRaises(ValueError) as caught:
                        load_insight_watch_config()

                self.assertIn(message, str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_defaults(self):
        config_path = self._write_config(
            """
            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            runtime_config = load_config("pruner")
            pruner_config = load_pruner_config()

        self.assertEqual(runtime_config.role, "pruner")
        self.assertEqual(runtime_config.model_id, "openai/pruner")
        self.assertEqual(pruner_config.memory_root, Path("/home/example/.rightmemory"))
        self.assertEqual(pruner_config.generation_commits, 70)
        self.assertEqual(pruner_config.revival_grace_checkpoints, 2)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_accepts_generation_values(self):
        config_path = self._write_config(
            """
            [pruner]
            generation_commits = 12
            revival_grace_checkpoints = 3

            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            runtime_config = load_config("pruner")
            pruner_config = load_pruner_config()

        self.assertEqual(runtime_config.role, "pruner")
        self.assertEqual(pruner_config.generation_commits, 12)
        self.assertEqual(pruner_config.revival_grace_checkpoints, 3)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_rejects_bool_generation_commits(self):
        config_path = self._write_config(
            """
            [pruner]
            generation_commits = true

            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_pruner_config()

        self.assertIn("[pruner].generation_commits must be a positive integer", str(caught.exception))

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

    def test_retrieve_prompt_uses_context_first_contract(self):
        instructions = build_instructions(Path("/memory"), "retrieve")

        self.assertIn("supplies a daily snapshot", instructions)
        self.assertIn("MEMORY.md", instructions)
        self.assertIn("PURSUITS.md", instructions)
        self.assertIn("read_detail", instructions)
        self.assertIn("read_markdown", instructions)
        self.assertIn("read_skill", instructions)
        self.assertIn("read_mf", instructions)
        self.assertIn("MQ#", instructions)
        self.assertIn("provider question", instructions.lower())
        self.assertIn("reusable instruction", instructions)
        self.assertIn("Available retrieve tools", instructions)
        self.assertIn(
            "`read_detail(detail_id)` resolves a relevant `F#` id",
            instructions,
        )
        self.assertIn("`read_markdown(markdown_id)`", instructions)
        self.assertIn("`read_skill(skill_id)`", instructions)
        self.assertIn("`read_mf(mf_id)`", instructions)
        self.assertNotIn("offset, limit", instructions)
        self.assertIn("terminal retrieve-selection output type", instructions)
        self.assertIn('"recent_candidates"', instructions)
        self.assertNotIn("Read `MEMORY.md` before retrieval", instructions)
        self.assertNotIn("Follow each with a one-line note", instructions)
        self.assertNotIn("read_command", instructions)
        self.assertNotIn("retrieve_shared_view", instructions)
        self.assertNotIn("rightmemory shared-view ask", instructions)

    def test_write_role_prompts_preserve_shared_view_boundary(self):
        for role in ("update", "dreamer"):
            prompt = build_instructions(Path("/memory"), role)
            self.assertIn("MF#", prompt)
            self.assertIn("MQ#", prompt)
            self.assertIn("provider", prompt)
            self.assertNotIn("rightmemory shared-view retrieve", prompt)

    def test_retrieve_runtime_does_not_expose_shared_view_tool(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="standalone",
            model_id="openai/test",
            memory_root=Path("/memory"),
            state_root=Path("/memory"),
        )

        with patch.object(RightMemoryRuntime, "_build_agent", return_value=object()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime._agent_tools()}
        self.assertEqual(tool_names, {"read_detail", "read_markdown", "read_skill", "read_mf"})
        self.assertNotIn("retrieve_shared_view", tool_names)
        self.assertNotIn("read_command", tool_names)
        self.assertNotIn("grep", tool_names)

    def test_shared_view_builder_role_loads_prompt(self):
        instructions = build_instructions(Path("/memory"), "shared-view-builder")

        self.assertIn("shared-view builder", instructions)
        self.assertIn("recipe.toml", instructions)
        self.assertIn("question.toml", instructions)
        self.assertIn("create_extractive_file_view", instructions)
        self.assertIn("create_generative_file_view", instructions)
        self.assertNotIn("create_file_view_recipe", instructions)
        self.assertIn("create_question_view", instructions)
        self.assertIn("Do not edit provider private memory", instructions)

    def test_shared_view_builder_runtime_exposes_compiler_tools(self):
        config = RuntimeConfig(
            role="shared-view-builder",
            runtime_mode="standalone",
            model_id="openai/test",
            memory_root=Path("/memory"),
            state_root=Path("/memory"),
        )

        with patch.object(RightMemoryRuntime, "_build_agent", return_value=object()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime._agent_tools()}
        self.assertIn("create_extractive_file_view", tool_names)
        self.assertIn("create_generative_file_view", tool_names)
        self.assertNotIn("create_file_view_recipe", tool_names)
        self.assertIn("create_question_view", tool_names)

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

    def test_runtime_config_replace_preserves_explicit_state_root_when_memory_root_changes(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test")
        isolated_memory_root = Path("/isolated/rightmemory")

        replaced = replace(config, memory_root=isolated_memory_root, state_root=config.state_root)

        self.assertEqual(replaced.memory_root, isolated_memory_root)
        self.assertEqual(replaced.state_root, config.state_root)

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

    def test_standalone_retrieve_uses_native_terminal_selection_type(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        self.assertIs(runtime.agent.kwargs["output_type"], RetrieveSelection)
        self.assertEqual(
            {tool.__name__ for tool in runtime.agent.tools},
            {"read_detail", "read_markdown", "read_skill", "read_mf"},
        )

    def test_cli_agent_runtime_uses_executor_without_pydantic_agent(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_stateless_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "no strong match")
        executor_class.assert_called_once_with(
            Path(self.tempdir.name),
            "retrieve",
            AgentCliConfig(provider="codex"),
            state_root=Path(self.tempdir.name),
            fresh_provider_session=False,
        )

    def test_retrieve_pulls_mf_views_before_model_without_prompt_pollution(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)
        captured: dict[str, str] = {}

        class FakeResult:
            output = RetrieveSelection()

            def all_messages_json(self):
                return b"[]"

        class FakeAgent:
            def run_sync(self, message, **kwargs):
                captured["message"] = message
                return FakeResult()

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            with patch.object(RightMemoryRuntime, "_build_agent", return_value=FakeAgent()):
                runtime = RightMemoryRuntime(config)
            with patch("rightmemory.runtime.pull_all_file_views", return_value=[]):
                output = runtime.run_session_turn("agent-session", "what do we know?")

        self.assertEqual(output, "no strong match")
        self.assertTrue(captured["message"].startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("===== MEMORY.md =====", captured["message"])
        self.assertTrue(captured["message"].rstrip().endswith("# Query\n\nwhat do we know?"))

    def test_update_turn_publishes_approved_file_views_after_success(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)

        class FakeResult:
            output = "updated"

            def all_messages_json(self):
                return b"[]"

        class FakeAgent:
            def run_sync(self, message, **kwargs):
                return FakeResult()

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            with patch.object(RightMemoryRuntime, "_build_agent", return_value=FakeAgent()):
                runtime = RightMemoryRuntime(config)
            with (
                patch.object(RightMemoryRuntime, "_should_isolate_write_turn", return_value=False),
                patch("rightmemory.runtime.publish_approved_file_views") as publish,
                patch("rightmemory.runtime.record_file_view_publish_results") as record,
            ):
                publish.return_value = [FileViewPublishResult("auth-api-files", "failed", "hub offline")]
                runtime.run_session_turn("agent-session", "remember this")

        publish.assert_called_once_with(root)
        record.assert_called_once_with(
            root,
            [FileViewPublishResult("auth-api-files", "failed", "hub offline")],
            trigger="update-write",
        )

    def test_write_role_session_turn_uses_isolated_runner(self):
        cases = [
            (
                RuntimeConfig(role="dreamer", model_id="openai/test", memory_root=Path(self.tempdir.name)),
                patch.dict("sys.modules", self._fake_pydantic_modules()),
                "_run_session_model",
            ),
            (
                RuntimeConfig(role="insight", model_id="openai/test", memory_root=Path(self.tempdir.name)),
                patch.dict("sys.modules", self._fake_pydantic_modules()),
                "_run_session_model",
            ),
            (
                RuntimeConfig(role="update", model_id="openai/test", memory_root=Path(self.tempdir.name)),
                patch.dict("sys.modules", self._fake_pydantic_modules()),
                "_run_session_model",
            ),
            (
                RuntimeConfig(role="pruner", model_id="openai/test", memory_root=Path(self.tempdir.name)),
                patch.dict("sys.modules", self._fake_pydantic_modules()),
                "_run_session_model",
            ),
        ]

        for config, build_context, direct_method in cases:
            with self.subTest(role=config.role, runtime_mode=config.runtime_mode):
                with (
                    build_context,
                    patch.object(RightMemoryRuntime, "_run_session_turn_isolated", return_value="isolated reply") as isolated,
                    patch.object(
                        RightMemoryRuntime,
                        direct_method,
                        side_effect=AssertionError("direct path should not run"),
                    ),
                ):
                    runtime = RightMemoryRuntime(config)
                    result = runtime.run_session_turn("agent-session", "remember one")

                self.assertEqual(result, "isolated reply")
                isolated.assert_called_once_with("agent-session", "remember one")

    def test_reviewer_session_turn_is_read_only_and_not_isolated(self):
        config = RuntimeConfig(
            role="reviewer",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with (
            patch("rightmemory.runtime.CliAgentExecutor"),
            patch.object(RightMemoryRuntime, "_run_session_cli_agent", return_value="candidate bundle") as direct,
            patch.object(
                RightMemoryRuntime,
                "_run_session_turn_isolated",
                side_effect=AssertionError("reviewer must not use a write worktree"),
            ),
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("review-session", "extract candidates")

        self.assertEqual(result, "candidate bundle")
        direct.assert_called_once_with("review-session", "extract candidates")

    def test_reviewer_tools_are_read_only(self):
        config = RuntimeConfig(
            role="reviewer",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.tools}
        self.assertIn("read", tool_names)
        self.assertIn("validate_memory", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("git_add", tool_names)
        self.assertNotIn("git_commit", tool_names)

    def test_run_cycle_passes_operator_hint_message(self):
        config = RuntimeConfig(
            role="insight",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name) / "state",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_cycle("insight-watch", operator_hint="focus on risks")

        self.assertEqual(result, "reply 1")
        sent = runtime.agent.calls[0]["message"]
        self.assertIn("<rightmemory_cycle>", sent)
        self.assertIn("role: insight", sent)
        self.assertIn("operator_hint: focus on risks", sent)

    def test_sync_reconciler_session_turn_does_not_isolate(self):
        config = RuntimeConfig(
            role="sync-reconciler",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch.object(RightMemoryRuntime, "_run_session_turn_isolated", side_effect=AssertionError("should not isolate")),
            patch.object(RightMemoryRuntime, "_run_session_model", return_value="direct reply") as direct,
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("sync-repair", "resolve conflict")

        self.assertEqual(result, "direct reply")
        direct.assert_called_once_with("sync-repair", "resolve conflict")

    def test_isolated_worktree_runtime_uses_temp_state_and_disables_sync(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        state_root = main_root / ".runtime" / "isolated-state" / "update-123"
        nested_configs = []
        calls = []
        trace = object()

        class FakeNestedRuntime:
            def __init__(self, config):
                self.config = config
                self._active_trace = None
                nested_configs.append(config)

            def _run_locked_turn(self, callback, *, isolate_write=False):
                calls.append(("locked", isolate_write))
                return callback(), None

            def _run_session_model(self, session_id, message):
                calls.append(("model", session_id, message, self._active_trace))
                return "model result"

            def cleanup(self):
                calls.append(("cleanup",))

        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=main_root,
            sync=load_sync_config_for_test(main_root, enabled=True),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._active_trace = trace

        with patch("rightmemory.runtime.RightMemoryRuntime", FakeNestedRuntime):
            result = runtime._run_session_turn_in_worktree(worktree, state_root, "agent-session", "remember one")

        self.assertEqual(result, "model result")
        self.assertEqual(len(nested_configs), 1)
        nested_config = nested_configs[0]
        self.assertEqual(nested_config.memory_root, worktree)
        self.assertEqual(nested_config.state_root, state_root)
        self.assertFalse(nested_config.fresh_provider_session)
        self.assertFalse(nested_config.sync.enabled)
        self.assertEqual(nested_config.sync.memory_root, worktree)
        self.assertEqual(
            calls,
            [
                ("locked", False),
                ("model", "agent-session", "remember one", trace),
                ("cleanup",),
            ],
        )

    def test_isolated_worktree_runtime_uses_cli_agent_path(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "reviewer-123"
        state_root = main_root / ".runtime" / "isolated-state" / "reviewer-123"
        nested_configs = []
        calls = []

        class FakeNestedRuntime:
            def __init__(self, config):
                self.config = config
                nested_configs.append(config)

            def _run_locked_turn(self, callback, *, isolate_write=False):
                calls.append(("locked", isolate_write))
                return callback(), None

            def _run_session_cli_agent(self, session_id, message):
                calls.append(("cli-agent", session_id, message))
                return "cli result"

            def cleanup(self):
                calls.append(("cleanup",))

        config = RuntimeConfig(
            role="reviewer",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=main_root,
            sync=load_sync_config_for_test(main_root, enabled=True),
        )

        with patch("rightmemory.runtime.CliAgentExecutor"):
            runtime = RightMemoryRuntime(config)

        with patch("rightmemory.runtime.RightMemoryRuntime", FakeNestedRuntime):
            result = runtime._run_session_turn_in_worktree(worktree, state_root, "agent-session", "review one")

        self.assertEqual(result, "cli result")
        self.assertEqual(nested_configs[0].memory_root, worktree)
        self.assertEqual(nested_configs[0].state_root, state_root)
        self.assertTrue(nested_configs[0].fresh_provider_session)
        self.assertFalse(nested_configs[0].sync.enabled)
        self.assertEqual(
            calls,
            [
                ("locked", False),
                ("cli-agent", "agent-session", "review one"),
                ("cleanup",),
            ],
        )

    def test_nested_worktree_runtime_does_not_reisolate(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=worktree,
            state_root=main_root,
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch.object(
                RightMemoryRuntime,
                "_run_session_turn_isolated",
                side_effect=AssertionError("nested runtime should not re-isolate"),
            ),
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "reply 1")
        history_path = main_root / ".runtime" / "sessions" / "update" / "agent-session.json"
        self.assertEqual(json.loads(history_path.read_text(encoding="utf-8")), ["message 1"])

    def test_isolated_write_turn_does_not_hold_main_lock_around_model(self):
        events = []

        class FakeLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append(("lock_enter", self.memory_root))
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append(("lock_exit", self.memory_root))

        def isolated(self, session_id, message):
            events.append(("isolated", session_id, message))
            return "isolated reply"

        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.MemoryWriteLock", FakeLock),
            patch.object(RightMemoryRuntime, "_run_session_turn_isolated", isolated),
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "isolated reply")
        self.assertEqual(events, [("isolated", "agent-session", "remember one")])

    def test_isolated_helper_returns_supervisor_output(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        calls = []

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                calls.append(("supervisor", memory_root, role))

            def run(self, callback):
                calls.append(("run",))
                return IsolatedWriteResult(output=callback(worktree), commits_landed=1)

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=main_root)

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FakeSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", return_value="nested output") as nested,
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertEqual(result, "nested output")
        self.assertEqual(calls, [("supervisor", main_root, "update"), ("run",)])
        nested.assert_called_once()
        nested_worktree, state_root, nested_session_id, nested_message = nested.call_args.args
        self.assertEqual(nested_worktree, worktree)
        self.assertTrue(state_root.is_relative_to(main_root / ".runtime" / "isolated-state"))
        self.assertEqual((nested_session_id, nested_message), ("agent-session", "remember one"))
        self.assertFalse(state_root.exists())

    def test_isolated_update_creates_review_before_state_promotion(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        events = []

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                output = callback(worktree)
                events.append("landed")
                return IsolatedWriteResult(
                    output=output,
                    commits_landed=1,
                    start_commit="base123",
                    landed_commit="tip456",
                    changed_paths=("MEMORY.md",),
                )

        class FakeState:
            def __init__(self, *args, **kwargs):
                self.root = main_root / ".runtime" / "fake-state"

            def __enter__(self):
                return self.root

            def __exit__(self, exc_type, exc, traceback):
                return None

            def promote(self):
                events.append("promote")

            def archive_failed_provider_session(self):
                events.append("archive")

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=main_root)
        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FakeSupervisor),
            patch("rightmemory.runtime._IsolatedStateOverlay", FakeState),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", return_value="updated"),
        ):
            runtime = RightMemoryRuntime(config)
            with patch.object(runtime, "_create_update_review", side_effect=lambda *_args: events.append("review")):
                result = runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertEqual(result, "updated")
        self.assertEqual(events, ["landed", "review", "promote"])

    def test_normal_update_review_uses_actual_isolated_landing_metadata(self):
        root = Path(self.tempdir.name)
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._last_write_result = IsolatedWriteResult(
            output="updated",
            commits_landed=1,
            start_commit="base123",
            landed_commit="tip456",
            changed_paths=("MEMORY.md",),
        )

        with (
            patch("rightmemory.runtime._git_rightmemory_diff", return_value="diff text") as diff,
            patch("rightmemory.runtime.UpdateReviewStore.create_review") as create,
        ):
            runtime._create_update_review("unrelated-head", "updated summary")

        diff.assert_called_once_with(root, "base123", "tip456")
        self.assertEqual(create.call_args.kwargs["base_commit"], "base123")
        self.assertEqual(create.call_args.kwargs["update_commit"], "tip456")
        self.assertEqual(create.call_args.kwargs["write_surface"], "Memory")

    def test_review_correction_mode_never_creates_another_review(self):
        root = Path(self.tempdir.name)
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config, update_mode="review-correction")
        runtime._last_write_result = IsolatedWriteResult(
            output="corrected",
            commits_landed=1,
            start_commit="base123",
            landed_commit="tip456",
            changed_paths=("MEMORY.md", "corrections.md"),
        )

        with patch("rightmemory.runtime.UpdateReviewStore.create_review") as create:
            runtime._create_update_review("base123", "corrected")

        create.assert_not_called()

    def test_isolated_run_holds_main_session_lock_around_supervisor_execution(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        events = []

        class FakeSessionLock:
            def __enter__(self):
                events.append("main_session_lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("main_session_lock_exit")

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                events.append("supervisor_start")
                output = callback(worktree)
                events.append("supervisor_end")
                return IsolatedWriteResult(output=output, commits_landed=1)

        def locked(session_id):
            events.append(("main_session_locked", session_id))
            return FakeSessionLock()

        def nested(_runtime, _worktree, _state_root, session_id, message):
            events.append(("nested", session_id, message))
            return "nested output"

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=main_root)

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FakeSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", nested),
        ):
            runtime = RightMemoryRuntime(config)
            runtime.sessions.locked = locked
            result = runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertEqual(result, "nested output")
        self.assertEqual(
            events,
            [
                ("main_session_locked", "agent-session"),
                "main_session_lock_enter",
                "supervisor_start",
                ("nested", "agent-session", "remember one"),
                "supervisor_end",
                "main_session_lock_exit",
            ],
        )

    def test_successful_isolated_run_promotes_temp_session_and_provider_state(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        seeded = []
        temp_roots = []
        self._write_runtime_state(main_root, "update", "agent-session", history='["old message"]', provider='{"old": true}')

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                return IsolatedWriteResult(output=callback(worktree), commits_landed=1)

        def nested(_runtime, _worktree, state_root, session_id, _message):
            temp_roots.append(state_root)
            seeded.append(
                (
                    self._runtime_history_path(state_root, "update", session_id).read_text(encoding="utf-8"),
                    self._provider_session_path(state_root, "update", session_id).read_text(encoding="utf-8"),
                )
            )
            self._write_runtime_state(
                state_root,
                "update",
                session_id,
                history='["new message"]',
                provider='{"new": true}',
            )
            return "nested output"

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=main_root)

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FakeSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", nested),
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertEqual(result, "nested output")
        self.assertEqual(seeded, [('["old message"]', '{"old": true}')])
        self.assertEqual(
            self._runtime_history_path(main_root, "update", "agent-session").read_text(encoding="utf-8"),
            '["new message"]',
        )
        self.assertEqual(
            self._provider_session_path(main_root, "update", "agent-session").read_text(encoding="utf-8"),
            '{"new": true}',
        )
        self.assertEqual(len(temp_roots), 1)
        self.assertFalse(temp_roots[0].exists())

    def test_failed_isolated_run_discards_temp_session_and_provider_state(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        temp_roots = []
        self._write_runtime_state(main_root, "update", "agent-session", history='["old message"]', provider='{"old": true}')

        class FailingSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                callback(worktree)
                raise RuntimeError("validation failed")

        def nested(_runtime, _worktree, state_root, session_id, _message):
            temp_roots.append(state_root)
            self._write_runtime_state(
                state_root,
                "update",
                session_id,
                history='["new message"]',
                provider='{"new": true}',
            )
            return "nested output"

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=main_root)

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FailingSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", nested),
        ):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(RuntimeError) as caught:
                runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertIn("validation failed", str(caught.exception))
        self.assertEqual(
            self._runtime_history_path(main_root, "update", "agent-session").read_text(encoding="utf-8"),
            '["old message"]',
        )
        self.assertEqual(
            self._provider_session_path(main_root, "update", "agent-session").read_text(encoding="utf-8"),
            '{"old": true}',
        )
        self.assertEqual(len(temp_roots), 1)
        self.assertFalse(temp_roots[0].exists())

    def test_cli_agent_isolated_run_does_not_seed_prior_provider_session(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "reviewer-123"
        old_provider = self._provider_record_json("reviewer", "agent-session", "old-thread")
        new_provider = self._provider_record_json("reviewer", "agent-session", "new-thread")
        self._write_runtime_state(main_root, "reviewer", "agent-session", history='["old message"]', provider=old_provider)

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                return IsolatedWriteResult(output=callback(worktree), commits_landed=1)

        def nested(_runtime, _worktree, state_root, session_id, _message):
            self.assertEqual(
                self._runtime_history_path(state_root, "reviewer", session_id).read_text(encoding="utf-8"),
                '["old message"]',
            )
            self.assertFalse(self._provider_session_path(state_root, "reviewer", session_id).exists())
            self._write_runtime_state(
                state_root,
                "reviewer",
                session_id,
                history='["new message"]',
                provider=new_provider,
            )
            return "nested output"

        config = RuntimeConfig(
            role="reviewer",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=main_root,
        )

        with (
            patch("rightmemory.runtime.CliAgentExecutor"),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FakeSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", nested),
        ):
            runtime = RightMemoryRuntime(config)
            result = runtime._run_session_turn_isolated("agent-session", "review one")

        self.assertEqual(result, "nested output")
        provider = json.loads(self._provider_session_path(main_root, "reviewer", "agent-session").read_text(encoding="utf-8"))
        self.assertEqual(provider["provider_session_id"], "new-thread")

    def test_failed_cli_agent_isolated_run_keeps_prior_provider_session(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "reviewer-123"
        old_provider = self._provider_record_json("reviewer", "agent-session", "old-thread")
        new_provider = self._provider_record_json("reviewer", "agent-session", "new-thread")
        self._write_runtime_state(main_root, "reviewer", "agent-session", history='["old message"]', provider=old_provider)

        class FailingSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback):
                callback(worktree)
                raise RuntimeError("validation failed")

        def nested(_runtime, _worktree, state_root, session_id, _message):
            self.assertFalse(self._provider_session_path(state_root, "reviewer", session_id).exists())
            self._write_runtime_state(
                state_root,
                "reviewer",
                session_id,
                history='["new message"]',
                provider=new_provider,
            )
            return "nested output"

        config = RuntimeConfig(
            role="reviewer",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=main_root,
        )

        with (
            patch("rightmemory.runtime.CliAgentExecutor"),
            patch("rightmemory.runtime.IsolatedWriteSupervisor", FailingSupervisor),
            patch.object(RightMemoryRuntime, "_run_session_turn_in_worktree", nested),
        ):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(RuntimeError):
                runtime._run_session_turn_isolated("agent-session", "review one")

        provider = json.loads(self._provider_session_path(main_root, "reviewer", "agent-session").read_text(encoding="utf-8"))
        self.assertEqual(provider["provider_session_id"], "old-thread")
        self.assertTrue(ProviderSessionStore.is_internal_provider_session(main_root, "codex", "new-thread"))

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

        def run_stateless_turn(message):
            events.append(("agent", message))
            return EMPTY_RETRIEVE_SELECTION_JSON

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_stateless_turn.side_effect = run_stateless_turn
            runtime = RightMemoryRuntime(config)
            runtime.sessions.locked = locked
            result = runtime.run_turn("remember one")

        self.assertEqual(result, "no strong match")
        self.assertEqual(
            events,
            [
                ("locked", NO_SESSION_RIGHTMEMORY_SESSION_ID),
                "lock_enter",
                ("agent", "Daily RightMemory root snapshot\n\n# Query\n\nremember one\n"),
                "lock_exit",
            ],
        )

    def test_rejects_reserved_public_session_id_for_all_runtime_modes(self):
        configs = [
            RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name)),
            RuntimeConfig(
                role="retrieve",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=Path(self.tempdir.name),
            ),
        ]

        for config in configs:
            with self.subTest(runtime_mode=config.runtime_mode):
                context = (
                    patch("rightmemory.runtime.CliAgentExecutor")
                    if config.runtime_mode == "cli-agent"
                    else patch.dict("sys.modules", self._fake_pydantic_modules())
                )
                with context:
                    runtime = RightMemoryRuntime(config)
                    with self.assertRaises(ValueError) as caught:
                        runtime.run_session_turn(NO_SESSION_RIGHTMEMORY_SESSION_ID, "remember one")

                self.assertIn("reserved", str(caught.exception))

    def test_update_run_turn_uses_reserved_internal_session_path(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
        )
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with (
            patch.object(runtime, "_run_session_turn_unlocked", return_value="updated") as run,
            patch.object(runtime, "_create_update_review") as create_review,
        ):
            result = runtime.run_turn("remember one")

        self.assertEqual(result, "updated")
        run.assert_called_once_with(
            NO_SESSION_RIGHTMEMORY_SESSION_ID,
            "remember one",
            allow_internal_session=True,
        )
        create_review.assert_called_once()

    def test_run_turn_preserves_message_history(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            model_kwargs={"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_turn("remember one")
            second = runtime.run_turn("what was that?")

        self.assertEqual(first, "no strong match")
        self.assertEqual(second, "no strong match")
        self.assertIsNone(runtime.agent.calls[0]["message_history"])
        self.assertIsNone(runtime.agent.calls[1]["message_history"])
        self.assertIn("# Prior retrieve conversation", runtime.agent.calls[1]["message"])
        self.assertIn("User: remember one", runtime.agent.calls[1]["message"])
        self.assertIn("Assistant: no strong match", runtime.agent.calls[1]["message"])
        self.assertEqual(
            runtime.agent.calls[0]["model_settings"],
            {"extra_body": {"chat_template_kwargs": {"thinking": True}}},
        )
        self.assertEqual(runtime.agent.calls[0]["usage_limits"].request_limit, 100)

    def test_retrieve_turn_keeps_unselected_recent_submitted_memory_visible(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember first submitted detail",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_session_turn("agent-session", "find one")
            self._write_async_update_state(
                "update-a",
                pending=[
                    {
                        "id": 1,
                        "message": "remember first submitted detail",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                    },
                    {
                        "id": 2,
                        "message": "remember second submitted detail",
                        "submitted_at": "2026-05-19T00:01:00+00:00",
                    },
                ],
            )
            second = runtime.run_session_turn("agent-session", "find two")
            other_session = runtime.run_session_turn("other-session", "find three")

        self.assertEqual(first, "no strong match")
        self.assertEqual(second, "no strong match")
        self.assertEqual(other_session, "no strong match")
        self.assertIn("Recent submitted RightMemory candidates", runtime.agent.calls[0]["message"])
        self.assertIn("remember first submitted detail", runtime.agent.calls[0]["message"])
        self.assertLess(
            runtime.agent.calls[0]["message"].index("# Recent submitted RightMemory candidates"),
            runtime.agent.calls[0]["message"].index("# Query"),
        )
        self.assertTrue(runtime.agent.calls[0]["message"].rstrip().endswith("# Query\n\nfind one"))
        self.assertIn("Recent submitted RightMemory candidates", runtime.agent.calls[1]["message"])
        self.assertIn("remember second submitted detail", runtime.agent.calls[1]["message"])
        self.assertIn("remember first submitted detail", runtime.agent.calls[1]["message"])
        self.assertLess(
            runtime.agent.calls[1]["message"].index("# Recent submitted RightMemory candidates"),
            runtime.agent.calls[1]["message"].index("# Query"),
        )
        self.assertTrue(runtime.agent.calls[1]["message"].rstrip().endswith("# Query\n\nfind two"))
        self.assertIn("Recent submitted RightMemory candidates", runtime.agent.calls[2]["message"])
        self.assertIn("remember first submitted detail", runtime.agent.calls[2]["message"])
        self.assertIn("remember second submitted detail", runtime.agent.calls[2]["message"])
        self.assertLess(
            runtime.agent.calls[2]["message"].index("# Recent submitted RightMemory candidates"),
            runtime.agent.calls[2]["message"].index("# Query"),
        )
        self.assertTrue(runtime.agent.calls[2]["message"].rstrip().endswith("# Query\n\nfind three"))

    def test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n\nremembered root\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            first = runtime.run_session_turn("agent-session", "find root")
            second = runtime.run_session_turn("agent-session", "find again")

        self.assertEqual(first, "no strong match")
        self.assertEqual(second, "no strong match")
        self.assertTrue(runtime.agent.calls[0]["message"].startswith("Daily RightMemory root snapshot\n"))
        self.assertIn("===== MEMORY.md =====", runtime.agent.calls[0]["message"])
        self.assertTrue(runtime.agent.calls[0]["message"].rstrip().endswith("# Query\n\nfind root"))
        self.assertIsNone(runtime.agent.calls[0]["message_history"])
        self.assertIsNone(runtime.agent.calls[1]["message_history"])
        self.assertIn("# Prior retrieve conversation", runtime.agent.calls[1]["message"])
        self.assertIn("User: find root", runtime.agent.calls[1]["message"])
        self.assertIn("Assistant: no strong match", runtime.agent.calls[1]["message"])

        state_path = root / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            state["turns"],
            [
                {"query": "find root", "answer": "no strong match"},
                {"query": "find again", "answer": "no strong match"},
            ],
        )
        self.assertNotIn("Daily RightMemory root snapshot", state_path.read_text(encoding="utf-8"))

    def test_retrieve_turn_does_not_record_context_state_after_failure(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                raise RuntimeError("model failed")

            runtime.agent.run_sync = run_sync
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("agent-session", "find root")

        self.assertFalse((root / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json").exists())

    def test_retrieve_appends_diff_only_when_memory_head_changes(self):
        root = Path(self.tempdir.name)
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "MEMORY.md").write_text("# Root {#root}\n\nfirst\n", encoding="utf-8")
        self._git(root, "add", "MEMORY.md")
        self._git(root, "commit", "-m", "initial memory")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find first")
            (root / "MEMORY.md").write_text("# Root {#root}\n\nsecond\n", encoding="utf-8")
            self._git(root, "add", "MEMORY.md")
            self._git(root, "commit", "-m", "update memory")
            runtime.run_session_turn("agent-session", "find second")
            runtime.run_session_turn("agent-session", "find third")

        second_message = runtime.agent.calls[1]["message"]
        third_message = runtime.agent.calls[2]["message"]
        self.assertIn("# RightMemory root changes since previous retrieve turn", second_message)
        self.assertIn("+second", second_message)
        self.assertNotIn("# RightMemory root changes since previous retrieve turn", third_message)

    def test_retrieve_request_prefix_is_byte_identical_before_first_volatile_block(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Root {#root}\n\nstable\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            first_runtime = RightMemoryRuntime(config)
            first_runtime.run_session_turn("session-a", "find alpha")
            second_runtime = RightMemoryRuntime(config)
            second_runtime.run_session_turn("session-b", "find beta")

        first = first_runtime.agent.calls[0]["message"].split("# Query", 1)[0]
        second = second_runtime.agent.calls[0]["message"].split("# Query", 1)[0]
        self.assertEqual(first, second)

    def test_retrieve_turn_records_recent_submitted_delivery_after_success(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember successful delivery",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def select_candidate(message, **kwargs):
                return type(
                    "Result",
                    (),
                    {"output": RetrieveSelection(recent_candidates=["update-a:1"])},
                )()

            runtime.agent.run_sync = select_candidate
            runtime.run_session_turn("agent-session", "find one")

        state_path = Path(self.tempdir.name) / ".runtime" / "recent_submitted" / "retrieve" / "agent-session.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["session_id"], "agent-session")
        self.assertEqual(state["delivered"], ["update-a:1:2026-05-19T00:00:00+00:00"])

    def test_retrieve_turn_does_not_record_recent_submitted_delivery_after_failure(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember failed delivery retry",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )
        seen_messages = []

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                seen_messages.append(message)
                raise RuntimeError("model failed")

            runtime.agent.run_sync = run_sync
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("agent-session", "find one")

        state_path = Path(self.tempdir.name) / ".runtime" / "recent_submitted" / "retrieve" / "agent-session.json"
        self.assertFalse(state_path.exists())
        self.assertIn("remember failed delivery retry", seen_messages[0])

    def test_cli_agent_retrieve_receives_recent_submitted_memory(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )
        self._write_async_update_state(
            "update-a",
            pending=[
                {
                    "id": 1,
                    "message": "remember cli submitted detail",
                    "submitted_at": "2026-05-19T00:00:00+00:00",
                }
            ],
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_stateless_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "find one")

        self.assertEqual(result, "no strong match")
        executor_class.return_value.run_stateless_turn.assert_called_once()
        (message,) = executor_class.return_value.run_stateless_turn.call_args.args
        self.assertTrue(message.startswith("Daily RightMemory root snapshot\n"))
        self.assertLess(message.index("# Recent submitted RightMemory candidates"), message.index("# Query"))
        self.assertTrue(message.rstrip().endswith("# Query\n\nfind one"))
        self.assertIn("remember cli submitted detail", message)
        state_path = Path(self.tempdir.name) / ".runtime" / "recent_submitted" / "retrieve" / "agent-session.json"
        self.assertFalse(state_path.exists())

    def test_write_role_creates_memory_lock_and_gitignore(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name) / "state",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        self.assertTrue((Path(self.tempdir.name) / ".runtime" / "memory.lock").exists())
        self.assertEqual(
            (Path(self.tempdir.name) / ".gitignore").read_text(encoding="utf-8"),
            "*\n"
            "!MEMORY.md\n"
            "!MEMORY_*.md\n"
            "!PURSUITS.md\n"
            "!PURSUIT_*.md\n"
            "!PURSUIT_RULES.md\n"
            "!corrections.md\n"
            "!shared_views.toml\n"
            "!shares.toml\n"
            "!shared_views/\n"
            "!shared_views/*/\n"
            "!shared_views/*/view.md\n"
            "!shared_views/*/retriever.md\n"
            "!shared_views/*/recipe.toml\n"
            "!shared_views/*/question.toml\n"
            "!shared_views/*/.gitignore\n"
            "!insight_logs/\n"
            "!insight_logs/*.md\n",
        )

    def test_retrieve_role_does_not_record_recent_submitted_state_without_entries(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        self.assertFalse((Path(self.tempdir.name) / ".runtime" / "recent_submitted").exists())

    def test_update_turn_runs_sync_preflight_without_exposing_context(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name) / "state",
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
            state_root=Path(self.tempdir.name) / "state",
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
            state_root=Path(self.tempdir.name) / "state",
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

    def test_dirty_main_guard_runs_sync_reconciler_when_sync_disabled(self):
        repairs = []
        calls = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=False),
        )

        def run_model():
            if not calls:
                calls.append("dirty")
                raise MainMemoryDirtyError(["MEMORY.md"])
            calls.append("model")
            return "updated"

        def repair(runtime, result):
            repairs.append((result.status, result.message, result.files))

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch.object(RightMemoryRuntime, "_run_sync_reconciler", repair),
        ):
            runtime = RightMemoryRuntime(config)
            result, post_sync = runtime._run_isolated_locked_turn(run_model)

        self.assertEqual(result, "updated")
        self.assertEqual(post_sync, None)
        self.assertEqual(calls, ["dirty", "model"])
        self.assertEqual(
            repairs,
            [
                (
                    "dirty",
                    "local main memory has uncommitted changes before automatic semantic work",
                    ["MEMORY.md"],
                )
            ],
        )

    def test_dirty_main_guard_retries_full_sync_preflight_after_repair(self):
        repairs = []
        calls = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        def run_model():
            if not calls:
                calls.append("dirty")
                raise MainMemoryDirtyError(["MEMORY.md"])
            calls.append("model")
            return "updated"

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch.object(RightMemoryRuntime, "_run_sync_reconciler", lambda self, result: repairs.append(result.status)),
        ):
            manager_class.return_value.preflight.side_effect = [
                SyncResult("synced", "local memory is current"),
                SyncResult("synced", "local memory is current"),
            ]
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            result, post_sync = runtime._run_isolated_locked_turn(run_model)

        self.assertEqual(result, "updated")
        self.assertEqual(post_sync, None)
        self.assertEqual(calls, ["dirty", "model"])
        self.assertEqual(repairs, ["dirty"])
        self.assertEqual(manager_class.return_value.preflight.call_count, 2)
        manager_class.return_value.push.assert_called_once()

    def test_dirty_main_guard_fails_after_one_uncleared_repair(self):
        repairs = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name),
        )

        def run_model():
            raise MainMemoryDirtyError(["MEMORY.md"])

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch.object(RightMemoryRuntime, "_run_sync_reconciler", lambda self, result: repairs.append(result.status)),
        ):
            runtime = RightMemoryRuntime(config)
            with self.assertRaises(RuntimeError) as caught:
                runtime._run_isolated_locked_turn(run_model)

        self.assertEqual(repairs, ["dirty"])
        self.assertIn("dirty-main repair did not clear memory files: MEMORY.md", str(caught.exception))

    def test_dirty_push_runs_sync_reconciler_after_update_agent(self):
        repairs = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name) / "state",
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

    def test_runtime_sync_reconciler_loads_selected_memory_root(self):
        memory_root = Path(self.tempdir.name) / "profile-root"
        memory_root.mkdir()
        loaded_roots = []
        nested_calls = []
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=memory_root,
            sync=load_sync_config_for_test(memory_root, enabled=True),
        )

        def fake_load_config(role, memory_root=None):
            loaded_roots.append((role, memory_root))
            return RuntimeConfig(
                role=role,
                model_id="openai/test",
                memory_root=memory_root,
                sync=load_sync_config_for_test(memory_root, enabled=True),
            )

        class FakeNestedRuntime:
            def __init__(self, runtime_config):
                nested_calls.append(("init", runtime_config.memory_root))

            def run_session_turn(self, session_id, message):
                nested_calls.append(("turn", session_id, message))

            def cleanup(self):
                nested_calls.append(("cleanup",))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with (
            patch("rightmemory.runtime.load_config", side_effect=fake_load_config),
            patch("rightmemory.runtime.RightMemoryRuntime", FakeNestedRuntime),
        ):
            runtime._run_sync_reconciler(SyncResult("dirty", "local memory is dirty", ["MEMORY.md"]))

        self.assertEqual(loaded_roots, [("sync-reconciler", memory_root)])
        self.assertEqual(nested_calls[0], ("init", memory_root))
        self.assertEqual(nested_calls[-1], ("cleanup",))

    def test_prune_turn_checks_generation_after_sync_preflight(self):
        events = []
        memory_root = Path(self.tempdir.name)
        config = RuntimeConfig(
            role="pruner",
            model_id="openai/test",
            memory_root=memory_root,
            state_root=memory_root / "state",
            sync=load_sync_config_for_test(memory_root, enabled=True),
        )

        def fake_prune_due_status(root, pruner_config):
            events.append(("status", root))
            return PruneDueStatus(
                due=False,
                message="prune not due after sync",
                commits_since_boundary=1,
                generation_commits=70,
            )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch("rightmemory.runtime.prune_due_status", side_effect=fake_prune_due_status),
        ):
            manager_class.return_value.preflight.side_effect = lambda: events.append(("preflight", memory_root)) or SyncResult(
                "synced",
                "local memory is current",
            )
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            result = runtime.run_prune_turn("prune-session", PrunerConfig(memory_root=memory_root))

        self.assertEqual(result, "prune not due after sync")
        self.assertEqual(events, [("preflight", memory_root), ("status", memory_root)])
        self.assertEqual(runtime.agent.calls, [])

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

    def test_historian_turn_does_not_run_sync_preflight(self):
        config = RuntimeConfig(
            role="historian",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find old memory")

        manager_class.assert_not_called()

    def test_sync_reconciler_receives_sync_push_tool_when_sync_enabled(self):
        for role in ("dreamer", "pruner", "reviewer", "sync-reconciler", "update"):
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

    def test_history_roles_receive_git_history_read_tools(self):
        for role in ("historian", "pruner"):
            with self.subTest(role=role):
                config = RuntimeConfig(
                    role=role,
                    model_id="openai/test",
                    memory_root=Path(self.tempdir.name),
                )

                with patch.dict("sys.modules", self._fake_pydantic_modules()):
                    runtime = RightMemoryRuntime(config)

                tool_names = {tool.__name__ for tool in runtime.agent.tools}
                self.assertIn("git_log", tool_names)
                self.assertIn("git_show_file", tool_names)
                if role == "historian":
                    self.assertNotIn("git_commit", tool_names)
                else:
                    self.assertIn("git_commit", tool_names)

    def test_insight_role_tools_exclude_memory_validation(self):
        config = RuntimeConfig(
            role="insight",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            state_root=Path(self.tempdir.name) / "state",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = [tool.__name__ for tool in runtime.agent.tools]
        self.assertIn("create_file", tool_names)
        self.assertIn("git_commit", tool_names)
        self.assertNotIn("validate_memory", tool_names)

    def test_write_roles_do_not_receive_sync_push_tool_when_sync_disabled(self):
        for role in ("dreamer", "pruner", "reviewer", "sync-reconciler", "update"):
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

    def test_update_prompt_owns_unified_lifecycle_and_bounded_correction_surfaces(self):
        instructions = build_instructions(Path("/memory"), "update")

        self.assertIn("evolving account", instructions)
        self.assertIn("MEMORY_agent-corrections-writing.md", instructions)
        self.assertIn("MEMORY_agent-corrections-design.md", instructions)
        self.assertIn("corrections.md", instructions)
        self.assertIn("15", instructions)
        self.assertIn("MEMORY.md", instructions)
        self.assertIn("PURSUITS.md", instructions)

    def test_run_session_turn_preserves_message_history_on_disk(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            first_runtime = RightMemoryRuntime(config)
            first = first_runtime.run_session_turn("agent-session", "remember one")
            first_runtime.cleanup()

            second_runtime = RightMemoryRuntime(config)
            second = second_runtime.run_session_turn("agent-session", "what was that?")

        self.assertEqual(first, "no strong match")
        self.assertEqual(second, "no strong match")
        self.assertIsNone(first_runtime.agent.calls[0]["message_history"])
        self.assertIsNone(second_runtime.agent.calls[0]["message_history"])
        self.assertIn("# Prior retrieve conversation", second_runtime.agent.calls[0]["message"])
        self.assertIn("User: remember one", second_runtime.agent.calls[0]["message"])
        self.assertIn("Assistant: no strong match", second_runtime.agent.calls[0]["message"])
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertFalse(history_path.exists())
        retrieve_state_path = (
            Path(self.tempdir.name) / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json"
        )
        retrieve_state = json.loads(retrieve_state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            retrieve_state["turns"],
            [
                {"query": "remember one", "answer": "no strong match"},
                {"query": "what was that?", "answer": "no strong match"},
            ],
        )
        gitignore_path = Path(self.tempdir.name) / ".runtime" / ".gitignore"
        self.assertEqual(gitignore_path.read_text(encoding="utf-8"), "*\n")

    def test_strips_visible_thinking_from_output_and_saved_history(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name) / "memory",
            state_root=Path(self.tempdir.name),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                class FakeResult:
                    output = "I should inspect the memory first.</think> final answer"

                    def all_messages_json(self):
                        return json.dumps(
                            [
                                {
                                    "parts": [
                                        {
                                            "part_kind": "text",
                                            "content": "I should inspect the memory first.</think> final answer",
                                        }
                                    ],
                                    "kind": "response",
                                }
                            ]
                        ).encode()

                return FakeResult()

            runtime.agent.run_sync = run_sync
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "final answer")
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "update" / "agent-session.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(history[0]["parts"][0]["content"], "final answer")

    def test_sanitizer_preserves_structured_thinking_and_non_response_text(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name) / "memory",
            state_root=Path(self.tempdir.name),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                class FakeResult:
                    output = "<think>hidden but structured elsewhere</think> final answer"

                    def all_messages_json(self):
                        return json.dumps(
                            [
                                {
                                    "parts": [
                                        {
                                            "part_kind": "user-prompt",
                                            "content": "literal user text </think> keep this",
                                        }
                                    ],
                                    "kind": "request",
                                },
                                {
                                    "parts": [
                                        {
                                            "part_kind": "thinking",
                                            "content": "good structured thinking </think> keep this",
                                        },
                                        {
                                            "part_kind": "tool-return",
                                            "content": "tool output </think> keep this",
                                        },
                                        {
                                            "part_kind": "text",
                                            "content": "<think>visible leaked thinking</think> final answer",
                                        },
                                    ],
                                    "kind": "response",
                                },
                            ]
                        ).encode()

                return FakeResult()

            runtime.agent.run_sync = run_sync
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "final answer")
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "update" / "agent-session.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(history[0]["parts"][0]["content"], "literal user text </think> keep this")
        self.assertEqual(history[1]["parts"][0]["content"], "good structured thinking </think> keep this")
        self.assertEqual(history[1]["parts"][1]["content"], "tool output </think> keep this")
        self.assertEqual(history[1]["parts"][2]["content"], "final answer")

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

        self.assertEqual(result, "no strong match")
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertFalse(history_path.exists())
        retrieve_state_path = (
            Path(self.tempdir.name) / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json"
        )
        self.assertEqual(
            json.loads(retrieve_state_path.read_text(encoding="utf-8"))["turns"],
            [{"query": "remember one", "answer": "no strong match"}],
        )
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            ["run_started", "history_loaded", "model_started", "model_finished", "run_finished"],
        )
        self.assertEqual(events[0]["message"], "remember one")
        self.assertEqual(events[0]["model_id"], "openai/test")
        self.assertEqual(events[3]["output"], "no strong match")

    def test_cli_agent_debug_trace_uses_cli_model_id(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex", model="gpt-5"),
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_stateless_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "no strong match")
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["run_started", "model_started", "model_finished", "run_finished"])
        self.assertEqual(events[0]["model_id"], "gpt-5")
        self.assertEqual(events[2]["output"], "no strong match")

    def test_debug_trace_records_tool_events(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            debug_trace=True,
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            (Path(self.tempdir.name) / "MEMORY_SKILL_alpha.md").write_text("# Alpha\n", encoding="utf-8")
            with runtime._debug_trace("agent-session"):
                read_skill = next(tool for tool in runtime.agent.kwargs["tools"] if tool.__name__ == "read_skill")
                read_skill("alpha")

        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in events], ["tool_started", "tool_finished"])
        self.assertEqual(events[0]["tool"], "read_skill")

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
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            model_kwargs={"api_version": "2026-01-01"},
        )

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
                tools["read_skill"]("../bad")

        self.assertIn("id must contain only letters", str(caught.exception))

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
        self.assertEqual(tool_names, {"read_detail", "read_markdown", "read_skill", "read_mf"})
        self.assertNotIn("read", tool_names)
        self.assertNotIn("grep", tool_names)
        self.assertNotIn("glob", tool_names)
        self.assertNotIn("read_command", tool_names)
        self.assertNotIn("search_files", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("create_file", tool_names)
        self.assertNotIn("delete_file", tool_names)
        self.assertNotIn("rename_file", tool_names)
        self.assertNotIn("apply_patch", tool_names)
        self.assertNotIn("git_add", tool_names)
        self.assertNotIn("git_discard", tool_names)
        self.assertNotIn("git_commit", tool_names)

    def _write_runtime_state(self, root: Path, role: str, session_id: str, *, history: str, provider: str) -> None:
        history_path = self._runtime_history_path(root, role, session_id)
        provider_path = self._provider_session_path(root, role, session_id)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        provider_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(history, encoding="utf-8")
        provider_path.write_text(provider, encoding="utf-8")

    def _runtime_history_path(self, root: Path, role: str, session_id: str) -> Path:
        return root / ".runtime" / "sessions" / role / f"{session_id}.json"

    def _provider_session_path(self, root: Path, role: str, session_id: str) -> Path:
        return root / ".runtime" / "agent_cli_sessions" / role / f"{session_id}.json"

    def _provider_record_json(self, role: str, session_id: str, provider_session_id: str) -> str:
        return json.dumps(
            {
                "provider": "codex",
                "provider_session_id": provider_session_id,
                "role": role,
                "rightmemory_session_id": session_id,
                "created_at": "2026-05-20T00:00:00+00:00",
                "updated_at": "2026-05-20T00:00:00+00:00",
            }
        )

    def _git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()

    def _fake_pydantic_modules(self):
        class FakeModelRetry(Exception):
            pass

        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.model = kwargs["model"]
                self.tools = kwargs["tools"]
                self.output_type = kwargs.get("output_type")

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
                output = self.output_type() if self.output_type is not None else f"reply {call_count}"

                class FakeResult:
                    def all_messages(self):
                        return [f"message {call_count}"]

                    def all_messages_json(self):
                        return json.dumps(self.all_messages()).encode()

                FakeResult.output = output
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

    def _write_async_update_state(self, session_id, *, pending=None, current_batch=None):
        state_path = Path(self.tempdir.name) / ".runtime" / "async" / "update" / f"{session_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "session_id": session_id,
                    "role": "update",
                    "phase": "waiting",
                    "started_at": "2026-05-19T00:00:00+00:00",
                    "finished_at": None,
                    "pid": None,
                    "result": None,
                    "error": None,
                    "next_flush_at": "2026-05-19T01:00:00+00:00",
                    "current_batch": current_batch or [],
                    "pending": pending or [],
                    "next_id": 10,
                }
            ),
            encoding="utf-8",
        )


class PromptTests(unittest.TestCase):
    def test_cli_agent_prompt_assembles_without_standalone_tools(self):
        for role in (
            "dreamer",
            "historian",
            "insight",
            "pruner",
            "retrieve",
            "reviewer",
            "shared-view-builder",
            "sync-reconciler",
            "update",
        ):
            memory_root = Path("/home/example/.rightmemory")
            prompt = build_cli_agent_instructions(memory_root, role)

            self.assertIn(f"The configured memory root is {memory_root}.", prompt)
            self.assertIn("RightMemory Schema", prompt)
            self.assertIn(f"{role.title().replace('-', ' ')} Role", prompt)
            self.assertNotIn("Command-selected behavior", prompt)
            self.assertNotIn("Standalone adaptation", prompt)
            self.assertNotIn("validate_memory", prompt)
            self.assertNotIn("retrieve_shared_view", prompt)
            self.assertNotIn("git_discard", prompt)
            self.assertNotIn("sync_push", prompt)
            self.assertNotIn("{{MEMORY_ROOT}}", prompt)
            self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_cli_agent_retrieve_prompt_requires_strict_selection_without_ask_command(self):
        prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), "retrieve")

        self.assertIn("MF#", prompt)
        self.assertIn("MQ#", prompt)
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn(".runtime/shared_views/imports/<mf-id>/dist/MEMORY.md", prompt)
        self.assertIn("The `read_*` names", prompt)
        self.assertIn("provider CLI's read-only file tools", prompt)
        self.assertNotIn("rightmemory shared-view retrieve", prompt)
        self.assertNotIn("rightmemory shared-view ask", prompt)
        self.assertNotIn("retrieve_shared_view", prompt)

    def test_cli_agent_prompt_rejects_unknown_role(self):
        with self.assertRaises(ValueError) as caught:
            build_cli_agent_instructions(Path("/home/example/.rightmemory"), "curator")

        self.assertIn("role must be one of:", str(caught.exception))

    def test_standalone_prompts_assemble_for_each_role(self):
        for role in ("dreamer", "historian", "insight", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"):
            prompt = build_instructions(Path("/home/example/.rightmemory"), role)

            self.assertIn("RightMemory Schema", prompt)
            self.assertIn(f"{role.title().replace('-', ' ')} Role", prompt)
            self.assertIn("Command-selected behavior", prompt)
            self.assertNotIn("{{MEMORY_ROOT}}", prompt)
            self.assertNotIn("{{SKILLS_ROOT}}", prompt)

    def test_dreamer_prompt_no_longer_mentions_dream_logs(self):
        prompt = build_instructions(Path("/memory"), "dreamer")

        self.assertNotIn("dream_logs", prompt)
        self.assertNotIn("dream report", prompt.lower())
        self.assertIn("# Open Context Questions", prompt)

    def test_insight_prompt_uses_insight_logs_and_excludes_memory_validation(self):
        prompt = build_instructions(Path("/memory"), "insight")

        self.assertIn("Insight Role", prompt)
        self.assertIn("insight_logs/", prompt)
        self.assertIn("operator hint", prompt)
        self.assertIn("Transcript review only extracts updater candidates", prompt)
        self.assertNotIn("validate_memory", prompt)
        self.assertNotIn("dream_logs", prompt)

    def test_sync_reconciler_standalone_prompt_includes_registry_tool_scope(self):
        prompt = build_instructions(Path("/memory"), "sync-reconciler")

        self.assertIn("Commit and edit tools are scoped", prompt)
        self.assertIn("shared_views.toml", prompt)
        self.assertIn("shares.toml", prompt)
        self.assertIn("shared_views/<view-id>/view.md", prompt)
        self.assertIn("insight_logs/*.md", prompt)
        self.assertIn("git_discard", prompt)
        self.assertNotIn("Commit tools are scoped to `MEMORY.md` and `MEMORY_*.md`", prompt)

    def test_standalone_prompt_does_not_embed_memory_root_path(self):
        first = build_instructions(Path("/home/example/.rightmemory/.runtime/worktrees/update-111"), "update")
        second = build_instructions(Path("/home/example/.rightmemory/.runtime/worktrees/update-222"), "update")

        self.assertEqual(first, second)
        self.assertNotIn("/home/example/.rightmemory", first)
        self.assertIn("store-relative paths", first)

    def test_schema_level_memory_skill_guidance_is_in_role_prompts(self):
        retrieve_instructions = build_instructions(Path("/memory"), "retrieve")
        self.assertIn("S#", retrieve_instructions)
        self.assertIn("MEMORY_SKILL_<slug>.md", retrieve_instructions)
        self.assertIn("complete S# instruction", retrieve_instructions)

        for role in ("update", "reviewer", "dreamer"):
            with self.subTest(role=role):
                instructions = build_instructions(Path("/memory"), role)
                self.assertIn("reusable instruction asset", instructions)
                self.assertIn("ordinary memory", instructions)
                self.assertIn("rigid", instructions)

    def test_dreamer_prompt_includes_pending_semantic_upgrade_context(self):
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

        prompt = build_instructions(Path("/home/example/.rightmemory"), "dreamer", semantic_upgrades=context)

        self.assertIn("example-note", prompt)
        self.assertIn("Reconsider older memory.", prompt)

    def test_non_dreamer_prompt_ignores_semantic_upgrade_context(self):
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

        prompt = build_instructions(Path("/home/example/.rightmemory"), "update", semantic_upgrades=context)

        self.assertNotIn("example-note", prompt)

    def test_external_skill_assets_are_included_in_wheel(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(force_include["skills"], "rightmemory/skills")
        self.assertNotIn("rightmemory/prompts", force_include)
        self.assertNotIn("rightmemory/semantic_upgrades", force_include)


def load_sync_config_for_test(memory_root: Path, enabled: bool):
    from rightmemory.config import SyncConfig

    return SyncConfig(memory_root=memory_root, enabled=enabled)


if __name__ == "__main__":
    unittest.main()
