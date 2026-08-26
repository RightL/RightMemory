from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Callable

from .codex_app_server import CodexAppServerClient

_CODEX_SANDBOXES = {"read-only": "read_only", "workspace-write": "workspace_write"}
_CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
_OPPORTUNISTIC_CLEANUP_INTERVAL_SECONDS = 5 * 60
_CODEX_SDK_INSTALL_ERROR = (
    "Codex SDK support is not installed. Reinstall RightMemory with `--mode cli-agent` "
    "or install `rightmemory[codex-sdk]`."
)


@dataclass(frozen=True, slots=True)
class _CodexSdkBindings:
    approval_mode: Any
    codex: Any
    config: Any
    sandbox: Any
    transport_closed_error: type[Exception]
    invalid_request_error: type[Exception]
    reasoning_effort: Any
    thread_unsubscribe_response: Any


@dataclass(frozen=True, slots=True)
class CodexSdkTiming:
    client_start_ms: float = 0.0
    thread_open_ms: float = 0.0
    turn_ms: float = 0.0
    thread_release_ms: float = 0.0
    thread_release_error_type: str | None = None
    server_duration_ms: int | None = None
    total_ms: float = 0.0
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CodexSdkRunResult:
    provider_session_id: str
    text: str
    timing: CodexSdkTiming


@dataclass(slots=True)
class _CodexClientLease:
    codex: Any
    active_calls: int = 0
    retired: bool = False
    closing: bool = False


class CodexSdkRunner:
    """Runs each role turn in a short-lived Codex SDK/App Server connection."""

    def __init__(
        self,
        *,
        codex_factory: Callable[[Any], Any] | None = None,
        archive_client_factory: Callable[[Path], Any] = CodexAppServerClient,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._codex_factory = codex_factory
        self._archive_client_factory = archive_client_factory
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._client_leases: dict[int, _CodexClientLease] = {}
        self._active_calls = 0
        self._closing_clients = 0
        self._closed = False
        self._shutdown_complete = False
        self._cleanup_claims: dict[str, float] = {}

    def run_turn(
        self,
        *,
        prompt: str,
        provider_session_id: str | None,
        cwd: Path,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        on_thread_started: Callable[[str], None] | None = None,
        on_timing: Callable[[CodexSdkTiming], None] | None = None,
    ) -> CodexSdkRunResult:
        return self._run_turn(
            prompt=prompt,
            provider_session_id=provider_session_id,
            source_provider_session_id=None,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            on_thread_started=on_thread_started,
            on_timing=on_timing,
        )

    def run_forked_turn(
        self,
        *,
        prompt: str,
        source_provider_session_id: str,
        cwd: Path,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        on_thread_started: Callable[[str], None] | None = None,
        on_timing: Callable[[CodexSdkTiming], None] | None = None,
    ) -> CodexSdkRunResult:
        """Fork an existing provider thread and run the first turn on its child."""
        return self._run_turn(
            prompt=prompt,
            provider_session_id=None,
            source_provider_session_id=source_provider_session_id,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            on_thread_started=on_thread_started,
            on_timing=on_timing,
        )

    def _run_turn(
        self,
        *,
        prompt: str,
        provider_session_id: str | None,
        source_provider_session_id: str | None,
        cwd: Path,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        on_thread_started: Callable[[str], None] | None,
        on_timing: Callable[[CodexSdkTiming], None] | None,
    ) -> CodexSdkRunResult:
        sdk = _load_codex_sdk()
        started_at = self._clock()
        client_start_ms = 0.0
        thread_open_ms = 0.0
        turn_ms = 0.0
        thread_release_ms = 0.0
        thread_release_error_type: str | None = None
        server_duration_ms: int | None = None
        usage: dict[str, Any] | None = None
        codex: Any | None = None
        thread_id = ""
        connection_usable = True
        archive_thread_ids: list[str] = []
        timing: CodexSdkTiming | None = None
        final_response = ""

        try:
            effort = _reasoning_effort(reasoning_effort, sdk)
            sdk_sandbox = _sandbox(sandbox, sdk)
            codex, client_start_ms = self._acquire_codex(sdk)

            thread_opened_at = self._clock()
            try:
                if source_provider_session_id is not None:
                    _unarchive_thread_if_needed(codex, source_provider_session_id, sdk)
                    archive_thread_ids.append(source_provider_session_id)
                    thread = codex.thread_fork(
                        source_provider_session_id,
                        approval_mode=sdk.approval_mode.deny_all,
                        cwd=str(cwd),
                        model=model,
                        sandbox=sdk_sandbox,
                    )
                elif provider_session_id is None:
                    thread = codex.thread_start(
                        approval_mode=sdk.approval_mode.deny_all,
                        cwd=str(cwd),
                        model=model,
                        sandbox=sdk_sandbox,
                    )
                else:
                    _unarchive_thread_if_needed(codex, provider_session_id, sdk)
                    archive_thread_ids.append(provider_session_id)
                    thread = codex.thread_resume(
                        provider_session_id,
                        approval_mode=sdk.approval_mode.deny_all,
                        cwd=str(cwd),
                        model=model,
                        sandbox=sdk_sandbox,
                    )
            finally:
                thread_open_ms = _milliseconds(self._clock() - thread_opened_at)

            thread_id = _non_empty_string(getattr(thread, "id", None))
            if not thread_id:
                raise RuntimeError("Codex SDK did not return a thread id")
            archive_thread_ids.append(thread_id)
            if (
                source_provider_session_id is not None
                and thread_id == source_provider_session_id
            ):
                raise RuntimeError("Codex fork did not create a new provider thread")
            if (
                source_provider_session_id is None
                and provider_session_id is not None
                and thread_id != provider_session_id
            ):
                raise RuntimeError("Codex resumed a different provider thread")
            if (
                source_provider_session_id is not None or provider_session_id is None
            ) and on_thread_started is not None:
                on_thread_started(thread_id)

            turn_started_at = self._clock()
            try:
                result = thread.run(
                    prompt,
                    approval_mode=sdk.approval_mode.deny_all,
                    cwd=str(cwd),
                    effort=effort,
                    model=model,
                    sandbox=sdk_sandbox,
                )
            finally:
                turn_ms = _milliseconds(self._clock() - turn_started_at)
            server_duration_ms = _optional_int(getattr(result, "duration_ms", None))
            usage = _usage_payload(getattr(result, "usage", None))
            final_response = _non_empty_string(getattr(result, "final_response", None))
            if not final_response:
                raise RuntimeError("Codex SDK did not include a final response")
        except sdk.transport_closed_error:
            if codex is not None:
                connection_usable = False
            raise
        finally:
            if codex is not None:
                release_started_at = self._clock()
                if thread_id and connection_usable:
                    try:
                        _unsubscribe_thread(codex, thread_id, sdk)
                    except sdk.transport_closed_error:
                        thread_release_error_type = sdk.transport_closed_error.__name__
                    except Exception as exc:
                        thread_release_error_type = type(exc).__name__
                        _warn_release(thread_id, exc)

                close_error = self._release_codex(codex)
                if close_error is not None:
                    if thread_release_error_type is None:
                        thread_release_error_type = type(close_error).__name__
                    _warn_release(thread_id or "unknown", close_error)

                if archive_thread_ids:
                    try:
                        archive_results = self._archive_client_factory(Path(cwd)).archive_threads(
                            archive_thread_ids
                        )
                        failed = [result for result in archive_results if not result.archived]
                        if failed:
                            if thread_release_error_type is None:
                                thread_release_error_type = "CodexThreadArchiveError"
                            detail = "; ".join(
                                f"{result.thread_id}: {result.error or 'unknown error'}"
                                for result in failed
                            )
                            _warn_release(thread_id or "unknown", RuntimeError(detail))
                    except Exception as exc:
                        if thread_release_error_type is None:
                            thread_release_error_type = type(exc).__name__
                        _warn_release(thread_id or "unknown", exc)
                thread_release_ms = _milliseconds(self._clock() - release_started_at)
            timing = CodexSdkTiming(
                client_start_ms=client_start_ms,
                thread_open_ms=thread_open_ms,
                turn_ms=turn_ms,
                thread_release_ms=thread_release_ms,
                thread_release_error_type=thread_release_error_type,
                server_duration_ms=server_duration_ms,
                total_ms=_milliseconds(self._clock() - started_at),
                usage=usage,
            )
            if on_timing is not None:
                on_timing(timing)

        return CodexSdkRunResult(
            provider_session_id=thread_id,
            text=final_response,
            timing=timing,
        )

    def claim_opportunistic_cleanup(self, memory_root: Path) -> bool:
        key = os.path.normcase(str(Path(memory_root).resolve(strict=False)))
        now = self._clock()
        with self._condition:
            last_claimed = self._cleanup_claims.get(key)
            if (
                last_claimed is not None
                and now - last_claimed < _OPPORTUNISTIC_CLEANUP_INTERVAL_SECONDS
            ):
                return False
            self._cleanup_claims[key] = now
            return True

    def close(self) -> None:
        with self._condition:
            if self._closed:
                while not self._shutdown_complete:
                    self._condition.wait()
                return
            self._closed = True
            while self._active_calls or self._closing_clients:
                self._condition.wait()
            self._shutdown_complete = True
            self._condition.notify_all()

    def _acquire_codex(self, sdk: _CodexSdkBindings) -> tuple[Any, float]:
        with self._condition:
            if self._closed:
                raise RuntimeError("Codex SDK runner is closed")
            client_started_at = self._clock()
            codex_factory = self._codex_factory or sdk.codex
            codex = codex_factory(
                sdk.config(
                    client_name="rightmemory",
                    client_title="RightMemory",
                    client_version="0.1.0",
                    config_overrides=("mcp_servers={}",),
                )
            )
            client_start_ms = _milliseconds(self._clock() - client_started_at)
            lease = _CodexClientLease(codex, active_calls=1, retired=True)
            self._client_leases[id(codex)] = lease
            self._active_calls += 1
            return codex, client_start_ms

    def _release_codex(self, codex: Any) -> Exception | None:
        close_now: _CodexClientLease | None = None
        with self._condition:
            lease = self._client_leases[id(codex)]
            lease.active_calls -= 1
            self._active_calls -= 1
            close_now = self._schedule_retired_close_locked(lease)
            if self._active_calls == 0:
                self._condition.notify_all()
        if close_now is not None:
            return self._close_retired_codex(close_now)
        return None

    def _schedule_retired_close_locked(
        self,
        lease: _CodexClientLease,
    ) -> _CodexClientLease | None:
        if not lease.retired or lease.active_calls or lease.closing:
            return None
        lease.closing = True
        self._closing_clients += 1
        return lease

    def _close_retired_codex(self, lease: _CodexClientLease) -> Exception | None:
        close_error: Exception | None = None
        try:
            lease.codex.close()
        except Exception as exc:
            close_error = exc
        finally:
            with self._condition:
                current = self._client_leases.get(id(lease.codex))
                if current is lease:
                    del self._client_leases[id(lease.codex)]
                self._closing_clients -= 1
                self._condition.notify_all()
        return close_error


@cache
def _load_codex_sdk() -> _CodexSdkBindings:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox, TransportClosedError
        from openai_codex.errors import InvalidRequestError
        from openai_codex.generated.v2_all import ThreadUnsubscribeResponse
        from openai_codex.types import ReasoningEffort
    except ImportError as exc:
        raise RuntimeError(_CODEX_SDK_INSTALL_ERROR) from exc
    return _CodexSdkBindings(
        approval_mode=ApprovalMode,
        codex=Codex,
        config=CodexConfig,
        sandbox=Sandbox,
        transport_closed_error=TransportClosedError,
        invalid_request_error=InvalidRequestError,
        reasoning_effort=ReasoningEffort,
        thread_unsubscribe_response=ThreadUnsubscribeResponse,
    )


def _unsubscribe_thread(codex: Any, thread_id: str, sdk: _CodexSdkBindings) -> None:
    unsubscribe = getattr(codex, "thread_unsubscribe", None)
    if callable(unsubscribe):
        response = unsubscribe(thread_id)
    else:
        client = getattr(codex, "_client", None)
        request = getattr(client, "request", None)
        if not callable(request):
            raise RuntimeError("Codex SDK does not expose thread/unsubscribe")
        response = request(
            "thread/unsubscribe",
            {"threadId": thread_id},
            response_model=sdk.thread_unsubscribe_response,
        )

    status = getattr(response, "status", response)
    value = getattr(status, "value", status)
    if value not in {"unsubscribed", "notSubscribed", "notLoaded"}:
        raise RuntimeError(f"unexpected Codex thread/unsubscribe status: {value!r}")


def _unarchive_thread_if_needed(codex: Any, thread_id: str, sdk: _CodexSdkBindings) -> None:
    unarchive = getattr(codex, "thread_unarchive", None)
    if not callable(unarchive):
        raise RuntimeError("Codex SDK does not expose thread/unarchive")
    try:
        unarchive(thread_id)
    except sdk.invalid_request_error as exc:
        message = str(exc).lower()
        expected = f"no archived rollout found for thread id {thread_id}".lower()
        if expected not in message:
            raise


def _warn_release(thread_id: str, exc: Exception) -> None:
    print(
        "Warning: RightMemory could not fully release and archive Codex thread "
        f"{thread_id}: {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )


def _reasoning_effort(value: str | None, sdk: _CodexSdkBindings) -> Any | None:
    if value is None:
        return None
    if value not in _CODEX_REASONING_EFFORTS:
        options = ", ".join(_CODEX_REASONING_EFFORTS)
        raise ValueError(f"Codex reasoning_effort must be one of: {options}")
    return sdk.reasoning_effort(value)


def _sandbox(value: str, sdk: _CodexSdkBindings) -> Any:
    try:
        attribute = _CODEX_SANDBOXES[value]
    except KeyError as exc:
        options = ", ".join(sorted(_CODEX_SANDBOXES))
        raise ValueError(f"Codex sandbox must be one of: {options}") from exc
    return getattr(sdk.sandbox, attribute)


def _usage_payload(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        return None
    payload = model_dump(by_alias=True, mode="json")
    return payload if isinstance(payload, dict) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _non_empty_string(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _milliseconds(seconds: float) -> float:
    return round(max(0.0, seconds) * 1000, 3)
