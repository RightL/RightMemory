from __future__ import annotations

from contextlib import redirect_stdout
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from rightmemory.config import EmbeddingRetrieveConfig, RuntimeConfig, load_config
from rightmemory.embedding_retrieval import (
    EmbeddingRetriever, EmbeddingServiceClient, EmbeddingServiceError, ModelInfo, _vector,
)
from rightmemory.embedding_service import create_app
from rightmemory.embedding_models import adapter_identity, model_fingerprint
from rightmemory.profiles import _profile_seed_config
from rightmemory.recent_submitted import RecentSubmittedMemoryEntry
from rightmemory.retrieval_index import RetrievalCorpus, RetrievalEntry, build_retrieval_corpus, search_passages
from rightmemory.runtime import RightMemoryRuntime
from rightmemory.session import MessageSessionStore
from tests import test_retrieve_selection


class FakeClient:
    def __init__(self):
        self.model = "embedding-one"
        self.reranker_model = "reranker-one"
        self.document_counts = []
        self.query_count = 0
        self.rerank_counts = []

    def info(self):
        return ModelInfo(self.model, self.reranker_model, 2, 125, 8)

    def embed(self, texts, *, query, info):
        if query:
            self.query_count += len(texts)
        else:
            self.document_counts.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    def rerank(self, query, documents, info):
        self.rerank_counts.append(len(documents))
        return list(reversed(range(len(documents))))


class EmbeddingRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_retrieve_selection.RetrieveSelectionRendererTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.config = EmbeddingRetrieveConfig("http://127.0.0.1:8766")
        self.retriever = EmbeddingRetriever(self.root, self.root, self.config, max_output_chars=100000)
        self.client = FakeClient()
        self.retriever.client = self.client

    def corpus(self):
        return build_retrieval_corpus(self.root)

    def test_all_existing_source_types_have_distinct_units(self):
        entries = {entry.key: entry for entry in self.corpus().entries}
        self.assertTrue({"project", "project-fact", "child-fact", "detail-fact", "active-work",
                         "S#review-skill", "MF#external-api:token-expiry",
                         "MF#external-api:deep-detail", "MF#external-api/S#review-checklist",
                         "AC#writing:1", "AC#design:1", "provider-context"} <= entries.keys())
        self.assertTrue(any(key.startswith("M#notes:") for key in entries))
        self.assertTrue(any(key.startswith("MF#external-api/M#incident-evidence:") for key in entries))
        self.assertEqual(len(entries), len(self.corpus().entries))

    def test_parent_changes_invalidate_child_but_sibling_changes_do_not(self):
        before = {entry.key: entry.version for entry in self.corpus().entries}
        path = self.root / "MEMORY.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("Project body.", "Changed parent rule."), encoding="utf-8")
        parent = {entry.key: entry.version for entry in self.corpus().entries}
        self.assertNotEqual(before["child-fact"], parent["child-fact"])
        self.assertEqual(before["detail-fact"], parent["detail-fact"])
        path.write_text(original.replace("A child fact.", "An edited child fact."), encoding="utf-8")
        child = {entry.key: entry.version for entry in self.corpus().entries}
        self.assertEqual(before["project"], child["project"])
        self.assertNotEqual(before["child-fact"], child["child-fact"])

    def test_backing_and_import_changes_invalidate_the_index(self):
        before = self.corpus().fingerprint
        path = self.root / "MEMORY_SKILL_review-skill.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nAdditional step.\n", encoding="utf-8")
        self.assertNotEqual(before, self.corpus().fingerprint)
        before = self.corpus().fingerprint
        path = self.root / ".runtime/shared_views/imports/external-api/dist/MEMORY.md"
        path.write_text(path.read_text(encoding="utf-8").replace("hourly", "daily"), encoding="utf-8")
        self.assertNotEqual(before, self.corpus().fingerprint)

    def test_pending_entries_keep_their_status_and_disappear_when_consumed(self):
        entry = RecentSubmittedMemoryEntry("update-test", 7, "2026-09-29T00:00:00Z", "Pending observation.", "uid-seven")
        with patch("rightmemory.retrieval_index.collect_recent_submitted_memory", return_value=[entry]):
            pending = self.corpus()
        candidates = [entry for entry in pending.entries if entry.pending]
        self.assertEqual([entry.key for entry in candidates], ["pending:uid-seven"])
        self.assertNotEqual(pending.fingerprint, self.corpus().fingerprint)
        self.assertFalse(any(entry.pending for entry in self.corpus().entries))

    def test_missing_or_invalid_import_is_not_silently_omitted(self):
        path = self.root / ".runtime/shared_views/imports/external-api/dist/manifest.toml"
        path.write_text("version = 1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.corpus()

    def test_long_sources_are_searchable_without_multiple_skill_result_slots(self):
        path = self.root / "MEMORY_SKILL_review-skill.md"
        path.write_text("# Skill\n\n" + "One complete step.\n" * 500, encoding="utf-8")
        skills = [entry for entry in self.corpus().entries if entry.key == "S#review-skill"]
        self.assertEqual(len(skills), 1)
        self.assertGreater(len(search_passages(skills[0].text)), 1)
        # This checks source splitting, not agent-facing instructions or output wording.
        source = "αβ\n" * 3000
        self.assertEqual(sum(map(len, search_passages(source))), len(source))

    def test_top_count_applies_across_sources_and_repeat_queries_are_allowed(self):
        first = self.retriever.retrieve("Find project context")
        second = self.retriever.retrieve("Find project context")
        self.assertEqual(len(first.entries), 10)
        self.assertEqual([entry.key for entry in first.entries], [entry.key for entry in second.entries])
        self.assertEqual(self.client.query_count, 2)
        self.assertEqual(self.client.rerank_counts, [len(self.corpus().entries)] * 2)

    def test_only_changed_passages_are_reembedded_and_deleted_cache_keys_are_pruned(self):
        self.retriever.retrieve("Find project context")
        embedded = sum(self.client.document_counts)
        self.retriever.retrieve("Find project context")
        self.assertEqual(sum(self.client.document_counts), embedded)
        path = self.root / "MEMORY.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("A child fact.", "A changed child fact."), encoding="utf-8")
        self.retriever.retrieve("Find project context")
        self.assertEqual(sum(self.client.document_counts) - embedded, 1)
        path.write_text(original.replace("- `child-fact` A child fact. → []\n", ""), encoding="utf-8")
        self.retriever.retrieve("Find project context")
        cache = json.loads((self.root / ".runtime/embedding_retrieval/vectors.json").read_text(encoding="utf-8"))
        from rightmemory.retrieval_index import text_hash
        current = {text_hash(part) for entry in self.corpus().entries for part in search_passages(entry.text)}
        self.assertEqual(set(cache["vectors"]), current)

    def test_model_change_and_corrupt_cache_trigger_reembedding(self):
        self.retriever.retrieve("Find project context")
        first = sum(self.client.document_counts)
        self.client.model = "embedding-two"
        self.retriever.retrieve("Find project context")
        self.assertEqual(sum(self.client.document_counts), first * 2)
        path = self.root / ".runtime/embedding_retrieval/vectors.json"
        path.write_text("broken cache", encoding="utf-8")
        self.retriever.retrieve("Find project context")
        self.assertEqual(sum(self.client.document_counts), first * 3)

    def test_reranker_change_reuses_document_vectors(self):
        self.retriever.retrieve("Find context")
        embedded = sum(self.client.document_counts)
        self.client.reranker_model = "another-reranker"
        self.retriever.retrieve("Find context")
        self.assertEqual(sum(self.client.document_counts), embedded)
        self.assertEqual(len(self.client.rerank_counts), 2)

    def test_replacement_adapters_control_batch_and_candidate_limits(self):
        embedding, reranker = FakeEmbeddingAdapter(), FakeRerankerAdapter()
        config = EmbeddingRetrieveConfig("http://service", candidate_count=150)
        retriever = EmbeddingRetriever(self.root, self.root, config, max_output_chars=100000)
        entries = tuple(RetrievalEntry(str(i), f"Value {i}", str(i)) for i in range(160))
        with TestClient(create_app(embedding, reranker)) as client:
            def request(route, payload=None):
                response = client.get(route) if payload is None else client.post(route, json=payload)
                response.raise_for_status()
                return response.json()

            with patch.object(retriever.client, "_request", side_effect=request), \
                    patch("rightmemory.embedding_retrieval.build_retrieval_corpus",
                          return_value=RetrievalCorpus(entries, "fixed")):
                result = retriever.retrieve("Find context")
                self.assertEqual([entry.key for entry in result.entries], [str(i) for i in range(149, 139, -1)])
                self.assertEqual(reranker.calls, [150])
                batches = [count for kind, count in embedding.calls if kind == "passage"]
                self.assertEqual(max(batches), 3)
                self.assertEqual(sum(batches), 160)
                reranker.max_candidates = 100
                with self.assertRaises(EmbeddingServiceError):
                    retriever.retrieve("Find context")
                self.assertEqual(reranker.calls, [150])

    def test_source_change_during_scoring_retries_once(self):
        original = self.retriever._select
        count = 0

        def select(query, corpus):
            nonlocal count
            count += 1
            selected = original(query, corpus)
            if count == 1:
                path = self.root / "MEMORY_SKILL_review-skill.md"
                path.write_text(path.read_text(encoding="utf-8") + "\nUpdated.\n", encoding="utf-8")
            return selected

        with patch.object(self.retriever, "_select", side_effect=select):
            result = self.retriever.retrieve("Find project context")
        self.assertEqual(count, 2)
        self.assertEqual(result.fingerprint, self.corpus().fingerprint)

    def test_continuously_changing_source_fails_without_stale_delivery(self):
        original = self.retriever._select

        def select(query, corpus):
            selected = original(query, corpus)
            path = self.root / "MEMORY_SKILL_review-skill.md"
            path.write_text(path.read_text(encoding="utf-8") + "\nUpdated.\n", encoding="utf-8")
            return selected

        with patch.object(self.retriever, "_select", side_effect=select), self.assertRaises(EmbeddingServiceError):
            self.retriever.retrieve("Find project context")

    def test_output_limit_errors_instead_of_silently_cutting_sources(self):
        self.retriever.max_output_chars = 20
        with self.assertRaises(ValueError):
            self.retriever.retrieve("Find project context")

    def test_empty_corpus_does_not_call_the_model_service(self):
        with patch("rightmemory.embedding_retrieval.build_retrieval_corpus", return_value=RetrievalCorpus((), "empty")):
            result = self.retriever.retrieve("Find context")
        self.assertEqual(result.entries, ())
        self.assertEqual(self.client.query_count, 0)

    def test_failed_service_does_not_start_an_agent_or_save_a_successful_session(self):
        config = RuntimeConfig(role="retrieve", memory_root=self.root, retrieve_backend="embedding", embedding=self.config)
        with patch.object(RightMemoryRuntime, "_build_agent", side_effect=AssertionError("agent constructed")), \
                patch.object(RightMemoryRuntime, "_pull_file_views_for_retrieve"):
            runtime = RightMemoryRuntime(config)
            with patch.object(runtime._embedding_retriever.client, "info", side_effect=EmbeddingServiceError("offline")), \
                    self.assertRaises(EmbeddingServiceError):
                runtime.run_session_turn("failed-request", "Find context")
            runtime.cleanup()
        path = MessageSessionStore(self.root, "retrieve-embedding").paths("failed-request").history
        self.assertFalse(path.exists())

    def test_cli_agent_installation_can_use_embedding_without_a_provider(self):
        config = RuntimeConfig(role="retrieve", runtime_mode="cli-agent", memory_root=self.root,
                               retrieve_backend="embedding", embedding=self.config)
        with patch.object(RightMemoryRuntime, "_build_cli_agent", side_effect=AssertionError("CLI agent constructed")), \
                patch("rightmemory.runtime.CodexSdkRunner", side_effect=AssertionError("SDK constructed")), \
                patch.object(RightMemoryRuntime, "_pull_file_views_for_retrieve"):
            runtime = RightMemoryRuntime(config)
            runtime._embedding_retriever.client = self.client
            runtime.run_chat_turn("Find context", "cli-agent-install")
            runtime.cleanup()
        self.assertEqual(self.client.query_count, 1)

    def test_backing_symlink_cannot_escape_the_memory_root(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "outside.md"
            target.write_text("External data.", encoding="utf-8")
            path = self.root / "MEMORY_notes.md"
            path.unlink()
            try:
                path.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(ValueError):
                self.corpus()

    def test_40_candidates_are_selected_before_reranking_to_ten(self):
        corpus = RetrievalCorpus(tuple(RetrievalEntry(str(i), f"Source content {i}", str(i)) for i in range(60)), "test")
        result = self.retriever._select("Find context", corpus)
        self.assertEqual(self.client.rerank_counts, [40])
        self.assertEqual([entry.key for entry in result], [str(i) for i in range(39, 29, -1)])

    def test_runtime_bypasses_agents_and_preserves_their_session_state(self):
        config = RuntimeConfig(role="retrieve", memory_root=self.root, retrieve_backend="embedding", embedding=self.config)
        old_path = self.root / ".runtime/retrieve_context/sessions/existing.json"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(b"preserved opaque agent state")
        with patch.object(RightMemoryRuntime, "_build_agent", side_effect=AssertionError("agent constructed")), \
                patch.object(RightMemoryRuntime, "_build_cli_agent", side_effect=AssertionError("CLI agent constructed")), \
                patch.object(RightMemoryRuntime, "_pull_file_views_for_retrieve") as sync:
            runtime = RightMemoryRuntime(config)
            runtime._embedding_retriever.client = self.client
            started = []
            runtime.run_session_turn("existing", "Find context", on_started=lambda: started.append(True))
            runtime.run_session_turn("existing", "Find context", include_returned=True)
            runtime.run_turn("Find context")
            runtime.cleanup()
        self.assertEqual(started, [True])
        self.assertEqual(sync.call_count, 3)
        self.assertEqual(old_path.read_bytes(), b"preserved opaque agent state")
        path = MessageSessionStore(self.root, "retrieve-embedding").paths("existing").history
        state = json.loads(path.read_bytes())
        self.assertEqual(len(state["entries"]), 10)
        self.assertNotIn("model_history", state)

    def test_cli_calls_real_embedding_runtime(self):
        from rightmemory import cli
        config = RuntimeConfig(role="retrieve", memory_root=self.root, retrieve_backend="embedding", embedding=self.config)
        with patch("rightmemory.cli.load_config", return_value=config), \
                patch("rightmemory.embedding_retrieval.EmbeddingServiceClient", return_value=self.client), \
                patch.object(RightMemoryRuntime, "_pull_file_views_for_retrieve"), redirect_stdout(io.StringIO()):
            code = cli.main(["retrieve", "--session", "cli-test", "Find project context"])
        self.assertEqual(code, 0)
        self.assertEqual(self.client.query_count, 1)

    def test_mcp_calls_real_embedding_runtime(self):
        from mcp import Client
        from rightmemory.mcp import create_mcp_server
        (self.root / "rightmemory.toml").write_text(
            '[retrieve]\nbackend="embedding"\n[retrieve.embedding]\nurl="http://127.0.0.1:8766"\n',
            encoding="utf-8",
        )
        server = create_mcp_server(self.root)
        async def call():
            async with Client(server, raise_exceptions=True) as client:
                return await client.call_tool("rightmemory_retrieve", {"session_id": "mcp-test", "need": "Find context"})
        with patch("rightmemory.embedding_retrieval.EmbeddingServiceClient", return_value=self.client), \
                patch.object(RightMemoryRuntime, "_build_agent", side_effect=AssertionError("agent constructed")), \
                patch.object(RightMemoryRuntime, "_pull_file_views_for_retrieve"):
            asyncio.run(call())
        self.assertEqual(self.client.query_count, 1)
        path = MessageSessionStore(self.root, "retrieve-embedding").paths("mcp-test").history
        self.assertEqual(len(json.loads(path.read_bytes())["entries"]), 10)


class EmbeddingConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def configure(self, body):
        (self.root / "rightmemory.toml").write_text(body, encoding="utf-8")
        return load_config("retrieve", self.root)

    def test_embedding_requires_no_model_or_cli_provider(self):
        config = self.configure('[retrieve]\nbackend="embedding"\n[retrieve.embedding]\nurl="http://localhost:8766"\n')
        self.assertEqual(config.retrieve_backend, "embedding")
        self.assertEqual(config.embedding.candidate_count, 40)
        self.assertEqual(config.embedding.result_count, 10)
        self.assertIsNone(config.model_id)
        self.assertIsNone(config.agent_cli)

    def test_rejects_ambiguous_or_invalid_configuration(self):
        base = '[retrieve]\nbackend="embedding"\n[retrieve.embedding]\n'
        for body in (
            'url="file:///models"', 'url="http://user:pass@localhost"', 'url="http://localhost:99999"',
            'url="http://localhost"\ncandidate_count=3\nresult_count=10',
            'url="http://localhost"\nresult_count=true', 'url="http://localhost"\nthreshold=0.1',
            'url="http://localhost"\ntimeout_seconds=nan',
            'url="http://localhost"\n[retrieve.model]\nmodel_id="test"',
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.configure(base + body)
        with self.assertRaises(ValueError):
            self.configure('[retrieve.embedding]\nurl="http://localhost"')

    def test_profile_seed_preserves_retrieval_service_settings(self):
        settings = {"backend": "embedding", "embedding": {"url": "http://localhost:8766"}, "max_output_chars": 15000}
        self.assertEqual(_profile_seed_config({"retrieve": settings})["retrieve"], settings)

    def test_candidate_count_is_not_capped_by_a_specific_model(self):
        config = self.configure('[retrieve]\nbackend="embedding"\n[retrieve.embedding]\n'
                                'url="http://localhost:8766"\ncandidate_count=150\n')
        self.assertEqual(config.embedding.candidate_count, 150)

    def test_service_entrypoint_does_not_require_a_memory_root(self):
        from rightmemory import entrypoint
        with patch("rightmemory.embedding_service.main", return_value=0) as main, \
                patch("rightmemory.entrypoint.resolve_memory_root", side_effect=AssertionError("root resolved")):
            self.assertEqual(entrypoint.main(["embedding-service", "--help"]), 0)
        main.assert_called_once_with(["--help"])


class FakeEmbeddingAdapter:
    identity = "embedding-one"
    dimensions = 2
    max_batch_size = 3

    def __init__(self, path=None, *, device=None):
        self.calls = []

    def encode(self, texts, *, kind):
        self.calls.append((kind, len(texts)))
        return [[1.0, 0.0] for _ in texts]


class FakeRerankerAdapter:
    identity = "reranker-one"
    max_candidates = 200

    def __init__(self, path=None, *, device=None):
        self.calls = []

    def rerank(self, query, documents):
        self.calls.append(len(documents))
        return list(reversed(range(len(documents))))


class EmbeddingProtocolTests(unittest.TestCase):
    def test_bad_vectors_are_rejected(self):
        for value in ([1], [0, 0], [float("nan"), 1], [float("inf"), 1], [True, 1], ["1", 1]):
            with self.subTest(value=value), self.assertRaises(EmbeddingServiceError):
                _vector(value, 2)
        self.assertAlmostEqual(sum(x * x for x in _vector([3, 4], 2)), 1)

    def test_bad_rerank_results_and_model_changes_are_rejected(self):
        client = EmbeddingServiceClient(EmbeddingRetrieveConfig("http://localhost"))
        info = ModelInfo("embedding-one", "reranker-one", 2, 125, 8)
        for indices in ([0, 0], [1], [1, True], [0, 2]):
            with patch.object(client, "_request", return_value={"model": "reranker-one", "indices": indices}), \
                    self.assertRaises(EmbeddingServiceError):
                client.rerank("Find context", ["a", "b"], info)
        with patch.object(client, "_request", return_value={"model": "changed", "vectors": [[1, 0]]}), \
                self.assertRaises(EmbeddingServiceError):
            client.embed(["Find context"], query=True, info=info)

    def test_service_authentication_and_model_identity(self):
        with TestClient(create_app(FakeEmbeddingAdapter(), FakeRerankerAdapter(), api_key="test-token")) as client:
            self.assertEqual(client.get("/info").status_code, 401)
            headers = {"Authorization": "Bearer test-token"}
            self.assertEqual(client.get("/info", headers=headers).status_code, 200)
            request = {"model": "embedding-one", "texts": ["A passage"], "kind": "passage"}
            self.assertEqual(client.post("/embed", json=request, headers=headers).json()["vectors"], [[1, 0]])
            request["model"] = "stale-model"
            self.assertEqual(client.post("/embed", json=request, headers=headers).status_code, 409)
            self.assertEqual(client.post("/rerank", json={"model": "reranker-one", "query": "q",
                             "documents": ["a", "b"]}, headers=headers).json()["indices"], [1, 0])

    def test_service_enforces_each_adapters_limits_before_inference(self):
        embedding, reranker = FakeEmbeddingAdapter(), FakeRerankerAdapter()
        reranker.max_candidates = 1
        with TestClient(create_app(embedding, reranker)) as client:
            response = client.post("/embed", json={"model": embedding.identity, "kind": "passage", "texts": ["x"] * 4})
            self.assertEqual(response.status_code, 422)
            response = client.post("/rerank", json={"model": reranker.identity, "query": "q", "documents": ["x"] * 2})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(embedding.calls, [])
        self.assertEqual(reranker.calls, [])

    def test_invalid_service_capabilities_are_rejected(self):
        client = EmbeddingServiceClient(EmbeddingRetrieveConfig("http://localhost"))
        valid = {"version": 1, "embedding_model": "e", "reranker_model": "r",
                 "dimensions": 2, "max_candidates": 200, "max_batch_size": 3}
        for field in ("dimensions", "max_candidates", "max_batch_size"):
            for value in (0, -1, True, 1.5, None):
                with self.subTest(field=field, value=value), \
                        patch.object(client, "_request", return_value={**valid, field: value}), \
                        self.assertRaises(EmbeddingServiceError):
                    client.info()

    def test_service_selects_adapters_independently(self):
        from rightmemory import embedding_service
        with patch.dict(embedding_service.EMBEDDING_ADAPTERS, {"other-embedding": FakeEmbeddingAdapter}), \
                patch.dict(embedding_service.RERANKER_ADAPTERS, {"other-reranker": FakeRerankerAdapter}), \
                patch("uvicorn.run") as run:
            status = embedding_service.main([
                "--embedding-adapter", "other-embedding", "--reranker-adapter", "other-reranker",
                "--embedding-model", "embed", "--reranker-model", "rerank", "--device", "cpu",
            ])
        self.assertEqual(status, 0)
        with TestClient(run.call_args.args[0]) as client:
            info = client.get("/info").json()
        self.assertEqual((info["embedding_model"], info["reranker_model"]), ("embedding-one", "reranker-one"))
        self.assertEqual((info["max_batch_size"], info["max_candidates"]), (3, 200))

    def test_adapter_identity_includes_settings_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.safetensors").write_bytes(b"fixed weights")
            first = adapter_identity(root, "test", revision=1, settings={"dimensions": 2, "normalize": True})
            self.assertEqual(first, adapter_identity(root, "test", revision=1, settings={"normalize": True, "dimensions": 2}))
            self.assertNotEqual(first, adapter_identity(root, "test", revision=1, settings={"dimensions": 3, "normalize": True}))
            self.assertNotEqual(first, adapter_identity(root, "test", revision=2, settings={"dimensions": 2, "normalize": True}))

    def test_model_fingerprint_changes_with_weights_or_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.safetensors").write_bytes(b"first weights")
            (root / "modeling.py").write_bytes(b"first code")
            first = model_fingerprint(root)
            (root / "modeling.py").write_bytes(b"second code")
            self.assertNotEqual(first, model_fingerprint(root))
            second = model_fingerprint(root)
            (root / "model.safetensors").write_bytes(b"second weights")
            self.assertNotEqual(second, model_fingerprint(root))


if __name__ == "__main__":
    unittest.main()
