from __future__ import annotations

from dataclasses import asdict
from importlib import resources
from pathlib import Path
import tempfile
import time
from typing import Callable

from .codex_sdk import CodexSdkRunner, CodexSdkTiming
from .config import AgentCliConfig


class RetrievalFilter:
    """Extract useful context from one query's supplied retrieval results."""

    def __init__(
        self,
        config: AgentCliConfig,
        codex_runner: CodexSdkRunner,
        trace_event: Callable[..., None] | None = None,
    ) -> None:
        if config.provider != "codex":
            raise ValueError("retrieval filtering requires the Codex CLI provider")
        self.config = config
        self._runner = codex_runner
        self._trace_event = trace_event

    def run(self, query: str, material: str) -> str:
        if not query.strip():
            raise ValueError("retrieval filter query must not be empty")
        if not material.strip():
            raise ValueError("retrieval filter material must not be empty")
        instructions = resources.files("rightmemory").joinpath(
            "prompts/retrieve-filter.md"
        ).read_text(encoding="utf-8")
        prompt = f"Current request:\n{query}\n\nRetrieved source material:\n{material}"
        started = time.perf_counter()
        timing: CodexSdkTiming | None = None

        def record_timing(value: CodexSdkTiming) -> None:
            nonlocal timing
            timing = value

        try:
            # A neutral directory avoids project discovery. No conversation or
            # provider-session state is written into the user's memory root.
            with tempfile.TemporaryDirectory(prefix="rightmemory-filter-") as directory:
                result = self._runner.run_ephemeral_text_turn(
                    prompt=prompt,
                    instructions=instructions,
                    cwd=Path(directory),
                    model=self.config.model,
                    reasoning_effort=self.config.reasoning_effort,
                    on_timing=record_timing,
                )
            text = result.text.strip()
            if not text:
                raise RuntimeError("retrieval filter did not return a final response")
        except BaseException as exc:
            self._trace(timing, started, "error", error_type=type(exc).__name__)
            raise
        self._trace(
            timing or result.timing, started, "success",
            provider_session_id=result.provider_session_id, ephemeral=True,
        )
        return text

    def _trace(
        self,
        timing: CodexSdkTiming | None,
        started: float,
        outcome: str,
        **extra: object,
    ) -> None:
        if self._trace_event is None:
            return
        values = asdict(timing) if timing is not None else {}
        values["total_ms"] = (time.perf_counter() - started) * 1000.0
        self._trace_event(
            "retrieval_filter_timing",
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
            outcome=outcome,
            **values,
            **extra,
        )
