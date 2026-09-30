"""Model-specific adapters used by the optional retrieval service."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Literal, Protocol


class EmbeddingAdapter(Protocol):
    identity: str
    dimensions: int
    max_batch_size: int

    def encode(self, texts: list[str], *, kind: Literal["query", "passage"]) -> list[list[float]]: ...


class RerankerAdapter(Protocol):
    identity: str
    max_candidates: int

    def rerank(self, query: str, documents: list[str]) -> list[int]: ...


def model_fingerprint(path: Path) -> str:
    """Include weights, tokenizer, and executable model code in the cache identity."""
    path = path.expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("model path must be a downloaded snapshot directory")
    files = sorted(file for file in path.rglob("*") if file.is_file()
                   and file.suffix in {".safetensors", ".json", ".py", ".model", ".txt", ".tiktoken"})
    if not any(file.suffix == ".safetensors" for file in files):
        raise ValueError("model snapshot must contain safetensors weights")
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(path).as_posix().encode("utf-8") + b"\0")
        with file.open("rb") as handle:
            digest.update(hashlib.file_digest(handle, "sha256").digest())
    return digest.hexdigest()


def adapter_identity(path: Path, name: str, *, revision: int, settings: dict) -> str:
    # Bump revision when adapter behavior changes beyond the recorded settings.
    payload = {"weights": model_fingerprint(path), "adapter": name, "revision": revision, "settings": settings}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
    return f"{name}:{digest}"


def _dependencies():
    try:
        import torch
        import transformers
    except ImportError as exc:
        raise RuntimeError("install rightmemory[embedding-service] on the model host") from exc
    torch.set_num_threads(4)
    return torch, transformers


class Nemotron3Embedding:
    max_batch_size = 8

    def __init__(self, path: Path, *, device: str):
        torch, transformers = _dependencies()
        self.torch, self.device = torch, device
        path = path.expanduser().resolve(strict=True)
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(path), local_files_only=True, padding_side="left",
        )
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = transformers.AutoModel.from_pretrained(
            str(path), local_files_only=True, use_safetensors=True,
            dtype=dtype, attn_implementation="sdpa",
        ).to(device).eval()
        self.dimensions = self.model.config.hidden_size
        self.max_tokens = min(32768, self.model.config.max_position_embeddings)
        self.prefixes = {"query": "query: ", "passage": "passage: "}
        self.identity = adapter_identity(path, "nemotron3", revision=1, settings={
            "prefixes": self.prefixes, "pooling": "masked-mean", "normalization": "l2",
            "padding_side": "left", "dtype": str(dtype), "attention": "sdpa",
            "dimensions": self.dimensions, "max_tokens": self.max_tokens,
            "torch": torch.__version__, "transformers": transformers.__version__,
        })

    def encode(self, texts: list[str], *, kind: Literal["query", "passage"]) -> list[list[float]]:
        batch = self.tokenizer([self.prefixes[kind] + text for text in texts],
                               padding=True, truncation=False, return_tensors="pt")
        if batch["input_ids"].shape[1] > self.max_tokens:
            raise ValueError("embedding input exceeds the model context; shorten the query or source passage")
        batch = batch.to(self.device)
        with self.torch.inference_mode():
            hidden = self.model(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
            vectors = self.torch.nn.functional.normalize(pooled.float(), p=2, dim=1)
        return vectors.cpu().tolist()


class JinaV5NanoEmbedding:
    """The merged jina-embeddings-v5-text-nano-retrieval checkpoint."""

    max_batch_size = 8

    def __init__(self, path: Path, *, device: str):
        torch, transformers = _dependencies()
        self.torch, self.device = torch, device
        path = path.expanduser().resolve(strict=True)
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(path), local_files_only=True, trust_remote_code=True, padding_side="right",
        )
        self.model = transformers.AutoModel.from_pretrained(
            str(path), local_files_only=True, use_safetensors=True, trust_remote_code=True,
            dtype=dtype, attn_implementation="sdpa",
        ).to(device).eval()
        self.dimensions = self.model.config.hidden_size
        self.max_tokens = min(8192, self.model.config.max_position_embeddings)
        self.prefixes = {"query": "Query: ", "passage": "Document: "}
        self.identity = adapter_identity(path, "jina-v5-nano", revision=1, settings={
            "checkpoint": "jinaai/jina-embeddings-v5-text-nano-retrieval",
            "prefixes": self.prefixes, "pooling": "last-token", "normalization": "l2",
            "padding_side": "right", "dtype": str(dtype), "attention": "sdpa",
            "dimensions": self.dimensions, "max_tokens": self.max_tokens,
            "torch": torch.__version__, "transformers": transformers.__version__,
        })

    def encode(self, texts: list[str], *, kind: Literal["query", "passage"]) -> list[list[float]]:
        batch = self.tokenizer([self.prefixes[kind] + text for text in texts],
                               padding=True, truncation=False, return_tensors="pt")
        if batch["input_ids"].shape[1] > self.max_tokens:
            raise ValueError("embedding input exceeds the model context; shorten the query or source passage")
        batch = batch.to(self.device)
        with self.torch.inference_mode():
            hidden = self.model(**batch).last_hidden_state
            # With right padding, each row ends at its own last non-padding token.
            last_tokens = batch["attention_mask"].sum(dim=1) - 1
            pooled = hidden[self.torch.arange(hidden.shape[0], device=hidden.device), last_tokens]
            vectors = self.torch.nn.functional.normalize(pooled.float(), p=2, dim=1)
        return vectors.cpu().tolist()


class Jina35Reranker:
    max_candidates = 125
    max_query_tokens = 1024
    max_passage_tokens = 8192

    def __init__(self, path: Path, *, device: str):
        torch, transformers = _dependencies()
        self.torch = torch
        path = path.expanduser().resolve(strict=True)
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self.model = transformers.AutoModel.from_pretrained(
            str(path), local_files_only=True, use_safetensors=True, trust_remote_code=True,
            dtype=dtype, attn_implementation="sdpa",
        ).to(device).eval()
        # The custom model's lazy tokenizer loader otherwise omits local_files_only.
        self.model._tokenizer = transformers.AutoTokenizer.from_pretrained(
            str(path), local_files_only=True, trust_remote_code=True, padding_side="left",
        )
        if self.model._tokenizer.pad_token_id is None:
            self.model._tokenizer.pad_token = self.model._tokenizer.unk_token
        self.formatter = sys.modules[type(self.model).__module__].format_docs_prompts_func
        self.identity = adapter_identity(path, "jina-v3.5", revision=1, settings={
            "no_thinking": True, "dtype": str(dtype), "attention": "sdpa", "padding_side": "left",
            "max_query_tokens": self.max_query_tokens, "max_passage_tokens": self.max_passage_tokens,
            "torch": torch.__version__, "transformers": transformers.__version__,
        })

    def rerank(self, query: str, documents: list[str]) -> list[int]:
        tokenizer = self.model._tokenizer
        if len(tokenizer.encode(query)) >= self.max_query_tokens:
            raise ValueError(f"reranker query must be shorter than {self.max_query_tokens} tokens")
        if any(len(tokenizer.encode(document)) >= self.max_passage_tokens for document in documents):
            raise ValueError(f"reranker passage exceeds its {self.max_passage_tokens}-token limit")
        prompt = self.formatter(query, documents, special_tokens=self.model.special_tokens, no_thinking=True)
        limit = min(self.model.config.max_position_embeddings, tokenizer.model_max_length)
        if len(tokenizer.encode(prompt)) > limit:
            raise ValueError("candidate list exceeds the reranker context; reduce candidate_count")
        with self.torch.inference_mode():
            results = self.model.rerank(query, documents)
        if any(not math.isfinite(float(result["relevance_score"])) for result in results):
            raise RuntimeError("reranker produced non-finite scores")
        return [int(result["index"]) for result in results]


EMBEDDING_ADAPTERS = {"nemotron3": Nemotron3Embedding, "jina-v5-nano": JinaV5NanoEmbedding}
RERANKER_ADAPTERS = {"jina-v3.5": Jina35Reranker}
