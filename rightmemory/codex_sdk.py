from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Callable

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
    reasoning_effort: Any


@dataclass(frozen=True, slots=True)
class CodexSdkTiming:
    client_start_ms: float = 0.0
    thread_open_ms: float = 0.0
    turn_ms: float = 0.0
    server_duration_ms: int | None = None
    total_ms: float = 0.0
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CodexSdkRunResult:
    provider_session_id: str
    text: str
    timing: CodexSdkTiming


class CodexSdkRunner:
    """Lazily owns one Codex SDK/App Server connection for many turns."""

    def __init__(
        self,
        *,
        codex_factory: Callable[[Any], Any] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._codex_factory = codex_factory
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._codex: Any | None = None
        self._active_calls = 0
        self._closed = False
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
        sdk = _load_codex_sdk()
        started_at = self._clock()
        client_start_ms = 0.0
        thread_open_ms = 0.0
        turn_ms = 0.0
        server_duration_ms: int | None = None
        usage: dict[str, Any] | None = None
        codex: Any | None = None
        timing: CodexSdkTiming | None = None

        try:
            effort = _reasoning_effort(reasoning_effort, sdk)
            sdk_sandbox = _sandbox(sandbox, sdk)
            codex, client_start_ms = self._acquire_codex(sdk)

            thread_opened_at = self._clock()
            try:
                if provider_session_id is None:
                    thread = codex.thread_start(
                        approval_mode=sdk.approval_mode.deny_all,
                        cwd=str(cwd),
                        model=model,
                        sandbox=sdk_sandbox,
                    )
                else:
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
            if provider_session_id is not None and thread_id != provider_session_id:
                raise RuntimeError("Codex resumed a different provider thread")
            if provider_session_id is None and on_thread_started is not None:
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

            timing = CodexSdkTiming(
                client_start_ms=client_start_ms,
                thread_open_ms=thread_open_ms,
                turn_ms=turn_ms,
                server_duration_ms=server_duration_ms,
                total_ms=_milliseconds(self._clock() - started_at),
                usage=usage,
            )
            return CodexSdkRunResult(
                provider_session_id=thread_id,
                text=final_response,
                timing=timing,
            )
        except sdk.transport_closed_error:
            if codex is not None:
                self._invalidate(codex)
            raise
        finally:
            if codex is not None:
                self._release_codex()
            if timing is None:
                timing = CodexSdkTiming(
                    client_start_ms=client_start_ms,
                    thread_open_ms=thread_open_ms,
                    turn_ms=turn_ms,
                    server_duration_ms=server_duration_ms,
                    total_ms=_milliseconds(self._clock() - started_at),
                    usage=usage,
                )
            if on_timing is not None:
                on_timing(timing)

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
                return
            self._closed = True
            while self._active_calls:
                self._condition.wait()
            codex = self._codex
            self._codex = None
        if codex is not None:
            codex.close()

    def _acquire_codex(self, sdk: _CodexSdkBindings) -> tuple[Any, float]:
        with self._condition:
            if self._closed:
                raise RuntimeError("Codex SDK runner is closed")
            client_start_ms = 0.0
            if self._codex is None:
                client_started_at = self._clock()
                codex_factory = self._codex_factory or sdk.codex
                self._codex = codex_factory(
                    sdk.config(
                        client_name="rightmemory",
                        client_title="RightMemory",
                        client_version="0.1.0",
                    )
                )
                client_start_ms = _milliseconds(self._clock() - client_started_at)
            self._active_calls += 1
            return self._codex, client_start_ms

    def _release_codex(self) -> None:
        with self._condition:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._condition.notify_all()

    def _invalidate(self, codex: Any) -> None:
        with self._condition:
            if self._codex is codex:
                self._codex = None
        try:
            codex.close()
        except Exception:
            # The transport is already unusable. Preserve its original error;
            # a best-effort close failure must not make retry safety ambiguous.
            pass


@cache
def _load_codex_sdk() -> _CodexSdkBindings:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox, TransportClosedError
        from openai_codex.types import ReasoningEffort
    except ImportError as exc:
        raise RuntimeError(_CODEX_SDK_INSTALL_ERROR) from exc
    return _CodexSdkBindings(
        approval_mode=ApprovalMode,
        codex=Codex,
        config=CodexConfig,
        sandbox=Sandbox,
        transport_closed_error=TransportClosedError,
        reasoning_effort=ReasoningEffort,
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
