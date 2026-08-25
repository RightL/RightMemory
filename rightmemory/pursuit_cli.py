from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .pursuit_tasks import (
    apply_reconciliation,
    dismiss_reconciliation,
    link_current_codex_task,
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
    apply_operations,
    preview_operations,
    redo,
    undo,
)


def pursuit_main(memory_root: Path, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rightmemory pursuit",
        description="Edit live Pursuit as a Markdown-backed map and coordinate linked agent tasks.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    studio = subparsers.add_parser("studio", help="launch the editable local Pursuit Studio")
    studio.add_argument("--host", default="127.0.0.1")
    studio.add_argument("--port", type=int, default=8767)
    studio.add_argument("--no-open", action="store_true")

    show = subparsers.add_parser("show", help="show the Pursuit workspace")
    show.add_argument("id", nargs="?")
    show.add_argument("--json", action="store_true")

    preview = subparsers.add_parser("preview", help="validate and preview structured operations")
    _add_operations_arguments(preview)

    apply_parser = subparsers.add_parser("apply", help="apply structured operations")
    _add_operations_arguments(apply_parser)
    apply_parser.add_argument("--commit", action="store_true")

    create = subparsers.add_parser("create", help="create a Pursuit")
    create.add_argument("id")
    create.add_argument("--title", required=True)
    create.add_argument("--parent")
    create.add_argument("--objective", default="")
    create.add_argument("--state", default="")
    create.add_argument("--next", action="append", default=[])
    create.add_argument("--done-when", default="")
    create.add_argument("--status", choices=("active", "parked"), default="active")
    create.add_argument("--edge", action="append", default=[])
    create.add_argument("--index", type=int)
    create.add_argument("--commit", action="store_true")

    edit = subparsers.add_parser("edit", help="edit canonical Pursuit fields")
    edit.add_argument("id")
    edit.add_argument("--title")
    edit.add_argument("--objective")
    edit.add_argument("--state")
    edit.add_argument("--next", action="append")
    edit.add_argument("--done-when")
    edit.add_argument("--status", choices=("active", "parked"))
    edit.add_argument("--edge", action="append")
    edit.add_argument("--commit", action="store_true")

    move = subparsers.add_parser("move", help="move a Pursuit subtree")
    move.add_argument("id")
    move.add_argument("--parent")
    move.add_argument("--index", type=int)
    move.add_argument("--commit", action="store_true")

    reorder = subparsers.add_parser("reorder", help="change a Pursuit's sibling order")
    reorder.add_argument("id")
    reorder.add_argument("index", type=int)
    reorder.add_argument("--commit", action="store_true")

    delete = subparsers.add_parser("delete", help="remove a Pursuit from the live tree")
    delete.add_argument("id")
    delete.add_argument("--cascade", action="store_true")
    delete.add_argument("--commit", action="store_true")

    focus = subparsers.add_parser("focus", help="replace the ordered Focus list")
    focus.add_argument("ids", nargs="*")
    focus.add_argument("--commit", action="store_true")

    for command, help_text in (
        ("park", "park a Pursuit"),
        ("unpark", "return a Pursuit to active state"),
        ("split", "move a Pursuit's children into PURSUIT_<id>.md"),
        ("inline", "move an F# Pursuit's children back inline"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("id")
        child.add_argument("--commit", action="store_true")

    undo_parser = subparsers.add_parser("undo", help="undo the latest Studio edit")
    undo_parser.add_argument("--commit", action="store_true")
    redo_parser = subparsers.add_parser("redo", help="redo the latest undone Studio edit")
    redo_parser.add_argument("--commit", action="store_true")

    task = subparsers.add_parser("task", help="manage tasks linked to Pursuits")
    task_subparsers = task.add_subparsers(dest="task_command", required=True)
    task_list = task_subparsers.add_parser("list")
    task_list.add_argument("--pursuit")
    task_list.add_argument("--json", action="store_true")

    task_link = task_subparsers.add_parser("link", help="link an existing provider thread")
    task_link.add_argument("--pursuit", action="append", required=True)
    task_link.add_argument("--provider", default="codex")
    thread_group = task_link.add_mutually_exclusive_group(required=True)
    thread_group.add_argument("--thread")
    thread_group.add_argument("--current", action="store_true")
    task_link.add_argument("--title", required=True)
    task_link.add_argument("--project")
    task_link.add_argument("--host")
    task_link.add_argument("--status", choices=("planned", "active", "completed", "failed", "cancelled"), default="active")

    task_plan = task_subparsers.add_parser("plan", help="create a planned task from Pursuit context")
    task_plan.add_argument("--pursuit", required=True)
    task_plan.add_argument("--action")
    task_plan.add_argument("--title")
    task_plan.add_argument("--project")
    task_plan.add_argument("--host")

    task_run = task_subparsers.add_parser("run", help="start and execute a planned Codex task")
    task_run.add_argument("task_id")
    task_run.add_argument("--project")
    task_run.add_argument("--model")
    task_run.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh"))
    task_run.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="workspace-write")

    task_update = task_subparsers.add_parser("update", help="record task status or result")
    task_update.add_argument("task_id")
    task_update.add_argument("--status", choices=("planned", "active", "completed", "failed", "cancelled"))
    task_update.add_argument("--result")
    task_update.add_argument("--result-file", type=Path)
    task_update.add_argument("--error")
    task_update.add_argument("--title")

    task_unlink = task_subparsers.add_parser("unlink")
    task_unlink.add_argument("task_id")
    task_unlink.add_argument("--pursuit")

    reconcile = subparsers.add_parser("reconcile", help="review task results back into Pursuit")
    reconcile_subparsers = reconcile.add_subparsers(dest="reconcile_command", required=True)
    reconcile_list = reconcile_subparsers.add_parser("list")
    reconcile_list.add_argument("--status", choices=("pending", "applied", "dismissed"))
    reconcile_list.add_argument("--json", action="store_true")

    reconcile_propose = reconcile_subparsers.add_parser("propose")
    reconcile_propose.add_argument("task_id")
    reconcile_propose.add_argument("--summary", required=True)
    _add_operations_arguments(reconcile_propose)

    reconcile_apply = reconcile_subparsers.add_parser("apply")
    reconcile_apply.add_argument("reconciliation_id")
    reconcile_apply.add_argument("--commit", action="store_true")

    reconcile_dismiss = reconcile_subparsers.add_parser("dismiss")
    reconcile_dismiss.add_argument("reconciliation_id")

    args = parser.parse_args([] if argv is None else argv)
    root = Path(memory_root).expanduser().resolve()

    if args.command == "studio":
        from .pursuit_web import serve_pursuit_studio

        return serve_pursuit_studio(root, host=args.host, port=args.port, open_browser=not args.no_open)
    if args.command == "show":
        return _show(root, args.id, as_json=args.json)
    if args.command == "preview":
        operations = _load_operations(args)
        result = preview_operations(root, operations, expected_revision=args.revision)
        print(result.diff or "No changes.")
        return 0
    if args.command == "apply":
        result = apply_operations(
            root,
            _load_operations(args),
            expected_revision=args.revision,
            commit=args.commit,
        )
        _print_json(result.to_json())
        return 0
    if args.command == "create":
        operation = {
            "op": "create",
            "id": args.id,
            "title": args.title,
            "parent_id": args.parent,
            "objective": args.objective,
            "state": args.state,
            "next": args.next,
            "done_when": args.done_when,
            "status": args.status,
            "edges": args.edge,
            "index": args.index,
        }
        return _apply_single(root, operation, args.commit)
    if args.command == "edit":
        operation: dict[str, Any] = {"op": "update", "id": args.id}
        for argument, key in (
            (args.title, "title"),
            (args.objective, "objective"),
            (args.state, "state"),
            (args.next, "next"),
            (args.done_when, "done_when"),
            (args.status, "status"),
            (args.edge, "edges"),
        ):
            if argument is not None:
                operation[key] = argument
        return _apply_single(root, operation, args.commit)
    if args.command == "move":
        return _apply_single(root, {"op": "move", "id": args.id, "parent_id": args.parent, "index": args.index}, args.commit)
    if args.command == "reorder":
        return _apply_single(root, {"op": "reorder", "id": args.id, "index": args.index}, args.commit)
    if args.command == "delete":
        linked = list_tasks(root, args.id)
        if linked:
            ids = ", ".join(task.task_id for task in linked)
            raise ValueError(f"unlink Pursuit {args.id} from tasks before deletion: {ids}")
        return _apply_single(root, {"op": "delete", "id": args.id, "cascade": args.cascade}, args.commit)
    if args.command == "focus":
        return _apply_single(root, {"op": "set_focus", "ids": args.ids}, args.commit)
    if args.command in {"park", "unpark", "split", "inline"}:
        operation_name = {"split": "split_file", "inline": "inline_file"}.get(args.command, args.command)
        return _apply_single(root, {"op": operation_name, "id": args.id}, args.commit)
    if args.command == "undo":
        _print_json(undo(root, commit=args.commit).to_json())
        return 0
    if args.command == "redo":
        _print_json(redo(root, commit=args.commit).to_json())
        return 0
    if args.command == "task":
        return _task_command(root, args)
    if args.command == "reconcile":
        return _reconcile_command(root, args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _show(root: Path, item_id: str | None, *, as_json: bool) -> int:
    snapshot = PursuitEditor(root).snapshot()
    if item_id is None:
        if as_json:
            _print_json(snapshot)
            return 0
        by_id = {node["id"]: node for node in snapshot["nodes"]}
        for root_id in snapshot["roots"]:
            _print_tree(root_id, by_id, prefix="")
        return 0
    node = next((node for node in snapshot["nodes"] if node["id"] == item_id), None)
    if node is None:
        raise ValueError(f"unknown Pursuit id: {item_id}")
    if as_json:
        _print_json(node)
    else:
        print(f"{node['title']} (`{node['id']}`)")
        print(node["objective"] or "(no objective)")
        if node["state"]:
            print(f"State: {node['state']}")
        for movement in node["next"]:
            print(f"Next {movement['kind']}: {movement['text']}")
        if node["done_when"]:
            print(f"Done when: {node['done_when']}")
    return 0


def _print_tree(item_id: str, by_id: dict[str, dict[str, Any]], prefix: str) -> None:
    node = by_id[item_id]
    markers = []
    if node["focused"]:
        markers.append("focus")
    if node["parked"]:
        markers.append("parked")
    suffix = f" [{', '.join(markers)}]" if markers else ""
    print(f"{prefix}- {node['title']} (`{item_id}`){suffix}")
    for child_id in node["children"]:
        if child_id in by_id:
            _print_tree(child_id, by_id, prefix + "  ")


def _apply_single(root: Path, operation: dict[str, Any], commit: bool) -> int:
    result = apply_operations(root, [operation], commit=commit)
    _print_json(result.to_json())
    return 0


def _task_command(root: Path, args: argparse.Namespace) -> int:
    if args.task_command == "list":
        tasks = list_tasks(root, args.pursuit)
        if args.json:
            _print_json({"revision": registry_revision(root), "tasks": [task.to_json() for task in tasks]})
        else:
            for task in tasks:
                thread = f" thread={task.thread_id}" if task.thread_id else ""
                print(f"{task.task_id}\t{task.status}\t{task.title}{thread}\tpursuits={','.join(task.pursuit_ids)}")
        return 0
    if args.task_command == "link":
        if args.current:
            record = link_current_codex_task(
                root,
                pursuit_ids=args.pursuit,
                title=args.title,
                project=args.project,
                status=args.status,
            )
        else:
            record = link_task(
                root,
                pursuit_ids=args.pursuit,
                provider=args.provider,
                thread_id=args.thread,
                title=args.title,
                project=args.project,
                host=args.host,
                status=args.status,
            )
        _print_json(record.to_json())
        return 0
    if args.task_command == "plan":
        record = plan_task(
            root,
            pursuit_id=args.pursuit,
            action=args.action,
            title=args.title,
            project=args.project,
            host=args.host,
        )
        _print_json(record.to_json())
        return 0
    if args.task_command == "run":
        record = run_task(
            root,
            args.task_id,
            project=args.project,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            sandbox=args.sandbox,
        )
        _print_json(record.to_json())
        return 0
    if args.task_command == "update":
        result = args.result
        if args.result_file is not None:
            result = args.result_file.read_text(encoding="utf-8")
        record = update_task(
            root,
            args.task_id,
            status=args.status,
            result=result,
            error=args.error,
            title=args.title,
        )
        _print_json(record.to_json())
        return 0
    if args.task_command == "unlink":
        unlink_task(root, args.task_id, args.pursuit)
        print("unlinked")
        return 0
    raise ValueError(f"unknown task command: {args.task_command}")


def _reconcile_command(root: Path, args: argparse.Namespace) -> int:
    if args.reconcile_command == "list":
        records = list_reconciliations(root, status=args.status)
        if args.json:
            _print_json({"reconciliations": [record.to_json() for record in records]})
        else:
            for record in records:
                print(f"{record.reconciliation_id}\t{record.status}\t{record.task_id}\t{record.summary}")
        return 0
    if args.reconcile_command == "propose":
        record = propose_reconciliation(
            root,
            task_id=args.task_id,
            summary=args.summary,
            operations=_load_operations(args),
            expected_revision=args.revision,
        )
        _print_json(record.to_json())
        return 0
    if args.reconcile_command == "apply":
        _print_json(apply_reconciliation(root, args.reconciliation_id, commit=args.commit))
        return 0
    if args.reconcile_command == "dismiss":
        _print_json(dismiss_reconciliation(root, args.reconciliation_id).to_json())
        return 0
    raise ValueError(f"unknown reconcile command: {args.reconcile_command}")


def _add_operations_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--operations-json")
    source.add_argument("--operations-file", type=Path)
    parser.add_argument("--revision")


def _load_operations(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.operations_file is not None:
        raw = args.operations_file.read_text(encoding="utf-8")
    else:
        raw = args.operations_json
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid operations JSON: {exc}") from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("operations JSON must be an object or list of objects")
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    print("Use `rightmemory pursuit ...` through the package entry point.", file=sys.stderr)
    raise SystemExit(2)
