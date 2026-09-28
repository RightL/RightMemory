"""Isolated retrieval experiment. Private corpora and outputs belong under tmp/.

The snapshot and selector commands use RightMemory's canonical graph and renderer.
The embed command needs torch, transformers and numpy, but does not import RightMemory.
No command installs an index or changes a live Memory root.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selection_for(ids):
    from rightmemory.retrieve_selection import RetrieveSelection, SourceSelection
    local, sources = [], defaultdict(list)
    for item in dict.fromkeys(ids):
        if item.startswith("AC#"):
            source, entry = item.rsplit(":", 1)
            sources[source].append(entry)
        else:
            local.append(item)
    return RetrieveSelection(ids=local, sources=[
        SourceSelection(source_id=source, ids=entries) for source, entries in sources.items()
    ])


def snapshot(args):
    from rightmemory.config import load_config
    from rightmemory.corrections import AGENT_CORRECTION_SOURCE_PATHS, agent_correction_entries
    from rightmemory.graph import build_graph_manifest
    from rightmemory.retrieve_selection import RetrieveSelectionRenderer

    src, out = args.source.resolve(), args.out.resolve()
    if out.exists():
        raise ValueError("Use a new snapshot directory; existing snapshots are immutable.")
    manifest = build_graph_manifest(src)
    if manifest.errors:
        raise ValueError(manifest.errors)
    unsupported = {i.anchor_kind for i in manifest.items.values()} & {"M#", "S#", "MF#", "MQ#"}
    if unsupported:
        raise ValueError(f"This experiment does not yet cover these linked source types: {unsupported}")
    paths = set(manifest.files)
    paths.update(src / name for name in AGENT_CORRECTION_SOURCE_PATHS.values() if (src / name).exists())
    if (src / "AGENTS.md").exists():
        paths.add(src / "AGENTS.md")
    paths.add(src / "rightmemory.toml")
    if any(not p.resolve().is_relative_to(src) for p in paths):
        raise ValueError("Snapshot source escaped the Memory root.")
    before = {str(p.relative_to(src)): digest(p) for p in paths}
    root = out / "root"
    root.mkdir(parents=True)
    for path in paths:
        if path.name == "rightmemory.toml":
            continue
        dest = root / path.relative_to(src)
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?i)(password\s+is\s+`)[^`]+(`)", r"\1[REDACTED]\2", text)
        dest.write_text(text, encoding="utf-8")
    cfg = load_config("retrieve", src)
    if cfg.runtime_mode != "cli-agent" or cfg.agent_cli.provider != "codex":
        raise ValueError("This selector experiment currently expects a Codex CLI-agent configuration.")
    model, effort = cfg.agent_cli.model, cfg.agent_cli.reasoning_effort
    config_text = (
        '[agent_cli]\nprovider = "codex"\n[retrieve.agent_cli]\n'
        f'model = {json.dumps(model)}\nreasoning_effort = {json.dumps(effort)}\n'
        '[sync]\nenabled = false\n[debug]\ntrace = true\n'
    )
    (root / "rightmemory.toml").write_text(config_text, encoding="utf-8")
    (root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q", str(root)],
        ["git", "-C", str(root), "add", "--all"],
        ["git", "-C", str(root), "-c", "user.name=Retrieval experiment", "-c",
         "user.email=experiment@localhost", "commit", "-qm", "Frozen private retrieval corpus"],
    ):
        subprocess.run(command, check=True, capture_output=True)
    if before != {str(p.relative_to(src)): digest(p) for p in paths}:
        raise RuntimeError("The live corpus changed during the copy; discard and take a new snapshot.")
    m = build_graph_manifest(root)
    renderer = RetrieveSelectionRenderer(root, max_output_chars=sys.maxsize)

    def own(block):
        if block.kind == "node":
            return block.line
        return "\n".join([block.line] + [part.text for part in block.logical_text_parts]).strip()

    docs = []
    for item in sorted(m.items.values(), key=lambda x: x.traversal_rank):
        block = m.blocks[item.block_key]
        ancestors, parent = [], block.logical_parent
        while parent is not None:
            parent_block = m.blocks[parent]
            if parent_block.kind != "root":
                ancestors.append(own(parent_block))
            parent = parent_block.logical_parent
        body = own(block)
        text = "\n\n".join([*reversed(ancestors), body])
        delivery = renderer.render(selection_for([item.id])).delivery
        docs.append({
            "id": item.id, "family": item.family, "kind": item.item_kind,
            "text": text, "body": body, "file": str(item.file.relative_to(root)),
            "line": item.line_number,
            "coverage": sorted(set(delivery.local_items) | set(delivery.source_items)),
        })
    for source, filename in AGENT_CORRECTION_SOURCE_PATHS.items():
        path = root / filename
        if not path.exists():
            continue
        for entry in agent_correction_entries(path.read_text(encoding="utf-8")):
            key = f"{source}:{entry.position}"
            docs.append({"id": key, "family": "correction", "kind": "entry",
                         "text": f"Agent Corrections ({source})\n{entry.text}", "body": entry.text,
                         "file": filename, "line": entry.start_line, "coverage": [key]})
    # Structural headings without their own prose are not independent relevance units.
    evaluation_ids = [d["id"] for d in docs if d["kind"] != "heading" or
                      len(d["body"].splitlines()) > 1]
    write_json(out / "corpus.json", {
        "documents": docs, "evaluation_ids": evaluation_ids,
        "source_hashes": before, "snapshot_hashes": {
            str(p.relative_to(root)): digest(p) for p in root.rglob("*.md")
        },
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "selector": {"model": model, "reasoning_effort": effort},
        "repository_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True).strip(),
        "limitations": ["No pending submissions, external views, or changing-session deltas.",
                        "Source-root AGENTS.md is preserved for both selector variants.",
                        "An explicitly labelled password is redacted identically in both variants."],
    })
    print(json.dumps({"snapshot": str(out), "documents": len(docs),
                      "evaluation_units": len(evaluation_ids), "selector": model}))


def terms(text):
    # Identifiers, words, and overlapping Han bigrams; this is a comparison baseline.
    parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", text.lower())
    han = re.findall(r"[\u3400-\u9fff]+", text)
    return parts + [run[i:i+2] for run in han for i in range(len(run)-1)]


def bm25_scores(documents, query):
    tokenized = [Counter(terms(d["text"])) for d in documents]
    lengths = [sum(t.values()) for t in tokenized]
    average = statistics.mean(lengths) or 1
    dfs = Counter(term for counts in tokenized for term in counts)
    q = set(terms(query))
    scores = []
    for counts, length in zip(tokenized, lengths):
        total = 0.0
        for term in q:
            count = counts[term]
            if count:
                inverse = math.log(1 + (len(documents)-dfs[term]+0.5)/(dfs[term]+0.5))
                total += inverse * count * 2.5 / (count + 1.5*(0.25+0.75*length/average))
        scores.append(total)
    return scores


def rank_indices(scores):
    return sorted(range(len(scores)), key=lambda i: (-float(scores[i]), i))


def fused_ranks(dense, lexical, lexical_scores):
    scores = defaultdict(float)
    for rank, index in enumerate(dense, 1):
        scores[index] += 1 / (60 + rank)
    for rank, index in enumerate(lexical, 1):
        if lexical_scores[index] > 0:
            scores[index] += 1 / (60 + rank)
    return sorted(scores, key=lambda i: (-scores[i], i))


def embed(args):
    import numpy as np
    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer

    corpus, cases = read_json(args.corpus), read_json(args.cases)["cases"]
    docs = corpus["documents"]
    torch.set_num_threads(4)
    load_start = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, padding_side="left")
    model = AutoModel.from_pretrained(
        args.model, revision=args.revision, dtype=torch.float16,
        attn_implementation="sdpa").to(args.device).eval()
    load_s = time.perf_counter() - load_start

    def sync():
        if str(args.device).startswith("cuda"):
            torch.cuda.synchronize()

    @torch.inference_mode()
    def encode(texts):
        batch = tokenizer(texts, padding=True, truncation=False, return_tensors="pt").to(args.device)
        if batch["input_ids"].shape[1] > 32768:
            raise ValueError("A document exceeds model context; no silent truncation is allowed.")
        hidden = model(**batch).last_hidden_state[:, -1]
        return torch.nn.functional.normalize(hidden.float(), p=2, dim=1).cpu().numpy()

    sync()
    start = time.perf_counter()
    vectors = np.concatenate([encode([d["text"] for d in docs[i:i+args.batch_size]])
                              for i in range(0, len(docs), args.batch_size)])
    sync()
    build_s = time.perf_counter() - start
    query_prefix = ("Instruct: Given a task or question, retrieve stored context, facts, decisions, "
                    "and user guidance that would help answer it or avoid a mistake.\nQuery: ")
    encode([query_prefix + "Warm up retrieval."])
    rankings = {}
    for case in cases:
        latencies, encode_times, dense_times, lexical_times = [], [], [], []
        for _ in range(args.repeats):
            sync()
            start = time.perf_counter()
            qvector = encode([query_prefix + case["query"]])[0]
            sync()
            encoded = time.perf_counter()
            dense_scores = vectors @ qvector
            dense = rank_indices(dense_scores)
            ranked = time.perf_counter()
            lexical_scores = bm25_scores(docs, case["query"])
            lexical = rank_indices(lexical_scores)
            combined = fused_ranks(dense, lexical, lexical_scores)
            end = time.perf_counter()
            encode_times.append(encoded-start)
            dense_times.append(ranked-encoded)
            lexical_times.append(end-ranked)
            latencies.append(end-start)
        rankings[case["id"]] = {
            "dense": [docs[i]["id"] for i in dense],
            "bm25": [docs[i]["id"] for i in lexical],
            "hybrid": [docs[i]["id"] for i in combined],
            "dense_scores": [float(dense_scores[i]) for i in dense],
            "seconds": {"query_encode": encode_times, "dense_search": dense_times,
                        "lexical_and_fusion": lexical_times, "hybrid_total": latencies},
        }
    write_json(args.out, {
        "corpus_sha256": digest(args.corpus), "cases_sha256": digest(args.cases),
        "model": args.model, "revision": args.revision,
        "query_instruction": query_prefix, "dtype": "float16", "attention": "sdpa",
        "dimensions": int(vectors.shape[1]), "device": torch.cuda.get_device_name() if
        str(args.device).startswith("cuda") else args.device,
        "torch": torch.__version__, "transformers": transformers.__version__,
        "load_seconds": load_s, "index_seconds": build_s, "rankings": rankings,
        "peak_gpu_bytes": torch.cuda.max_memory_allocated() if str(args.device).startswith("cuda") else 0,
    })
    print(json.dumps({"queries": len(cases), "index_seconds": build_s, "load_seconds": load_s}))


def serve(args):
    """Temporary loopback-only GPU endpoint for end-to-end experiment timing."""
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    from http.server import BaseHTTPRequestHandler, HTTPServer

    corpus = read_json(args.corpus)
    docs = corpus["documents"]
    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, padding_side="left")
    model = AutoModel.from_pretrained(
        args.model, revision=args.revision, dtype=torch.float16,
        attn_implementation="sdpa").to(args.device).eval()

    @torch.inference_mode()
    def encode(texts):
        batch = tokenizer(texts, padding=True, truncation=False, return_tensors="pt").to(args.device)
        if batch["input_ids"].shape[1] > 32768:
            raise ValueError("Context limit exceeded")
        vectors = model(**batch).last_hidden_state[:, -1]
        return torch.nn.functional.normalize(vectors.float(), p=2, dim=1).cpu().numpy()

    vectors = np.concatenate([encode([d["text"] for d in docs[i:i+8]])
                              for i in range(0, len(docs), 8)])
    prefix = ("Instruct: Given a task or question, retrieve stored context, facts, decisions, "
              "and user guidance that would help answer it or avoid a mistake.\nQuery: ")
    encode([prefix + "Warm up retrieval."])
    corpus_hash = digest(args.corpus)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 100000:
                self.send_error(400)
                return
            request = json.loads(self.rfile.read(length))
            if request["corpus_sha256"] != corpus_hash:
                self.send_error(409, "Corpus hash mismatch")
                return
            start = time.perf_counter()
            vector = encode([prefix + request["query"]])[0]
            scores = vectors @ vector
            dense = rank_indices(scores)
            if request["method"] == "hybrid":
                lexical_scores = bm25_scores(docs, request["query"])
                dense = fused_ranks(dense, rank_indices(lexical_scores), lexical_scores)
            payload = json.dumps({"ids": [docs[i]["id"] for i in dense],
                                  "server_seconds": time.perf_counter()-start}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    print(json.dumps({"ready": True, "port": args.port, "documents": len(docs)}), flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


def score_ids(delivered, required, optional=()):
    delivered, required, optional = set(delivered), set(required), set(optional)
    found = delivered & required
    extra = delivered - required - optional
    return {"required": len(required), "found": len(found),
            "recall": len(found)/len(required) if required else None,
            "all_required": required <= delivered,
            "returned": len(delivered), "unlabelled": len(extra),
            "label_precision": len(delivered & (required | optional))/len(delivered) if delivered else None,
            "missing_ids": sorted(required-delivered), "unlabelled_ids": sorted(extra),
            "empty": not delivered}


def verified_inputs(args):
    corpus, cases = read_json(args.corpus), read_json(args.cases)
    docs = {d["id"]: d for d in corpus["documents"]}
    if len(docs) != len(corpus["documents"]):
        raise ValueError("Duplicate document ids.")
    ids = [c["id"] for c in cases["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case ids.")
    for case in cases["cases"]:
        if not case["query"].strip():
            raise ValueError("Empty query.")
        unknown = (set(case["required"]) | set(case.get("optional", []))) - docs.keys()
        if unknown:
            raise ValueError(f"Unknown labels for {case['id']}: {unknown}")
    rankings = read_json(args.rankings) if getattr(args, "rankings", None) else None
    if rankings and (rankings["corpus_sha256"] != digest(args.corpus) or
                     rankings["cases_sha256"] != digest(args.cases)):
        raise ValueError("Corpus/labels differ from the files used for ranking.")
    return corpus, cases, docs, rankings


def remote_ranking(url, corpus_hash, query, method):
    from urllib.request import Request, urlopen
    payload = json.dumps({"query": query, "method": method, "corpus_sha256": corpus_hash}).encode()
    with urlopen(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=60) as response:
        return json.loads(response.read())


def direct(args):
    from rightmemory.retrieve_selection import NO_STRONG_MATCH, RetrieveSelectionRenderer
    corpus, cases, docs, _ = verified_inputs(args)
    root = args.root.resolve()
    for relative, expected in corpus["snapshot_hashes"].items():
        if digest(root / relative) != expected:
            raise ValueError(f"Snapshot changed: {relative}")
    renderer = RetrieveSelectionRenderer(root, max_output_chars=sys.maxsize)
    units = set(corpus["evaluation_ids"])
    rows = []
    for repeat in range(args.repeats):
        for case in cases["cases"]:
            start = time.perf_counter()
            result = remote_ranking(args.embedding_url, digest(args.corpus), case["query"], args.method)
            embedded = time.perf_counter()
            rendered = renderer.render(selection_for(result["ids"][:args.top_k]))
            done = time.perf_counter()
            delivered = (set(rendered.delivery.local_items) | set(rendered.delivery.source_items)) & units
            rows.append({"case_id": case["id"], "repeat": repeat, "method": args.method,
                         "category": case["category"], "top_k": args.top_k,
                         "seconds": done-start, "embedding_roundtrip_seconds": embedded-start,
                         "render_seconds": done-embedded, "server_seconds": result["server_seconds"],
                         "delivered": sorted(delivered), "output_chars": len(rendered.text),
                         "no_match": rendered.text == NO_STRONG_MATCH,
                         "score": score_ids(delivered, case["required"], case.get("optional", []))})
    write_json(args.out, {"corpus_sha256": digest(args.corpus), "cases_sha256": digest(args.cases),
                          "rows": rows})
    print(json.dumps({"runs": len(rows), "median_seconds": statistics.median(r["seconds"] for r in rows)}))


def select(args):
    from rightmemory.config import load_config, SyncConfig
    from rightmemory.runtime import RightMemoryRuntime, PreparedRetrieveTurn
    from rightmemory.retrieve_context import build_retrieve_request_text
    from rightmemory.retrieve_selection import NO_STRONG_MATCH, RetrieveSelectionRenderer

    corpus, case_data, docs, rankings = verified_inputs(args)
    root = args.root.resolve()
    for relative, expected in corpus["snapshot_hashes"].items():
        if digest(root / relative) != expected:
            raise ValueError(f"Snapshot changed: {relative}")
    config = replace(load_config("retrieve", root),
                     state_root=args.state.resolve(), sync=SyncConfig(root, enabled=False))
    renderer = RetrieveSelectionRenderer(root, max_output_chars=sys.maxsize)
    evaluation_ids = set(corpus["evaluation_ids"])

    class MeasuredRuntime(RightMemoryRuntime):
        def _prepare_retrieve_turn(self, session_id, message, **kwargs):
            if args.method == "baseline":
                prepared = super()._prepare_retrieve_turn(session_id, message, **kwargs)
            else:
                if args.embedding_url:
                    started = time.perf_counter()
                    retrieved = remote_ranking(args.embedding_url, digest(args.corpus), message, args.method)
                    self.embedding_seconds = time.perf_counter()-started
                    self.server_seconds = retrieved["server_seconds"]
                    ids = retrieved["ids"][:args.top_k]
                else:
                    ids = rankings["rankings"][self.case_id][args.method][:args.top_k]
                candidate = renderer.render(selection_for(ids)).text
                context = "Candidate context for this query:\n\n" + candidate
                prepared = PreparedRetrieveTurn(
                    message=build_retrieve_request_text(context_parts=(context,), query=message),
                    query=message, context_parts=(context,), prefix_context=None,
                    recent_submitted_entries=[], visible_recent_candidates={},
                    memory_commit=None, model_history_json=None)
            self.input_chars = len(prepared.message) + len(prepared.prefix_context or "")
            return prepared

        def _render_retrieve_selection(self, prepared, selection):
            rendered = super()._render_retrieve_selection(prepared, selection)
            self.last_selection = selection.model_dump()
            self.delivered = sorted((set(rendered.delivery.local_items) |
                                     set(rendered.delivery.source_items)) & evaluation_ids)
            return rendered

    cases = case_data["cases"]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
        if wanted != {c["id"] for c in cases}:
            raise ValueError("Requested case ids are absent from the label set.")
    schedule = [(repeat, case) for repeat in range(args.repeats) for case in cases]
    random.Random(args.seed).shuffle(schedule)
    existing = []
    if args.out.exists():
        existing = [json.loads(line) for line in args.out.read_text(encoding="utf-8").splitlines()]
    for row in existing:
        if (row["corpus_sha256"] != digest(args.corpus) or row["cases_sha256"] != digest(args.cases)
                or row["method"] != args.method or row["top_k"] != args.top_k):
            raise ValueError("Existing results use different inputs or selection settings.")
    done = {(x["case_id"], x["repeat"]) for x in existing if "error" not in x}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Separate logical sessions avoid answer/history contamination. The baseline retains
    # the runtime's shared prefix cache; hybrid sends its varying shortlist in one turn.
    runtime = None
    try:
        for repeat, case in schedule:
            if (case["id"], repeat) in done:
                continue
            start = time.perf_counter()
            runtime = MeasuredRuntime(config)
            runtime.case_id = case["id"]
            row = {"case_id": case["id"], "repeat": repeat, "method": args.method,
                   "top_k": args.top_k, "category": case["category"],
                   "live_embedding": bool(args.embedding_url),
                   "corpus_sha256": digest(args.corpus), "cases_sha256": digest(args.cases)}
            try:
                session = f"embedding-{args.method}-{args.top_k}-{case['id']}-{repeat}-{time.time_ns()}"
                output = runtime.run_session_turn(session, case["query"])
                row.update({"delivered": runtime.delivered, "selection": runtime.last_selection,
                            "output_chars": len(output), "input_chars": runtime.input_chars,
                            "no_match": output == NO_STRONG_MATCH,
                            "session_id": session,
                            "embedding_seconds": getattr(runtime, "embedding_seconds", None),
                            "server_embedding_seconds": getattr(runtime, "server_seconds", None),
                            "score": score_ids(runtime.delivered, case["required"], case.get("optional", []))})
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                runtime.cleanup()
                runtime = None
            row["seconds"] = time.perf_counter()-start
            with args.out.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False)+"\n")
            print(json.dumps({k: row[k] for k in ("case_id", "repeat", "seconds", "score", "error") if k in row}), flush=True)
    finally:
        if runtime:
            runtime.cleanup()


def summarize(args):
    corpus, case_data, docs, rankings = verified_inputs(args)
    units = set(corpus["evaluation_ids"])
    report = {"candidate": {}, "selector": {}}
    for method in ("bm25", "dense", "hybrid"):
        for k in args.ks:
            rows = []
            for case in case_data["cases"]:
                ids = rankings["rankings"][case["id"]][method][:k]
                delivered = {x for key in ids for x in docs[key]["coverage"]} & units
                rows.append({"id": case["id"], "category": case["category"],
                             **score_ids(delivered, case["required"], case.get("optional", []))})
            report["candidate"][f"{method}@{k}"] = rows
    for path in args.selector:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if any(row["corpus_sha256"] != digest(args.corpus) or row["cases_sha256"] != digest(args.cases) for row in rows):
            raise ValueError(f"Stale selector results: {path}")
        report["selector"][path.stem] = rows
    write_json(args.out, report)
    for name, rows in report["candidate"].items():
        positive = [row for row in rows if row["required"]]
        print(name, json.dumps({"mean_recall": statistics.mean(r["recall"] for r in positive),
                                 "all_required": sum(r["all_required"] for r in positive),
                                 "cases": len(positive),
                                 "median_returned": statistics.median(r["returned"] for r in rows)}))
    for name, rows in report["selector"].items():
        good = [r for r in rows if "score" in r]
        positive = [r for r in good if r["score"]["required"]]
        print(name, json.dumps({"errors": len(rows)-len(good), "runs": len(good),
                               "median_s": statistics.median(r["seconds"] for r in good) if good else None,
                               "mean_recall": statistics.mean(r["score"]["recall"] for r in positive) if positive else None,
                               "all_required": sum(r["score"]["all_required"] for r in positive)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(run=snapshot)
    p = sub.add_parser("serve")
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B")
    p.add_argument("--revision", required=True)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--port", type=int, default=18762)
    p.set_defaults(run=serve)
    p = sub.add_parser("embed")
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="Qwen/Qwen3-Embedding-0.6B")
    p.add_argument("--revision", required=True)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--repeats", type=int, default=5)
    p.set_defaults(run=embed)
    p = sub.add_parser("direct")
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--embedding-url", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--method", choices=["dense", "hybrid"], default="dense")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--repeats", type=int, default=3)
    p.set_defaults(run=direct)
    for name, function in (("select", select), ("summarize", summarize)):
        p = sub.add_parser(name)
        p.add_argument("--corpus", type=Path, required=True)
        p.add_argument("--cases", type=Path, required=True)
        p.add_argument("--rankings", type=Path, required=name == "summarize")
        p.add_argument("--out", type=Path, required=True)
        p.set_defaults(run=function)
        if name == "select":
            p.add_argument("--root", type=Path, required=True)
            p.add_argument("--state", type=Path, required=True)
            p.add_argument("--method", choices=["baseline", "dense", "hybrid"], required=True)
            p.add_argument("--top-k", type=int, default=10)
            p.add_argument("--repeats", type=int, default=2)
            p.add_argument("--seed", type=int, default=42)
            p.add_argument("--ids")
            p.add_argument("--embedding-url")
        else:
            p.add_argument("--selector", type=Path, nargs="*", default=[])
            p.add_argument("--ks", type=int, nargs="+", default=[1, 3, 5, 10, 20, 40])
    args = parser.parse_args()
    if getattr(args, "repeats", 1) < 1 or getattr(args, "top_k", 1) < 1:
        parser.error("repeats and top-k must be positive")
    if args.command == "select" and args.method != "baseline" and not (args.rankings or args.embedding_url):
        parser.error("candidate selection needs --rankings or --embedding-url")
    args.run(args)


if __name__ == "__main__":
    main()
