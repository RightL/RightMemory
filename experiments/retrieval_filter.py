r"""Compare embedding output with its natural-language filter on the same candidates.

Example (private inputs and outputs belong in an ignored directory)::

    python experiments/retrieval_filter.py --source-root <memory-root> \
        --cases tmp/retrieval-filter/cases.json --url http://127.0.0.1:18766 \
        --out tmp/retrieval-filter/results.json

Cases are a JSON array of objects with nonempty ``id`` and ``query`` strings, or
an object containing that array as ``cases``. Extra case fields are retained as
review context, not scored. This script makes real embedding-service and Codex
SDK calls. It needs the optional Codex SDK dependency and an existing login.

The source must be a Git root. Its config is never loaded or rewritten. A
hash-checked disposable copy includes imported file-view packages, learning,
Memory and Pursuit files; Git, other runtime state, and worktrees are excluded.
The copy uses embedding defaults (40 candidates, 10 results) and Codex
gpt-6-luna with low reasoning. Sync is disabled. Output must be outside the
source, and ignored by Git when inside a repository. Reports contain private
text. The adjacent state directory retains the snapshot hash manifest, derived
vector cache, and timing traces after snapshot deletion. The filter uses fresh
ephemeral SDK threads and writes no provider-session ownership records.

This measures the filter stage, not the complete CLI/MCP request path. Review
quality manually from full before/after text. Filter output has no structured
source-ID contract, so only baseline candidate IDs are recorded. Word counts
mean whitespace-separated runs, not model tokens or linguistic word counts.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rightmemory.codex_sdk import CodexSdkRunner
from rightmemory.config import load_config
from rightmemory.embedding_retrieval import EmbeddingRetriever


EXCLUDED_NAMES = {".git", ".runtime", ".worktree", ".worktrees", "worktree", "worktrees"}
IMPORTS = Path(".runtime/shared_views/imports")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Read-only status must not refresh the source index on disk.
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        check=check, capture_output=True,
    )


def _git_state(root: Path) -> dict:
    top = _git(root, "rev-parse", "--show-toplevel")
    if Path(os.fsdecode(top.stdout).strip()).resolve() != root:
        raise ValueError("--source-root must be a Git repository root")
    head = _git(root, "rev-parse", "--verify", "HEAD", check=False)
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    return {
        "head": head.stdout.decode("ascii").strip() if head.returncode == 0 else None,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "clean": not status,
    }


def _regular(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError(f"snapshot requires regular files and directories: {path}")


def _content_manifest(root: Path) -> dict[str, str]:
    """Hash the exact copy set without reading excluded runtime or Git files."""
    files: dict[str, str] = {}

    def on_error(error: OSError) -> None:
        raise error

    def walk(start: Path) -> None:
        for directory, subdirs, names in os.walk(start, followlinks=False, onerror=on_error):
            directory = Path(directory)
            _regular(directory, directory=True)
            subdirs[:] = sorted(name for name in subdirs if name not in EXCLUDED_NAMES)
            for name in subdirs:
                _regular(directory / name, directory=True)
            for name in sorted(names):
                if name in EXCLUDED_NAMES:
                    continue
                path = directory / name
                relative = path.relative_to(root)
                if relative == Path("rightmemory.toml"):
                    continue
                _regular(path, directory=False)
                files[relative.as_posix()] = _sha256(path)

    walk(root)
    imports = root / IMPORTS
    if imports.exists() or imports.is_symlink():
        for relative in (Path(".runtime"), Path(".runtime/shared_views"), IMPORTS):
            _regular(root / relative, directory=True)
        walk(imports)
    return dict(sorted(files.items()))


def snapshot(source: Path, destination: Path) -> dict:
    """Refuse a changing source before any model call; never lock/write source."""
    before_git = _git_state(source)
    before = _content_manifest(source)
    destination.mkdir(parents=True)
    for relative in before:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    after = _content_manifest(source)
    after_git = _git_state(source)
    if before_git != after_git or before != after or _content_manifest(destination) != before:
        raise RuntimeError("source changed while creating the snapshot; retry with a stable source")
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode("utf-8")).hexdigest()
    return {"git": before_git, "files": before, "content_sha256": digest}


def _cases(path: Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list) or not cases:
        raise ValueError("--cases must contain a nonempty JSON array or an object with a cases array")
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or any(
            not isinstance(case.get(key), str) or not case[key].strip() for key in ("id", "query")
        ):
            raise ValueError("each case needs nonempty id and query strings")
        if case["id"] in ids:
            raise ValueError("case ids must be unique")
        ids.add(case["id"])
    return cases, hashlib.sha256(raw).hexdigest()


def _private_output(path: Path, source: Path) -> None:
    if path.is_relative_to(source):
        raise ValueError("benchmark output and state must be outside --source-root")
    ancestor = path.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    git = _git(ancestor, "rev-parse", "--show-toplevel", check=False)
    if git.returncode == 0:
        root = Path(os.fsdecode(git.stdout).strip()).resolve()
        relative = path.relative_to(root).as_posix()
        ignored = _git(root, "check-ignore", "-q", "--", relative, check=False)
        if ignored.returncode != 0:
            raise ValueError("private benchmark outputs inside Git must be ignored (for example, use tmp/)")


def _write_report(path: Path, report: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _text_metrics(text: str) -> dict:
    return {"text": text, "chars": len(text), "words": len(text.split()),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _timing_summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"median_ms": statistics.median(ordered) * 1000,
            "p95_ms": ordered[max(0, math.ceil(len(ordered) * .95) - 1)] * 1000,
            "min_ms": ordered[0] * 1000, "max_ms": ordered[-1] * 1000}


def benchmark(root: Path, state_root: Path, cases: list[dict], repeats: int, report: dict, out: Path) -> None:
    from rightmemory.retrieval_filter import RetrievalFilter

    config = replace(load_config("retrieve", root), state_root=state_root)
    if config.embedding is None or config.retrieve_filter is None:
        raise RuntimeError("benchmark config did not enable embedding retrieval and filtering")
    retriever = EmbeddingRetriever(
        root, state_root, config.embedding, max_output_chars=config.retrieve_max_output_chars,
    )
    report["models"] = {
        "embedding_service": asdict(retriever.client.info()), "filter": asdict(config.retrieve_filter),
    }
    report["settings"] = {"candidate_count": config.embedding.candidate_count,
                          "result_count": config.embedding.result_count,
                          "max_output_chars": config.retrieve_max_output_chars}
    # A separately reported first retrieval populates the passage-vector cache.
    # Every measured pair below has precisely one fresh embedding retrieval.
    start = time.perf_counter()
    warmed = retriever.retrieve(cases[0]["query"])
    report["warmup"] = {"seconds": time.perf_counter() - start,
                        "case_id": cases[0]["id"], "fingerprint": warmed.fingerprint}
    _write_report(out, report)
    tasks = [(repeat, case) for repeat in range(repeats) for case in cases]
    random.Random(73).shuffle(tasks)
    runner = CodexSdkRunner()
    try:
        with (state_root / "filter-trace.jsonl").open("a", encoding="utf-8") as trace_file:
            for repeat, case in tasks:
                row: dict[str, Any] = {"id": case["id"], "repeat": repeat, "case": case,
                                       "status": "retrieving", "filter_trace": []}
                report["results"].append(row)
                _write_report(out, report)
                start = time.perf_counter()
                raw = retriever.retrieve(case["query"])
                row["baseline_seconds"] = time.perf_counter() - start
                if raw.fingerprint != warmed.fingerprint:
                    raise RuntimeError("disposable snapshot changed between retrievals")
                row["fingerprint"] = raw.fingerprint
                row["before"] = _text_metrics(raw.text)
                row["before"]["source_ids"] = [entry.key for entry in raw.entries]
                row["before"]["sources"] = [entry.source for entry in raw.entries]
                row["status"] = "filtering"
                _write_report(out, report)
                filter_start = time.perf_counter()

                def trace(event: str, **fields: Any) -> None:
                    payload = {"event": event, "fields": fields,
                               "elapsed_ms": (time.perf_counter() - filter_start) * 1000}
                    row["filter_trace"].append(payload)
                    trace_file.write(json.dumps({"id": case["id"], "repeat": repeat, **payload},
                                                ensure_ascii=False, allow_nan=False) + "\n")
                    trace_file.flush()
                    os.fsync(trace_file.fileno())

                filter_stage = RetrievalFilter(config.retrieve_filter, runner, trace_event=trace)
                filtered = filter_stage.run(case["query"], raw.text)
                row["filter_seconds"] = time.perf_counter() - filter_start
                row["baseline_plus_filter_seconds"] = row["baseline_seconds"] + row["filter_seconds"]
                row["after"] = _text_metrics(filtered)
                row["status"] = "complete"
                _write_report(out, report)
    finally:
        runner.close()
    results = report["results"]
    report["summary"] = {
        "cases": len(cases), "repeats": repeats, "samples": len(results),
        **{field: _timing_summary([row[field] for row in results]) for field in
           ("baseline_seconds", "filter_seconds", "baseline_plus_filter_seconds")},
        "mean_before_chars": statistics.mean(row["before"]["chars"] for row in results),
        "mean_after_chars": statistics.mean(row["after"]["chars"] for row in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    source, out = args.source_root.expanduser().resolve(strict=True), args.out.expanduser().resolve()
    if not source.is_dir():
        parser.error("--source-root must be a directory")
    if out.exists():
        parser.error("--out already exists; choose a new report name")
    state_root = out.parent / f"{out.stem}.state-{uuid4().hex}"
    try:
        _private_output(out, source)
        _private_output(state_root, source)
        cases, cases_sha256 = _cases(args.cases)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    state_root.mkdir(parents=True)
    report = {
        "version": 1, "status": "running", "started_at": datetime.now(UTC).isoformat(),
        "source_root": str(source), "state_root": str(state_root),
        "cases_sha256": cases_sha256, "results": [],
        "limitations": [
            "Stage comparison on identical candidates; not complete runtime, CLI, or MCP latency.",
            "Quality requires manual review; any supplied case labels are not automatically scored.",
            "Only baseline source IDs are authoritative; natural-language filtering does not return selected IDs.",
            "Warmup is one full retrieval of the first case, excluded from measured pairs; no filter warmup.",
            "Word counts split on whitespace and are not model token counts or linguistic word counts.",
            "Default candidate/result limits and max output apply; source configuration is excluded.",
            "No sync, imported-view refresh, or remote question-view queries; cached file-view imports are included.",
            "Filter SDK threads are ephemeral; the adjacent state_root retains derived cache and timing evidence.",
        ],
    }
    _write_report(out, report)
    try:
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=state_root) as temporary:
            root = Path(temporary) / "memory"
            frozen = snapshot(source, root)
            _write_report(state_root / "snapshot-manifest.json", frozen)
            report["snapshot"] = {key: value for key, value in frozen.items() if key != "files"}
            report["snapshot"]["file_count"] = len(frozen["files"])
            (root / "rightmemory.toml").write_text(
                '[retrieve]\nbackend = "embedding"\n[retrieve.embedding]\nurl = '
                + json.dumps(args.url) + '\n[retrieve.filter]\n'
                'model = "gpt-6-luna"\nreasoning_effort = "low"\n', encoding="utf-8",
            )
            benchmark(root, state_root, cases, args.repeats, report, out)
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        _write_report(out, report)
    print(json.dumps({"out": str(out), "state_root": str(state_root), **report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
