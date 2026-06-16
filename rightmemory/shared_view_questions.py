from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from hashlib import sha256
import json
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .hub.client import HubClient, HubClientError
from .shared_view_models import (
    PROVIDER_VIEWS_DIR,
    load_connections,
    load_shared_view_credential,
    validate_heading_id,
)


DEFAULT_START_TIMEOUT_SECONDS = 10
DEFAULT_ANSWER_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class QuestionViewConfig:
    view_id: str
    title: str
    intent: str
    approved: bool = False
    start_timeout_seconds: int = DEFAULT_START_TIMEOUT_SECONDS
    answer_timeout_seconds: int = DEFAULT_ANSWER_TIMEOUT_SECONDS
    provider_role: str = "retrieve"
    access_token_hashes: tuple[str, ...] = ()


def write_question_view(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    retriever_instructions: str,
    approved: bool = False,
    start_timeout_seconds: int = DEFAULT_START_TIMEOUT_SECONDS,
    answer_timeout_seconds: int = DEFAULT_ANSWER_TIMEOUT_SECONDS,
    access_tokens: list[str] | tuple[str, ...] = (),
) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    clean_title = _required_text(title, "title")
    clean_intent = _required_text(intent, "intent")
    clean_instructions = _required_text(retriever_instructions, "retriever_instructions")
    view_dir = root / PROVIDER_VIEWS_DIR / clean_view_id
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_text(view_dir / "view.md", f"# {clean_title}\n\n{clean_intent}\n")
    _write_text(view_dir / "retriever.md", clean_instructions.rstrip() + "\n")
    _write_text(
        view_dir / "question.toml",
        _render_question_toml(
            QuestionViewConfig(
                view_id=clean_view_id,
                title=clean_title,
                intent=clean_intent,
                approved=approved,
                start_timeout_seconds=start_timeout_seconds,
                answer_timeout_seconds=answer_timeout_seconds,
                access_token_hashes=tuple(question_token_hash(token) for token in access_tokens if token.strip()),
            )
        ),
    )
    return f"wrote question view {clean_view_id}"


def load_question_view(memory_root: Path, view_id: str) -> QuestionViewConfig:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    path = root / PROVIDER_VIEWS_DIR / clean_view_id / "question.toml"
    if not path.is_file():
        raise FileNotFoundError(f"question view config not found: shared_views/{clean_view_id}/question.toml")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("kind") != "question":
        raise ValueError(f"shared_views/{clean_view_id}/question.toml is not a question view")
    return QuestionViewConfig(
        view_id=validate_heading_id(str(data.get("view_id", clean_view_id))),
        title=str(data.get("title") or clean_view_id),
        intent=str(data.get("intent") or ""),
        approved=bool(data.get("approved", False)),
        start_timeout_seconds=_positive_int(data.get("start_timeout_seconds"), DEFAULT_START_TIMEOUT_SECONDS),
        answer_timeout_seconds=_positive_int(data.get("answer_timeout_seconds"), DEFAULT_ANSWER_TIMEOUT_SECONDS),
        provider_role=str(data.get("provider_role") or "retrieve"),
        access_token_hashes=_token_hashes(data.get("access_token_hashes", ())),
    )


def approve_question_view(memory_root: Path, view_id: str) -> str:
    root = Path(memory_root).expanduser()
    config = load_question_view(root, view_id)
    approved = QuestionViewConfig(
        view_id=config.view_id,
        title=config.title,
        intent=config.intent,
        approved=True,
        start_timeout_seconds=config.start_timeout_seconds,
        answer_timeout_seconds=config.answer_timeout_seconds,
        provider_role=config.provider_role,
        access_token_hashes=config.access_token_hashes,
    )
    _write_text(root / PROVIDER_VIEWS_DIR / config.view_id / "question.toml", _render_question_toml(approved))
    return f"approved question view {config.view_id}"


def publish_question_view(
    memory_root: Path,
    view_id: str,
    *,
    hub_url: str,
    credential_id: str,
    question_base_url: str,
    label: str | None = None,
    expires_at: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    config = load_question_view(root, view_id)
    if not config.approved:
        raise ValueError(f"question view is not approved: {config.view_id}")
    clean_hub_url = _required_text(hub_url, "hub_url")
    clean_credential_id = validate_heading_id(credential_id)
    clean_question_base_url = _required_text(question_base_url, "question_base_url")
    credential = load_shared_view_credential(root, clean_credential_id)
    question_token = secrets.token_urlsafe(32)
    updated_hashes = tuple(dict.fromkeys((*config.access_token_hashes, question_token_hash(question_token))))
    updated_config = QuestionViewConfig(
        view_id=config.view_id,
        title=config.title,
        intent=config.intent,
        approved=config.approved,
        start_timeout_seconds=config.start_timeout_seconds,
        answer_timeout_seconds=config.answer_timeout_seconds,
        provider_role=config.provider_role,
        access_token_hashes=updated_hashes,
    )
    _write_text(root / PROVIDER_VIEWS_DIR / config.view_id / "question.toml", _render_question_toml(updated_config))
    client = HubClient(clean_hub_url, credential["token"])
    client.register_question_view(
        config.view_id,
        title=config.title,
        description=config.intent,
        question_base_url=clean_question_base_url,
        question_token=question_token,
    )
    invitation = client.create_invitation(config.view_id, label=label, expires_at=expires_at)
    invitation_url = invitation.get("invitation_url")
    if not isinstance(invitation_url, str) or not invitation_url:
        raise ValueError("hub did not return an invitation_url")
    return f"published question view {config.view_id}\ninvitation_url\t{invitation_url}"


def ask_question_view(memory_root: Path, heading_id: str, question: str) -> str:
    root = Path(memory_root).expanduser()
    clean_heading_id = validate_heading_id(heading_id)
    clean_question = _required_text(question, "question")
    connection = load_connections(root).get(clean_heading_id)
    if connection is None or connection.view_type != "question":
        return _format_unavailable(clean_heading_id, "question view connection not found")
    target = connection.target
    if target.kind != "http-question" or not target.question_base_url or not target.question_credential_id:
        return _format_unavailable(clean_heading_id, "question view does not have an HTTP question target")
    try:
        credential = load_shared_view_credential(root, target.question_credential_id)
        response = HubClient(
            target.question_base_url,
            credential["token"],
            timeout=DEFAULT_ANSWER_TIMEOUT_SECONDS + DEFAULT_START_TIMEOUT_SECONDS,
        ).ask_question(
            target.view_id or clean_heading_id,
            clean_question,
        )
    except (KeyError, ValueError, HubClientError) as exc:
        return _format_unavailable(clean_heading_id, str(exc))
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    text = data.get("text") if isinstance(data.get("text"), str) else None
    if text and _formatted_status(text) == "unavailable":
        return _format_unavailable(clean_heading_id, _formatted_reason(text) or "provider question unavailable")
    if text and _formatted_status(text) == "answered":
        return _relabel_formatted_answer(text, clean_heading_id)
    status = str(data.get("status") or response.get("status") or "answered")
    if status != "answered":
        return _format_unavailable(clean_heading_id, str(response.get("reason") or data.get("reason") or status))
    answer = response.get("answer") or data.get("answer") or text
    return f"Shared question: {clean_heading_id}\nStatus: answered\nAnswer:\n{str(answer or '').strip()}\n"


def answer_question_view(memory_root: Path, view_id: str, question: str) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    clean_question = _required_text(question, "question")
    config = load_question_view(root, clean_view_id)
    if not config.approved:
        return _format_unavailable(clean_view_id, "question view is not approved")
    retriever = root / PROVIDER_VIEWS_DIR / clean_view_id / "retriever.md"
    if not retriever.is_file():
        return _format_unavailable(clean_view_id, "question view retriever prompt is missing")
    prompt = "\n".join(
        [
            retriever.read_text(encoding="utf-8").strip(),
            "",
            "Provider question:",
            clean_question,
        ]
    )
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"rightmemory-mq-{clean_view_id}")
    started = Event()
    future = executor.submit(_run_provider_question, root, config.provider_role, clean_view_id, prompt, started)
    try:
        if not started.wait(timeout=config.start_timeout_seconds):
            if future.done():
                future.result(timeout=0)
            executor.shutdown(wait=False, cancel_futures=True)
            return _format_unavailable(
                clean_view_id,
                f"provider did not start within {config.start_timeout_seconds} seconds",
            )
        answer = future.result(timeout=config.answer_timeout_seconds)
    except TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        return _format_unavailable(
            clean_view_id,
            f"provider answer timed out after {config.answer_timeout_seconds} seconds",
        )
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        return _format_unavailable(clean_view_id, str(exc))
    executor.shutdown(wait=True)
    return f"Shared question: {clean_view_id}\nStatus: answered\nAnswer:\n{answer.strip()}\n"


def verify_question_view_token(memory_root: Path, view_id: str, token: str) -> bool:
    clean_token = token.strip()
    if not clean_token:
        return False
    try:
        config = load_question_view(memory_root, view_id)
    except (FileNotFoundError, ValueError):
        return False
    return config.approved and question_token_hash(clean_token) in set(config.access_token_hashes)


def question_token_hash(token: str) -> str:
    return sha256(token.strip().encode("utf-8")).hexdigest()


def _format_unavailable(heading_id: str, reason: str) -> str:
    return f"Shared question: {heading_id}\nStatus: unavailable\nReason: {reason}\n"


def _run_provider_question(root: Path, provider_role: str, view_id: str, prompt: str, started: Event) -> str:
    from .config import load_config
    from .runtime import RightMemoryRuntime

    runtime = RightMemoryRuntime(load_config(provider_role, memory_root=root))
    try:
        return runtime.run_session_turn(f"shared-view-question-{view_id}", prompt, on_started=started.set)
    finally:
        runtime.cleanup()


def question_response_payload(text: str) -> dict[str, str]:
    status = _formatted_status(text)
    if status == "unavailable":
        return {
            "status": "unavailable",
            "reason": _formatted_reason(text) or "provider question unavailable",
            "text": text,
        }
    return {
        "status": "answered",
        "answer": _formatted_answer(text) or text.strip(),
        "text": text,
    }


def _formatted_status(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("Status:"):
            return line.split(":", 1)[1].strip()
    return None


def _formatted_reason(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("Reason:"):
            return line.split(":", 1)[1].strip()
    return None


def _formatted_answer(text: str) -> str | None:
    marker = "\nAnswer:\n"
    if marker not in text:
        return None
    return text.split(marker, 1)[1].strip()


def _relabel_formatted_answer(text: str, heading_id: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("Shared question:"):
        lines[0] = f"Shared question: {heading_id}"
    return "\n".join(lines).rstrip() + "\n"


def _render_question_toml(config: QuestionViewConfig) -> str:
    return "\n".join(
        [
            "version = 1",
            f'view_id = "{config.view_id}"',
            'kind = "question"',
            f"title = {_toml_string(config.title)}",
            f"approved = {str(config.approved).lower()}",
            f"intent = {_toml_string(config.intent)}",
            f"start_timeout_seconds = {int(config.start_timeout_seconds)}",
            f"answer_timeout_seconds = {int(config.answer_timeout_seconds)}",
            f"provider_role = {_toml_string(config.provider_role)}",
            f"access_token_hashes = {_toml_string_array(config.access_token_hashes)}",
            "",
        ]
    )


def _positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("question view timeout must be a positive integer")
    result = int(value)
    if result < 1:
        raise ValueError("question view timeout must be a positive integer")
    return result


def _token_hashes(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("question view access_token_hashes must be a TOML array")
    hashes: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("question view access_token_hashes entries must be non-empty strings")
        hashes.append(item.strip())
    return tuple(hashes)


def _required_text(value: str, label: str) -> str:
    clean = str(value).strip()
    if not clean:
        raise ValueError(f"question view {label} must not be empty")
    return clean


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
