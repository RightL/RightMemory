from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rightmemory.config import AgentCliConfig, EmbeddingRetrieveConfig, RuntimeConfig, load_config
from rightmemory.embedding_retrieval import EmbeddingRetrieveResult, EmbeddingServiceError
from rightmemory.retrieval_index import RetrievalCorpus, RetrievalEntry
from rightmemory.retrieve_selection import RetrieveSelectionError
from rightmemory.runtime import RightMemoryRuntime
from rightmemory.session import MessageSessionStore


class FilterConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load(self, extra=""):
        (self.root / "rightmemory.toml").write_text(
            '[retrieve]\nbackend="embedding"\n[retrieve.embedding]\nurl="http://localhost:8766"\n' + extra,
            encoding="utf-8",
        )
        return load_config("retrieve", self.root)

    def test_filter_is_opt_in_and_defaults_to_luna_low(self):
        self.assertIsNone(self.load().retrieve_filter)
        self.assertEqual(self.load("[retrieve.filter]\n").retrieve_filter,
                         AgentCliConfig("codex", "gpt-6-luna", "low"))

    def test_filter_settings_are_independent_of_fallback(self):
        config = self.load('[retrieve.agent_cli]\nprovider="codex"\nmodel="gpt-6-luna"\nreasoning_effort="high"\n'
                           '[retrieve.filter]\nmodel="gpt-6-sol"\nreasoning_effort="medium"\n')
        self.assertEqual(config.agent_cli.reasoning_effort, "high")
        self.assertEqual(config.retrieve_filter, AgentCliConfig("codex", "gpt-6-sol", "medium"))

    def test_invalid_filter_configuration_is_rejected(self):
        for extra in ('filter=3\n', '[retrieve.filter]\nmodel=""\n',
                      '[retrieve.filter]\nreasoning_effort="invalid"\n',
                      '[retrieve.filter]\nprovider="claude"\n'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.load(extra)
        (self.root / "rightmemory.toml").write_text(
            '[retrieve]\nbackend="agent"\n[retrieve.filter]\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config("retrieve", self.root)


class FilterRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = RuntimeConfig(
            role="retrieve", memory_root=self.root, retrieve_backend="embedding",
            embedding=EmbeddingRetrieveConfig("http://localhost:8766"),
            retrieve_filter=AgentCliConfig("codex", "gpt-6-luna", "low"),
        )
        self.filter = Mock()
        self.filter.run.return_value = "Useful context in ordinary language."
        factory = patch("rightmemory.retrieval_filter.RetrievalFilter", return_value=self.filter)
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        for name in ("_pull_file_views_for_retrieve", "_finish_retrieve_sync"):
            patcher = patch.object(RightMemoryRuntime, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runner = Mock()
        self.runtime = RightMemoryRuntime(self.config, codex_runner=self.runner)
        self.addCleanup(self.runtime.cleanup)
        self.entry = RetrievalEntry("entry", "source content", "source", display_parts=())
        self.retriever = Mock(return_value=EmbeddingRetrieveResult("Raw result", (self.entry,), "snapshot"))
        self.runtime._embedding_retriever.retrieve = self.retriever
        corpus = patch("rightmemory.retrieval_index.build_retrieval_corpus",
                       return_value=RetrievalCorpus((self.entry,), "snapshot"))
        self.corpus = corpus.start()
        self.addCleanup(corpus.stop)

    def test_returns_free_text_and_keeps_embedding_bookkeeping_separate(self):
        for _ in range(2):
            self.assertEqual(self.runtime.run_session_turn("same-session", "query"), self.filter.run.return_value)
        self.assertEqual(self.filter.run.call_count, 2)
        path = MessageSessionStore(self.root, "retrieve-embedding").paths("same-session").history
        state = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(state["source_fingerprint"], "snapshot")
        self.assertEqual(list(state["entries"]), ["entry"])
        self.assertFalse(MessageSessionStore(self.root, "retrieve").paths("same-session").history.exists())
        self.runtime.cleanup()
        self.runner.close.assert_not_called()

    def test_empty_corpus_skips_filter(self):
        self.retriever.return_value = EmbeddingRetrieveResult("No entries", (), "empty")
        self.assertEqual(self.runtime.run_session_turn("empty", "query"), "No entries")
        self.filter.run.assert_not_called()

    def test_source_changed_during_filter_retries_with_fresh_results(self):
        self.corpus.return_value = RetrievalCorpus((self.entry,), "new-snapshot")
        self.retriever.side_effect = [self.retriever.return_value,
                                     EmbeddingRetrieveResult("New result", (self.entry,), "new-snapshot")]
        self.runtime.run_session_turn("changed", "query")
        self.assertEqual(self.retriever.call_count, 2)
        self.assertEqual(self.filter.run.call_count, 2)
        path = MessageSessionStore(self.root, "retrieve-embedding").paths("changed").history
        self.assertEqual(json.loads(path.read_text())["source_fingerprint"], "new-snapshot")

    def test_repeated_source_changes_fail_without_saving_success(self):
        self.corpus.return_value = RetrievalCorpus((self.entry,), "changed-again")
        with self.assertRaisesRegex(RuntimeError, "both filtered retrieval attempts"):
            self.runtime.run_session_turn("unstable", "query")
        self.assertEqual(self.filter.run.call_count, 2)
        self.assertFalse(MessageSessionStore(self.root, "retrieve-embedding").paths("unstable").history.exists())

    def test_filter_failure_never_returns_raw_or_marks_success(self):
        self.filter.run.side_effect = RuntimeError("filter failed")
        with self.assertRaisesRegex(RuntimeError, "filter failed"):
            self.runtime.run_session_turn("failed", "query")
        self.assertFalse(MessageSessionStore(self.root, "retrieve-embedding").paths("failed").history.exists())

    def test_filtered_output_limit_does_not_save_success(self):
        self.runtime.config = replace(self.config, retrieve_max_output_chars=10)
        with self.assertRaises(RetrieveSelectionError):
            self.runtime.run_session_turn("oversized", "query")
        self.assertFalse(MessageSessionStore(self.root, "retrieve-embedding").paths("oversized").history.exists())

    def test_service_failure_uses_normal_fallback_without_filter(self):
        self.runtime.config = replace(self.config, agent_cli=AgentCliConfig("codex", "gpt-6-luna", "high"),
                                      runtime_mode="cli-agent")
        self.retriever.side_effect = EmbeddingServiceError("offline")
        with patch.object(RightMemoryRuntime, "_build_cli_agent", return_value=Mock()), \
                patch.object(RightMemoryRuntime, "_run_session_cli_agent", return_value="fallback result"), \
                self.assertLogs("rightmemory.runtime", level="WARNING"):
            self.assertEqual(self.runtime.run_session_turn("fallback", "query"), "fallback result")
        self.filter.run.assert_not_called()
        self.assertEqual(self.factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
