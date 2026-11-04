from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from rightmemory.codex_sdk import CodexSdkRunResult, CodexSdkRunner, CodexSdkTiming
from rightmemory.config import AgentCliConfig
from rightmemory.retrieval_filter import RetrievalFilter
from tests.test_agent_cli import FakeSdkCodex, FakeSdkThread


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
            self.assertEqual(client.resume_calls, [])
            self.assertEqual(client.unarchive_calls, [])
            self.assertEqual(client.unsubscribe_calls, [f"thread-{index}"])
            self.assertEqual(client.close_calls, 1)
            directories.append(call["cwd"])
            self.assertFalse(Path(call["cwd"]).exists())
            self.assertEqual(threads[index].run_calls[0][1]["effort"].value, "low")
        self.assertNotEqual(*directories)

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
