from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException, Request, Response, status

from ..session import _ensure_runtime_gitignore, _fsync_directory
from .models import error_detail


SESSION_COOKIE = "rightmemory_session"
CSRF_HEADER = "x-csrf-token"
WEB_RUNTIME_DIR = ".runtime/web"
SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class WebSession:
    session_id: str
    csrf_token: str
    created_at: str
    active_root: str


def web_runtime_dir(memory_root: Path) -> Path:
    return Path(memory_root) / WEB_RUNTIME_DIR


def operator_token_hash_path(memory_root: Path) -> Path:
    return web_runtime_dir(memory_root) / "operator-token.sha256"


def session_secret_path(memory_root: Path) -> Path:
    return web_runtime_dir(memory_root) / "session-secret"


def ensure_web_auth_files(memory_root: Path, *, operator_token: str | None = None) -> str | None:
    runtime = web_runtime_dir(memory_root)
    _ensure_runtime_gitignore(Path(memory_root) / ".runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    generated_token: str | None = None
    token_path = operator_token_hash_path(memory_root)
    if operator_token is not None:
        _atomic_write_secret(token_path, _hash_token(operator_token) + "\n")
    elif not token_path.exists():
        generated_token = secrets.token_urlsafe(24)
        _atomic_write_secret(token_path, _hash_token(generated_token) + "\n")
    secret_path = session_secret_path(memory_root)
    if not secret_path.exists():
        _atomic_write_secret(secret_path, secrets.token_urlsafe(48) + "\n")
    return generated_token


def verify_operator_token(memory_root: Path, token: str) -> bool:
    ensure_web_auth_files(memory_root)
    try:
        expected = operator_token_hash_path(memory_root).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return hmac.compare_digest(expected, _hash_token(token))


def create_session_cookie(
    memory_root: Path,
    *,
    active_root: Path | str | None = None,
    session_id: str | None = None,
    csrf_token: str | None = None,
    created_at: str | None = None,
) -> tuple[str, WebSession]:
    ensure_web_auth_files(memory_root)
    session = WebSession(
        session_id=session_id or secrets.token_urlsafe(24),
        csrf_token=csrf_token or secrets.token_urlsafe(24),
        created_at=created_at or datetime.now(UTC).replace(microsecond=0).isoformat(),
        active_root=str(Path(active_root).expanduser() if active_root is not None else Path(memory_root).expanduser()),
    )
    payload = {
        "sid": session.session_id,
        "csrf": session.csrf_token,
        "created_at": session.created_at,
        "active_root": session.active_root,
    }
    return _sign_payload(memory_root, payload), session


def set_session_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def read_session(memory_root: Path, request: Request) -> WebSession | None:
    return read_session_cookie(memory_root, request.cookies.get(SESSION_COOKIE))


def read_session_cookie(memory_root: Path, cookie: str | None) -> WebSession | None:
    """Validate one signed session cookie against current expiry and revocation state."""

    if not cookie:
        return None
    try:
        payload = _verify_payload(memory_root, cookie)
        session_id = _required_payload_str(payload, "sid")
        csrf_token = _required_payload_str(payload, "csrf")
        created_at = _required_payload_str(payload, "created_at")
        active_root = _required_payload_str(payload, "active_root")
    except (OSError, ValueError):
        return None
    if _session_is_expired(created_at) or _session_is_revoked(memory_root, session_id):
        return None
    return WebSession(
        session_id=session_id,
        csrf_token=csrf_token,
        created_at=created_at,
        active_root=active_root,
    )


def require_session(memory_root: Path, request: Request) -> WebSession:
    session = read_session(memory_root, request)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_detail("login required"))
    return session


def require_csrf(
    memory_root: Path,
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias=CSRF_HEADER),
) -> WebSession:
    session = require_session(memory_root, request)
    if not x_csrf_token or not hmac.compare_digest(x_csrf_token, session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_detail("invalid csrf token"))
    return session


def revoke_session(memory_root: Path, session_id: str) -> None:
    clean_session_id = _required_payload_str({"sid": session_id}, "sid")
    runtime = web_runtime_dir(memory_root)
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "revoked-sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        data["sessions"] = sessions
    sessions[clean_session_id] = datetime.now(UTC).replace(microsecond=0).isoformat()
    _atomic_write_secret(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_is_expired(created_at: str) -> bool:
    try:
        parsed = _parse_session_datetime(created_at)
    except ValueError:
        return True
    return parsed + timedelta(seconds=SESSION_TTL_SECONDS) <= datetime.now(UTC)


def _session_is_revoked(memory_root: Path, session_id: str) -> bool:
    path = web_runtime_dir(memory_root) / "revoked-sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    sessions = data.get("sessions", {})
    return isinstance(sessions, dict) and session_id in sessions


def _parse_session_datetime(value: str) -> datetime:
    clean = value.strip()
    if clean.endswith("Z"):
        clean = f"{clean[:-1]}+00:00"
    parsed = datetime.fromisoformat(clean)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _secret(memory_root: Path) -> bytes:
    ensure_web_auth_files(memory_root)
    return session_secret_path(memory_root).read_text(encoding="utf-8").strip().encode("utf-8")


def _sign_payload(memory_root: Path, payload: dict[str, str]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(_secret(memory_root), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_payload(memory_root: Path, token: str) -> dict[str, Any]:
    try:
        body, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError("invalid session token") from exc
    expected = hmac.new(_secret(memory_root), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid session signature")
    padded = body + ("=" * (-len(body) % 4))
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("session payload must be an object")
    return data


def _required_payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"session payload missing {key}")
    return value


def _atomic_write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _fsync_directory(path.parent)
