from __future__ import annotations

from dataclasses import dataclass


SHARE_CAPABILITIES = {"auto", "file_context", "live_questions", "both"}


@dataclass(frozen=True)
class ShareCapabilityStatus:
    capability: str
    artifact_id: str | None = None
    status: str = "unknown"
    preview_path: str | None = None
    message: str | None = None

    def to_json(self) -> dict[str, str]:
        payload = {"capability": self.capability, "status": self.status}
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if self.preview_path:
            payload["preview_path"] = self.preview_path
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True)
class ShareOperationResult:
    share_id: str
    title: str
    role: str
    state: str
    capability: str
    builder_final_message: str = ""
    statuses: tuple[ShareCapabilityStatus, ...] = ()
    invitation_url: str | None = None
    next_action: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "share_id": self.share_id,
            "title": self.title,
            "role": self.role,
            "state": self.state,
            "capability": self.capability,
            "statuses": [status.to_json() for status in self.statuses],
        }
        if self.builder_final_message.strip():
            payload["builder_final_message"] = self.builder_final_message.strip()
        if self.invitation_url:
            payload["invitation_url"] = self.invitation_url
        if self.next_action:
            payload["next_action"] = self.next_action
        return payload


def normalize_share_capability(value: str | None) -> str:
    clean = (value or "auto").strip().lower().replace("-", "_")
    if clean in {"file", "context", "file_context"}:
        return "file_context"
    if clean in {"question", "questions", "live_question", "live_questions"}:
        return "live_questions"
    if clean in {"both", "all"}:
        return "both"
    if clean == "auto":
        return "auto"
    raise ValueError("share capability must be one of: auto, file-context, live-questions, both")


def capability_from_parts(parts: tuple[str, ...] | list[str]) -> str:
    normalized = set(parts)
    if normalized == {"file"}:
        return "file_context"
    if normalized == {"question"}:
        return "live_questions"
    if normalized == {"file", "question"}:
        return "both"
    return "auto"


def format_share_operation_result(result: ShareOperationResult) -> str:
    lines = [f"{result.share_id} {result.role} {result.state} capability={result.capability}"]
    if result.builder_final_message.strip():
        lines.extend(["", "Builder summary:", result.builder_final_message.strip()])
    if result.statuses:
        lines.append("")
        lines.append("Status:")
        for status in result.statuses:
            artifact = status.artifact_id or "-"
            line = f"{status.capability} {artifact} {status.status}"
            if status.message:
                line = f"{line}: {status.message}"
            lines.append(line)
    if result.invitation_url:
        lines.extend(["", f"invitation_url\t{result.invitation_url}"])
    if result.next_action:
        lines.extend(["", "Next:", result.next_action])
    return "\n".join(lines).rstrip() + "\n"
