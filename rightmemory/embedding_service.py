"""HTTP transport for independently selected embedding and reranking adapters."""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import secrets
from threading import Lock
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .embedding_models import EMBEDDING_ADAPTERS, RERANKER_ADAPTERS, EmbeddingAdapter, RerankerAdapter


PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))


def _allows_keyless_bind(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in PRIVATE_NETWORKS)


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    texts: list[Annotated[str, Field(min_length=1, max_length=131072)]] = Field(min_length=1)
    kind: Literal["query", "passage"]
    model: str


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=131072)
    documents: list[Annotated[str, Field(min_length=1, max_length=131072)]] = Field(min_length=1)
    model: str


def create_app(embedding: EmbeddingAdapter, reranker: RerankerAdapter, *, api_key: str | None = None) -> FastAPI:
    lock = Lock()
    for value in (embedding.dimensions, embedding.max_batch_size, reranker.max_candidates):
        if type(value) is not int or value < 1:
            raise ValueError("model adapters must report positive integer dimensions and limits")
    if not all(isinstance(value, str) and value for value in (embedding.identity, reranker.identity)):
        raise ValueError("model adapters must report nonempty identities")

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if api_key and not secrets.compare_digest(authorization or "", f"Bearer {api_key}"):
            raise HTTPException(status_code=401, detail="invalid model-service credentials")

    app = FastAPI(title="RightMemory embedding service", dependencies=[Depends(authorize)])

    @app.get("/info")
    def info():
        return {"version": 1, "embedding_model": embedding.identity, "reranker_model": reranker.identity,
                "dimensions": embedding.dimensions, "max_batch_size": embedding.max_batch_size,
                "max_candidates": reranker.max_candidates}

    @app.post("/embed")
    def embed(request: EmbedRequest):
        if request.model != embedding.identity:
            raise HTTPException(status_code=409, detail="embedding model changed; retry retrieval")
        if len(request.texts) > embedding.max_batch_size:
            raise HTTPException(status_code=422, detail="embedding batch exceeds the adapter's limit")
        if any(not text.strip() for text in request.texts):
            raise HTTPException(status_code=422, detail="embedding text must not be blank")
        with lock:
            try:
                vectors = embedding.encode(request.texts, kind=request.kind)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"model": request.model, "vectors": vectors}

    @app.post("/rerank")
    def rerank(request: RerankRequest):
        if request.model != reranker.identity:
            raise HTTPException(status_code=409, detail="reranker model changed; retry retrieval")
        if len(request.documents) > reranker.max_candidates:
            raise HTTPException(status_code=422, detail="candidate list exceeds the adapter's limit")
        if not request.query.strip() or any(not text.strip() for text in request.documents):
            raise HTTPException(status_code=422, detail="query and passages must not be blank")
        with lock:
            try:
                indices = reranker.rerank(request.query, request.documents)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"model": request.model, "indices": indices}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory embedding-service")
    parser.add_argument("--embedding-adapter", choices=EMBEDDING_ADAPTERS, default="nemotron3")
    parser.add_argument("--reranker-adapter", choices=RERANKER_ADAPTERS, default="jina-v3.5")
    parser.add_argument("--embedding-model", type=Path, required=True, help="downloaded embedding snapshot")
    parser.add_argument("--reranker-model", type=Path, required=True, help="downloaded reranker snapshot")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--api-key-env", default="RIGHTMEMORY_EMBEDDING_API_KEY")
    args = parser.parse_args(argv)
    api_key = os.environ.get(args.api_key_env) or None
    if not _allows_keyless_bind(args.host) and not api_key:
        parser.error("without an API key, bind to localhost or an explicit private IP address")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    import uvicorn

    embedding = EMBEDDING_ADAPTERS[args.embedding_adapter](args.embedding_model, device=args.device)
    reranker = RERANKER_ADAPTERS[args.reranker_adapter](args.reranker_model, device=args.device)
    embedding.encode(["Warm up retrieval."], kind="query")
    reranker.rerank("Find relevant memory.", ["A relevant memory."])
    uvicorn.run(create_app(embedding, reranker, api_key=api_key), host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
