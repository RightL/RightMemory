"""Measure the actual embedding CLI/MCP path on a disposable frozen Memory copy."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rightmemory.config import load_config
from rightmemory.embedding_retrieval import EmbeddingServiceClient
from rightmemory.entrypoint import main as cli_main
from rightmemory.mcp import create_mcp_server
from rightmemory.retrieval_index import build_retrieval_corpus
from rightmemory.runtime import RightMemoryRuntime
from rightmemory.session import MessageSessionStore


def delivered(root: Path, session_id: str) -> list[str]:
    path = MessageSessionStore(root, "retrieve-embedding").paths(session_id).history
    return list(json.loads(path.read_bytes())["entries"])


async def benchmark(root: Path, cases: list[dict], repeats: int) -> dict:
    from mcp import Client

    config = load_config("retrieve", root)
    info = EmbeddingServiceClient(config.embedding).info()
    runtime = RightMemoryRuntime(config)
    start = time.perf_counter()
    try:
        runtime.run_session_turn("index-warmup", cases[0]["query"])
    finally:
        runtime.cleanup()
    startup = time.perf_counter() - start
    tasks = [(repeat, case) for repeat in range(repeats) for case in cases]
    random.Random(73).shuffle(tasks)
    results: list[dict] = []
    server = create_mcp_server(root)
    async with Client(server, raise_exceptions=True) as client:
        for repeat, case in tasks:
            session_id = f"benchmark-{case['id']}"
            start = time.perf_counter()
            response = await client.call_tool("rightmemory_retrieve", {"session_id": session_id, "need": case["query"]})
            elapsed = time.perf_counter() - start
            if response.is_error:
                raise RuntimeError(f"MCP retrieval failed for {case['id']}")
            selected = delivered(root, session_id)
            results.append({"id": case["id"], "repeat": repeat, "selected": selected,
                            "required": case["required"], "complete": set(case["required"]) <= set(selected),
                            "seconds": elapsed})
    # Exercise the public CLI dispatcher too, using the same service and source root.
    import os
    previous = os.environ.get("RIGHTMEMORY_ROOT")
    try:
        os.environ["RIGHTMEMORY_ROOT"] = str(root)
        start = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            cli_status = cli_main(["retrieve", "--session", "cli-benchmark", cases[0]["query"]])
        cli_seconds = time.perf_counter() - start
    finally:
        if previous is None:
            os.environ.pop("RIGHTMEMORY_ROOT", None)
        else:
            os.environ["RIGHTMEMORY_ROOT"] = previous
    if cli_status != 0 or delivered(root, "cli-benchmark") != next(
        row["selected"] for row in results if row["id"] == cases[0]["id"]
    ):
        raise RuntimeError("CLI and MCP did not return the same ranked source IDs")
    positive = [row for row in results if row["required"]]
    timings = sorted(row["seconds"] for row in results)
    summary = {
        "cases": len(cases), "repeats": repeats, "samples": len(results),
        "positive_measurements": len(positive), "complete_positive_measurements": sum(row["complete"] for row in positive),
        "median_ms": statistics.median(timings) * 1000,
        "p95_ms": timings[min(len(timings) - 1, int(len(timings) * .95))] * 1000,
        "max_returned": max(len(row["selected"]) for row in results),
        "cli_seconds": cli_seconds, "cli_matches_mcp": True,
        "cold_index_and_first_request_seconds": startup,
        "source_entries": len(build_retrieval_corpus(root).entries),
    }
    return {"summary": summary, "models": vars(info), "results": results,
            "limitations": ["Agent-authored reference labels, not independent answer-quality judgments.",
                            "Windows runtime plus SSH transport and GPU inference; disposable sync-disabled root.",
                            "Frozen benchmark omits live pending submissions and imported views; those have focused tests."]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-benchmark-", dir=args.out.parent) as directory:
        root = Path(directory).resolve() / "memory"
        shutil.copytree(args.source_root, root, ignore=shutil.ignore_patterns(".git", ".runtime", "rightmemory.toml"))
        imports = args.source_root / ".runtime/shared_views/imports"
        if imports.exists():
            shutil.copytree(imports, root / ".runtime/shared_views/imports")
        (root / "rightmemory.toml").write_text(
            '[retrieve]\nbackend="embedding"\n[retrieve.embedding]\nurl=' + json.dumps(args.url) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "*.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=RightMemory benchmark", "-c",
                        "user.email=benchmark@localhost", "commit", "-qm", "Frozen validation input"], check=True)
        report = asyncio.run(benchmark(root, cases, args.repeats))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
