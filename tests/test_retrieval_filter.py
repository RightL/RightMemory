from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rightmemory.codex_sdk import CodexSdkRunResult, CodexSdkRunner, CodexSdkTiming
from rightmemory.config import AgentCliConfig
from rightmemory.retrieval_filter import RetrievalFilter
from tests.test_agent_cli import FakeSdkCodex as BaseFakeSdkCodex, FakeSdkThread


class FakeSdkCodex(BaseFakeSdkCodex):
    def __init__(self, threads, *, unsubscribe_error=None, config=None, config_error=None):
        super().__init__(threads, unsubscribe_error=unsubscribe_error)
        response = SimpleNamespace(config=SimpleNamespace(model_extra=config))
        self._client = SimpleNamespace(request=Mock(return_value=response, side_effect=config_error))


class RetrievalFilterTests(unittest.TestCase):
    def setUp(self):
        # Prompt wording is intentionally outside the tests' contract.
        resource = Mock()
        resource.joinpath.return_value.read_text.return_value = "test instructions"
        self.resources = patch("rightmemory.retrieval_filter.resources.files", return_value=resource)
        self.resources.start()
        self.addCleanup(self.resources.stop)
        self.config = AgentCliConfig("codex", "gpt-6-luna", "low")

    def test_each_query_starts_and_releases_an_ephemeral_conversation(self):
        threads = [FakeSdkThread(f"thread-{index}") for index in range(2)]
        clients = [FakeSdkCodex([thread]) for thread in threads]
        factory = Mock(side_effect=clients)
        archive_factory = Mock()
        runner = CodexSdkRunner(codex_factory=factory, archive_client_factory=archive_factory)
        self.addCleanup(runner.close)
        filter_ = RetrievalFilter(self.config, runner)

        self.assertEqual(filter_.run("first query", "first evidence"), "done")
        self.assertEqual(filter_.run("second query", "second evidence"), "done")

        self.assertEqual(factory.call_count, 2)
        archive_factory.assert_not_called()
        directories = []
        for index, client in enumerate(clients):
            self.assertEqual(len(client.start_calls), 1)
            call = client.start_calls[0]
            self.assertTrue(call["ephemeral"])
            self.assertEqual(call["model"], "gpt-6-luna")
            self.assertEqual(call["config"]["project_doc_max_bytes"], 0)
            self.assertFalse(call["config"]["skills"]["include_instructions"])
            self.assertFalse(call["config"]["features"]["shell_tool"])
            self.assertEqual(call["config"]["web_search"], "disabled")
            self.assertEqual(client._client.request.call_args.args, (
                "config/read", {"cwd": call["cwd"], "includeLayers": False},
            ))
            self.assertEqual(client.resume_calls, [])
            self.assertEqual(client.unarchive_calls, [])
            self.assertEqual(client.unsubscribe_calls, [f"thread-{index}"])
            self.assertEqual(client.close_calls, 1)
            directories.append(call["cwd"])
            self.assertFalse(Path(call["cwd"]).exists())
            self.assertEqual(threads[index].run_calls[0][1]["effort"].value, "low")
        self.assertNotEqual(*directories)

    def test_inherited_servers_are_disabled_without_copying_their_settings(self):
        servers = {
            "local.tools": {"command": "helper", "env": {"TOKEN": "synthetic-secret"}},
            "remote": {"url": "https://example.invalid/mcp", "enabled": True},
            "already_disabled": {"enabled": False},
        }
        thread = FakeSdkThread("isolated-thread")
        client = FakeSdkCodex([thread], config={"mcp_servers": servers})
        runner = CodexSdkRunner(codex_factory=lambda config: client)
        self.addCleanup(runner.close)

        RetrievalFilter(self.config, runner).run("query", "evidence")

        config = client.start_calls[0]["config"]
        self.assertEqual(config["mcp_servers"], {name: {"enabled": False} for name in servers})
        self.assertTrue(servers["remote"]["enabled"])
        for name in (
            "include_permissions_instructions", "include_apps_instructions",
            "include_collaboration_mode_instructions", "include_environment_context",
            "include_environment_context_time",
        ):
            self.assertFalse(config[name])
        self.assertEqual(config["tools"], {
            "experimental_request_user_input": {"enabled": False},
            "update_plan": {"enabled": False},
        })
        self.assertFalse(config["agents"]["enabled"])
        self.assertFalse(config["features"]["multi_agent"])
        self.assertFalse(config["features"]["multi_agent_v2"])
        self.assertEqual(client.start_calls[0]["sandbox"].value, "read-only")
        self.assertEqual(client.start_calls[0]["approval_mode"].value, "deny_all")
        self.assertEqual(thread.run_calls[0][1]["sandbox"].value, "read-only")
        self.assertEqual(thread.run_calls[0][1]["approval_mode"].value, "deny_all")

    def test_filter_preserves_inherited_features_and_applies_its_overrides(self):
        disabled = dict.fromkeys((
            "shell_tool", "view_image", "apps", "plugins", "multi_agent",
            "multi_agent_v2", "memories", "hooks",
        ), False)
        enabled = dict.fromkeys(disabled, True)
        for proxy in ({"respect_system_proxy": True}, {"respect_system_proxy": False}, {}):
            with self.subTest(proxy=proxy):
                inherited = {
                    **proxy,
                    "structured_feature": {"enabled": True, "options": {"values": ["example"]}},
                }
                configured = {**inherited, **enabled, "network_proxy": None}
                client = FakeSdkCodex(
                    [FakeSdkThread("isolated-thread")], config={"features": configured},
                )
                runner = CodexSdkRunner(codex_factory=lambda config: client)
                self.addCleanup(runner.close)

                RetrievalFilter(self.config, runner).run("query", "evidence")

                self.assertEqual(
                    client.start_calls[0]["config"]["features"], {**inherited, **disabled},
                )
                self.assertEqual(configured, {**inherited, **enabled, "network_proxy": None})

    def test_invalid_inherited_features_prevent_thread_start(self):
        for features in (None, False, ["respect_system_proxy"]):
            with self.subTest(features=features):
                client = FakeSdkCodex([], config={"features": features})
                runner = CodexSdkRunner(codex_factory=lambda config: client)
                self.addCleanup(runner.close)

                with self.assertRaisesRegex(RuntimeError, "invalid features map"):
                    RetrievalFilter(self.config, runner).run("query", "evidence")

                self.assertEqual(client.start_calls, [])
                self.assertEqual(client.close_calls, 1)

    def test_config_read_failure_closes_client_before_any_thread_starts(self):
        client = FakeSdkCodex([], config_error=RuntimeError("config unavailable"))
        archive_factory = Mock()
        runner = CodexSdkRunner(
            codex_factory=lambda config: client, archive_client_factory=archive_factory,
        )
        self.addCleanup(runner.close)
        with self.assertRaisesRegex(RuntimeError, "config unavailable"):
            RetrievalFilter(self.config, runner).run("query", "evidence")
        self.assertEqual(client.start_calls, [])
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(client.unsubscribe_calls, [])
        archive_factory.assert_not_called()
        self.assertFalse(Path(client._client.request.call_args.args[1]["cwd"]).exists())

    def test_ordinary_role_does_not_read_or_override_inherited_configuration(self):
        client = FakeSdkCodex([FakeSdkThread("ordinary-thread")], config_error=AssertionError("unexpected config read"))
        archive_factory = Mock()
        archive_factory.return_value.archive_threads.return_value = []
        runner = CodexSdkRunner(
            codex_factory=lambda config: client, archive_client_factory=archive_factory,
        )
        self.addCleanup(runner.close)
        runner.run_turn(
            prompt="query", provider_session_id=None, cwd=Path("ordinary-root"),
            model="gpt-6-luna", reasoning_effort="low", sandbox="read-only",
        )
        client._client.request.assert_not_called()
        self.assertNotIn("config", client.start_calls[0])

    def test_failed_or_interrupted_turn_releases_conversation_and_directory(self):
        for error in (RuntimeError("provider failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                thread = FakeSdkThread("failed-thread", error=error)
                client = FakeSdkCodex([thread])
                archive_factory = Mock()
                runner = CodexSdkRunner(
                    codex_factory=lambda config: client,
                    archive_client_factory=archive_factory,
                )
                events = []
                filter_ = RetrievalFilter(
                    self.config, runner, lambda name, **data: events.append((name, data))
                )
                with self.assertRaises(type(error)):
                    filter_.run("query", "evidence")
                runner.close()
                self.assertEqual(client.unsubscribe_calls, ["failed-thread"])
                self.assertEqual(client.close_calls, 1)
                self.assertFalse(Path(client.start_calls[0]["cwd"]).exists())
                archive_factory.assert_not_called()
                self.assertEqual(events[-1][1]["outcome"], "error")
                self.assertEqual(events[-1][1]["error_type"], type(error).__name__)

    def test_start_failure_closes_client_without_archiving(self):
        client = FakeSdkCodex([])
        client.thread_start = Mock(side_effect=RuntimeError("start failed"))
        archive_factory = Mock()
        runner = CodexSdkRunner(
            codex_factory=lambda config: client,
            archive_client_factory=archive_factory,
        )
        self.addCleanup(runner.close)
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            RetrievalFilter(self.config, runner).run("query", "evidence")
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(client.unsubscribe_calls, [])
        archive_factory.assert_not_called()
        self.assertFalse(Path(client.thread_start.call_args.kwargs["cwd"]).exists())

    def test_release_failure_preserves_output_and_reports_timing(self):
        thread = FakeSdkThread("thread", result=SimpleNamespace(
            final_response="Useful context.", duration_ms=42, usage=None,
        ))
        client = FakeSdkCodex([thread], unsubscribe_error=RuntimeError("unsubscribe failed"))
        archive_factory = Mock()
        runner = CodexSdkRunner(
            codex_factory=lambda config: client,
            archive_client_factory=archive_factory,
        )
        self.addCleanup(runner.close)
        events = []
        filter_ = RetrievalFilter(
            self.config, runner, lambda name, **data: events.append((name, data))
        )
        with patch("rightmemory.codex_sdk._warn_release"):
            output = filter_.run("query", "evidence")
        self.assertEqual(output, "Useful context.")
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(events[-1][1]["thread_release_error_type"], "RuntimeError")
        archive_factory.assert_not_called()

    def test_natural_language_passes_through_without_selection_parsing(self):
        runner = Mock(spec=CodexSdkRunner)
        runner.run_ephemeral_text_turn.return_value = CodexSdkRunResult(
            "transient", "  A condition applies.\n\nThe evidence remains uncertain.  ", CodexSdkTiming(),
        )
        events = []
        result = RetrievalFilter(
            self.config, runner, lambda name, **data: events.append((name, data))
        ).run("query", "evidence")
        self.assertEqual(result, "A condition applies.\n\nThe evidence remains uncertain.")
        runner.close.assert_not_called()
        self.assertEqual(events[-1][1]["outcome"], "success")
        self.assertEqual(events[-1][1]["provider_session_id"], "transient")
        self.assertTrue(events[-1][1]["ephemeral"])
        self.assertGreaterEqual(events[-1][1]["total_ms"], 0.0)

    def test_empty_output_and_invalid_inputs_do_not_fall_back_to_material(self):
        runner = Mock(spec=CodexSdkRunner)
        runner.run_ephemeral_text_turn.return_value = CodexSdkRunResult(
            "transient", " ", CodexSdkTiming(),
        )
        filter_ = RetrievalFilter(self.config, runner)
        with self.assertRaisesRegex(RuntimeError, "final response"):
            filter_.run("query", "unfiltered material")
        for query, material in (("", "evidence"), ("query", "")):
            with self.subTest(query=query, material=material), self.assertRaises(ValueError):
                filter_.run(query, material)
        self.assertEqual(runner.run_ephemeral_text_turn.call_count, 1)
        with self.assertRaises(ValueError):
            RetrievalFilter(AgentCliConfig("claude"), runner)


if __name__ == "__main__":
    unittest.main()
