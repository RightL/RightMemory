"""Nemotron + Jina reranking experiment; private inputs/results stay under tmp/.

Scores exact memory entries, without expanding headings/subtrees. This keeps the
final entry count bounded. Model downloads and cold startup are excluded from
warm timing. No production configuration or live Memory root is changed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

from retrieval_embeddings import (
    digest, pool_hidden_state, rank_indices, read_json, score_ids,
    verified_inputs, write_json,
)


def candidate_union(rankings, eligible, count):
    """Take each search's first count substantive entries, then deduplicate."""
    merged = []
    seen = set()
    for ranking in rankings:
        selected = [key for key in ranking if key in eligible][:count]
        for key in selected:
            if key not in seen:
                merged.append(key)
                seen.add(key)
    return merged


def bounded_selection(ids, scores, top_k, threshold=None):
    if len(ids) != len(scores) or len(set(ids)) != len(ids):
        raise ValueError("Scores must have one unique source ID each.")
    if not all(math.isfinite(float(x)) for x in scores):
        raise ValueError("Non-finite reranker score.")
    ranked = sorted(zip(ids, scores), key=lambda pair: -pair[1])
    return [key for key, score in ranked if threshold is None or score >= threshold][:top_k]


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def aggregate(rows):
    positive = [r for r in rows if r["required"]]
    negative = [r for r in rows if not r["required"]]
    return {
        "positive_cases": len(positive),
        "complete_positive_cases": sum(r["all_required"] for r in positive),
        "mean_required_recall": statistics.mean(r["recall"] for r in positive) if positive else None,
        "positive_cases_returning_nothing": sum(r["empty"] for r in positive),
        "negative_cases": len(negative),
        "correct_empty_negative_cases": sum(r["empty"] for r in negative),
        "mean_returned_entries": statistics.mean(r["returned"] for r in rows),
        "max_returned_entries": max(r["returned"] for r in rows),
    }


def summarize(results, cases, top_k, thresholds):
    policies = {"nemotron": {}, "jina": {}}
    for method in policies:
        for threshold in [None, *thresholds]:
            rows = []
            for case in cases:
                result = results[case["id"]]
                ids, scores = result[method]["ids"], result[method]["scores"]
                selected = bounded_selection(ids, scores, top_k, threshold)
                rows.append({"id": case["id"], "selected": selected,
                             **score_ids(selected, case["required"], case.get("optional", []))})
            label = "unfiltered" if threshold is None else f"threshold={threshold:g}"
            policies[method][label] = {"summary": aggregate(rows), "cases": rows}
    timing = {}
    for stage in ("retrieval", "rerank", "total"):
        values = [value for result in results.values() for value in result["seconds"][stage]]
        if values:
            timing[stage] = {"samples": len(values), "median": statistics.median(values),
                             "mean": statistics.mean(values), "p95": percentile(values, 0.95)}
    return {"policies": policies, "timing_seconds": timing}


class NemotronSearch:
    def __init__(self, model_path, revision, documents, device):
        import numpy as np
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.np, self.torch, self.device = np, torch, device
        self.documents = documents
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, revision=revision, local_files_only=True, padding_side="left")
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModel.from_pretrained(
            model_path, revision=revision, local_files_only=True,
            dtype=torch.bfloat16, attn_implementation="sdpa").to(device).eval()
        self.vectors = np.concatenate([
            self.encode(["passage: " + d["text"] for d in documents[start:start+8]])
            for start in range(0, len(documents), 8)
        ])
        self.encode(["query: Warm up retrieval."])

    def encode(self, texts):
        with self.torch.inference_mode():
            batch = self.tokenizer(texts, padding=True, truncation=False, return_tensors="pt").to(self.device)
            if batch["input_ids"].shape[1] > 32768:
                raise ValueError("Embedding input would be truncated.")
            pooled = pool_hidden_state(self.model(**batch).last_hidden_state, batch["attention_mask"], "mean")
            return self.torch.nn.functional.normalize(pooled.float(), p=2, dim=1).cpu().numpy()

    def search(self, queries):
        output = []
        for query in queries:
            vector = self.encode(["query: " + query])[0]
            scores = self.vectors @ vector
            order = rank_indices(scores)
            output.append({"ids": [self.documents[i]["id"] for i in order],
                           "scores": {d["id"]: float(scores[i]) for i, d in enumerate(self.documents)}})
        return output


def run(args):
    import torch
    import transformers
    from transformers import AutoModel

    corpus, case_data, docs, reference = verified_inputs(args)
    cases = case_data["cases"]
    if args.ids:
        chosen = set(args.ids.split(","))
        if chosen - {c["id"] for c in cases}:
            raise ValueError("Unknown requested case.")
        cases = [c for c in cases if c["id"] in chosen]
    extras = []
    if args.additional_search and len(cases) != 1:
        raise ValueError("Additional search diagnostics need exactly one selected original case.")
    for case_path, ranking_path in args.additional_search:
        extra_cases, extra_ranks = read_json(case_path), read_json(ranking_path)
        if extra_ranks["corpus_sha256"] != digest(args.corpus) or extra_ranks["cases_sha256"] != digest(case_path):
            raise ValueError("Additional search snapshot mismatch.")
        extras.extend((c, extra_ranks["rankings"][c["id"]]) for c in extra_cases["cases"])
    eligible = set(corpus["evaluation_ids"])
    if any(set(c["required"]) - eligible for c in cases):
        raise ValueError("Required labels are not independently scored memory entries.")
    if not cases:
        raise ValueError("No cases selected.")
    torch.set_num_threads(4)
    device = args.device

    def sync():
        if str(device).startswith("cuda"):
            torch.cuda.synchronize()

    start = time.perf_counter()
    search = NemotronSearch(args.embedding_model, args.embedding_revision, corpus["documents"], device)
    sync()
    embedding_startup = time.perf_counter() - start
    start = time.perf_counter()
    model = AutoModel.from_pretrained(
        str(args.model), trust_remote_code=True, local_files_only=True,
        dtype=torch.bfloat16, attn_implementation="sdpa").to(device).eval()
    model._ensure_tokenizer()
    tokenizer = model._tokenizer
    formatter = sys.modules[model.__class__.__module__].format_docs_prompts_func
    sync()
    reranker_startup = time.perf_counter() - start
    model.rerank("Which passage answers the question?", ["A relevant passage.", "Unrelated material."])
    sync()
    results = {}
    tasks = [(repeat, case) for repeat in range(args.repeats) for case in cases]
    random.Random(args.seed).shuffle(tasks)
    for repeat, case in tasks:
        reference_rows = [reference["rankings"][case["id"]], *[r for _, r in extras]]
        queries = [case["query"], *[c["query"] for c, _ in extras]]
        sync()
        t0 = time.perf_counter()
        retrieved = search.search(queries)
        candidates = candidate_union([r["ids"] for r in retrieved], eligible, args.candidate_k)
        if args.order == "reverse":
            candidates.reverse()
        elif args.order == "shuffle":
            random.Random(f"{args.seed}:{case['id']}").shuffle(candidates)
        texts = [docs[key]["text"] for key in candidates]
        sync()
        t1 = time.perf_counter()
        # Validate once, outside the measured reranker call. Never silently truncate.
        if case["id"] not in results:
            for actual, expected in zip(retrieved, reference_rows):
                if actual["ids"] != expected["dense"]:
                    raise ValueError(f"Fresh embedding ranking differs from frozen control: {case['id']}")
            if len(tokenizer.encode(case["query"])) >= 1024:
                raise ValueError("Jina would truncate the query.")
            if any(len(tokenizer.encode(text)) >= 8192 for text in texts):
                raise ValueError("Jina would truncate a document.")
            prompt = formatter(case["query"], texts, special_tokens=model.special_tokens, no_thinking=True)
            tokens = len(tokenizer.encode(prompt))
            if tokens > model.config.max_position_embeddings or len(candidates) > 125:
                raise ValueError("This experiment requires one untruncated Jina candidate list.")
            results[case["id"]] = {
                "candidate_ids": candidates, "candidate_count": len(candidates), "input_tokens": tokens,
                "candidate_complete": set(case["required"]) <= set(candidates),
                "query_count": len(queries), "category": case["category"],
                "nemotron": {"ids": [key for key in retrieved[0]["ids"] if key in eligible],
                             "scores": [retrieved[0]["scores"][key] for key in retrieved[0]["ids"] if key in eligible]},
                "seconds": {"retrieval": [], "rerank": [], "total": []},
                "ranking_identical_across_repeats": True, "max_score_drift": 0.0,
            }
        sync()
        t2 = time.perf_counter()
        ranked = model.rerank(case["query"], texts)
        sync()
        t3 = time.perf_counter()
        indexes = [int(r["index"]) for r in ranked]
        if sorted(indexes) != list(range(len(candidates))):
            raise ValueError("Jina did not return each candidate exactly once.")
        ids = [candidates[i] for i in indexes]
        scores = [float(r["relevance_score"]) for r in ranked]
        bounded_selection(ids, scores, args.top_k)  # Check unique IDs and finite scores.
        if any(abs(score) > 1.00001 for score in scores):
            raise ValueError("Cosine score outside expected bounds.")
        result = results[case["id"]]
        if "jina" in result:
            result["ranking_identical_across_repeats"] &= ids == result["jina"]["ids"]
            previous = dict(zip(result["jina"]["ids"], result["jina"]["scores"]))
            result["max_score_drift"] = max(result["max_score_drift"],
                                            max(abs(previous[key]-score) for key, score in zip(ids, scores)))
        else:
            result["jina"] = {"ids": ids, "scores": scores}
        result["seconds"]["retrieval"].append(t1-t0)
        result["seconds"]["rerank"].append(t3-t2)
        result["seconds"]["total"].append((t1-t0) + (t3-t2))
        print(json.dumps({"case": case["id"], "repeat": repeat, "candidates": len(candidates),
                          "seconds": round((t1-t0) + (t3-t2), 4)}), flush=True)
    report = {
        "corpus_sha256": digest(args.corpus), "cases_sha256": digest(args.cases),
        "reference_rankings_sha256": digest(args.rankings),
        "model": str(args.model), "model_revision": args.revision,
        "model_code_sha256": digest(args.model / "modeling.py"),
        "embedding_model": args.embedding_model, "embedding_revision": args.embedding_revision,
        "candidate_k_per_query": args.candidate_k, "final_top_k": args.top_k,
        "candidate_order": args.order, "seed": args.seed, "repeats": args.repeats,
        "additional_searches": [{"cases_sha256": digest(c), "rankings_sha256": digest(r)}
                               for c, r in args.additional_search],
        "unit_policy": "Exact substantive memory IDs; no heading or subtree expansion.",
        "device": torch.cuda.get_device_name() if str(device).startswith("cuda") else device,
        "dtype": "bfloat16", "attention": "sdpa", "torch": torch.__version__,
        "transformers": transformers.__version__,
        "startup_seconds": {"embedding_and_index": embedding_startup, "reranker": reranker_startup},
        "peak_gpu_bytes": torch.cuda.max_memory_allocated() if str(device).startswith("cuda") else 0,
        "results": results, **summarize(results, cases, args.top_k, args.thresholds),
        "limitations": [
            "Agent-authored source-ID labels, not independent answer-quality judgments.",
            "Optional labels are incomplete; unlabelled selections are not proven irrelevant.",
            "Threshold sweep is exploratory on these cases, not held-out calibration.",
            "Warm local timing excludes network, cold startup, validation, and final rendering.",
            "Additional-query and order variants are diagnostics, not new independent cases.",
        ],
    }
    write_json(args.out, report)
    print(json.dumps({"output": str(args.out), "timing": report["timing_seconds"],
                      "nemotron": report["policies"]["nemotron"]["unfiltered"]["summary"],
                      "jina": report["policies"]["jina"]["unfiltered"]["summary"]}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--rankings", type=Path, required=True)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--embedding-model", default="nvidia/Nemotron-3-Embed-1B-BF16")
    p.add_argument("--embedding-revision", default="c0c9fea93ea424587517f2c59e20db9f1d6bf615")
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--candidate-k", type=int, default=40)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--seed", type=int, default=719)
    p.add_argument("--thresholds", type=float, nargs="+", default=[0, .1, .2, .3, .4, .5, .6, .7, .8, .9])
    p.add_argument("--ids")
    p.add_argument("--order", choices=["native", "reverse", "shuffle"], default="native")
    p.add_argument("--additional-search", nargs=2, type=Path, action="append", default=[],
                   metavar=("CASES", "RANKINGS"))
    args = p.parse_args()
    if min(args.candidate_k, args.top_k, args.repeats) < 1 or args.top_k > 10:
        p.error("Counts must be positive and final top-k cannot exceed ten.")
    if not all(math.isfinite(t) for t in args.thresholds):
        p.error("Thresholds must be finite.")
    run(args)


if __name__ == "__main__":
    main()
