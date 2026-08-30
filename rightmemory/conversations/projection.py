"""Small, version-tolerant projection of Codex App Server messages.

The App Server protocol is intentionally richer than the browser transcript.
This module keeps the durable event vocabulary compact while retaining the
provider payload needed to render completed items and reconcile live deltas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


_NOTIFICATION_KINDS = {
    "thread/started": "thread.started",
    "thread/status/changed": "thread.status",
    "thread/name/updated": "thread.name",
    "thread/archived": "thread.archived",
    "turn/started": "turn.started",
    "turn/completed": "turn.completed",
    "item/started": "item.started",
    "item/completed": "item.completed",
    "item/agentMessage/delta": "agent.delta",
    "item/reasoning/summaryPartAdded": "reasoning.summary_part",
    "item/reasoning/summaryTextDelta": "reasoning.summary_delta",
    "item/plan/delta": "plan.delta",
    "turn/plan/updated": "plan.updated",
    "item/commandExecution/outputDelta": "command.output",
    "item/fileChange/outputDelta": "file.output",
    "item/mcpToolCall/progress": "mcp.progress",
    "error": "protocol.error",
}

_MODERN_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }
)
_LEGACY_APPROVAL_METHODS = frozenset({"execCommandApproval", "applyPatchApproval"})
_INPUT_METHODS = frozenset(
    {
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
        "item/tool/call",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectedNotification:
    kind: str
    thread_id: str | None
    turn_id: str | None
    payload: dict[str, Any]
    completed_final_answer: bool = False
    status: str | None = None
    active_turn_id: str | None = None
    clears_active_turn: bool = False
    thread_title: str | None = None
    persist: bool = True


@dataclass(frozen=True, slots=True)
class ProjectedServerRequest:
    thread_id: str | None
    turn_id: str | None
    payload: dict[str, Any]
    status: str


def project_notification(method: object, params: object) -> ProjectedNotification:
    """Normalize one provider notification without rejecting future methods."""
    safe_method = method if isinstance(method, str) and method else "unknown"
    safe_params = dict(params) if isinstance(params, Mapping) else {"value": params}
    thread_id = _identifier(safe_params.get("threadId")) or _nested_id(safe_params, "thread")
    turn_id = _identifier(safe_params.get("turnId")) or _nested_id(safe_params, "turn")
    kind = _NOTIFICATION_KINDS.get(safe_method, "protocol.notification")
    completed_final_answer = _is_completed_final_answer(safe_method, safe_params)
    persist = not (
        (
            safe_method.startswith("item/reasoning/")
            and safe_method not in {
                "item/reasoning/summaryPartAdded",
                "item/reasoning/summaryTextDelta",
            }
        )
        or safe_method.startswith("rawResponse")
    )
    if safe_method in {
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
    }:
        payload = bounded_json_object(_reasoning_summary_payload(safe_params))
    elif persist:
        payload = bounded_json_object(_public_provider_payload(safe_params))
    else:
        payload = {}
    if kind == "protocol.notification" and persist:
        payload = {"method": _bounded_string(safe_method, 512), "params": payload}

    status: str | None = None
    active_turn_id: str | None = None
    clears_active_turn = False
    thread_title: str | None = None

    if safe_method == "thread/started":
        thread = safe_params.get("thread")
        if isinstance(thread, Mapping):
            thread_title = _optional_title(thread.get("name") or thread.get("title"))
            status = status_from_thread(thread.get("status"))
    elif safe_method == "thread/status/changed":
        status = status_from_thread(safe_params.get("status"))
    elif safe_method == "thread/name/updated":
        thread_title = _optional_title(safe_params.get("threadName"))
    elif safe_method == "thread/archived":
        status = "idle"
        clears_active_turn = True
    elif safe_method == "turn/started":
        status = "running"
        active_turn_id = turn_id
    elif safe_method == "turn/completed":
        turn = safe_params.get("turn")
        status = status_from_turn(turn.get("status") if isinstance(turn, Mapping) else None)
        clears_active_turn = True
    elif safe_method == "error":
        if turn_id is not None and safe_params.get("willRetry") is False:
            status = "failed"
            clears_active_turn = True

    return ProjectedNotification(
        kind=kind,
        thread_id=thread_id,
        turn_id=turn_id,
        payload=payload,
        completed_final_answer=completed_final_answer,
        status=status,
        active_turn_id=active_turn_id,
        clears_active_turn=clears_active_turn,
        thread_title=thread_title,
        persist=persist,
    )


def _reasoning_summary_payload(params: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only provider-authored summary data and routing identifiers."""
    payload: dict[str, Any] = {}
    for key in ("threadId", "turnId", "itemId"):
        identifier = _identifier(params.get(key))
        if identifier is not None:
            payload[key] = identifier
    summary_index = params.get("summaryIndex")
    if isinstance(summary_index, int) and not isinstance(summary_index, bool) and summary_index >= 0:
        payload["summaryIndex"] = summary_index
    delta = params.get("delta")
    if isinstance(delta, str):
        payload["delta"] = delta
    for key in ("part", "summaryPart"):
        if key in params:
            part = _public_summary(params.get(key))
            if part is not None:
                payload[key] = part
    return payload


def _public_provider_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    public = _public_provider_value(value)
    return public if isinstance(public, dict) else {}


def public_provider_object(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a browser-safe provider object without raw reasoning content."""
    return _public_provider_payload(value)


def _public_provider_value(value: Any) -> Any:
    """Recursively remove raw reasoning while retaining model-written summaries."""
    if isinstance(value, Mapping):
        if value.get("type") == "reasoning":
            return _public_reasoning_item(value)
        return {str(key): _public_provider_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_provider_value(child) for child in value]
    return value


def _public_reasoning_item(item: Mapping[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {"type": "reasoning"}
    item_id = _identifier(item.get("id"))
    if item_id is not None:
        public["id"] = item_id
    status = item.get("status")
    if isinstance(status, str) and status:
        public["status"] = status
    summary = _public_summary(item.get("summary"))
    if summary is not None:
        public["summary"] = summary
    return public


def _public_summary(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        if not isinstance(text, str):
            return None
        part: dict[str, Any] = {"text": text}
        kind = value.get("type")
        if isinstance(kind, str) and kind:
            part["type"] = kind
        return part
    if isinstance(value, (list, tuple)):
        parts = [part for child in value if (part := _public_summary(child)) is not None]
        return parts
    return None


def _is_completed_final_answer(method: str, params: Mapping[str, Any]) -> bool:
    """Classify the terminal message before transcript payload bounding."""
    if method != "item/completed":
        return False
    item = params.get("item")
    return (
        isinstance(item, Mapping)
        and item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
    )


def project_server_request(method: object, params: object) -> ProjectedServerRequest:
    safe_method = method if isinstance(method, str) and method else "unknown"
    safe_params = dict(params) if isinstance(params, Mapping) else {"value": params}
    thread_id = _identifier(safe_params.get("threadId")) or _nested_id(safe_params, "thread")
    turn_id = _identifier(safe_params.get("turnId")) or _nested_id(safe_params, "turn")
    status = "waiting_input" if safe_method in _INPUT_METHODS else "waiting_approval"
    return ProjectedServerRequest(
        thread_id=thread_id,
        turn_id=turn_id,
        payload=bounded_json_object(safe_params, maximum_bytes=120 * 1024),
        status=status,
    )


def server_request_result(
    method: str,
    *,
    decision: object = None,
    response: object = None,
    request_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the installed stable response shape for a server request."""
    params = request_params or {}
    if method in _MODERN_APPROVAL_METHODS:
        clean_decision = _approval_decision(decision)
        if clean_decision is None:
            raise ValueError("Choose accept, acceptForSession, decline, or cancel.")
        return {"decision": clean_decision}
    if method in _LEGACY_APPROVAL_METHODS:
        legacy = {
            "accept": "approved",
            "acceptForSession": "approved_for_session",
            "decline": {"denied": {"rejection": "User declined in RightMemory."}},
            "cancel": "abort",
        }.get(decision) if isinstance(decision, str) else None
        if legacy is None:
            raise ValueError("Choose accept, acceptForSession, decline, or cancel.")
        return {"decision": legacy}
    if method == "item/permissions/requestApproval":
        clean_decision = _approval_decision(decision)
        if clean_decision is None:
            raise ValueError("Choose accept, acceptForSession, decline, or cancel.")
        requested = params.get("permissions")
        if clean_decision in {"accept", "acceptForSession"} and not isinstance(requested, Mapping):
            raise ValueError("The permission request does not contain a grantable profile.")
        # The permission response is a grant profile, not a ReviewDecision.
        # An empty profile grants nothing and safely represents decline/cancel.
        permissions = (
            dict(requested)
            if isinstance(requested, Mapping) and clean_decision.startswith("accept")
            else {}
        )
        return {
            "permissions": bounded_json_object(permissions, maximum_bytes=120 * 1024),
            "scope": "session" if clean_decision == "acceptForSession" else "turn",
        }
    if method == "item/tool/requestUserInput":
        answers = _tool_answers(response, params)
        return {"answers": bounded_json_object(answers, maximum_bytes=120 * 1024)}
    if method == "mcpServer/elicitation/request":
        action = (
            {"accept": "accept", "decline": "decline", "cancel": "cancel"}.get(decision)
            if isinstance(decision, str)
            else None
        )
        if action is None and isinstance(response, Mapping):
            supplied = response.get("action")
            action = supplied if supplied in {"accept", "decline", "cancel"} else "accept"
        if action is None:
            raise ValueError("Choose accept, decline, or cancel for this elicitation.")
        if action == "accept":
            content = response.get("content") if isinstance(response, Mapping) and "content" in response else response
            if not isinstance(content, Mapping):
                raise ValueError("An accepted elicitation needs object content.")
            return {"action": action, "content": bounded_json_object(content, maximum_bytes=120 * 1024)}
        return {"action": action, "content": None}
    if method == "item/tool/call":
        if not isinstance(response, Mapping):
            raise ValueError("The tool-call response must be an object.")
        return bounded_json_object(response, maximum_bytes=120 * 1024)
    if isinstance(response, Mapping):
        return bounded_json_object(response, maximum_bytes=120 * 1024)
    if isinstance(decision, str) and decision:
        return {"decision": decision}
    raise ValueError("This server request needs an object response.")


def _tool_answers(response: object, params: Mapping[str, Any]) -> dict[str, Any]:
    questions = params.get("questions")
    question_ids = [
        question.get("id")
        for question in questions
        if isinstance(question, Mapping) and isinstance(question.get("id"), str) and question.get("id")
    ] if isinstance(questions, list) else []
    if isinstance(response, Mapping):
        candidate = response.get("answers") if set(response) == {"answers"} else response
        if not isinstance(candidate, Mapping):
            raise ValueError("User-input answers must be an object.")
        answers: dict[str, Any] = {}
        for question_id, value in candidate.items():
            if not isinstance(question_id, str) or not question_id:
                raise ValueError("Every user-input answer needs a question id.")
            if isinstance(value, Mapping) and isinstance(value.get("answers"), list):
                values = value["answers"]
            elif isinstance(value, list):
                values = value
            else:
                values = [value]
            if not values or any(not isinstance(item, str) for item in values):
                raise ValueError("Each user-input answer must contain text.")
            answers[question_id] = {"answers": values}
        if question_ids and set(answers) != set(question_ids):
            raise ValueError("Answer every requested question exactly once.")
        return answers
    if isinstance(response, str) and len(question_ids) == 1 and response:
        return {question_ids[0]: {"answers": [response]}}
    raise ValueError("User-input answers must match the requested questions.")


def _approval_decision(value: object) -> str | None:
    return value if isinstance(value, str) and value in {
        "accept", "acceptForSession", "decline", "cancel"
    } else None


def status_from_thread(value: object) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    kind = value.get("type")
    if kind == "idle":
        return "idle"
    if kind == "systemError":
        return "failed"
    if kind == "active":
        active_flags = value.get("activeFlags")
        flags = (
            {flag for flag in active_flags if isinstance(flag, str)}
            if isinstance(active_flags, (list, tuple, set, frozenset))
            else set()
        )
        if "waitingOnUserInput" in flags or value.get("waitingOnUserInput") is True:
            return "waiting_input"
        if "waitingOnApproval" in flags or value.get("waitingOnApproval") is True:
            return "waiting_approval"
        return "running"
    return "unknown"


def status_from_turn(value: object) -> str:
    if value == "completed":
        return "completed"
    if value == "failed":
        return "failed"
    if value == "interrupted":
        return "interrupted"
    if value == "inProgress":
        return "running"
    return "unknown"


def bounded_json_object(value: Mapping[str, Any], *, maximum_bytes: int = 220 * 1024) -> dict[str, Any]:
    """Return JSON-safe provider data within the durable-store envelope."""
    remaining = [maximum_bytes // 2]
    bounded = _bound_value(dict(value), remaining, 0)
    if not isinstance(bounded, dict):
        bounded = {"value": bounded}
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) <= maximum_bytes:
        return bounded
    # The recursive budget is deliberately conservative, but very wide unicode
    # values can still exceed its byte estimate. Preserve identifiers and mark
    # the exceptional payload instead of failing the App Server reader thread.
    return {
        "truncated": True,
        "summary": _bounded_string(encoded, min(32_000, maximum_bytes // 4)),
    }


def _bound_value(value: Any, remaining: list[int], depth: int) -> Any:
    if remaining[0] <= 0:
        return "[truncated]"
    if depth >= 10:
        remaining[0] -= 16
        return "[depth truncated]"
    if value is None or isinstance(value, (bool, int)):
        remaining[0] -= 8
        return value
    if isinstance(value, float):
        remaining[0] -= 16
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        maximum = min(65_536, max(0, remaining[0]))
        result = _bounded_string(value, maximum)
        remaining[0] -= len(result)
        return result
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= 512 or remaining[0] <= 0:
                output["_truncated"] = True
                break
            safe_key = _bounded_string(str(key), 512)
            remaining[0] -= len(safe_key)
            output[safe_key] = _bound_value(child, remaining, depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        output = []
        for index, child in enumerate(value):
            if index >= 512 or remaining[0] <= 0:
                output.append("[truncated]")
                break
            output.append(_bound_value(child, remaining, depth + 1))
        return output
    rendered = _bounded_string(repr(value), min(4096, max(0, remaining[0])))
    remaining[0] -= len(rendered)
    return rendered


def _bounded_string(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    if maximum <= 16:
        return value[:maximum]
    return value[: maximum - 14] + "...[truncated]"


def _identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if not clean or len(clean) > 512 or any(character in clean for character in "\x00\r\n"):
        return None
    return clean


def _nested_id(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    return _identifier(value.get("id")) if isinstance(value, Mapping) else None


def _optional_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return _bounded_string(clean, 500) if clean else None
