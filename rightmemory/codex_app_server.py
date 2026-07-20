from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .platform import prepare_command


DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CodexThreadDeleteResult:
    thread_id: str
    deleted: bool
    error: str | None = None


class CodexAppServerClient:
    def __init__(
        self,
        memory_root: Path,
        *,
        timeout_seconds: int = DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS,
    ):
        if timeout_seconds < 1:
            raise ValueError("Codex App Server timeout must be positive")
        self.memory_root = Path(memory_root)
        self.timeout_seconds = timeout_seconds

    def delete_threads(self, thread_ids: list[str]) -> list[CodexThreadDeleteResult]:
        unique_ids = list(dict.fromkeys(_thread_id(thread_id) for thread_id in thread_ids))
        if not unique_ids:
            return []

        request_ids = {index + 1: thread_id for index, thread_id in enumerate(unique_ids)}
        initialize = {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "rightmemory",
                    "title": "RightMemory",
                    "version": "1",
                }
            },
        }
        delete_messages = [
            {
                "method": "thread/delete",
                "id": request_id,
                "params": {"threadId": thread_id},
            }
            for request_id, thread_id in request_ids.items()
        ]

        try:
            process = subprocess.Popen(
                _app_server_command(),
                cwd=str(self.memory_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            error = _bounded_error(f"Codex App Server failed: {type(exc).__name__}: {exc}")
            return [CodexThreadDeleteResult(thread_id=thread_id, deleted=False, error=error) for thread_id in unique_ids]

        if process.stdin is None or process.stdout is None or process.stderr is None:
            _terminate_process(process)
            error = "Codex App Server did not expose stdio pipes"
            return [CodexThreadDeleteResult(thread_id=thread_id, deleted=False, error=error) for thread_id in unique_ids]

        deadline = time.monotonic() + self.timeout_seconds
        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        stderr_lines: list[str] = []
        stdout_thread = _start_reader(process.stdout, output_queue, "stdout")
        stderr_thread = _start_stderr_reader(process.stderr, stderr_lines)
        responses: dict[int, dict[str, Any]] = {}
        parse_errors: list[str] = []
        startup_error: str | None = None
        try:
            _send_messages(process.stdin, [initialize])
            initialized = _wait_for_responses(
                output_queue,
                responses,
                expected={0},
                deadline=deadline,
                parse_errors=parse_errors,
            )
            initialize_response = responses.get(0)
            if not initialized or initialize_response is None:
                startup_error = parse_errors[0] if parse_errors else "missing initialize response"
            else:
                startup_error = _response_error(initialize_response)
            if startup_error is None:
                _send_messages(
                    process.stdin,
                    [{"method": "initialized", "params": {}}, *delete_messages],
                )
                _wait_for_responses(
                    output_queue,
                    responses,
                    expected=set(request_ids),
                    deadline=deadline,
                    parse_errors=parse_errors,
                )
        except (BrokenPipeError, OSError, TimeoutError) as exc:
            startup_error = f"Codex App Server protocol failed: {type(exc).__name__}: {exc}"
        finally:
            returncode = _finish_process(process, deadline)
            stdout_thread.join(timeout=0.2)
            stderr_thread.join(timeout=0.2)

        if returncode != 0:
            detail = "".join(stderr_lines).strip() or f"exit status {returncode}"
            startup_error = f"Codex App Server exited with status {returncode}: {detail}"
        if parse_errors and startup_error is None:
            startup_error = parse_errors[0]

        results: list[CodexThreadDeleteResult] = []
        for request_id, thread_id in request_ids.items():
            response = responses.get(request_id)
            error = startup_error or _delete_response_error(response)
            if response is None and error is None:
                error = "missing thread/delete response"
            results.append(
                CodexThreadDeleteResult(
                    thread_id=thread_id,
                    deleted=error is None,
                    error=_bounded_error(error) if error is not None else None,
                )
            )
        return results


def _send_messages(stdin: TextIO, messages: list[dict[str, Any]]) -> None:
    payload = "".join(json.dumps(message, ensure_ascii=False) + "\n" for message in messages)
    stdin.write(payload)
    stdin.flush()


def _wait_for_responses(
    output_queue: queue.Queue[tuple[str, str | None]],
    responses: dict[int, dict[str, Any]],
    *,
    expected: set[int],
    deadline: float,
    parse_errors: list[str],
) -> bool:
    line_number = 0
    while not expected.issubset(responses):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for App Server response")
        try:
            kind, raw = output_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for App Server response") from exc
        if kind == "eof":
            return False
        if kind == "error":
            parse_errors.append(raw or "App Server stdout reader failed")
            continue
        line_number += 1
        if raw is None or not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"invalid App Server JSON on line {line_number}: {exc.msg}")
            continue
        if not isinstance(value, dict):
            parse_errors.append(f"App Server line {line_number} was not a JSON object")
            continue
        request_id = value.get("id")
        if isinstance(request_id, int):
            responses[request_id] = value
    return True


def _start_reader(
    stream: TextIO,
    output_queue: queue.Queue[tuple[str, str | None]],
    label: str,
) -> threading.Thread:
    def read() -> None:
        try:
            for line in stream:
                output_queue.put(("line", line))
        except Exception as exc:
            output_queue.put(("error", f"App Server {label} reader failed: {type(exc).__name__}: {exc}"))
        finally:
            output_queue.put(("eof", None))

    thread = threading.Thread(target=read, name=f"rightmemory-codex-{label}", daemon=True)
    thread.start()
    return thread


def _start_stderr_reader(stream: TextIO, lines: list[str]) -> threading.Thread:
    def read() -> None:
        try:
            lines.extend(stream)
        except Exception as exc:
            lines.append(f"App Server stderr reader failed: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=read, name="rightmemory-codex-stderr", daemon=True)
    thread.start()
    return thread


def _finish_process(process: subprocess.Popen[str], deadline: float) -> int:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    remaining = max(0.0, deadline - time.monotonic())
    try:
        return process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return _terminate_process(process)


def _terminate_process(process: subprocess.Popen[str]) -> int:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        return process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            return process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return process.returncode if process.returncode is not None else -1


def _response_error(response: dict[str, Any] | None) -> str | None:
    if response is None:
        return None
    error = response.get("error")
    if error is None:
        return None
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(message, str) and message.strip():
            return f"App Server error {code}: {message.strip()}" if code is not None else message.strip()
    return f"App Server error: {error}"


def _delete_response_error(response: dict[str, Any] | None) -> str | None:
    if response is not None:
        error = response.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            if (
                code == -32600
                and isinstance(message, str)
                and message.lower().startswith("no rollout found for thread id ")
            ):
                return None
    return _response_error(response)


def _app_server_command() -> list[str]:
    command = ["codex", "app-server", "--stdio"]
    prepared = prepare_command(command)
    if os.name != "nt":
        return prepared
    try:
        file_index = prepared.index("-File") + 1
        shim = Path(prepared[file_index])
    except (ValueError, IndexError):
        return prepared
    script = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if shim.stem.lower() != "codex" or not script.is_file():
        return prepared
    bundled_node = shim.parent / "node.exe"
    node = str(bundled_node) if bundled_node.is_file() else shutil.which("node.exe") or shutil.which("node")
    if node is None:
        return prepared
    return [node, str(script), *command[1:]]


def _thread_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Codex thread id must be a non-empty string")
    return value.strip()


def _bounded_error(value: str, limit: int = 2000) -> str:
    clean = value.strip()
    return clean if len(clean) <= limit else clean[:limit] + "...[truncated]"
