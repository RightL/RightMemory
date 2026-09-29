"""Independent-query retrieval using a local source index and a model service."""
from __future__ import annotations

import json
import math
import os
import uuid
from http.client import HTTPException
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import EmbeddingRetrieveConfig
from .retrieval_index import RetrievalCorpus, RetrievalEntry, build_retrieval_corpus, search_passages, text_hash
from .session import MemoryWriteLock, MessageSessionStore, _ensure_runtime_gitignore


class EmbeddingServiceError(RuntimeError):
    """The configured embedding service did not complete a valid request."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EmbeddingServiceError("embedding service redirects are not supported; configure its final URL")


@dataclass(frozen=True)
class ModelInfo:
    embedding_model: str
    reranker_model: str
    dimensions: int
    max_candidates: int
    max_batch_size: int


class EmbeddingServiceClient:
    def __init__(self, config: EmbeddingRetrieveConfig):
        self.config = config
        self.opener = build_opener(_NoRedirect())

    def _request(self, route: str, payload: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.config.url + route, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise EmbeddingServiceError("embedding service response is too large")
            result = json.loads(raw)
        except HTTPError as exc:
            detail = ""
            try:
                error = json.loads(exc.read(8192))
                if isinstance(error, dict) and isinstance(error.get("detail"), str):
                    detail = ": " + error["detail"][:300]
            except (OSError, ValueError, UnicodeError, HTTPException):
                pass
            raise EmbeddingServiceError(f"embedding service rejected {route}: HTTP {exc.code}{detail}") from exc
        except (URLError, OSError, TimeoutError, HTTPException) as exc:
            raise EmbeddingServiceError(f"embedding service is unavailable: {exc}") from exc
        except (ValueError, UnicodeError) as exc:
            raise EmbeddingServiceError("embedding service returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise EmbeddingServiceError("embedding service response must be an object")
        return result

    def info(self) -> ModelInfo:
        data = self._request("/info")
        if (type(data.get("version")) is not int or data["version"] != 1
                or not all(isinstance(data.get(key), str) and data[key] for key in ("embedding_model", "reranker_model"))
                or any(type(data.get(key)) is not int or data[key] < 1
                       for key in ("dimensions", "max_candidates", "max_batch_size"))):
            raise EmbeddingServiceError("embedding service returned invalid model metadata")
        return ModelInfo(data["embedding_model"], data["reranker_model"], data["dimensions"],
                         data["max_candidates"], data["max_batch_size"])

    def embed(self, texts: list[str], *, query: bool, info: ModelInfo) -> list[list[float]]:
        data = self._request("/embed", {
            "texts": texts, "kind": "query" if query else "passage", "model": info.embedding_model,
        })
        if data.get("model") != info.embedding_model:
            raise EmbeddingServiceError("embedding model changed during retrieval; retry the request")
        values = data.get("vectors")
        if not isinstance(values, list) or len(values) != len(texts):
            raise EmbeddingServiceError("embedding service returned the wrong number of vectors")
        return [_vector(value, info.dimensions) for value in values]

    def rerank(self, query: str, documents: list[str], info: ModelInfo) -> list[int]:
        data = self._request("/rerank", {"query": query, "documents": documents, "model": info.reranker_model})
        if data.get("model") != info.reranker_model:
            raise EmbeddingServiceError("reranker model changed during retrieval; retry the request")
        order = data.get("indices")
        if (not isinstance(order, list) or any(type(index) is not int for index in order)
                or sorted(order) != list(range(len(documents)))):
            raise EmbeddingServiceError("reranker must return each candidate index exactly once")
        return order


@dataclass(frozen=True)
class EmbeddingRetrieveResult:
    text: str
    entries: tuple[RetrievalEntry, ...]
    fingerprint: str


class EmbeddingRetriever:
    def __init__(self, memory_root: Path, state_root: Path, config: EmbeddingRetrieveConfig, *, max_output_chars: int):
        self.memory_root = Path(memory_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.config = config
        self.max_output_chars = max_output_chars
        self.client = EmbeddingServiceClient(config)

    def retrieve(self, query: str) -> EmbeddingRetrieveResult:
        if not query.strip():
            raise ValueError("retrieval query must not be empty")
        # Network/model work stays outside the Memory lock. A changed snapshot is retried once.
        for _attempt in range(2):
            with MemoryWriteLock(self.memory_root):
                corpus = build_retrieval_corpus(self.memory_root)
            selected = self._select(query, corpus)
            with MemoryWriteLock(self.memory_root):
                current = build_retrieval_corpus(self.memory_root)
                if current.fingerprint != corpus.fingerprint:
                    continue
                return self._render(corpus, selected)
        raise RuntimeError("Memory changed during both retrieval attempts; retry the query")

    def _select(self, query: str, corpus: RetrievalCorpus) -> list[RetrievalEntry]:
        if not corpus.entries:
            return []
        info = self.client.info()
        if self.config.candidate_count > info.max_candidates:
            raise EmbeddingServiceError("candidate_count exceeds the configured reranker's supported list size")
        passages = [search_passages(entry.text) for entry in corpus.entries]
        vectors = self._vectors([part for parts in passages for part in parts], info)
        query_vector = self.client.embed([query], query=True, info=info)[0]
        candidates: list[tuple[float, int, str]] = []
        for index, parts in enumerate(passages):
            scored = [(sum(a * b for a, b in zip(vectors[text_hash(part)], query_vector)), part) for part in parts]
            score, best_passage = max(scored, key=lambda item: item[0])
            candidates.append((score, index, best_passage))
        # Each source entry occupies at most one slot, even when it has several search passages.
        candidates.sort(key=lambda item: (-item[0], item[1]))
        candidates = candidates[:self.config.candidate_count]
        order = self.client.rerank(query, [item[2] for item in candidates], info)
        return [corpus.entries[candidates[index][1]] for index in order[:self.config.result_count]]

    def _vectors(self, texts: list[str], info: ModelInfo) -> dict[str, list[float]]:
        unique = {text_hash(text): text for text in texts}
        cache_path = self.state_root / ".runtime" / "embedding_retrieval" / "vectors.json"
        with MessageSessionStore(self.state_root, "embedding-index").locked("vectors"):
            cache: dict = {}
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if (isinstance(loaded, dict) and loaded.get("model") == info.embedding_model
                        and loaded.get("root") == str(self.memory_root) and isinstance(loaded.get("vectors"), dict)):
                    cache = loaded["vectors"]
            except (OSError, ValueError, UnicodeError):
                pass  # This is a disposable derived cache, not Memory state.
            vectors: dict[str, list[float]] = {}
            for key in unique:
                if key in cache:
                    try:
                        vectors[key] = _vector(cache[key], info.dimensions)
                    except EmbeddingServiceError:
                        pass
            missing = [key for key in unique if key not in vectors]
            for offset in range(0, len(missing), info.max_batch_size):
                keys = missing[offset:offset + info.max_batch_size]
                values = self.client.embed([unique[key] for key in keys], query=False, info=info)
                vectors.update(zip(keys, values))
            if missing or set(cache) != set(vectors):
                _write_cache(cache_path, {"root": str(self.memory_root), "model": info.embedding_model, "vectors": vectors})
            return vectors

    def _render(self, corpus: RetrievalCorpus, selected: list[RetrievalEntry]) -> EmbeddingRetrieveResult:
        if not selected:
            return EmbeddingRetrieveResult("No searchable Memory entries are available.", (), corpus.fingerprint)
        # Source order keeps facts under their original headings, as in the agent retriever.
        tree = {}
        selected_keys = {entry.key for entry in selected}
        for entry in corpus.entries:
            if entry.key not in selected_keys:
                continue
            branch = tree
            parts = entry.display_parts or ((entry.key, f"Source: `{entry.source}`\n\n{entry.text}"),)
            for key, body in parts:
                _, branch = branch.setdefault(key, (body, {}))

        def render(branch: dict) -> str:
            return "\n\n".join(
                part for body, children in branch.values()
                for part in (body, render(children)) if part
            )

        text = render(tree)
        if len(text) > self.max_output_chars:
            raise ValueError(
                f"retrieval output is {len(text)} characters; reduce [retrieve.embedding].result_count "
                "or increase [retrieve].max_output_chars; source content was not truncated"
            )
        return EmbeddingRetrieveResult(text, tuple(selected), corpus.fingerprint)


def _vector(value: object, dimensions: int) -> list[float]:
    if (not isinstance(value, list) or len(value) != dimensions
            or any(type(number) not in {int, float} for number in value)):
        raise EmbeddingServiceError("embedding vector has invalid dimensions or non-finite values")
    try:
        values = [float(number) for number in value]
    except OverflowError as exc:
        raise EmbeddingServiceError("embedding vector contains an out-of-range number") from exc
    if any(not math.isfinite(number) for number in values):
        raise EmbeddingServiceError("embedding vector contains a non-finite value")
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm <= 0:
        raise EmbeddingServiceError("embedding vector must have a finite positive norm")
    return [number / norm for number in values]


def _write_cache(path: Path, value: dict) -> None:
    _ensure_runtime_gitignore(path.parent.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
