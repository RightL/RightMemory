from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any


_write_lock = threading.Lock()


def _emit(value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    with _write_lock:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


def _respond_later(request_id: int, value: Any, delay: float) -> None:
    time.sleep(delay)
    _emit({"id": request_id, "result": value})


def main() -> int:
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        message = json.loads(raw_line)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params", {})

        if method == "initialize":
            _emit(
                {
                    "id": request_id,
                    "result": {
                        "serverInfo": {"name": "fake-codex", "version": "test"},
                        "received": params,
                    },
                }
            )
        elif method == "initialized":
            _emit({"method": "fake/initialized", "params": params})
        elif method == "test/echo":
            _emit({"id": request_id, "result": params})
        elif method == "test/delay":
            thread = threading.Thread(
                target=_respond_later,
                args=(request_id, params, float(params.get("delay", 0.05))),
                daemon=True,
            )
            thread.start()
        elif method == "test/stderr":
            sys.stderr.write("x" * int(params.get("size", 1000)) + "\n")
            sys.stderr.flush()
            _emit({"id": request_id, "result": {}})
        elif method == "test/flood":
            for index in range(int(params.get("count", 100))):
                _emit({"method": "test/event", "params": {"index": index}})
            _emit({"id": request_id, "result": {}})
        elif method == "test/crash":
            sys.stderr.write("deliberate fake crash\n")
            sys.stderr.flush()
            os._exit(7)
        elif method == "thread/start":
            _emit(
                {
                    "id": request_id,
                    "result": {"thread": {"id": "thread-1"}, "received": params},
                }
            )
        elif method == "thread/resume":
            _emit(
                {
                    "id": request_id,
                    "result": {"thread": {"id": params["threadId"]}, "received": params},
                }
            )
        elif method == "thread/read":
            _emit(
                {
                    "id": request_id,
                    "result": {"thread": {"id": params["threadId"]}, "received": params},
                }
            )
        elif method == "thread/list":
            _emit({"id": request_id, "result": {"data": [], "received": params}})
        elif method == "thread/archive":
            _emit({"id": request_id, "result": {"received": params}})
        elif method == "turn/start":
            _emit(
                {
                    "id": request_id,
                    "result": {"turn": {"id": "turn-1"}, "received": params},
                }
            )
            _emit(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": "turn-1"},
                    },
                }
            )
            _emit(
                {
                    "id": "approval-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": params["threadId"], "turnId": "turn-1"},
                }
            )
        elif method == "turn/interrupt":
            _emit({"id": request_id, "result": {"received": params}})
        elif method is None and request_id == "approval-1":
            _emit(
                {
                    "method": "fake/serverResponse",
                    "params": {
                        "result": message.get("result"),
                        "error": message.get("error"),
                    },
                }
            )
        elif request_id is not None:
            _emit(
                {
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method: {method}"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
