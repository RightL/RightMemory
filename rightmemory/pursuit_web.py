from __future__ import annotations

import secrets
import webbrowser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Response, status

from .codex_sdk import CodexSdkRunner
from .pursuit_tasks import (
    PursuitTaskError,
    apply_reconciliation,
    dismiss_reconciliation,
    link_task,
    list_reconciliations,
    list_tasks,
    plan_task,
    propose_reconciliation,
    registry_revision,
    run_task,
    unlink_task,
    update_task,
)
from .pursuit_workspace import (
    PursuitEditor,
    PursuitRevisionConflict,
    PursuitWorkspaceError,
    apply_operations,
    preview_operations,
    redo,
    undo,
)


RunnerFactory = Callable[[], CodexSdkRunner]


def create_pursuit_app(
    memory_root: Path,
    *,
    access_token: str | None = None,
    runner_factory: RunnerFactory | None = None,
) -> FastAPI:
    root = Path(memory_root).expanduser().resolve()
    token = access_token or secrets.token_urlsafe(32)
    static_root = Path(__file__).parent / "pursuit_static"
    app = FastAPI(title="RightMemory Pursuit Studio")
    app.state.access_token = token
    app.state.memory_root = root

    def require_token(authorization: str | None = Header(default=None)) -> None:
        scheme, separator, value = (authorization or "").partition(" ")
        if not separator or scheme.casefold() != "bearer" or not secrets.compare_digest(value.strip(), token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Pursuit Studio token")

    @app.get("/")
    def index() -> Response:
        return _static_file(static_root / "index.html", "text/html; charset=utf-8")

    @app.get("/static/{asset_name}")
    def static_asset(asset_name: str) -> Response:
        allowed = {"pursuit.js": "text/javascript; charset=utf-8", "pursuit.css": "text/css; charset=utf-8"}
        if asset_name not in allowed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="static asset not found")
        return _static_file(static_root / asset_name, allowed[asset_name])

    @app.get("/api/workspace", dependencies=[Depends(require_token)])
    def workspace() -> dict[str, Any]:
        editor = PursuitEditor(root)
        return _ok(
            {
                "workspace": editor.snapshot(),
                "tasks": [task.to_json() for task in list_tasks(root)],
                "task_revision": registry_revision(root),
                "reconciliations": [record.to_json() for record in list_reconciliations(root)],
            }
        )

    @app.post("/api/preview", dependencies=[Depends(require_token)])
    def preview(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _handle(
            lambda: preview_operations(
                root,
                _operations(payload),
                expected_revision=_optional_string(payload, "revision"),
            ).to_json()
        )

    @app.post("/api/apply", dependencies=[Depends(require_token)])
    def apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        operations = _operations(payload)
        _reject_linked_deletions(root, operations)
        return _handle(
            lambda: apply_operations(
                root,
                operations,
                expected_revision=_optional_string(payload, "revision"),
                commit=bool(payload.get("commit", False)),
            ).to_json()
        )

    @app.post("/api/undo", dependencies=[Depends(require_token)])
    def undo_api(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        return _handle(lambda: undo(root, commit=bool((payload or {}).get("commit", False))).to_json())

    @app.post("/api/redo", dependencies=[Depends(require_token)])
    def redo_api(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        return _handle(lambda: redo(root, commit=bool((payload or {}).get("commit", False))).to_json())

    @app.post("/api/tasks/link", dependencies=[Depends(require_token)])
    def task_link(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _handle(
            lambda: link_task(
                root,
                pursuit_ids=_string_list(payload, "pursuit_ids"),
                provider=_required_string(payload, "provider"),
                thread_id=_required_string(payload, "thread_id"),
                title=_required_string(payload, "title"),
                project=_optional_string(payload, "project"),
                host=_optional_string(payload, "host"),
                status=_optional_string(payload, "status") or "active",
                expected_revision=_optional_string(payload, "task_revision"),
            ).to_json()
        )

    @app.post("/api/tasks/plan", dependencies=[Depends(require_token)])
    def task_plan(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _handle(
            lambda: plan_task(
                root,
                pursuit_id=_required_string(payload, "pursuit_id"),
                action=_optional_string(payload, "action"),
                title=_optional_string(payload, "title"),
                project=_optional_string(payload, "project"),
                host=_optional_string(payload, "host"),
            ).to_json()
        )

    @app.post("/api/tasks/{task_id}/run", dependencies=[Depends(require_token)])
    def task_run(task_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        values = payload or {}

        def execute() -> dict[str, Any]:
            if runner_factory is None:
                record = run_task(
                    root,
                    task_id,
                    project=_optional_string(values, "project"),
                    model=_optional_string(values, "model"),
                    reasoning_effort=_optional_string(values, "reasoning_effort"),
                    sandbox=_optional_string(values, "sandbox") or "workspace-write",
                )
            else:
                runner = runner_factory()
                try:
                    record = run_task(
                        root,
                        task_id,
                        project=_optional_string(values, "project"),
                        model=_optional_string(values, "model"),
                        reasoning_effort=_optional_string(values, "reasoning_effort"),
                        sandbox=_optional_string(values, "sandbox") or "workspace-write",
                        runner=runner,
                    )
                finally:
                    close = getattr(runner, "close", None)
                    if callable(close):
                        close()
            return record.to_json()

        return _handle(execute)

    @app.post("/api/tasks/{task_id}/update", dependencies=[Depends(require_token)])
    def task_update(task_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _handle(
            lambda: update_task(
                root,
                task_id,
                status=_optional_string(payload, "status"),
                result=_optional_string(payload, "result"),
                error=_optional_string(payload, "error"),
                title=_optional_string(payload, "title"),
            ).to_json()
        )

    @app.post("/api/tasks/{task_id}/unlink", dependencies=[Depends(require_token)])
    def task_unlink(task_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        def execute() -> dict[str, Any]:
            unlink_task(root, task_id, _optional_string(payload or {}, "pursuit_id"))
            return {"task_id": task_id}

        return _handle(execute)

    @app.post("/api/reconciliations/propose", dependencies=[Depends(require_token)])
    def reconciliation_propose(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return _handle(
            lambda: propose_reconciliation(
                root,
                task_id=_required_string(payload, "task_id"),
                summary=_required_string(payload, "summary"),
                operations=_operations(payload),
                expected_revision=_optional_string(payload, "revision"),
            ).to_json()
        )

    @app.post("/api/reconciliations/{reconciliation_id}/apply", dependencies=[Depends(require_token)])
    def reconciliation_apply(
        reconciliation_id: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        return _handle(
            lambda: apply_reconciliation(
                root,
                reconciliation_id,
                commit=bool((payload or {}).get("commit", False)),
            )
        )

    @app.post("/api/reconciliations/{reconciliation_id}/dismiss", dependencies=[Depends(require_token)])
    def reconciliation_dismiss(reconciliation_id: str) -> dict[str, Any]:
        return _handle(lambda: dismiss_reconciliation(root, reconciliation_id).to_json())

    return app


def serve_pursuit_studio(
    memory_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8767,
    open_browser: bool = True,
) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise PursuitWorkspaceError("Pursuit Studio currently binds only to a local loopback host")
    if port < 1 or port > 65535:
        raise PursuitWorkspaceError("Pursuit Studio port must be between 1 and 65535")
    token = secrets.token_urlsafe(32)
    app = create_pursuit_app(memory_root, access_token=token)
    display_host = "127.0.0.1" if host == "localhost" else ("[::1]" if host == "::1" else host)
    url = f"http://{display_host}:{port}/?{urlencode({'token': token})}"
    print(f"Pursuit Studio: {url}")
    print("The token grants local edit and Codex-task access for this server process.")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _static_file(path: Path, media_type: str) -> Response:
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="static asset not found")
    return Response(content=path.read_bytes(), media_type=media_type)


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _handle(callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        return _ok(callback())
    except PursuitRevisionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (PursuitWorkspaceError, PursuitTaskError, ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("operations")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PursuitWorkspaceError("operations must be a list of objects")
    return [dict(item) for item in value]


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PursuitWorkspaceError(f"missing required field: {key}")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PursuitWorkspaceError(f"{key} must be a string")
    return value.strip() or None


def _string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PursuitWorkspaceError(f"{key} must be a non-empty list of strings")
    return [item.strip() for item in value]


def _reject_linked_deletions(root: Path, operations: list[dict[str, Any]]) -> None:
    for operation in operations:
        if operation.get("op") != "delete":
            continue
        pursuit_id = operation.get("id")
        if not isinstance(pursuit_id, str):
            continue
        tasks = list_tasks(root, pursuit_id)
        if tasks:
            linked = ", ".join(task.task_id for task in tasks)
            raise PursuitTaskError(
                f"unlink Pursuit {pursuit_id} from tasks before deletion: {linked}"
            )
