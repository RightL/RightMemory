"""Optional, local-only model loading for Nemotron 1B and Jina reranker v3.5."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import math
import os
from pathlib import Path
import secrets
import sys
from threading import Lock
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    texts: list[Annotated[str, Field(min_length=1, max_length=131072)]] = Field(min_length=1, max_length=8)
    kind: Literal["query", "passage"]
    model: str


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=131072)
    documents: list[Annotated[str, Field(min_length=1, max_length=131072)]] = Field(min_length=1, max_length=125)
    model: str


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


class LocalModels:
    def __init__(self, embedding_model: Path, reranker_model: Path, *, device: str):
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("install rightmemory[embedding-service] on the model host") from exc
        self.torch = torch
        self.device = device
        torch.set_num_threads(4)
        embedding_model = embedding_model.expanduser().resolve(strict=True)
        reranker_model = reranker_model.expanduser().resolve(strict=True)
        self.embedding_id = "nemotron-3-embed-1b:" + model_fingerprint(embedding_model)
        self.reranker_id = "jina-reranker-v3.5:" + model_fingerprint(reranker_model)
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(embedding_model), local_files_only=True, padding_side="left",
        )
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.embedding = AutoModel.from_pretrained(
            str(embedding_model), local_files_only=True, use_safetensors=True,
            dtype=dtype, attn_implementation="sdpa",
        ).to(device).eval()
        self.reranker = AutoModel.from_pretrained(
            str(reranker_model), local_files_only=True, use_safetensors=True, trust_remote_code=True,
            dtype=dtype, attn_implementation="sdpa",
        ).to(device).eval()
        # Supply an explicitly offline tokenizer; the custom model's lazy loader otherwise omits that flag.
        self.reranker._tokenizer = AutoTokenizer.from_pretrained(
            str(reranker_model), local_files_only=True, trust_remote_code=True, padding_side="left",
        )
        if self.reranker._tokenizer.pad_token_id is None:
            self.reranker._tokenizer.pad_token = self.reranker._tokenizer.unk_token
        self.formatter = sys.modules[type(self.reranker).__module__].format_docs_prompts_func
        self.dimensions = self.embedding.config.hidden_size
        self.encode(["Warm up retrieval."], kind="query")
        self.rerank("Find relevant memory.", ["A relevant memory.", "An unrelated passage."])

    def info(self) -> dict:
        return {"version": 1, "embedding_model": self.embedding_id, "reranker_model": self.reranker_id,
                "dimensions": self.dimensions, "max_candidates": 125}

    def encode(self, texts: list[str], *, kind: str) -> list[list[float]]:
        prefix = "query: " if kind == "query" else "passage: "
        batch = self.tokenizer([prefix + text for text in texts], padding=True, truncation=False, return_tensors="pt")
        if batch["input_ids"].shape[1] > min(32768, self.embedding.config.max_position_embeddings):
            raise ValueError("embedding input exceeds the model context; shorten the query or source passage")
        batch = batch.to(self.device)
        with self.torch.inference_mode():
            hidden = self.embedding(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
            vectors = self.torch.nn.functional.normalize(pooled.float(), p=2, dim=1)
        return vectors.cpu().tolist()

    def rerank(self, query: str, documents: list[str]) -> list[int]:
        tokenizer = self.reranker._tokenizer
        if len(tokenizer.encode(query)) >= 1024:
            raise ValueError("reranker query must be shorter than 1024 tokens; state the retrieval need concisely")
        if any(len(tokenizer.encode(document)) >= 8192 for document in documents):
            raise ValueError("reranker passage exceeds its 8192-token limit")
        prompt = self.formatter(query, documents, special_tokens=self.reranker.special_tokens, no_thinking=True)
        limit = min(self.reranker.config.max_position_embeddings, tokenizer.model_max_length)
        if len(tokenizer.encode(prompt)) > limit:
            raise ValueError("candidate list exceeds the reranker context; reduce candidate_count")
        with self.torch.inference_mode():
            results = self.reranker.rerank(query, documents)
        if any(not math.isfinite(float(result["relevance_score"])) for result in results):
            raise RuntimeError("reranker produced non-finite scores")
        return [int(result["index"]) for result in results]


def create_app(models, *, api_key: str | None = None) -> FastAPI:
    lock = Lock()

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if api_key and not secrets.compare_digest(authorization or "", f"Bearer {api_key}"):
            raise HTTPException(status_code=401, detail="invalid model-service credentials")

    app = FastAPI(title="RightMemory embedding service", dependencies=[Depends(authorize)])

    @app.get("/info")
    def info():
        return models.info()

    @app.post("/embed")
    def embed(request: EmbedRequest):
        if request.model != models.info()["embedding_model"]:
            raise HTTPException(status_code=409, detail="embedding model changed; retry retrieval")
        if any(not text.strip() for text in request.texts):
            raise HTTPException(status_code=422, detail="embedding text must not be blank")
        with lock:
            try:
                vectors = models.encode(request.texts, kind=request.kind)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"model": request.model, "vectors": vectors}

    @app.post("/rerank")
    def rerank(request: RerankRequest):
        if request.model != models.info()["reranker_model"]:
            raise HTTPException(status_code=409, detail="reranker model changed; retry retrieval")
        if not request.query.strip() or any(not text.strip() for text in request.documents):
            raise HTTPException(status_code=422, detail="query and passages must not be blank")
        with lock:
            try:
                indices = models.rerank(request.query, request.documents)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"model": request.model, "indices": indices}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory embedding-service")
    parser.add_argument("--embedding-model", type=Path, required=True, help="downloaded Nemotron 3 Embed 1B BF16 snapshot")
    parser.add_argument("--reranker-model", type=Path, required=True, help="downloaded Jina reranker v3.5 snapshot")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--api-key-env", default="RIGHTMEMORY_EMBEDDING_API_KEY")
    args = parser.parse_args(argv)
    api_key = os.environ.get(args.api_key_env) or None
    try:
        loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not api_key:
        parser.error("set the API key environment variable before binding beyond loopback")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn

    models = LocalModels(args.embedding_model, args.reranker_model, device=args.device)
    uvicorn.run(create_app(models, api_key=api_key), host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
