from __future__ import annotations

from contextlib import nullcontext
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from rightmemory import embedding_models, embedding_service
from tests.test_embedding_retrieval import FakeRerankerAdapter


class Tensor:
    """Small numeric test double; the ordinary test suite does not require torch."""

    def __init__(self, values):
        self.values = values
        self.shape = (len(values), len(values[0])) if isinstance(values[0], list) else (len(values),)
        self.device = "cpu"

    def sum(self, *, dim):
        assert dim == 1
        return Tensor([sum(row) for row in self.values])

    def __sub__(self, value):
        return Tensor([item - value for item in self.values])

    def __getitem__(self, indices):
        rows, columns = indices
        return Tensor([self.values[row][column] for row, column in zip(rows, columns.values, strict=True)])

    def float(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return self.values


def normalize(tensor, *, p, dim):
    assert (p, dim) == (2, 1)
    return Tensor([[value / max(math.hypot(*row), 1e-12) for value in row] for row in tensor.values])


class Batch(dict):
    def to(self, device):
        self.device = device
        return self


class JinaNanoEmbeddingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        (self.path / "model.safetensors").write_bytes(b"mock weights")
        self.batch = Batch(input_ids=Tensor([[1, 2, 0], [1, 2, 3]]),
                           attention_mask=Tensor([[1, 1, 0], [1, 1, 1]]))
        self.tokenizer = Mock(return_value=self.batch)
        self.model = Mock()
        self.model.config = SimpleNamespace(hidden_size=768, max_position_embeddings=8192)
        self.model.to.return_value = self.model
        self.model.eval.return_value = self.model
        self.model.return_value = SimpleNamespace(last_hidden_state=Tensor([
            [[9, 9], [3, 4], [100, 100]], [[9, 9], [100, 100], [0, 5]],
        ]))
        self.torch = SimpleNamespace(
            __version__="test", bfloat16="bfloat16", float32="float32",
            inference_mode=Mock(side_effect=nullcontext), arange=lambda count, **kwargs: range(count),
            nn=SimpleNamespace(functional=SimpleNamespace(normalize=Mock(side_effect=normalize))),
        )
        self.transformers = SimpleNamespace(
            __version__="test", AutoTokenizer=Mock(), AutoModel=Mock(),
        )
        self.transformers.AutoTokenizer.from_pretrained.return_value = self.tokenizer
        self.transformers.AutoModel.from_pretrained.return_value = self.model
        dependencies = patch.object(embedding_models, "_dependencies", return_value=(self.torch, self.transformers))
        dependencies.start()
        self.addCleanup(dependencies.stop)

    def test_loads_local_custom_checkpoint_with_device_precision(self):
        for device, dtype in (("cpu", "float32"), ("cuda:0", "bfloat16")):
            with self.subTest(device=device):
                adapter = embedding_models.JinaV5NanoEmbedding(self.path, device=device)
                self.transformers.AutoTokenizer.from_pretrained.assert_called_with(
                    str(self.path.resolve()), local_files_only=True, trust_remote_code=True, padding_side="right",
                )
                self.transformers.AutoModel.from_pretrained.assert_called_with(
                    str(self.path.resolve()), local_files_only=True, use_safetensors=True, trust_remote_code=True,
                    dtype=dtype, attn_implementation="sdpa",
                )
                self.model.to.assert_called_with(device)
                self.model.eval.assert_called_with()
                self.assertEqual((adapter.dimensions, adapter.max_tokens, adapter.max_batch_size), (768, 8192, 8))

    def test_missing_path_fails_before_loading(self):
        with self.assertRaises(FileNotFoundError):
            embedding_models.JinaV5NanoEmbedding(self.path / "missing", device="cpu")
        self.transformers.AutoModel.from_pretrained.assert_not_called()
        self.transformers.AutoTokenizer.from_pretrained.assert_not_called()

    def test_query_and_passage_prefixes_and_masked_last_token_pooling(self):
        adapter = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        for kind, prefix in (("query", "Query: "), ("passage", "Document: ")):
            with self.subTest(kind=kind):
                vectors = adapter.encode(["short", "longer text"], kind=kind)
                self.tokenizer.assert_called_with(
                    [prefix + "short", prefix + "longer text"],
                    padding=True, truncation=False, return_tensors="pt",
                )
                self.assertEqual(vectors, [[0.6, 0.8], [0.0, 1.0]])
                self.assertEqual(self.batch.device, "cpu")
                self.model.assert_called_with(**self.batch)
        self.assertEqual(self.torch.inference_mode.call_count, 2)

    def test_context_overflow_fails_before_device_transfer_and_inference(self):
        adapter = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        self.batch["input_ids"].shape = (2, 8193)
        with self.assertRaises(ValueError):
            adapter.encode(["too long"], kind="query")
        self.assertFalse(hasattr(self.batch, "device"))
        self.model.assert_not_called()

    def test_exact_context_boundary_is_allowed(self):
        adapter = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        self.batch["input_ids"].shape = (2, 8192)
        self.assertEqual(adapter.encode(["at limit"], kind="passage"), [[0.6, 0.8], [0.0, 1.0]])

    def test_context_respects_smaller_checkpoint_limit(self):
        self.model.config.max_position_embeddings = 4096
        adapter = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        self.assertEqual(adapter.max_tokens, 4096)
        self.model.config.max_position_embeddings = 32768
        adapter = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        self.assertEqual(adapter.max_tokens, 8192)

    def test_identity_tracks_precision_weights_and_adapter_settings(self):
        cpu = embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        self.assertTrue(cpu.identity.startswith("jina-v5-nano:"))
        self.assertEqual(cpu.identity, embedding_models.JinaV5NanoEmbedding(self.path, device="cpu").identity)
        self.assertNotEqual(cpu.identity, embedding_models.JinaV5NanoEmbedding(self.path, device="cuda:0").identity)
        with patch.object(embedding_models, "adapter_identity", return_value="identity") as identity:
            embedding_models.JinaV5NanoEmbedding(self.path, device="cpu")
        settings = identity.call_args.kwargs["settings"]
        self.assertEqual(settings["pooling"], "last-token")
        self.assertEqual(settings["normalization"], "l2")
        self.assertEqual(settings["padding_side"], "right")
        (self.path / "model.safetensors").write_bytes(b"changed weights")
        self.assertNotEqual(cpu.identity, embedding_models.JinaV5NanoEmbedding(self.path, device="cpu").identity)

    def test_cli_selects_nano_and_service_reports_its_identity_and_dimensions(self):
        with patch.dict(embedding_service.RERANKER_ADAPTERS, {"jina-v3.5": FakeRerankerAdapter}), \
                patch.dict("os.environ", {"RIGHTMEMORY_EMBEDDING_API_KEY": ""}), \
                patch("uvicorn.run") as run:
            self.assertEqual(embedding_service.main([
                "--embedding-adapter", "jina-v5-nano", "--embedding-model", str(self.path),
                "--reranker-model", "rerank", "--device", "cpu",
            ]), 0)
        with TestClient(run.call_args.args[0]) as client:
            info = client.get("/info").json()
            self.assertTrue(info["embedding_model"].startswith("jina-v5-nano:"))
            self.assertEqual((info["dimensions"], info["max_batch_size"]), (768, 8))
            response = client.post("/embed", json={"model": info["embedding_model"],
                                   "kind": "passage", "texts": ["short", "longer text"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["vectors"], [[0.6, 0.8], [0.0, 1.0]])
            self.batch["input_ids"].shape = (2, 8193)
            self.assertEqual(client.post("/embed", json={"model": info["embedding_model"],
                             "kind": "query", "texts": ["too long"]}).status_code, 422)

    def test_nemotron_remains_the_default(self):
        self.assertIs(embedding_models.EMBEDDING_ADAPTERS["nemotron3"], embedding_models.Nemotron3Embedding)
        from tests.test_embedding_retrieval import FakeEmbeddingAdapter
        embedding = Mock(side_effect=FakeEmbeddingAdapter)
        with patch.dict(embedding_service.EMBEDDING_ADAPTERS, {"nemotron3": embedding}), \
                patch.dict(embedding_service.RERANKER_ADAPTERS, {"jina-v3.5": FakeRerankerAdapter}), \
                patch.dict("os.environ", {"RIGHTMEMORY_EMBEDDING_API_KEY": ""}), patch("uvicorn.run"):
            self.assertEqual(embedding_service.main([
                "--embedding-model", "embed", "--reranker-model", "rerank", "--device", "cpu",
            ]), 0)
        embedding.assert_called_once_with(Path("embed"), device="cpu")


if __name__ == "__main__":
    unittest.main()
