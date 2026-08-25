from __future__ import annotations

import hashlib
import json
import os
import socket
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .codex_sdk import CodexSdkRunner
from .graph import build_graph_manifest, validate_item_id
from .session import MemoryWriteLock


REGISTRY_PATH = Path("pursuit_tasks.toml")
REGISTRY_VERSION = 1
TASK_STATUSES = {"planned", "active", "completed", "failed", "cancelled"}
RECONCILIATION_STATUSES = {"pending", "applied", "dismissed"}


class PursuitTaskError(ValueError):
    pass


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    provider: str
    pursuit_ids: list[str]
    title: str
    status: str = "planned"
    thread_id: str | None = None
    project: str | None = None
    host: str | None = None
    action: str | None = None
    prompt: str | None = None
    result: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def validate(self) -> None:
        if not self.task_id.strip():
            raise PursuitTaskError("task id must not be empty")
        if not self.provider.strip():
            raise PursuitTaskError("task provider must not be empty")
        if not self.title.strip():
            raise PursuitTaskError("task title must not be empty")
        if self.status not in TASK_STATUSES:
            raise PursuitTaskError(f"invalid task status: {self.status}")
        if not self.pursuit_ids:
            raise PursuitTaskError("task must link at least one Pursuit")
        if len(self.pursuit_ids) != len(set(self.pursuit_ids)):
            raise PursuitTaskError("task Pursuit links must be unique")
        for pursuit_id in self.pursuit_ids:
            validate_item_id(pursuit_id)
        if self.thread_id is not None and not self.thread_id.strip():
            raise PursuitTaskError("thread id must not be empty")

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "provider": self.provider,
            "pursuit_ids": list(self.pursuit_ids),
            "title": self.title,
            "status": self.status,
            "thread_id": self.thread_id,
            "project": self.project,
            "host": self.host,
            "action": self.action,
            "prompt": self.prompt,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ReconciliationRecord:
    reconciliation_id: str
    task_id: str
    summary: str
    operations: list[dict[str, Any]]
    base_revision: str
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def validate(self) -> None:
        if not self.reconciliation_id.strip():
            raise PursuitTaskError("reconciliation id must not be empty")
        if not self.task_id.strip():
            raise PursuitTaskError("reconciliation task id must not be empty")
        if not self.summary.strip():
            raise PursuitTaskError("reconciliation summary must not be empty")
        if not isinstance(self.operations, list) or not all(isinstance(item, dict) for item in self.operations):
            raise PursuitTaskError("reconciliation operations must be a list of objects")
        if self.status not in RECONCILIATION_STATUSES:
            raise PursuitTaskError(f"invalid reconciliation status: {self.status}")
        if len(self.base_revision) != 64:
            raise PursuitTaskError("reconciliation base revision is invalid")

    def to_json(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "task_id": self.task_id,
            "summary": self.summary,
            "operations": [dict(item) for item in self.operations],
            "base_revision": self.base_revision,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class TaskRegistry:
    tasks: list[TaskRecord] = field(default_factory=list)
    reconciliations: list[ReconciliationRecord] = field(default_factory=list)

    def validate(self, memory_root: Path) -> None:
        manifest = build_graph_manifest(memory_root)
        if manifest.errors:
            raise PursuitTaskError("RightMemory graph must be valid before task links can change")
        pursuit_ids = {
            item.id
            for item in manifest.items.values()
            if item.family == "pursuit" and item.item_kind == "heading"
        }
        task_ids: set[str] = set()
        thread_keys: set[tuple[str, str]] = set()
        for task in self.tasks:
            task.validate()
            if task.task_id in task_ids:
                raise PursuitTaskError(f"duplicate task id: {task.task_id}")
            task_ids.add(task.task_id)
            for pursuit_id in task.pursuit_ids:
                if pursuit_id not in pursuit_ids:
                    raise PursuitTaskError(f"task links unknown Pursuit: {pursuit_id}")
            if task.thread_id:
                key = (task.provider.casefold(), task.thread_id)
                if key in thread_keys:
                    raise PursuitTaskError(f"duplicate provider thread link: {task.provider}:{task.thread_id}")
                thread_keys.add(key)
        reconciliation_ids: set[str] = set()
        for reconciliation in self.reconciliations:
            reconciliation.validate()
            if reconciliation.reconciliation_id in reconciliation_ids:
                raise PursuitTaskError(f"duplicate reconciliation id: {reconciliation.reconciliation_id}")
            reconciliation_ids.add(reconciliation.reconciliation_id)
            if reconciliation.task_id not in task_ids:
                raise PursuitTaskError(f"reconciliation links unknown task: {reconciliation.task_id}")

    def task(self, task_id: str) -> TaskRecord:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise PursuitTaskError(f"unknown Pursuit task: {task_id}")

    def reconciliation(self, reconciliation_id: str) -> ReconciliationRecord:
        for reconciliation in self.reconciliations:
            if reconciliation.reconciliation_id == reconciliation_id:
                return reconciliation
        raise PursuitTaskError(f"unknown reconciliation: {reconciliation_id}")


def registry_revision(memory_root: Path) -> str:
    path = Path(memory_root).expanduser().resolve() / REGISTRY_PATH
    data = path.read_bytes() if path.is_file() else b""
    return hashlib.sha256(data).hexdigest()


def load_registry(memory_root: Path) -> TaskRegistry:
    root = Path(memory_root).expanduser().resolve()
    path = root / REGISTRY_PATH
    if path.is_symlink():
        raise PursuitTaskError(f"{REGISTRY_PATH} must be a regular file")
    if not path.is_file():
        if path.exists():
            raise PursuitTaskError(f"{REGISTRY_PATH} must be a regular file")
        return TaskRegistry()
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PursuitTaskError(f"invalid {REGISTRY_PATH}: {exc}") from exc
    if payload.get("version") != REGISTRY_VERSION:
        raise PursuitTaskError(f"unsupported {REGISTRY_PATH} version")
    raw_tasks = payload.get("tasks", [])
    raw_reconciliations = payload.get("reconciliations", [])
    if not isinstance(raw_tasks, list) or not isinstance(raw_reconciliations, list):
        raise PursuitTaskError(f"invalid {REGISTRY_PATH} table layout")
    tasks = [_task_from_toml(item) for item in raw_tasks]
    reconciliations = [_reconciliation_from_toml(item) for item in raw_reconciliations]
    registry = TaskRegistry(tasks, reconciliations)
    registry.validate(root)
    return registry


def list_tasks(memory_root: Path, pursuit_id: str | None = None) -> list[TaskRecord]:
    tasks = load_registry(memory_root).tasks
    if pursuit_id is not None:
        tasks = [task for task in tasks if pursuit_id in task.pursuit_ids]
    return sorted(tasks, key=lambda task: (task.updated_at, task.task_id), reverse=True)


def list_reconciliations(memory_root: Path, *, status: str | None = None) -> list[ReconciliationRecord]:
    records = load_registry(memory_root).reconciliations
    if status is not None:
        records = [record for record in records if record.status == status]
    return sorted(records, key=lambda record: (record.updated_at, record.reconciliation_id), reverse=True)


def link_task(
    memory_root: Path,
    *,
    pursuit_ids: Iterable[str],
    provider: str,
    thread_id: str,
    title: str,
    project: str | None = None,
    host: str | None = None,
    status: str = "active",
    expected_revision: str | None = None,
) -> TaskRecord:
    root = Path(memory_root).expanduser().resolve()
    clean_pursuits = _clean_pursuit_ids(pursuit_ids)
    clean_provider = _required_text(provider, "provider")
    clean_thread = _required_text(thread_id, "thread id")
    clean_title = _required_text(title, "title")
    with MemoryWriteLock(root):
        _check_registry_revision(root, expected_revision)
        registry = load_registry(root)
        existing = next(
            (
                task
                for task in registry.tasks
                if task.provider.casefold() == clean_provider.casefold() and task.thread_id == clean_thread
            ),
            None,
        )
        now = datetime.now(UTC).isoformat()
        if existing is not None:
            existing.pursuit_ids = list(dict.fromkeys([*existing.pursuit_ids, *clean_pursuits]))
            existing.title = clean_title
            existing.project = _clean_optional(project) or existing.project
            existing.host = _clean_optional(host) or existing.host
            existing.status = status
            existing.updated_at = now
            record = existing
        else:
            record = TaskRecord(
                task_id=_new_id("task"),
                provider=clean_provider,
                pursuit_ids=clean_pursuits,
                title=clean_title,
                status=status,
                thread_id=clean_thread,
                project=_clean_optional(project),
                host=_clean_optional(host) or socket.gethostname(),
                created_at=now,
                updated_at=now,
            )
            registry.tasks.append(record)
        _save_registry(root, registry)
        return record


def link_current_codex_task(
    memory_root: Path,
    *,
    pursuit_ids: Iterable[str],
    title: str,
    project: str | None = None,
    status: str = "active",
) -> TaskRecord:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise PursuitTaskError("CODEX_THREAD_ID is not available in this agent environment")
    return link_task(
        memory_root,
        pursuit_ids=pursuit_ids,
        provider="codex",
        thread_id=thread_id,
        title=title,
        project=project,
        status=status,
    )


def plan_task(
    memory_root: Path,
    *,
    pursuit_id: str,
    action: str | None = None,
    title: str | None = None,
    project: str | None = None,
    host: str | None = None,
    provider: str = "codex",
) -> TaskRecord:
    root = Path(memory_root).expanduser().resolve()
    pursuit_id = validate_item_id(pursuit_id.strip())
    prompt, resolved_action, resolved_title = build_task_prompt(
        root,
        pursuit_id=pursuit_id,
        action=action,
        title=title,
        project=project,
    )
    now = datetime.now(UTC).isoformat()
    record = TaskRecord(
        task_id=_new_id("task"),
        provider=_required_text(provider, "provider"),
        pursuit_ids=[pursuit_id],
        title=resolved_title,
        status="planned",
        project=_clean_optional(project),
        host=_clean_optional(host) or socket.gethostname(),
        action=resolved_action,
        prompt=prompt,
        created_at=now,
        updated_at=now,
    )
    with MemoryWriteLock(root):
        registry = load_registry(root)
        duplicate = next(
            (
                task
                for task in registry.tasks
                if task.status in {"planned", "active"}
                and pursuit_id in task.pursuit_ids
                and (task.action or "").casefold() == resolved_action.casefold()
            ),
            None,
        )
        if duplicate is not None:
            return duplicate
        registry.tasks.append(record)
        _save_registry(root, registry)
    return record


def run_task(
    memory_root: Path,
    task_id: str,
    *,
    project: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    sandbox: str = "workspace-write",
    runner: CodexSdkRunner | None = None,
) -> TaskRecord:
    root = Path(memory_root).expanduser().resolve()
    task = _claim_task_for_run(root, task_id, project=_clean_optional(project))
    cwd_value = _clean_optional(project) or task.project
    if not cwd_value:
        _mutate_task(root, task_id, status="failed", error="Codex task requires a project path")
        raise PursuitTaskError("Codex task requires a project path")
    cwd = Path(cwd_value).expanduser().resolve()
    if not cwd.is_dir():
        message = f"Codex task project does not exist: {cwd}"
        _mutate_task(root, task_id, status="failed", error=message)
        raise PursuitTaskError(message)
    prompt = task.prompt or build_task_prompt(
        root,
        pursuit_id=task.pursuit_ids[0],
        action=task.action,
        title=task.title,
        project=str(cwd),
    )[0]
    owns_runner = runner is None
    selected_runner = runner or CodexSdkRunner()

    def thread_started(thread_id: str) -> None:
        _mutate_task(root, task_id, thread_id=thread_id, provider="codex", status="active")

    try:
        result = selected_runner.run_turn(
            prompt=prompt,
            provider_session_id=None,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox=sandbox,
            on_thread_started=thread_started,
        )
    except Exception as exc:
        _mutate_task(root, task_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if owns_runner:
            selected_runner.close()
    return _mutate_task(
        root,
        task_id,
        thread_id=result.provider_session_id,
        provider="codex",
        status="completed",
        result=result.text,
        error=None,
        prompt=prompt,
        project=str(cwd),
    )


def update_task(
    memory_root: Path,
    task_id: str,
    *,
    status: str | None = None,
    result: str | None = None,
    error: str | None = None,
    title: str | None = None,
) -> TaskRecord:
    fields: dict[str, Any] = {}
    if status is not None:
        if status not in TASK_STATUSES:
            raise PursuitTaskError(f"invalid task status: {status}")
        fields["status"] = status
    if result is not None:
        fields["result"] = result.strip()
    if error is not None:
        fields["error"] = error.strip()
    if title is not None:
        fields["title"] = _required_text(title, "title")
    return _mutate_task(Path(memory_root).expanduser().resolve(), task_id, **fields)


def unlink_task(memory_root: Path, task_id: str, pursuit_id: str | None = None) -> None:
    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        registry = load_registry(root)
        task = registry.task(task_id)
        if pursuit_id is None:
            registry.tasks = [item for item in registry.tasks if item.task_id != task_id]
            registry.reconciliations = [
                item for item in registry.reconciliations if item.task_id != task_id
            ]
        else:
            task.pursuit_ids = [item for item in task.pursuit_ids if item != pursuit_id]
            if not task.pursuit_ids:
                registry.tasks = [item for item in registry.tasks if item.task_id != task_id]
                registry.reconciliations = [
                    item for item in registry.reconciliations if item.task_id != task_id
                ]
            else:
                task.updated_at = datetime.now(UTC).isoformat()
        _save_registry(root, registry)


def detach_pursuit(memory_root: Path, pursuit_id: str) -> None:
    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        registry = load_registry(root)
        removed_task_ids: set[str] = set()
        retained: list[TaskRecord] = []
        for task in registry.tasks:
            task.pursuit_ids = [item for item in task.pursuit_ids if item != pursuit_id]
            if task.pursuit_ids:
                retained.append(task)
            else:
                removed_task_ids.add(task.task_id)
        registry.tasks = retained
        registry.reconciliations = [
            item for item in registry.reconciliations if item.task_id not in removed_task_ids
        ]
        _save_registry(root, registry)


def propose_reconciliation(
    memory_root: Path,
    *,
    task_id: str,
    summary: str,
    operations: Iterable[dict[str, Any]],
    expected_revision: str | None = None,
) -> ReconciliationRecord:
    from .pursuit_workspace import PursuitEditor, preview_operations

    root = Path(memory_root).expanduser().resolve()
    operation_list = [dict(item) for item in operations]
    base_revision = expected_revision or PursuitEditor(root).revision()
    preview_operations(root, operation_list, expected_revision=base_revision)
    now = datetime.now(UTC).isoformat()
    record = ReconciliationRecord(
        reconciliation_id=_new_id("recon"),
        task_id=task_id,
        summary=_required_text(summary, "summary"),
        operations=operation_list,
        base_revision=base_revision,
        created_at=now,
        updated_at=now,
    )
    with MemoryWriteLock(root):
        registry = load_registry(root)
        registry.task(task_id)
        existing = next(
            (
                item
                for item in registry.reconciliations
                if item.task_id == task_id and item.status == "pending"
            ),
            None,
        )
        if existing is not None:
            existing.summary = record.summary
            existing.operations = record.operations
            existing.base_revision = record.base_revision
            existing.updated_at = now
            record = existing
        else:
            registry.reconciliations.append(record)
        _save_registry(root, registry)
    return record


def apply_reconciliation(
    memory_root: Path,
    reconciliation_id: str,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    from .pursuit_workspace import (
        PursuitEditor,
        PursuitWorkspaceError,
        _commit_files,
        _record_history,
        _require_clean_git_paths,
        _write_file_transaction,
        preview_operations,
    )

    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        registry = load_registry(root)
        reconciliation = registry.reconciliation(reconciliation_id)
        if reconciliation.status != "pending":
            raise PursuitTaskError("only pending reconciliation can be applied")
        for operation in reconciliation.operations:
            if operation.get("op") == "delete":
                raise PursuitTaskError(
                    "reconciliation cannot delete a Pursuit while task history is linked; "
                    "resolve task links and remove the Pursuit explicitly"
                )

        preview = preview_operations(
            root,
            reconciliation.operations,
            expected_revision=reconciliation.base_revision,
        )
        before = PursuitEditor(root)._current_pursuit_files()
        if commit:
            _require_clean_git_paths(root, (*preview.changed_files, *preview.removed_files))
        registry_path = root / REGISTRY_PATH
        registry_before = registry_path.read_bytes() if registry_path.is_file() else None
        gitignore_path = root / ".gitignore"
        gitignore_before = gitignore_path.read_bytes() if gitignore_path.is_file() else None

        try:
            _write_file_transaction(root, preview.files)
            actual = PursuitEditor(root)
            if actual.revision() != preview.candidate_revision:
                raise PursuitWorkspaceError(
                    "written Pursuit files did not match the validated reconciliation"
                )
            reconciliation.status = "applied"
            reconciliation.updated_at = datetime.now(UTC).isoformat()
            _save_registry(root, registry)
            _record_history(
                root,
                before,
                preview.files,
                preview.revision,
                preview.candidate_revision,
            )
        except Exception:
            _write_file_transaction(root, before)
            _restore_optional_file(registry_path, registry_before)
            _restore_optional_file(gitignore_path, gitignore_before)
            raise

        commit_paths: list[str] = [*preview.changed_files, *preview.removed_files, REGISTRY_PATH.as_posix()]
        if gitignore_before != (gitignore_path.read_bytes() if gitignore_path.is_file() else None):
            commit_paths.append(".gitignore")
        commit_sha = (
            _commit_files(root, commit_paths, f"pursuit: reconcile task {reconciliation.task_id}")
            if commit
            else None
        )
        return {
            "reconciliation": reconciliation.to_json(),
            "apply": {
                "revision": actual.revision(),
                "changed_files": list(preview.changed_files),
                "removed_files": list(preview.removed_files),
                "commit": commit_sha,
                "snapshot": actual.snapshot(),
            },
        }


def dismiss_reconciliation(memory_root: Path, reconciliation_id: str) -> ReconciliationRecord:
    root = Path(memory_root).expanduser().resolve()
    with MemoryWriteLock(root):
        registry = load_registry(root)
        record = registry.reconciliation(reconciliation_id)
        record.status = "dismissed"
        record.updated_at = datetime.now(UTC).isoformat()
        _save_registry(root, registry)
        return record


def build_task_prompt(
    memory_root: Path,
    *,
    pursuit_id: str,
    action: str | None = None,
    title: str | None = None,
    project: str | None = None,
) -> tuple[str, str, str]:
    from .pursuit_workspace import PursuitEditor

    root = Path(memory_root).expanduser().resolve()
    editor = PursuitEditor(root)
    node = editor.get_node(pursuit_id)
    resolved_action = _clean_optional(action)
    if resolved_action is None and node.body.next:
        resolved_action = node.body.next[0].text
    if resolved_action is None:
        raise PursuitTaskError("task creation requires an action or a Pursuit Next item")
    resolved_title = _clean_optional(title) or f"{node.title}: {resolved_action[:80]}"
    ancestor_ids = editor.ancestor_ids(pursuit_id)
    ancestor_lines: list[str] = []
    for ancestor_id in ancestor_ids:
        ancestor = editor.get_node(ancestor_id)
        ancestor_lines.append(f"- {ancestor.title} (`{ancestor_id}`): {ancestor.body.objective}")
    next_lines = "\n".join(f"- `{item.kind}` {item.text}" for item in node.body.next) or "- none recorded"
    memory_chunks: list[str] = []
    for context_id in [*ancestor_ids, pursuit_id]:
        context = _direct_memory_context(editor.manifest, context_id)
        if context and context not in memory_chunks:
            memory_chunks.append(context)
    memory_context = "\n\n".join(memory_chunks)
    project_line = _clean_optional(project) or "Use the project path supplied by the task runner."
    prompt = f"""You are working on one concrete execution task linked to a RightMemory Pursuit.

Pursuit
- id: {pursuit_id}
- title: {node.title}
- objective: {node.body.objective or '(not stated)'}
- current state: {node.body.state or '(not stated)'}
- done when: {node.body.done_when or '(not stated)'}
- project: {project_line}

Ancestor context
{chr(10).join(ancestor_lines) if ancestor_lines else '- none'}

Current movement
{next_lines}

Exact task
{resolved_action}

Relevant durable Memory
{memory_context or '- none directly linked'}

Work in the supplied project and verify the current repository state before changing it. Keep the implementation scoped to the exact task rather than treating the Pursuit as permission to do every possible follow-up. At the end, report what changed, verification performed, unresolved issues, and the smallest justified update to the Pursuit's State or Next. Do not mark the Pursuit complete merely because this task finishes.
"""
    return prompt.strip() + "\n", resolved_action, resolved_title


def _direct_memory_context(manifest: Any, pursuit_id: str, limit: int = 8000) -> str:
    item = manifest.items.get(pursuit_id)
    if item is None:
        return ""
    chunks: list[str] = []
    for edge_type, target in item.edges:
        target_item = manifest.items.get(target)
        if target_item is None or target_item.family != "memory" or target_item.block_key is None:
            continue
        text = _flatten_block(manifest, target_item.block_key).strip()
        if text:
            chunks.append(f"[{edge_type}:{target}]\n{text}")
    value = "\n\n".join(chunks)
    return value if len(value) <= limit else value[:limit] + "\n...[truncated]"


def _flatten_block(manifest: Any, key: Any) -> str:
    block = manifest.blocks[key]
    lines = [block.line] if block.kind != "root" else []
    for part in block.logical_parts:
        if isinstance(part, tuple):
            lines.append(_flatten_block(manifest, part))
        else:
            lines.append(part)
    return "\n".join(lines)



def _claim_task_for_run(root: Path, task_id: str, *, project: str | None) -> TaskRecord:
    with MemoryWriteLock(root):
        registry = load_registry(root)
        task = registry.task(task_id)
        if task.status != "planned":
            raise PursuitTaskError("only a planned task can be started")
        if task.provider.casefold() != "codex":
            raise PursuitTaskError("only Codex tasks can be started by this runner")
        task.status = "active"
        if project is not None:
            task.project = project
        task.updated_at = datetime.now(UTC).isoformat()
        _save_registry(root, registry)
        return task


def _mutate_task(root: Path, task_id: str, **changes: Any) -> TaskRecord:
    with MemoryWriteLock(root):
        registry = load_registry(root)
        task = registry.task(task_id)
        for key, value in changes.items():
            if not hasattr(task, key):
                raise PursuitTaskError(f"unknown task field: {key}")
            if value is not None or key in {"error", "result"}:
                setattr(task, key, value)
        task.updated_at = datetime.now(UTC).isoformat()
        _save_registry(root, registry)
        return task


def _save_registry(root: Path, registry: TaskRegistry) -> None:
    registry.validate(root)
    _ensure_registry_allowed(root)
    path = root / REGISTRY_PATH
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PursuitTaskError(f"{REGISTRY_PATH} must be a regular file")
    _atomic_write_bytes(path, _registry_to_toml(registry).encode("utf-8"))


def _ensure_registry_allowed(root: Path) -> None:
    gitignore = root / ".gitignore"
    if gitignore.is_symlink() or (gitignore.exists() and not gitignore.is_file()):
        raise PursuitTaskError(".gitignore must be a regular file")
    if not gitignore.is_file():
        return
    text = gitignore.read_text(encoding="utf-8")
    line = f"!{REGISTRY_PATH.as_posix()}"
    if any(item.strip() == line for item in text.splitlines()):
        return
    suffix = "" if text.endswith("\n") or not text else "\n"
    updated = text + suffix + line + "\n"
    _atomic_write_bytes(gitignore, updated.encode("utf-8"))



def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_optional_file(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(path, data)


def _registry_to_toml(registry: TaskRegistry) -> str:
    lines = [f"version = {REGISTRY_VERSION}", ""]
    for task in registry.tasks:
        lines.append("[[tasks]]")
        _append_toml(lines, "task_id", task.task_id)
        _append_toml(lines, "provider", task.provider)
        _append_toml_array(lines, "pursuit_ids", task.pursuit_ids)
        _append_toml(lines, "title", task.title)
        _append_toml(lines, "status", task.status)
        for key in ("thread_id", "project", "host", "action", "prompt", "result", "error"):
            value = getattr(task, key)
            if value is not None:
                _append_toml(lines, key, value)
        _append_toml(lines, "created_at", task.created_at)
        _append_toml(lines, "updated_at", task.updated_at)
        lines.append("")
    for reconciliation in registry.reconciliations:
        lines.append("[[reconciliations]]")
        _append_toml(lines, "reconciliation_id", reconciliation.reconciliation_id)
        _append_toml(lines, "task_id", reconciliation.task_id)
        _append_toml(lines, "summary", reconciliation.summary)
        _append_toml(lines, "operations_json", json.dumps(reconciliation.operations, ensure_ascii=False, separators=(",", ":")))
        _append_toml(lines, "base_revision", reconciliation.base_revision)
        _append_toml(lines, "status", reconciliation.status)
        _append_toml(lines, "created_at", reconciliation.created_at)
        _append_toml(lines, "updated_at", reconciliation.updated_at)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _append_toml(lines: list[str], key: str, value: str) -> None:
    lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")


def _append_toml_array(lines: list[str], key: str, values: list[str]) -> None:
    lines.append(f"{key} = [" + ", ".join(json.dumps(item, ensure_ascii=False) for item in values) + "]")


def _task_from_toml(value: object) -> TaskRecord:
    if not isinstance(value, dict):
        raise PursuitTaskError("task registry entry must be a table")
    pursuit_ids = value.get("pursuit_ids")
    if not isinstance(pursuit_ids, list) or not all(isinstance(item, str) for item in pursuit_ids):
        raise PursuitTaskError("task pursuit_ids must be a list of strings")
    return TaskRecord(
        task_id=_table_string(value, "task_id"),
        provider=_table_string(value, "provider"),
        pursuit_ids=list(pursuit_ids),
        title=_table_string(value, "title"),
        status=_table_string(value, "status"),
        thread_id=_table_optional_string(value, "thread_id"),
        project=_table_optional_string(value, "project"),
        host=_table_optional_string(value, "host"),
        action=_table_optional_string(value, "action"),
        prompt=_table_optional_string(value, "prompt"),
        result=_table_optional_string(value, "result"),
        error=_table_optional_string(value, "error"),
        created_at=_table_string(value, "created_at"),
        updated_at=_table_string(value, "updated_at"),
    )


def _reconciliation_from_toml(value: object) -> ReconciliationRecord:
    if not isinstance(value, dict):
        raise PursuitTaskError("reconciliation registry entry must be a table")
    raw_operations = _table_string(value, "operations_json")
    try:
        operations = json.loads(raw_operations)
    except json.JSONDecodeError as exc:
        raise PursuitTaskError("reconciliation operations_json is invalid") from exc
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise PursuitTaskError("reconciliation operations must be a list of objects")
    return ReconciliationRecord(
        reconciliation_id=_table_string(value, "reconciliation_id"),
        task_id=_table_string(value, "task_id"),
        summary=_table_string(value, "summary"),
        operations=operations,
        base_revision=_table_string(value, "base_revision"),
        status=_table_string(value, "status"),
        created_at=_table_string(value, "created_at"),
        updated_at=_table_string(value, "updated_at"),
    )


def _table_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise PursuitTaskError(f"registry field {key} must be a non-empty string")
    return item


def _table_optional_string(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise PursuitTaskError(f"registry field {key} must be a string")
    return item


def _check_registry_revision(root: Path, expected: str | None) -> None:
    if expected is not None and registry_revision(root) != expected:
        raise PursuitTaskError("Pursuit task registry changed since it was loaded")


def _clean_pursuit_ids(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = validate_item_id(_required_text(value, "Pursuit id"))
        if item not in result:
            result.append(item)
    if not result:
        raise PursuitTaskError("at least one Pursuit id is required")
    return result


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PursuitTaskError(f"{label} must be a non-empty string")
    return value.strip()


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PursuitTaskError("optional text value must be a string")
    return value.strip() or None


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"
