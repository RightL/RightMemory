import tempfile
import types
import json
import os
import subprocess
import tomllib
import unittest
from dataclasses import replace
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
from rightmemory.reference import REFERENCE_FILES
from rightmemory.provider_sessions import ProviderSessionStore
from rightmemory.provider_threads import ProviderThreadStore
from rightmemory.prune import PruneDueStatus
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry
from rightmemory.runtime import (
    SYNC_REPAIR_SESSION_ID,
    RightMemoryRuntime,
    _IsolatedStateOverlay,
    build_model,
)
from rightmemory.session import MessageSessionStore
from rightmemory.semantic_operation import OperationEffect, SemanticOperationStore
from rightmemory.retrieve_selection import RetrieveSelection
from rightmemory.shared_view_files import FileViewPublishResult
from rightmemory.sync import SyncResult
from rightmemory.update_queue import UpdateCandidate
from rightmemory.update_record import UpdateRecord, UpdateRecordStore


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
            reasoning_effort = "high"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("retrieve")

        self.assertEqual(config.role, "retrieve")
        self.assertIsNone(config.model_id)
        self.assertEqual(config.runtime_mode, "cli-agent")
        self.assertEqual(
            config.agent_cli,
            AgentCliConfig(provider="codex", model="gpt-5", reasoning_effort="high"),
        )

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_rejects_invalid_reasoning_effort(self):
        config_path = self._write_config(
            """
            [agent_cli]
            provider = "codex"

            [retrieve.agent_cli]
            model = "gpt-5"
            reasoning_effort = "extreme"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("[retrieve.agent_cli].reasoning_effort must be one of", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_agent_cli_rejects_reasoning_effort_for_claude(self):
        config_path = self._write_config(
            """
            [agent_cli]
            provider = "claude"

            [retrieve.agent_cli]
            model = "sonnet"
            reasoning_effort = "high"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_config("retrieve")

        self.assertIn("requires provider = \"codex\"", str(caught.exception))

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
        self.assertEqual(config.trigger_candidates, 15)
        self.assertEqual(config.target_batch_candidates, 30)
        self.assertEqual(config.max_wait_seconds, 24 * 60 * 60)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_parses_custom_values(self):
        config_path = self._write_config(
            """
            [update.model]
            model_id = "openai/update"

            [update.async]
            trigger_candidates = 18
            target_batch_candidates = 22
            max_wait_seconds = 7200
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            async_config = load_async_update_config()
            runtime_config = load_config("update")

        self.assertEqual(async_config.trigger_candidates, 18)
        self.assertEqual(async_config.target_batch_candidates, 22)
        self.assertEqual(async_config.max_wait_seconds, 7200)
        self.assertEqual(runtime_config.model_id, "openai/update")

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_async_update_config_rejects_invalid_values(self):
        cases = [
            ("trigger_candidates = 0", "[update.async].trigger_candidates must be a positive integer"),
            ("trigger_candidates = true", "[update.async].trigger_candidates must be a positive integer"),
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
    def test_async_update_config_rejects_target_below_trigger(self):
        config_path = self._write_config(
            """
            [update.async]
            trigger_candidates = 15
            target_batch_candidates = 14
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch(
            "pathlib.Path.exists",
            return_value=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "target_batch_candidates must be greater than or equal to",
            ):
                load_async_update_config()

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

    def test_builds_deepseek_model_with_deepseek_profile_and_openai_transport(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="deepseek-v4-flash",
            api_base="https://example.test/v1",
            api_key="token",
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            model = build_model(config)

        self.assertEqual(model.model_name, "deepseek-v4-flash")
        self.assertEqual(model.provider.kwargs, {"base_url": "https://example.test/v1", "api_key": "token"})
        self.assertEqual(model.profile, {"provider": "deepseek", "model_name": "deepseek-v4-flash"})

    def test_deepseek_v4_profile_preserves_thinking_tool_loops(self):
        try:
            __import__("pydantic_ai")
        except ImportError:
            self.skipTest("pydantic-ai is not installed")
        config = RuntimeConfig(
            role="retrieve",
            model_id="deepseek-v4-flash",
            api_base="https://example.test/v1",
            api_key="token",
        )

        model = build_model(config)

        self.assertTrue(model.profile["supports_thinking"])
        self.assertFalse(model.profile["openai_supports_tool_choice_required"])
        self.assertEqual(model.profile["openai_chat_thinking_field"], "reasoning_content")
        self.assertEqual(model.profile["openai_chat_send_back_thinking_parts"], "field")

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
        codex_runner = object()
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.has_saved_session.return_value = False
            executor_class.return_value.run_session_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config, codex_runner=codex_runner)
            result = runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(result, "no strong match")
        call = executor_class.return_value.run_session_turn.call_args
        self.assertEqual(call.args[0], "agent-session")
        self.assertIsNotNone(call.kwargs["prefix_context"])
        executor_class.assert_called_once_with(
            Path(self.tempdir.name),
            "retrieve",
            AgentCliConfig(provider="codex"),
            codex_runner=codex_runner,
            state_root=Path(self.tempdir.name),
            fresh_provider_session=False,
            trace_event=runtime._trace,
        )

    def test_cli_agent_resumed_retrieve_does_not_reseed_prefix(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=Path(self.tempdir.name),
        )

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.has_saved_session.return_value = True
            executor_class.return_value.run_session_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config)
            result = runtime.run_session_turn("agent-session", "follow-up")

        self.assertEqual(result, "no strong match")
        call = executor_class.return_value.run_session_turn.call_args
        self.assertIsNone(call.kwargs["prefix_context"])

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
            def __init__(self, config, *, codex_runner=None):
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
        nested_runners = []
        calls = []
        codex_runner = object()

        class FakeNestedRuntime:
            def __init__(self, config, *, codex_runner=None):
                self.config = config
                nested_configs.append(config)
                nested_runners.append(codex_runner)

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
            runtime = RightMemoryRuntime(config, codex_runner=codex_runner)

        with patch("rightmemory.runtime.RightMemoryRuntime", FakeNestedRuntime):
            result = runtime._run_session_turn_in_worktree(worktree, state_root, "agent-session", "review one")

        self.assertEqual(result, "cli result")
        self.assertEqual(nested_configs[0].memory_root, worktree)
        self.assertEqual(nested_configs[0].state_root, state_root)
        self.assertTrue(nested_configs[0].fresh_provider_session)
        self.assertFalse(nested_configs[0].sync.enabled)
        self.assertEqual(nested_runners, [codex_runner])
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

    @unittest.skipUnless(os.name == "nt", "Windows path-length regression")
    def test_cli_agent_update_batch_supports_long_isolated_session_path(self):
        base = Path(self.tempdir.name)
        padding_length = 70 - len(str(base)) - 1
        self.assertGreater(padding_length, 0)
        root = base / ("p" * padding_length)
        root.mkdir()
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n- `one` initial -> []\n",
            encoding="utf-8",
        )
        (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        self._git(root, "add", "MEMORY.md", "PURSUITS.md")
        self._git(root, "commit", "-m", "initial memory")
        config = RuntimeConfig(
            role="update",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=root,
            debug_trace=True,
        )
        codex_runner = unittest.mock.Mock()
        codex_runner.claim_opportunistic_cleanup.return_value = False

        def run_codex_turn(**kwargs):
            kwargs["on_thread_started"]("thread-1")
            timing = types.SimpleNamespace(
                client_start_ms=0.0,
                thread_open_ms=0.0,
                turn_ms=0.0,
                thread_release_ms=0.0,
                thread_release_error_type=None,
                server_duration_ms=0,
                total_ms=0.0,
                usage=None,
            )
            kwargs["on_timing"](timing)
            return types.SimpleNamespace(
                provider_session_id="thread-1",
                text="done",
                timing=timing,
            )

        codex_runner.run_turn.side_effect = run_codex_turn
        batch_id = f"update-batch-{'a' * 64}"
        state_root = SemanticOperationStore(root).state_root(batch_id)
        legacy_lock = state_root / ".runtime" / "sessions" / "update" / f"{batch_id}.lock"
        bounded_paths = MessageSessionStore(state_root, "update").paths(batch_id)

        runtime = RightMemoryRuntime(config, codex_runner=codex_runner)
        try:
            result = runtime.run_session_turn(
                batch_id,
                "connectivity check",
                operation_id=batch_id,
            )
        finally:
            runtime.cleanup()

        self.assertEqual(result, "done")
        self.assertEqual(len(str(root)), 70)
        self.assertEqual(len(str(legacy_lock)), 269)
        self.assertLess(len(str(bounded_paths.lock)), 260)
        self.assertEqual(bounded_paths.lock.parent.name, "hashed")
        codex_runner.close.assert_not_called()

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

            def run(self, callback, **_kwargs):
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
        nested_worktree, state_root, nested_session_id, _nested_message = nested.call_args.args
        self.assertEqual(nested_worktree, worktree)
        self.assertTrue(state_root.is_relative_to(main_root / ".runtime" / "operations" / "state"))
        self.assertEqual(nested_session_id, "agent-session")
        self.assertFalse(state_root.exists())

    def test_runtime_recovers_completed_semantic_operation_without_running_model_again(self):
        root = Path(self.tempdir.name)
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Test User")
        (root / "MEMORY.md").write_text("# Domain {#domain}\n\n- `one` initial → []\n", encoding="utf-8")
        (root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        self._git(root, "add", "MEMORY.md", "PURSUITS.md")
        self._git(root, "commit", "-m", "initial memory")
        calls = []

        def nested(worktree, _state_root, _session_id, _message):
            calls.append("model")
            memory = worktree / "MEMORY.md"
            memory.write_text(memory.read_text(encoding="utf-8") + "- `two` remembered → []\n", encoding="utf-8")
            self._git(worktree, "add", "MEMORY.md")
            self._git(worktree, "commit", "-m", "memory: remember two")
            return "updated once"

        config = RuntimeConfig(role="dreamer", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        with patch.object(runtime, "_run_session_turn_in_worktree", side_effect=nested):
            first = runtime.run_session_turn("dream-session", "dream", operation_id="dream-op-1")
            second = runtime.run_session_turn("dream-session", "dream", operation_id="dream-op-1")

        self.assertEqual((first, second), ("updated once", "updated once"))
        self.assertEqual(calls, ["model"])
        self.assertEqual(self._git(root, "rev-list", "--count", "HEAD~1..HEAD"), "1")
        self.assertIn("RightMemory-Operation: dream-op-1", self._git(root, "log", "-1", "--format=%B"))

    def test_terminal_operation_pressure_effect_is_idempotently_applied(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        input_data = {"role": "update", "session_id": "agent-session", "message": "remember"}
        store.begin("update-op-1", input_data)
        store.prepare_outcome(
            "update-op-1",
            output="updated",
            start_commit="base123",
            changed_paths=("MEMORY.md",),
            effects=(
                OperationEffect(
                    "memory-pressure",
                    metadata={"dreamer_points": 2.0, "insight_points": 3.0},
                ),
            ),
        )
        store.complete_commit("update-op-1", "tip456")
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        state = type("State", (), {})()

        with patch("rightmemory.runtime.record_memory_change_pressure_once") as pressure:
            runtime._run_operation_effects("update-op-1", state)
            runtime._run_operation_effects("update-op-1", state)

        pressure.assert_called_once_with(
            root,
            "update-op-1",
            dreamer_points=2.0,
            insight_points=3.0,
        )
        self.assertEqual(store.list_pending_effects("update-op-1"), ())

    def test_older_session_state_effect_cannot_overwrite_newer_state(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        states = []
        for operation_id, content in (("update-state-old", "old"), ("update-state-new", "new")):
            store.begin(operation_id, {"role": "update", "session_id": "agent-session"})
            store.prepare_outcome(
                operation_id,
                output=content,
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("session-state", metadata={"session_id": "agent-session"}),),
            )
            store.complete_no_change(operation_id)
            state = _IsolatedStateOverlay(root, "update", "agent-session", operation_id=operation_id)
            history = MessageSessionStore(state.overlay_root, "update").paths("agent-session").history
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(content, encoding="utf-8")
            states.append(state)

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._run_operation_effects("update-state-new", states[1])
        runtime._run_operation_effects("update-state-old", states[0])

        main_history = MessageSessionStore(root, "update").paths("agent-session").history
        self.assertEqual(main_history.read_text(encoding="utf-8"), "new")

    def test_long_session_state_promotes_with_namespaced_watermark(self):
        root = Path(self.tempdir.name)
        operation_id = "update-state-long-session"
        session_id = f"update-batch-{'a' * 64}"
        store = SemanticOperationStore(root)
        store.begin(operation_id, {"role": "update", "session_id": session_id})
        store.prepare_outcome(
            operation_id,
            output="saved",
            start_commit="base123",
            changed_paths=(),
            effects=(OperationEffect("session-state", metadata={"session_id": session_id}),),
        )
        store.complete_no_change(operation_id)
        state = _IsolatedStateOverlay(root, "update", session_id, operation_id=operation_id)
        isolated_history = MessageSessionStore(state.overlay_root, "update").paths(session_id).history
        isolated_history.parent.mkdir(parents=True, exist_ok=True)
        isolated_history.write_text("long-session-history", encoding="utf-8")

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._run_operation_effects(operation_id, state)

        main_store = MessageSessionStore(root, "update")
        main_history = main_store.paths(session_id).history
        watermark = (
            root
            / ".runtime"
            / "operations"
            / "session-state"
            / "update"
            / main_history.relative_to(main_store.root)
        )
        self.assertEqual(main_history.read_text(encoding="utf-8"), "long-session-history")
        self.assertTrue(watermark.is_file())
        self.assertEqual(watermark.parent.name, "hashed")

    def test_effect_order_follows_preparation_not_initial_failed_attempt(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        store.begin("created-first-landed-last", {"role": "update", "session_id": "agent-session"})
        store.begin("created-last-landed-first", {"role": "update", "session_id": "agent-session"})
        states = {}

        for operation_id, content in (
            ("created-last-landed-first", "first landing"),
            ("created-first-landed-last", "last landing"),
        ):
            store.prepare_outcome(
                operation_id,
                output=content,
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("session-state", metadata={"session_id": "agent-session"}),),
            )
            store.complete_no_change(operation_id)
            state = _IsolatedStateOverlay(root, "update", "agent-session", operation_id=operation_id)
            history = MessageSessionStore(state.overlay_root, "update").paths("agent-session").history
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(content, encoding="utf-8")
            states[operation_id] = state

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._run_operation_effects("created-first-landed-last", states["created-first-landed-last"])
        runtime._run_operation_effects("created-last-landed-first", states["created-last-landed-first"])

        main_history = MessageSessionStore(root, "update").paths("agent-session").history
        self.assertEqual(main_history.read_text(encoding="utf-8"), "last landing")

    def test_pending_state_from_the_same_session_blocks_the_next_turn_until_recovered(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        store.begin(
            "update-state-pending",
            {"role": "update", "session_id": "agent-session"},
        )
        store.prepare_outcome(
            "update-state-pending",
            output="saved",
            start_commit="base123",
            changed_paths=(),
            effects=(OperationEffect("session-state", metadata={"session_id": "agent-session"}),),
        )
        store.complete_no_change("update-state-pending", "base123")
        state = _IsolatedStateOverlay(
            root,
            "update",
            "agent-session",
            operation_id="update-state-pending",
        )
        history = MessageSessionStore(state.overlay_root, "update").paths("agent-session").history
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text("recovered history", encoding="utf-8")

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with (
            patch.object(_IsolatedStateOverlay, "promote_if_current", side_effect=OSError("disk full")),
            patch.object(runtime, "_warn_operation_effect_failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "previous semantic session state is still pending"):
                runtime._recover_pending_session_state("agent-session", exclude="next-operation")

        runtime._recover_pending_session_state("agent-session", exclude="next-operation")

        main_history = MessageSessionStore(root, "update").paths("agent-session").history
        self.assertEqual(main_history.read_text(encoding="utf-8"), "recovered history")
        self.assertEqual(store.list_pending_effects("update-state-pending"), ())

    def test_sync_finisher_retries_pending_state_on_a_fresh_result(self):
        root = Path(self.tempdir.name)
        operation_id = f"sync-repair-{'d' * 64}"
        store = SemanticOperationStore(root)
        store.begin(operation_id, {"kind": "sync-repair", "role": "sync-reconciler"})
        store.prepare_outcome(
            operation_id,
            output='{"files":[],"message":"published","status":"synced"}',
            start_commit="base123",
            changed_paths=("MEMORY.md",),
            effects=(
                OperationEffect(
                    "session-state",
                    metadata={
                        "role": "sync-reconciler",
                        "session_id": SYNC_REPAIR_SESSION_ID,
                    },
                ),
            ),
            metadata={"candidate_commit": "tip456"},
        )
        store.complete_commit(operation_id, "tip456")
        state = _IsolatedStateOverlay(
            root,
            "sync-reconciler",
            SYNC_REPAIR_SESSION_ID,
            operation_id=operation_id,
        )
        history = MessageSessionStore(
            state.overlay_root,
            "sync-reconciler",
        ).paths(SYNC_REPAIR_SESSION_ID).history
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text("recovered sync history", encoding="utf-8")

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        fresh = SyncResult("fresh", "last successful pull is fresh")

        with patch.object(_IsolatedStateOverlay, "promote_if_current", side_effect=OSError("disk full")):
            runtime._finish_sync_repair(fresh)
        self.assertTrue(store.list_pending_effects(operation_id))

        runtime._finish_sync_repair(fresh)

        main_history = MessageSessionStore(root, "sync-reconciler").paths(
            SYNC_REPAIR_SESSION_ID
        ).history
        self.assertEqual(main_history.read_text(encoding="utf-8"), "recovered sync history")
        self.assertEqual(store.list_pending_effects(operation_id), ())

    def test_invalid_pending_effect_record_does_not_starve_the_next_retry(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        for operation_id, input_data in (
            ("update-effect-poison", {"role": "update"}),
            ("update-effect-ready", {"role": "update", "session_id": "agent-session"}),
        ):
            store.begin(operation_id, input_data)
            store.prepare_outcome(
                operation_id,
                output="saved",
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("session-state", metadata={"session_id": "agent-session"}),),
            )
            store.complete_no_change(operation_id, "base123")

        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        runtime._retry_pending_operation_effects(exclude="current-operation")
        runtime._retry_pending_operation_effects(exclude="current-operation")

        self.assertTrue(store.list_pending_effects("update-effect-poison"))
        self.assertEqual(store.list_pending_effects("update-effect-ready"), ())

    def test_failed_follow_up_effect_does_not_change_terminal_outcome(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        input_data = {"role": "dreamer", "session_id": "dream-session", "message": "dream"}
        store.begin("dream-op-1", input_data)
        store.prepare_outcome(
            "dream-op-1",
            output="no change",
            start_commit="base123",
            changed_paths=(),
            effects=(OperationEffect("file-view-publish"),),
        )
        store.complete_no_change("dream-op-1")
        config = RuntimeConfig(role="dreamer", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        state = type("State", (), {})()

        with patch.object(runtime, "_publish_file_views_after_write", side_effect=OSError("hub offline")):
            runtime._run_operation_effects("dream-op-1", state)

        record = store.read("dream-op-1")
        self.assertEqual(record.phase, "no_change")
        self.assertEqual(record.outcome.output, "no change")
        self.assertEqual(store.list_pending_effects("dream-op-1")[0].status, "failed")

    def test_older_publish_effect_is_superseded_after_newer_publish(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        for operation_id in ("publish-old", "publish-new"):
            store.begin(operation_id, {"role": "dreamer", "session_id": operation_id})
            store.prepare_outcome(
                operation_id,
                output="saved",
                start_commit="base123",
                changed_paths=(),
                effects=(OperationEffect("file-view-publish"),),
            )
            store.complete_no_change(operation_id)
        config = RuntimeConfig(role="dreamer", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        with patch.object(runtime, "_publish_file_views_after_write") as publish:
            runtime._run_operation_effects("publish-new", type("State", (), {})())
            runtime._run_operation_effects("publish-old", type("State", (), {})())

        publish.assert_called_once_with(raise_on_failure=True, operation_id="publish-new")
        self.assertEqual(store.list_pending_effects("publish-old"), ())

    def test_publish_effect_reads_the_durable_operation_outbox(self):
        root = Path(self.tempdir.name)
        store = SemanticOperationStore(root)
        store.begin("publish-snapshot", {"role": "dreamer", "session_id": "dream-session"})
        store.prepare_outcome(
            "publish-snapshot",
            output="saved",
            start_commit="base123",
            changed_paths=(),
            effects=(OperationEffect("file-view-publish"),),
        )
        config = RuntimeConfig(role="dreamer", model_id="openai/test", memory_root=root)
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
        runtime._prepare_operation_effects("publish-snapshot", root)
        store.complete_no_change("publish-snapshot", "base123")

        with (
            patch("rightmemory.runtime.publish_file_view_outbox", return_value=[]) as publish,
            patch("rightmemory.runtime.record_file_view_publish_results"),
        ):
            runtime._publish_file_views_after_write(operation_id="publish-snapshot")

        publish.assert_called_once_with(
            store.effect_state_root("publish-snapshot", "file-view-publish"),
            operation_id="publish-snapshot",
            credential_root=root,
        )

    def test_isolated_update_prepares_managed_artifacts_before_state_promotion(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "update-123"
        events = []

        class FakeSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback, **kwargs):
                output = callback(worktree)
                kwargs["prepare_managed_artifacts"](
                    worktree,
                    "base123",
                    "tip456",
                    ("MEMORY.md",),
                    output,
                )
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
            with patch.object(
                runtime,
                "_prepare_update_artifacts",
                side_effect=lambda *_args: events.append("artifacts") or (),
            ):
                result = runtime._run_session_turn_isolated("agent-session", "remember one")

        self.assertEqual(result, "updated")
        self.assertEqual(events, ["artifacts", "landed", "promote"])

    def test_candidate_record_is_prepared_for_queued_update(self):
        root = Path(self.tempdir.name)
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        candidate = UpdateCandidate(
            uid="a" * 32,
            session_id="agent-session",
            display_id=1,
            message="exact candidate",
            submitted_at="2026-07-27T12:00:00+00:00",
        )
        record = UpdateRecord.from_candidates((candidate,))
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        paths = runtime._prepare_update_artifacts(
            record.operation_id,
            record,
            root,
            "base123",
            "tip456",
            ("MEMORY.md",),
            "updated summary",
        )

        self.assertEqual(
            paths,
            (f"update_records/{record.operation_id}.json",),
        )
        self.assertEqual(UpdateRecordStore(root).read(record.operation_id), record)

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

            def run(self, callback, **_kwargs):
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

            def run(self, callback, **_kwargs):
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

            def run(self, callback, **_kwargs):
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

            def run(self, callback, **_kwargs):
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
            ProviderThreadStore(state_root).record_created(
                provider="codex",
                provider_session_id="new-thread",
                role="reviewer",
                rightmemory_session_id=session_id,
                policy="one-shot",
                created_at="2026-07-17T00:00:00+00:00",
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
        self.assertEqual(ProviderThreadStore(main_root).load("codex", "new-thread").policy, "one-shot")

    def test_failed_cli_agent_isolated_run_keeps_prior_provider_session(self):
        main_root = Path(self.tempdir.name)
        worktree = main_root / ".runtime" / "worktrees" / "reviewer-123"
        old_provider = self._provider_record_json("reviewer", "agent-session", "old-thread")
        new_provider = self._provider_record_json("reviewer", "agent-session", "new-thread")
        self._write_runtime_state(main_root, "reviewer", "agent-session", history='["old message"]', provider=old_provider)

        class FailingSupervisor:
            def __init__(self, memory_root, role):
                pass

            def run(self, callback, **_kwargs):
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
            ProviderThreadStore(state_root).record_created(
                provider="codex",
                provider_session_id="new-thread",
                role="reviewer",
                rightmemory_session_id=session_id,
                policy="one-shot",
                created_at="2026-07-17T00:00:00+00:00",
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
        self.assertEqual(ProviderThreadStore(main_root).load("codex", "new-thread").policy, "one-shot")

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

        def run_one_shot_turn(session_id, message, *, prefix_context=None):
            self.assertEqual(session_id, NO_SESSION_RIGHTMEMORY_SESSION_ID)
            self.assertIsNotNone(prefix_context)
            events.append("agent")
            return EMPTY_RETRIEVE_SELECTION_JSON

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.run_one_shot_turn.side_effect = run_one_shot_turn
            runtime = RightMemoryRuntime(config)
            runtime.sessions.locked = locked
            result = runtime.run_turn("remember one")

        self.assertEqual(result, "no strong match")
        self.assertEqual(
            events,
            [
                ("locked", NO_SESSION_RIGHTMEMORY_SESSION_ID),
                "lock_enter",
                "agent",
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

    def test_automatic_write_run_turn_uses_reserved_internal_session_path(self):
        for role in ("dreamer", "insight", "pruner", "update"):
            with self.subTest(role=role):
                config = RuntimeConfig(
                    role=role,
                    model_id="openai/test",
                    memory_root=Path(self.tempdir.name),
                )
                with patch.dict("sys.modules", self._fake_pydantic_modules()):
                    runtime = RightMemoryRuntime(config)

                with patch.object(
                    runtime,
                    "_run_session_turn_unlocked",
                    return_value="updated",
                ) as run:
                    result = runtime.run_turn("remember one")

                self.assertEqual(result, "updated")
                run.assert_called_once_with(
                    NO_SESSION_RIGHTMEMORY_SESSION_ID,
                    "remember one",
                    allow_internal_session=True,
                )

    def test_include_returned_attaches_current_content_for_one_call_and_preserves_coverage(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text(
            "# Root {#root}\n\n"
            "- `fact` Remembered fact. -> []\n",
            encoding="utf-8",
        )
        (root / "PURSUITS.md").write_text("# Pursuits {#pursuits}\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)
        selections = iter(
            [
                RetrieveSelection(ids=["fact"]),
                RetrieveSelection(ids=["fact"]),
                RetrieveSelection(ids=["fact"]),
                RetrieveSelection(),
            ]
        )
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.agent.output_values = selections
            first = runtime.run_session_turn("agent-session", "find it")
            model_repeated = runtime.run_session_turn(
                "agent-session",
                "select it despite coverage",
            )
            override_repeated = runtime.run_session_turn(
                "agent-session",
                "show it again",
                include_returned=True,
            )
            final = runtime.run_session_turn("agent-session", "anything new?")

        self.assertIn("Remembered fact.", first)
        self.assertIn("Remembered fact.", model_repeated)
        self.assertIn("Remembered fact.", override_repeated)
        self.assertEqual(final, "no strong match")
        self.assertIn("fact", runtime.retrieve_context.load("agent-session").delivery_coverage.local_items)

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

    def test_retrieve_surfaces_changed_f_detail_with_the_same_id(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text(
            "# Root {#root}\n\n"
            "## Detail {F#detail}\n\n"
            "Detail summary.\n",
            encoding="utf-8",
        )
        detail = root / "MEMORY_detail.md"
        detail.write_text(
            "### Topic {#topic}\n\n"
            "- `detail-fact` Original detail. -> []\n",
            encoding="utf-8",
        )
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.agent.output_values = iter(
                [
                    RetrieveSelection(ids=["detail-fact"]),
                    RetrieveSelection(ids=["detail-fact"]),
                ]
            )
            first = runtime.run_session_turn("agent-session", "find detail")
            detail.write_text(
                "### Topic {#topic}\n\n"
                "- `detail-fact` Revised detail. -> []\n",
                encoding="utf-8",
            )
            second = runtime.run_session_turn("agent-session", "find revised detail")

        self.assertIn("Original detail.", first)
        self.assertIn("Revised detail.", second)

    def test_retrieve_turn_records_candidate_visibility_and_delivery_after_success(self):
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
            runtime.agent.output_values = iter(
                [RetrieveSelection(recent_candidates=["update-a:1"])]
            )
            runtime.run_session_turn("agent-session", "find one")

        state = runtime.retrieve_context.load("agent-session")
        self.assertEqual(
            state.visible_recent_candidates,
            {f"{1:032x}": "update-a:1"},
        )
        self.assertIn("update-a:1", state.delivery_coverage.recent_candidates)
        separate_state = (
            Path(self.tempdir.name)
            / ".runtime"
            / "recent_submitted"
            / "retrieve"
            / "agent-session.json"
        )
        self.assertFalse(separate_state.exists())

    def test_retrieve_turn_does_not_advance_candidate_state_after_failure(self):
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
        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

            def run_sync(message, message_history=None, model_settings=None, usage_limits=None):
                raise RuntimeError("model failed")

            runtime.agent.run_sync = run_sync
            with self.assertRaises(RuntimeError):
                runtime.run_session_turn("agent-session", "find one")

        state_path = (
            Path(self.tempdir.name)
            / ".runtime"
            / "retrieve_context"
            / "sessions"
            / "agent-session.json"
        )
        self.assertFalse(state_path.exists())

    def test_cli_agent_new_retrieve_starts_fresh_local_context(self):
        root = Path(self.tempdir.name)
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="cli-agent",
            agent_cli=AgentCliConfig(provider="codex"),
            memory_root=root,
        )
        old_entry = RecentSubmittedMemoryEntry("update-old", 1, "2026-07-16T00:00:00+00:00", "old candidate")

        with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
            executor_class.return_value.has_saved_session.return_value = False
            executor_class.return_value.run_session_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
            runtime = RightMemoryRuntime(config)
            runtime.retrieve_context.record_success(
                "agent-session",
                memory_commit="old-commit",
                model_history_json=None,
                visible_recent_candidates={old_entry.key: "update-old:1"},
            )
            result = runtime.run_session_turn("agent-session", "fresh question")

        self.assertEqual(result, "no strong match")
        state = runtime.retrieve_context.load("agent-session")
        self.assertNotEqual(state.delivered_memory_commit, "old-commit")
        self.assertEqual(state.visible_recent_candidates, {})
    def test_write_role_creates_memory_lock_without_synthesizing_root_gitignore(self):
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
        self.assertFalse((Path(self.tempdir.name) / ".gitignore").exists())

    def test_retrieve_role_does_not_record_recent_submitted_state_without_entries(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        self.assertFalse((Path(self.tempdir.name) / ".runtime" / "recent_submitted").exists())

    def test_update_turn_runs_sync_pull_without_exposing_context(self):
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
            manager_class.return_value.pull.return_value = SyncResult("synced", "local memory is current")
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        manager_class.return_value.pull.assert_called_once_with(repair=runtime._repair_sync_candidate)
        manager_class.return_value.push.assert_called_once_with(repair=runtime._repair_sync_candidate)

    def test_sync_manager_operations_run_outside_the_model_write_lock(self):
        events = []

        class FakeLock:
            def __init__(self, memory_root):
                self.memory_root = memory_root

            def __enter__(self):
                events.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock_exit")

        def pull(*, repair):
            self.assertEqual(repair, runtime._repair_sync_candidate)
            events.append("pull")
            return SyncResult("synced", "local memory is current")

        def push(*, repair):
            self.assertEqual(repair, runtime._repair_sync_candidate)
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
            manager_class.return_value.pull.side_effect = pull
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

        self.assertEqual(events, ["pull", "lock_enter", "model", "lock_exit", "push"])

    def test_dirty_pull_runs_sync_reconciler_before_update_agent(self):
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
            manager_class.return_value.pull.side_effect = [
                SyncResult("dirty", "local memory has uncommitted changes", ["MEMORY.md"]),
                SyncResult("synced", "local memory is current"),
            ]
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        self.assertEqual(repairs, ["dirty"])

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

    def test_dirty_main_guard_retries_full_sync_pull_after_repair(self):
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
            manager_class.return_value.pull.side_effect = [
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
        self.assertEqual(manager_class.return_value.pull.call_count, 2)
        for call in manager_class.return_value.pull.call_args_list:
            self.assertEqual(call.kwargs, {"repair": runtime._repair_sync_candidate})
        manager_class.return_value.push.assert_called_once_with(repair=runtime._repair_sync_candidate)

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

    def test_push_delegates_candidate_repair_to_sync_manager(self):
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
            manager_class.return_value.pull.return_value = SyncResult("synced", "local memory is current")
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        manager_class.return_value.push.assert_called_once_with(repair=runtime._repair_sync_candidate)

    def test_runtime_sync_reconciler_loads_selected_memory_root(self):
        memory_root = Path(self.tempdir.name) / "profile-root"
        memory_root.mkdir()
        loaded_roots = []
        nested_calls = []
        nested_runners = []
        codex_runner = object()
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
            def __init__(self, runtime_config, *, codex_runner=None):
                nested_calls.append(("init", runtime_config.memory_root))
                nested_runners.append(codex_runner)

            def run_session_turn(self, session_id, message):
                nested_calls.append(("turn", session_id, message))

            def cleanup(self):
                nested_calls.append(("cleanup",))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config, codex_runner=codex_runner)

        with (
            patch("rightmemory.runtime.load_config", side_effect=fake_load_config),
            patch("rightmemory.runtime.RightMemoryRuntime", FakeNestedRuntime),
        ):
            runtime._run_sync_reconciler(SyncResult("dirty", "local memory is dirty", ["MEMORY.md"]))

        self.assertEqual(loaded_roots, [("sync-reconciler", memory_root)])
        self.assertEqual(nested_calls[0], ("init", memory_root))
        self.assertEqual(nested_runners, [codex_runner])
        self.assertEqual(nested_calls[-1], ("cleanup",))

    def test_prune_turn_checks_generation_after_sync_pull(self):
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
            manager_class.return_value.pull.side_effect = lambda *, repair: events.append(
                ("pull", memory_root)
            ) or SyncResult("synced", "local memory is current")
            manager_class.return_value.push.return_value = SyncResult("pushed", "local memory pushed")
            runtime = RightMemoryRuntime(config)
            result = runtime.run_prune_turn("prune-session", PrunerConfig(memory_root=memory_root))

        self.assertEqual(result, "prune not due after sync")
        self.assertEqual(events, [("pull", memory_root), ("status", memory_root)])
        self.assertEqual(runtime.agent.calls, [])

    def test_retrieve_turn_refreshes_before_model_and_defers_incomplete_sync(self):
        events = []
        memory_root = Path(self.tempdir.name)
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=memory_root,
            sync=load_sync_config_for_test(memory_root, enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch(
                "rightmemory.runtime.schedule_deferred_sync",
                side_effect=lambda root: events.append(("deferred", root)),
            ) as schedule,
        ):
            manager_class.return_value.refresh_for_retrieve.side_effect = lambda: events.append(
                ("refresh", memory_root)
            ) or SyncResult("offline", "fetch timed out")
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn(
                "agent-session",
                "find one",
                on_started=lambda: events.append(("model", memory_root)),
            )

        manager_class.assert_called_once_with(config.sync)
        manager_class.return_value.refresh_for_retrieve.assert_called_once_with()
        manager_class.return_value.pull.assert_not_called()
        manager_class.return_value.push.assert_not_called()
        schedule.assert_called_once_with(memory_root)
        self.assertEqual(
            events,
            [
                ("refresh", memory_root),
                ("model", memory_root),
                ("deferred", memory_root),
            ],
        )

    def test_nested_retrieve_entrypoints_schedule_deferred_sync_once(self):
        memory_root = Path(self.tempdir.name)
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=memory_root,
            sync=load_sync_config_for_test(memory_root, enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch("rightmemory.runtime.schedule_deferred_sync") as schedule,
        ):
            manager_class.return_value.refresh_for_retrieve.side_effect = [
                SyncResult("conflict", "repair is required"),
                SyncResult("fresh", "retrieve sync refresh is not due"),
            ]
            runtime = RightMemoryRuntime(config)
            runtime.run_chat_turn("find one", session_id="agent-session")
            runtime.run_chat_turn("find two", session_id="agent-session")

        schedule.assert_called_once_with(memory_root)

    def test_retrieve_sync_wakes_update_worker_after_incoming_queue_lands(self):
        memory_root = Path(self.tempdir.name)
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=memory_root,
            sync=load_sync_config_for_test(memory_root, enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
            patch("rightmemory.runtime.UpdateQueueStore") as queue_class,
            patch("rightmemory.runtime.AsyncUpdateStore") as update_class,
            patch("rightmemory.runtime.schedule_deferred_sync"),
        ):
            manager_class.return_value.refresh_for_retrieve.return_value = SyncResult(
                "ahead",
                "incoming memory landed with local commits pending push",
            )
            queue_class.return_value.snapshot.return_value = types.SimpleNamespace(
                candidates=[object()]
            )
            queue_class.return_value.outbox_candidates.return_value = []
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        update_class.assert_called_once_with(memory_root, "update")
        update_class.return_value.wake_worker.assert_called_once_with()

    def test_historian_turn_does_not_run_sync_pull(self):
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

    def test_run_session_turn_preserves_message_history_on_disk(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=Path(self.tempdir.name))

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            first_runtime = RightMemoryRuntime(config)
            first_runtime.agent.extra_history_messages = [
                {
                    "kind": "response",
                    "parts": [
                        {"part_kind": "thinking", "content": "inspect the detail"},
                        {
                            "part_kind": "tool-call",
                            "tool_name": "read_detail",
                            "args": {"detail_id": "alpha"},
                        },
                    ],
                },
                {
                    "kind": "request",
                    "parts": [
                        {
                            "part_kind": "tool-return",
                            "tool_name": "read_detail",
                            "content": "authoritative detail body",
                        }
                    ],
                },
            ]
            first = first_runtime.run_session_turn("agent-session", "remember one")
            first_runtime.cleanup()

            second_runtime = RightMemoryRuntime(config)
            second = second_runtime.run_session_turn("agent-session", "what was that?")

        self.assertEqual(first, "no strong match")
        self.assertEqual(second, "no strong match")
        history_path = Path(self.tempdir.name) / ".runtime" / "sessions" / "retrieve" / "agent-session.json"
        self.assertFalse(history_path.exists())
        retrieve_state_path = (
            Path(self.tempdir.name) / ".runtime" / "retrieve_context" / "sessions" / "agent-session.json"
        )
        retrieve_state = json.loads(retrieve_state_path.read_text(encoding="utf-8"))
        self.assertIn("model_history", retrieve_state)
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
        retrieve_state = json.loads(retrieve_state_path.read_text(encoding="utf-8"))
        self.assertIsInstance(retrieve_state["model_history"], list)
        trace_path = Path(self.tempdir.name) / ".runtime" / "debug" / "retrieve" / "agent-session.jsonl"
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            ["run_started", "history_loaded", "model_started", "model_finished", "run_finished"],
        )
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
            executor_class.return_value.has_saved_session.return_value = False
            executor_class.return_value.run_session_turn.return_value = EMPTY_RETRIEVE_SELECTION_JSON
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
        return MessageSessionStore(root, role).paths(session_id).history

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

        class FakeUserPromptPart:
            def __init__(self, content):
                self.content = content
                self.part_kind = "user-prompt"

        class FakeModelRequest:
            def __init__(self, parts):
                self.parts = parts
                self.kind = "request"

        def serialize_message(value):
            if isinstance(value, FakeModelRequest):
                return {
                    "kind": "request",
                    "parts": [serialize_message(part) for part in value.parts],
                }
            if isinstance(value, FakeUserPromptPart):
                return {
                    "part_kind": "user-prompt",
                    "content": value.content,
                }
            return value

        class FakeAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.model = kwargs["model"]
                self.tools = kwargs["tools"]
                self.output_type = kwargs.get("output_type")
                self.output_validators = []

            def output_validator(self, validator):
                self.output_validators.append(validator)
                return validator

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
                if hasattr(self, "output_values"):
                    output = next(self.output_values)
                else:
                    output = self.output_type() if self.output_type is not None else f"reply {call_count}"
                for validator in self.output_validators:
                    output = validator(output)
                if self.output_type is RetrieveSelection:
                    all_messages = [
                        *(serialize_message(item) for item in (message_history or [])),
                        {
                            "kind": "request",
                            "parts": [{"part_kind": "user-prompt", "content": message}],
                        },
                        *getattr(self, "extra_history_messages", []),
                        {
                            "kind": "response",
                            "parts": [
                                {
                                    "part_kind": "text",
                                    "content": output.model_dump_json(),
                                }
                            ],
                        },
                    ]
                else:
                    all_messages = [f"message {call_count}"]

                class FakeResult:
                    def all_messages(self):
                        return all_messages

                    def all_messages_json(self):
                        return json.dumps(self.all_messages()).encode()

                FakeResult.output = output
                return FakeResult()

        class FakeProvider:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class FakeDeepSeekProvider:
            @staticmethod
            def model_profile(model_name):
                return {"provider": "deepseek", "model_name": model_name}

        class FakeModel:
            def __init__(self, model_name, provider=None, profile=None):
                self.model_name = model_name
                self.provider = provider
                self.profile = profile

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
            "pydantic_ai.messages": types.SimpleNamespace(
                ModelMessagesTypeAdapter=FakeModelMessagesTypeAdapter,
                ModelRequest=FakeModelRequest,
                UserPromptPart=FakeUserPromptPart,
            ),
            "pydantic_ai.models": types.SimpleNamespace(),
            "pydantic_ai.models.openai": types.SimpleNamespace(OpenAIChatModel=FakeModel),
            "pydantic_ai.providers": types.SimpleNamespace(),
            "pydantic_ai.providers.deepseek": types.SimpleNamespace(DeepSeekProvider=FakeDeepSeekProvider),
            "pydantic_ai.providers.openai": types.SimpleNamespace(OpenAIProvider=FakeProvider),
            "pydantic_ai.models.anthropic": types.SimpleNamespace(AnthropicModel=FakeModel),
            "pydantic_ai.providers.anthropic": types.SimpleNamespace(AnthropicProvider=FakeProvider),
        }

    def _failing_agent(self):
        class FailingAgent:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def output_validator(self, validator):
                return validator

            def run_sync(self, message, message_history=None, model_settings=None, usage_limits=None):
                raise RuntimeError("model failed")

        return FailingAgent

    def _write_async_update_state(self, session_id, *, pending=None, current_batch=None):
        def with_candidate_uids(jobs):
            return [
                {**job, "candidate_uid": job.get("candidate_uid", f"{job['id']:032x}")}
                for job in (jobs or [])
            ]

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
                    "current_batch": with_candidate_uids(current_batch),
                    "pending": with_candidate_uids(pending),
                    "next_id": 10,
                }
            ),
            encoding="utf-8",
        )


class PackageReferenceTests(unittest.TestCase):
    def test_package_references_need_no_wheel_remapping(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

        force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertNotIn("skills", force_include)
        for filename in REFERENCE_FILES.values():
            self.assertTrue((Path("rightmemory") / "reference" / filename).is_file())
        self.assertNotIn("rightmemory/semantic_upgrades", force_include)


def load_sync_config_for_test(memory_root: Path, enabled: bool):
    from rightmemory.config import SyncConfig

    return SyncConfig(memory_root=memory_root, enabled=enabled)


if __name__ == "__main__":
    unittest.main()
