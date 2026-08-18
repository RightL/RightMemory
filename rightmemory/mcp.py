from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .async_update import AsyncUpdateStore
from .config import load_config
from .guidance import submit_guidance
from .runtime import RightMemoryRuntime
from .update_alerts import collect_update_recovery_summary
from .update_queue import UpdateQueueStore


SESSION_ID_DESCRIPTION = (
    "A stable identifier chosen once for the current conversation and reused for every "
    "RightMemory call in that conversation."
)
RETRIEVE_NEED_DESCRIPTION = (
    "A concise description of the cross-session context needed for the current work, "
    "not a verbatim copy of the user's message."
)
UPDATE_EVIDENCE_DESCRIPTION = (
    "A self-contained account of what happened, what is currently true, and why "
    "preserving it may matter in future work."
)
GUIDANCE_EVIDENCE_DESCRIPTION = (
    "A self-contained account of the agent-behavior signal. For a redirection, include "
    "the prior approach or omission, the user's explicit or implicit signal, and the "
    "resulting direction."
)

RETRIEVE_DESCRIPTION = """\
Retrieve cross-session context when it could materially change how the current work is
understood or approached. Ask for the context needed rather than forwarding the user's
message verbatim.

Use relevant returned context in the work, but treat the current conversation and current
evidence as authoritative. Skip clearly self-contained requests for which stored context
is unlikely to matter.

If returned context is stale, wrong, misleading, or overbroad, do not follow it; submit
the correction and current evidence with rightmemory_submit_update."""

SUBMIT_UPDATE_DESCRIPTION = """\
Submit evidence for durable cross-session context or the current direction of meaningful
ongoing work when omitting it would likely cause poorer future decisions, substantial
rediscovery, or loss of continuity.

Submit at a natural boundary once the evidence is clear; completion is not required. Do
not submit transient progress, routine results, unresolved discussion by itself, or
implementation detail already adequately preserved in project-local artifacts. Combine
related evidence due at the same boundary.

State what happened, what is true now, and why it may matter. Do not prescribe stored
wording, identifiers, classification, placement, or edits.

Processing is asynchronous. After an empty successful result, continue the task without
waiting, polling, or resubmitting. Only actionable failures or recovery warnings are
returned."""

CAPTURE_GUIDANCE_DESCRIPTION = """\
Capture plausible evidence about how an agent should handle similar future work. Bias
toward capture rather than filtering: uncertainty about whether the pattern will recur is
not a reason to skip it, and similar captures from distinct occurrences are useful.

Capture both direct guidance and explicit or implicit user redirections. A redirection is
a user response that changes or reveals how identifiable prior work should proceed. Infer
an implicit redirection from the contrast between the approach you were taking and the
direction the user now indicates.

The signal may be a correction, rejection, unease, guiding question, added constraint or
information, or a change in conclusion, scope, reasoning, process, omissions, behavior,
or presentation. It does not need to be phrased as a general rule.

Do not require a fully settled general principle or task completion. Capture once the
signal is concrete enough to describe the prior direction and what should change.

Skip only mere continuation, selection among intentionally open options, an unrelated new
task, or a detail clearly confined to the current artifact with no plausible
agent-behavior lesson. Do not skip merely because the guidance may be one-off.

Capture each distinct occurrence once. Similar guidance may be captured again when a
later interaction independently provides the same pattern.

For a redirection, record the prior approach or omission, the user's signal, and the
resulting direction. For direct guidance, include enough context to judge its scope. Record
the interaction evidence; do not invent a broader rule, final stored wording, or
destination. Apply the resulting direction to the current work regardless of capture.

When the user explicitly asks RightMemory to remember or follow guidance in future, use
rightmemory_submit_update instead. The same interaction may use both tools when it
provides distinct durable context and agent-behavior evidence.

After an empty successful result, continue without waiting or polling."""

SessionId = Annotated[str, Field(description=SESSION_ID_DESCRIPTION, min_length=1)]
RetrieveNeed = Annotated[str, Field(description=RETRIEVE_NEED_DESCRIPTION, min_length=1)]
UpdateEvidence = Annotated[str, Field(description=UPDATE_EVIDENCE_DESCRIPTION, min_length=1)]
GuidanceEvidence = Annotated[str, Field(description=GUIDANCE_EVIDENCE_DESCRIPTION, min_length=1)]

_MAX_ERROR_DETAIL_CHARS = 400


class McpBackend(Protocol):
    def retrieve(self, session_id: str, need: str) -> str: ...

    def submit_update(self, session_id: str, evidence: str) -> str | None: ...

    def capture_guidance(self, session_id: str, evidence: str) -> None: ...

    def actionable_warning(self) -> str | None: ...


@dataclass(frozen=True)
class DefaultMcpBackend:
    memory_root: Path

    def retrieve(self, session_id: str, need: str) -> str:
        runtime = RightMemoryRuntime(load_config("retrieve", memory_root=self.memory_root))
        try:
            return runtime.run_session_turn(session_id, need)
        finally:
            runtime.cleanup()

    def submit_update(self, session_id: str, evidence: str) -> str | None:
        store = AsyncUpdateStore(self.memory_root, "update")
        candidate_uid = uuid.uuid4().hex
        try:
            store.submit(
                session_id,
                evidence,
                candidate_uid=candidate_uid,
            )
        except Exception as exc:
            if not self._candidate_was_saved(store, session_id, candidate_uid):
                raise
            return (
                "RightMemory saved the update evidence, but could not start or wake its "
                f"update worker: {_error_detail(exc)}. Tell the user to run "
                "`rightmemory status`; do not resubmit the evidence."
            )
        return None

    def _candidate_was_saved(
        self,
        store: AsyncUpdateStore,
        session_id: str,
        candidate_uid: str,
    ) -> bool:
        try:
            state = store.read(session_id)
        except Exception:
            state = None
        if state is not None and candidate_uid in state.accepted_candidate_uids:
            return True
        try:
            return UpdateQueueStore(self.memory_root).read_outbox(candidate_uid) is not None
        except Exception:
            return False

    def capture_guidance(self, session_id: str, evidence: str) -> None:
        submit_guidance(self.memory_root, session_id, evidence)

    def actionable_warning(self) -> str | None:
        return _actionable_update_warning(self.memory_root)


def create_mcp_server(
    memory_root: Path,
    *,
    backend: McpBackend | None = None,
) -> MCPServer:
    selected_backend = backend or DefaultMcpBackend(
        Path(memory_root).expanduser().resolve()
    )
    server = MCPServer("RightMemory", log_level="WARNING")

    @server.tool(
        name="rightmemory_retrieve",
        description=RETRIEVE_DESCRIPTION,
        structured_output=False,
    )
    def rightmemory_retrieve(
        session_id: SessionId,
        need: RetrieveNeed,
    ) -> CallToolResult:
        clean_session = _clean_session_id(session_id)
        clean_need = _clean_text(need, "retrieval need")
        output = selected_backend.retrieve(clean_session, clean_need)
        return _result(output, selected_backend.actionable_warning())

    @server.tool(
        name="rightmemory_submit_update",
        description=SUBMIT_UPDATE_DESCRIPTION,
        structured_output=False,
    )
    def rightmemory_submit_update(
        session_id: SessionId,
        evidence: UpdateEvidence,
    ) -> CallToolResult:
        clean_session = _clean_session_id(session_id)
        clean_evidence = _clean_text(evidence, "update evidence")
        warning = selected_backend.submit_update(clean_session, clean_evidence)
        if warning is None:
            warning = selected_backend.actionable_warning()
        return _result(warning)

    @server.tool(
        name="rightmemory_capture_guidance",
        description=CAPTURE_GUIDANCE_DESCRIPTION,
        structured_output=False,
    )
    def rightmemory_capture_guidance(
        session_id: SessionId,
        evidence: GuidanceEvidence,
    ) -> CallToolResult:
        clean_session = _clean_session_id(session_id)
        clean_evidence = _clean_text(evidence, "guidance evidence")
        selected_backend.capture_guidance(clean_session, clean_evidence)
        return _result(selected_backend.actionable_warning())

    return server


def mcp_main(memory_root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rightmemory mcp",
        description="Serve RightMemory ordinary-agent tools over local MCP stdio.",
    )
    parser.parse_args([] if argv is None else argv)
    create_mcp_server(memory_root).run(transport="stdio")
    return 0


def _actionable_update_warning(memory_root: Path) -> str | None:
    try:
        return collect_update_recovery_summary(Path(memory_root)).warning()
    except Exception as exc:
        return (
            "RightMemory could not inspect update recovery state: "
            f"{_error_detail(exc)}. Tell the user to run `rightmemory status`."
        )


def _result(*texts: str | None) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=text)
            for text in texts
            if isinstance(text, str) and text.strip()
        ]
    )


def _clean_session_id(value: str) -> str:
    clean = value.strip()
    if not clean or any(character in clean for character in "\x00\r\n"):
        raise ValueError("session id must be a non-empty single line")
    return clean


def _clean_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} must not be empty")
    return clean


def _error_detail(exc: Exception) -> str:
    raw = str(exc).strip()
    detail = raw.splitlines()[0] if raw else type(exc).__name__
    if len(detail) > _MAX_ERROR_DETAIL_CHARS:
        detail = detail[:_MAX_ERROR_DETAIL_CHARS] + "...[truncated]"
    return f"{type(exc).__name__}: {detail}"
