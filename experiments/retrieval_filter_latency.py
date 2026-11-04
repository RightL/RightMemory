"""Profile the production filter using unchanged inputs from a saved comparison.

First inspect one fresh-client baseline::

    python experiments/retrieval_filter_latency.py --from-report tmp/results.json \
        --out tmp/latency-baseline.json --case-ids CASE_ID --baseline-only

Then compare fresh conversations with one reused ephemeral conversation::

    python experiments/retrieval_filter_latency.py --from-report tmp/results.json \
        --out tmp/latency-pairs.json --baseline-calls 0 --repeats 2

All modes use the canonical production prompt, full saved candidate text,
gpt-6-luna, and low reasoning. reused-chat keeps one ephemeral conversation and
its SDK/App Server connection, so earlier requests and responses remain in context.
Other modes create a new ephemeral conversation for every request.
shared-client keeps only its SDK/App Server connection open between requests.
On Windows that retained process currently prevents the production temporary
working directory from being removed, so shared-client is not a usable comparison.
http-client and websocket-client use fresh connections and the same custom
OpenAI provider alias, differing only in whether WebSockets are enabled.
This is a sequential, private-output experiment, not a production implementation.
It does not read the memory root, rerun retrieval, or change stored memories.

Event timestamps measure when this Python consumer receives SDK notifications,
not server generation times or network packet arrival. Notifications may have
queued before the turn-start acknowledgement. The existing SDK collector builds
the final response unchanged; raw deltas and reasoning are never logged.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
from importlib.metadata import version
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai_codex import Codex, TransportClosedError
from openai_codex._run import _collect_turn_result

from experiments.retrieval_filter import _private_output, _text_metrics, _write_report
from rightmemory.codex_sdk import CodexSdkRunner, _load_codex_sdk, _unsubscribe_thread
from rightmemory.config import AgentCliConfig
from rightmemory.retrieval_filter import RetrievalFilter


MODES = ("fresh-client", "shared-client", "http-client", "websocket-client", "reused-chat")
TRANSPORT_PROVIDER = "rightmemory_latency_test"
REASONING_DELTAS = {"item/reasoning/textDelta", "item/reasoning/summaryTextDelta"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cases(path: Path, selected: list[str] | None, source_repeat: int) -> tuple[dict, list[dict], str]:
    raw = path.read_bytes()
    report = json.loads(raw)
    if report.get("status") != "complete":
        raise ValueError("the input comparison report must be complete")
    model = report.get("models", {}).get("filter", {})
    if model != {"provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "low"}:
        raise ValueError("input report must use the same Codex gpt-6-luna/low filter")
    cases = {}
    for row in report.get("results", []):
        if row.get("repeat") != source_repeat or row.get("status") != "complete":
            continue
        identifier, query, text = row.get("id"), row.get("case", {}).get("query"), row.get("before", {}).get("text")
        if not all(isinstance(value, str) and value.strip() for value in (identifier, query, text)):
            raise ValueError("a saved result is missing its id, query, or full before.text")
        if identifier in cases:
            raise ValueError("duplicate case id in the selected source repeat")
        if row["before"].get("sha256") != _sha(text):
            raise ValueError(f"saved input hash mismatch for {identifier}")
        cases[identifier] = {"id": identifier, "query": query, "material": text,
                             "source_repeat": source_repeat}
    order = selected if selected is not None else list(cases)
    if not order or len(set(order)) != len(order) or any(key not in cases for key in order):
        raise ValueError("--case-ids must select distinct saved cases from --source-repeat")
    return report, [cases[key] for key in order], hashlib.sha256(raw).hexdigest()


class EventObserver:
    def __init__(self, row: dict, journal: Any, save: Callable[[], None], clock=time.perf_counter):
        self.row, self.journal, self.save, self.clock = row, journal, save, clock
        self.started = clock()
        self.turn_started: float | None = None
        self.row["event_receipts"] = {}
        self.row["observer_write_ms"] = 0.0

    def milestone(self, name: str) -> None:
        self.row[name] = (self.clock() - self.started) * 1000
        self.save()

    def observe(self, event: Any) -> None:
        received = self.clock()
        elapsed = (received - self.started) * 1000
        method = event.method
        record = self.row["event_receipts"].setdefault(
            method, {"count": 0, "first_after_filter_start_ms": elapsed,
                     "last_after_filter_start_ms": elapsed},
        )
        record["count"] += 1
        record["last_after_filter_start_ms"] = elapsed
        payload = {"sample": self.row["sample"], "method": method,
                   "after_filter_start_ms": elapsed,
                   "after_turn_start_ms": (received - self.turn_started) * 1000
                   if self.turn_started is not None else None}
        delta = getattr(event.payload, "delta", None)
        if isinstance(delta, str):
            payload["delta_chars"] = len(delta)
        diagnostic = method in {"error", "warning"}
        if diagnostic:
            details = event.payload.model_dump(mode="json", by_alias=True)
            if method == "error":
                error = details.get("error") or {}
                payload["diagnostic"] = {
                    key: error.get(key) for key in ("message", "codexErrorInfo", "additionalDetails")
                }
                payload["diagnostic"]["willRetry"] = details.get("willRetry")
            else:
                payload["diagnostic"] = {"message": details.get("message")}
            self.row.setdefault("diagnostics", []).append(dict(payload))
        milestone = None
        if method == "item/agentMessage/delta":
            milestone = "first_assistant_delta_received_ms"
        elif method in REASONING_DELTAS:
            milestone = "first_reasoning_delta_received_ms"
        elif method == "turn/completed":
            milestone = "turn_completed_received_ms"
        first = milestone is not None and milestone not in self.row
        if first:
            self.row[milestone] = elapsed
        write_started = self.clock()
        self.journal.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        self.journal.flush()
        if first or diagnostic:
            self.save()
        self.row["observer_write_ms"] += (self.clock() - write_started) * 1000


class ProfiledThread:
    def __init__(self, thread: Any, observer: EventObserver, cwd: str | None = None):
        self._thread, self._observer = thread, observer
        self._cwd = cwd
        self.id = thread.id

    def run(self, prompt: str, **kwargs: Any) -> Any:
        observer = self._observer
        if self._cwd is not None:
            kwargs["cwd"] = self._cwd
        observer.turn_started = observer.clock()
        observer.row["turn_start_called_ms"] = (observer.turn_started - observer.started) * 1000
        turn = self._thread.turn(prompt, **kwargs)
        observer.row["turn_id"] = turn.id
        observer.milestone("turn_start_ack_received_ms")
        stream = turn.stream()

        def observed_stream():
            for event in stream:
                observer.observe(event)
                yield event

        try:
            result = _collect_turn_result(observed_stream(), turn_id=turn.id)
            observer.row["server_duration_ms"] = result.duration_ms
            observer.row["server_started_at"] = result.started_at
            observer.row["server_completed_at"] = result.completed_at
            if result.usage is not None:
                usage = result.usage.model_dump(mode="json", by_alias=True)
                observer.row["usage"] = usage
                observer.row["cached_input_tokens"] = usage["last"]["cachedInputTokens"]
                observer.row["provider_total_tokens"] = usage["total"]["totalTokens"]
            return result
        finally:
            stream.close()


class ProfiledClient:
    def __init__(self, client: Any, observer: EventObserver, shared: bool,
                 reused_chat: ClientFactory | None = None):
        self._delegate, self._observer, self._shared = client, observer, shared
        self._reused_chat = reused_chat

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def thread_start(self, **kwargs: Any) -> ProfiledThread:
        if kwargs.get("ephemeral") is not True:
            raise RuntimeError("latency experiment requires ephemeral threads")
        if self._reused_chat is not None:
            thread = self._reused_chat.chat_thread(self._delegate, kwargs)
            cwd = self._reused_chat.chat_directory.name
        else:
            thread, cwd = self._delegate.thread_start(**kwargs), None
        self._observer.row["provider_session_id"] = thread.id
        self._observer.milestone("thread_start_ack_received_ms")
        return ProfiledThread(thread, self._observer, cwd)

    def thread_unsubscribe(self, thread_id: str) -> str:
        if self._reused_chat is not None:
            if thread_id != self._reused_chat.reused_thread.id:
                raise RuntimeError("unexpected thread release in reused-chat mode")
            # Acknowledge the runner's lease release; retain the real subscription
            # until the experiment ends. Each turn's event stream closes normally.
            self._observer.row["thread_unsubscribe_deferred"] = True
        else:
            _unsubscribe_thread(self._delegate, thread_id, _load_codex_sdk())
        return "unsubscribed"

    def close(self) -> None:
        if not self._shared:
            self._delegate.close()


def transport_overrides(mode: str, cli_version: str) -> tuple[str, ...]:
    if mode not in {"http-client", "websocket-client"}:
        return ()
    prefix = f"model_providers.{TRANSPORT_PROVIDER}"
    return (
        f'model_provider="{TRANSPORT_PROVIDER}"',
        f'{prefix}.name="OpenAI"',
        f'{prefix}.wire_api="responses"',
        f"{prefix}.requires_openai_auth=true",
        f"{prefix}.supports_standalone_web_search=true",
        f"{prefix}.http_headers.version={json.dumps(cli_version)}",
        f"{prefix}.supports_websockets={'true' if mode == 'websocket-client' else 'false'}",
    )


class ClientFactory:
    def __init__(self, make_client: Callable[..., Any] = Codex, config_overrides: tuple[str, ...] = (),
                 transport_cli_version: str = "0.166.0"):
        self.make_client = make_client
        self.config_overrides = config_overrides
        self.transport_cli_version = transport_cli_version
        self.shared_client: Any | None = None
        self.chat_client: Any | None = None
        self.reused_thread: Any | None = None
        self.chat_directory: tempfile.TemporaryDirectory | None = None
        self.chat_options: dict | None = None
        self.chat_lifecycle: dict = {"turns": 0}
        self.global_warnings: list[dict] = []
        self.warning_listener_errors: list[str] = []
        self.warning_readers: list[threading.Thread] = []
        self.observer: EventObserver | None = None
        self.mode = "fresh-client"

    def __call__(self, config: Any) -> ProfiledClient:
        assert self.observer is not None
        shared = self.mode == "shared-client"
        reuse_chat = self.mode == "reused-chat"
        retained_client = self.chat_client if reuse_chat else self.shared_client if shared else None
        reused = retained_client is not None
        self.observer.row["connection_reused"] = reused
        overrides = (*self.config_overrides, *transport_overrides(self.mode, self.transport_cli_version))
        self.observer.row["client_config_overrides"] = list(overrides)
        config = replace(config, config_overrides=(*config.config_overrides, *overrides))
        client = retained_client if reused else self.make_client(config)
        if not reused:
            self.watch_warnings(client)
        if shared:
            self.shared_client = client
        if reuse_chat:
            self.chat_client = client
            if self.chat_directory is None:
                self.chat_directory = tempfile.TemporaryDirectory(prefix="rightmemory-reused-chat-")
                self.chat_lifecycle["neutral_cwd"] = self.chat_directory.name
        self.observer.milestone("client_ready_received_ms")
        return ProfiledClient(client, self.observer, shared or reuse_chat, self if reuse_chat else None)

    def watch_warnings(self, client: Any) -> None:
        next_notification = getattr(getattr(client, "_client", None), "next_notification", None)
        if not callable(next_notification):
            return

        def consume() -> None:
            try:
                while True:
                    event = next_notification()
                    if event.method == "warning":
                        self.global_warnings.append({
                            "method": event.method,
                            "message": event.payload.message,
                            "thread_id": event.payload.thread_id,
                            "received_perf_counter": time.perf_counter(),
                        })
            except TransportClosedError:
                pass
            except BaseException as exc:
                self.warning_listener_errors.append(type(exc).__name__)

        reader = threading.Thread(target=consume, name="latency-global-warnings", daemon=True)
        self.warning_readers.append(reader)
        reader.start()

    def join_warning_readers(self) -> None:
        for reader in self.warning_readers:
            reader.join(timeout=2)
            if reader.is_alive():
                self.warning_listener_errors.append("WarningReaderStillRunning")

    def chat_thread(self, client: Any, options: dict) -> Any:
        assert self.chat_directory is not None and self.observer is not None
        options = {**options, "cwd": self.chat_directory.name}
        if self.reused_thread is None:
            self.reused_thread = client.thread_start(**options)
            self.chat_options = options
            self.chat_lifecycle["provider_session_id"] = self.reused_thread.id
        elif options != self.chat_options:
            raise RuntimeError("reused-chat thread options changed during the experiment")
        self.observer.row["thread_reused"] = self.chat_lifecycle["turns"] > 0
        self.chat_lifecycle["turns"] += 1
        self.observer.row["conversation_turn"] = self.chat_lifecycle["turns"]
        return self.reused_thread

    def close(self) -> float:
        started = time.perf_counter()
        client, self.shared_client = self.shared_client, None
        try:
            if client is not None:
                client.close()
        finally:
            client, self.chat_client = self.chat_client, None
            if client is not None:
                try:
                    if self.reused_thread is not None:
                        _unsubscribe_thread(client, self.reused_thread.id, _load_codex_sdk())
                        self.chat_lifecycle["unsubscribed"] = True
                finally:
                    try:
                        client.close()
                        self.chat_lifecycle["client_closed"] = True
                    finally:
                        if self.chat_directory is not None:
                            self.chat_directory.cleanup()
                            self.chat_lifecycle["directory_removed"] = True
                            self.chat_directory = None
        return (time.perf_counter() - started) * 1000


def tasks(cases: list[dict], modes: list[str], repeats: int, baseline_calls: int, baseline_only: bool):
    for index in range(baseline_calls):
        yield "baseline", index, cases[index % len(cases)], "fresh-client"
    if not baseline_only:
        for repeat in range(repeats):
            for index, case in enumerate(cases):
                order = modes if (repeat + index) % 2 == 0 else list(reversed(modes))
                for mode in order:
                    yield "matched", repeat, case, mode


def run(report: dict, cases: list[dict], args: Any, out: Path) -> None:
    factory = ClientFactory(config_overrides=tuple(args.client_config_override),
                            transport_cli_version=args.transport_cli_version)
    runner = CodexSdkRunner(codex_factory=factory)
    config = AgentCliConfig("codex", "gpt-6-luna", "low")
    save = lambda: _write_report(out, report)
    try:
        with out.with_suffix(out.suffix + ".events.jsonl").open("x", encoding="utf-8") as journal:
            for phase, repeat, case, mode in tasks(
                cases, args.modes, args.repeats, args.baseline_calls, args.baseline_only,
            ):
                row = {"sample": len(report["results"]), "phase": phase, "repeat": repeat,
                       "id": case["id"], "mode": mode, "status": "running",
                       "input_sha256": _sha(case["material"]), "query_sha256": _sha(case["query"]),
                       "started_at": datetime.now(UTC).isoformat(), "filter_trace": []}
                report["results"].append(row)
                factory.mode, factory.observer = mode, EventObserver(row, journal, save)
                save()

                def trace(event: str, **fields: Any) -> None:
                    row["filter_trace"].append({"event": event, "fields": fields})
                    save()

                started = time.perf_counter()
                try:
                    filtered = RetrievalFilter(config, runner, trace_event=trace).run(case["query"], case["material"])
                    row["after"] = _text_metrics(filtered)
                    row["status"] = "complete"
                except BaseException as exc:
                    row["status"] = "failed"
                    row["error"] = {"type": type(exc).__name__, "message": str(exc)}
                    raise
                finally:
                    row["filter_wall_ms"] = (time.perf_counter() - started) * 1000
                    row["finished_at"] = datetime.now(UTC).isoformat()
                    save()
                print(json.dumps({key: row[key] for key in ("sample", "phase", "id", "mode", "status", "filter_wall_ms")}), flush=True)
    finally:
        try:
            runner.close()
        finally:
            try:
                report["shared_client_final_close_ms"] = factory.close()
            finally:
                factory.join_warning_readers()
                report["reused_chat_lifecycle"] = factory.chat_lifecycle
                report["global_warnings"] = factory.global_warnings
                report["warning_listener_errors"] = factory.warning_listener_errors
                save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--source-repeat", type=int, default=0)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["fresh-client", "reused-chat"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--baseline-calls", type=int, default=1)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--client-config-override", action="append", default=[], metavar="KEY=VALUE",
                        help="append a nonsecret transport-test Codex override for this process only; repeatable")
    parser.add_argument("--transport-cli-version", default="0.166.0",
                        help="version header for the custom HTTP/WebSocket provider modes")
    args = parser.parse_args()
    if os.name == "nt" and "shared-client" in args.modes:
        parser.error("shared-client retains the per-request temporary directory on Windows; use reused-chat")
    for override in args.client_config_override:
        key, separator, value = override.partition("=")
        if not separator or not key.strip() or not value.strip():
            parser.error("--client-config-override requires KEY=VALUE")
        if (re.search(r"api[_-]?key|token|password|secret|headers", key, re.IGNORECASE)
                and not key.strip().endswith(".http_headers.version")):
            parser.error("credential-bearing settings are not allowed in this transport experiment")
    if (args.repeats < 1 or args.baseline_calls < 0 or args.source_repeat < 0
            or args.baseline_only and args.baseline_calls == 0
            or len(args.modes) != len(set(args.modes))):
        parser.error("positive repeats, nonnegative source-repeat/baseline-calls, and distinct modes are required")
    out = args.out.expanduser().resolve()
    source = args.from_report.expanduser().resolve(strict=True)
    if out.exists() or out.with_suffix(out.suffix + ".events.jsonl").exists():
        parser.error("choose a new --out report name")
    try:
        original, cases, digest = load_cases(source, args.case_ids, args.source_repeat)
        _private_output(out, Path(original["source_root"]).resolve())
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {"version": 1, "status": "running", "started_at": datetime.now(UTC).isoformat(),
              "source_report": str(source), "source_report_sha256": digest,
              "sdk_version": version("openai-codex"), "models": original["models"],
              "settings": {"modes": args.modes, "repeats": args.repeats,
                           "baseline_calls": args.baseline_calls, "baseline_only": args.baseline_only,
                           "client_config_overrides": args.client_config_override,
                           "transport_cli_version": args.transport_cli_version},
              "cases": [{"id": case["id"], "query": case["query"], "source_repeat": case["source_repeat"],
                         "input_chars": len(case["material"]), "input_sha256": _sha(case["material"])} for case in cases],
              "results": [], "limitations": [
                  "Full saved query and candidate text are replayed unchanged; embedding retrieval is not measured.",
                  "All received_ms fields are local Python observation times relative to observer initialization immediately before filter.run.",
                  "Events may queue before the turn-start acknowledgement; first delta receipt is not exact server generation time.",
                  "The first shared-client call pays client startup; later calls reuse that connection but create fresh ephemeral conversations.",
                  "Shared-client is currently unusable on Windows: the retained App Server holds the filter temporary working directory open during cleanup.",
                  "Reused-chat intentionally accumulates earlier full requests and responses under the unchanged production prompt; this can alter relevance and output correctness.",
                  "Reused-chat defers thread unsubscribe and client close until experiment completion, then removes its stable neutral working directory; each turn stream still closes normally.",
                  "Shared client final shutdown is recorded separately; comparison should include its amortized cost.",
                  "Pairs alternate mode order; request caching, output length variation, and server load can still confound small samples.",
                  "Event journal stores method, timing, delta length, and error/warning diagnostics; raw assistant/reasoning deltas are excluded. Output text and query remain private report data.",
              ]}
    _write_report(out, report)
    try:
        run(report, cases, args, out)
        report["status"] = "complete"
    except BaseException as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        _write_report(out, report)


if __name__ == "__main__":
    main()
