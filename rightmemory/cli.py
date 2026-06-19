from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import secrets
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

import uvicorn

from .async_update import AsyncUpdateStore, format_retry_result, format_state, manual_recovery_warning
from .config import (
    ROLES,
    default_memory_root,
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_insight_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
from .dreamer_trigger import DreamerTriggerStore
from .doctor import format_doctor_report, run_agent_cli_doctor
from .hub.app import create_hub_app
from .hub.client import HubClientError
from .hub.store import DEFAULT_PUBLIC_BASE_URL, HubStore
from .insight_trigger import InsightTriggerStore
from .profiles import (
    ProfileError,
    create_profile,
    load_profiles,
    remove_profile,
    resolve_memory_root,
    validate_profile_name,
)
from .review import ReviewScanner, normalize_transcript
from .runtime import RightMemoryRuntime
from .session import MemoryWriteLock
from .shared_view_builder import refresh_file_view, run_file_view_builder, run_question_view_builder
from .shared_view_files import approve_file_view, invite_file_view, pull_all_file_views, pull_file_view
from .shared_view_questions import approve_question_view, ask_question_view, publish_question_view
from .shared_views import (
    accept_http_shared_view_invitation,
    accept_shared_view_invitation,
    list_http_shared_view_inbox,
    list_shared_view_inbox,
    list_shared_view_notes,
    load_connections,
    record_shared_view_note,
    save_shared_view_credential,
    shared_view_connection_status,
)
from .share_results import format_share_operation_result
from .shares import approve_share, create_share, join_share, list_shares, publish_share, revise_share, share_status
from .status import collect_status, format_status_dashboard
from .sync import SyncManager
from .watch import (
    MANAGED_WATCH_TARGETS,
    InstallStamp,
    ManagedWatchStatus,
    StopWatchResult,
    WatchLock,
    managed_watch_status,
    start_managed_watch,
    stop_managed_watch,
)
from .web.process import (
    format_stop_result as format_web_stop_result,
    format_web_status,
    start_web_service,
    stop_web_service,
    web_service_status,
)

DEFAULT_REVIEW_WATCH_INTERVAL_SECONDS = 2 * 60 * 60
DEFAULT_REVIEW_WATCH_RETRY_SECONDS = 60
DEFAULT_DREAMER_WATCH_RETRY_SECONDS = 60
DEFAULT_INSIGHT_WATCH_RETRY_SECONDS = 60
DEFAULT_PRUNER_WATCH_INTERVAL_SECONDS = 2 * 60 * 60
DEFAULT_PRUNER_WATCH_RETRY_SECONDS = 60
DEFAULT_SYNC_WATCH_INTERVAL_SECONDS = 60 * 60
DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_HUB_ROOT = Path("rightmemory-hub")
WATCH_REFRESH_POLL_SECONDS = 5
DREAMER_WATCH_SESSION_ID = "dreamer-watch"
_DREAMER_WATCH_SKIPPED = "skipped"
_DREAMER_WATCH_SUCCEEDED = "succeeded"
_DREAMER_WATCH_FAILED = "failed"
INSIGHT_WATCH_SESSION_ID = "insight-watch"
_INSIGHT_WATCH_SKIPPED = "skipped"
_INSIGHT_WATCH_SUCCEEDED = "succeeded"
_INSIGHT_WATCH_FAILED = "failed"
PRUNER_WATCH_SESSION_ID = "pruner-watch"
SYNC_WATCH_SESSION_ID = "sync-watch"


def _watch_failure_limit_reached(label: str, failures: int) -> bool:
    if failures < DEFAULT_WATCH_MAX_CONSECUTIVE_FAILURES:
        return False
    print(
        "rightmemory "
        f"{label} watch stopping after {failures} consecutive failed cycles; "
        "fix the error and restart the watch",
        file=sys.stderr,
        flush=True,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    profile_name, argv = _parse_global_args(argv)
    if argv and argv[0] == "profile":
        if profile_name is not None:
            raise ValueError("--profile is for runtime commands, not profile management")
        return _profile_main(argv[1:])
    if argv and argv[0] == "hub":
        if profile_name is not None:
            raise ValueError("--profile is for memory commands, not hub management")
        return _hub_main(argv[1:])
    if argv and argv[0] == "shared-view":
        active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
        return _shared_view_main(argv[1:], active.memory_root)
    if argv and argv[0] == "share":
        active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
        return _share_main(argv[1:], active.memory_root)
    if not argv:
        _top_level_parser().print_help()
        return 0
    if argv[0] in {"-h", "--help"}:
        _top_level_parser().parse_args(argv)
        return 0
    active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
    memory_root = active.memory_root
    if argv and argv[0] == "watch":
        return _watch_manager_main(argv[1:], memory_root)
    if argv and argv[0] == "web":
        return _web_main(argv[1:], memory_root)
    if argv and argv[0] == "review":
        return _review_main(argv[1:], memory_root)
    if argv and argv[0] == "sync":
        return _sync_main(argv[1:], memory_root)
    if argv and argv[0] == "doctor":
        return _doctor_main(argv[1:], memory_root)
    if argv and argv[0] == "status":
        return _status_main(argv[1:], memory_root)
    if argv and argv[0] == "prune":
        return _prune_main(argv[1:], memory_root)
    if argv and argv[0] == "history":
        return _history_main(argv[1:], memory_root)

    parser = _top_level_parser()

    args = parser.parse_args(argv[:1])
    remaining = argv[1:]
    if remaining == ["-h"] or remaining == ["--help"]:
        if args.role == "update":
            _update_parser().parse_args(remaining)
        else:
            _turn_parser(args.role).parse_args(remaining)
        return 0

    if remaining and remaining[0] == "submit":
        if args.role != "update":
            raise ValueError("submit is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _submit_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "pull":
        if args.role != "update":
            raise ValueError("pull is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _pull_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "undo":
        if args.role != "update":
            raise ValueError("undo is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _undo_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "retry":
        if args.role != "update":
            raise ValueError("retry is only supported for the update role")
        if _is_help_request(remaining[1:]):
            _retry_parser(args.role).parse_args(remaining[1:])
            return 0
    if remaining and remaining[0] == "_async-worker" and args.role != "update":
        raise ValueError("_async-worker is only supported for the update role")
    if remaining and remaining[0] == "chat" and _is_help_request(remaining[1:]):
        _chat_parser(args.role).parse_args(remaining[1:])
        return 0
    if remaining and remaining[0] == "daemon" and _is_help_request(remaining[1:]):
        _daemon_parser(args.role).parse_args(remaining[1:])
        return 0
    if remaining and remaining[0] == "watch":
        if args.role == "dreamer":
            if _is_help_request(remaining[1:]):
                _dreamer_watch_parser().parse_args(remaining[1:])
                return 0
            watch_args = _dreamer_watch_parser().parse_args(remaining[1:])
            return _dreamer_watch(watch_args.interval, watch_args.session, memory_root)
        if args.role == "insight":
            if _is_help_request(remaining[1:]):
                _insight_watch_parser().parse_args(remaining[1:])
                return 0
            watch_args = _insight_watch_parser().parse_args(remaining[1:])
            return _insight_watch(watch_args.interval, watch_args.session, memory_root)
        raise ValueError("watch is supported for dreamer and insight roles")

    config = load_config(args.role, memory_root=memory_root)
    if remaining and remaining[0] == "submit":
        submit_args = _submit_parser(args.role).parse_args(remaining[1:])
        return _submit(config.memory_root, args.role, submit_args.session, submit_args.message)
    if remaining and remaining[0] == "pull":
        pull_args = _pull_parser(args.role).parse_args(remaining[1:])
        return _pull(config.memory_root, args.role, pull_args.session)
    if remaining and remaining[0] == "undo":
        undo_args = _undo_parser(args.role).parse_args(remaining[1:])
        return _undo(config.memory_root, args.role, undo_args.session, undo_args.candidate_id)
    if remaining and remaining[0] == "retry":
        _retry_parser(args.role).parse_args(remaining[1:])
        return _retry(config.memory_root, args.role)

    runtime = RightMemoryRuntime(config)
    try:
        if remaining and remaining[0] == "_async-worker":
            return _async_worker(runtime, config.memory_root, args.role)
        if not remaining or remaining[0] == "chat":
            chat_args = _chat_parser(args.role).parse_args(remaining[1:] if remaining else [])
            return _chat(runtime, chat_args.session)
        if remaining[0] == "daemon":
            daemon_args = _daemon_parser(args.role).parse_args(remaining[1:])
            if not daemon_args.stdio_json:
                raise ValueError("daemon currently requires --stdio-json")
            return _daemon_stdio_json(runtime)
        turn_args = _turn_parser(args.role).parse_args(remaining)
        return _session_turn(runtime, turn_args.session, turn_args.message)
    finally:
        runtime.cleanup()


def _parse_global_args(argv: list[str]) -> tuple[str | None, list[str]]:
    parser = argparse.ArgumentParser(prog="rightmemory", add_help=False)
    parser.add_argument("--profile")
    namespace, remaining = parser.parse_known_args(argv)
    return namespace.profile, remaining


def _top_level_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory", description="RightMemory memory runtime")
    parser.add_argument("--profile", help="named memory profile for runtime commands")
    parser.add_argument("role", nargs="?", choices=tuple(sorted(ROLES)), help="RightMemory runtime role")
    return parser


def _profile_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory profile")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    create = subparsers.add_parser("create")
    create.add_argument("name")
    create.add_argument("--root", type=Path)
    show = subparsers.add_parser("show")
    show.add_argument("name")
    remove = subparsers.add_parser("remove")
    remove.add_argument("name")
    args = parser.parse_args(argv)
    home = default_memory_root()
    if args.command == "list":
        profiles = load_profiles(home)
        for name in sorted(profiles):
            print(f"{name}\t{profiles[name].root}")
        return 0
    if args.command == "create":
        profile = create_profile(home, args.name, root=args.root)
        print(f"{profile.name}\t{profile.root}")
        return 0
    if args.command == "show":
        name = validate_profile_name(args.name)
        profiles = load_profiles(home)
        if name not in profiles:
            raise ProfileError(f"profile not found: {name}. Create it with: rightmemory profile create {name}")
        print(f"{name}\t{profiles[name].root}")
        return 0
    if args.command == "remove":
        profile = remove_profile(home, args.name)
        print(f"removed {profile.name}; memory root remains at {profile.root}")
        return 0
    raise ValueError(f"unknown profile command: {args.command}")


def _resolve_hub_root(hub_root: Path | None) -> Path:
    return (hub_root or DEFAULT_HUB_ROOT).expanduser().resolve()


def _resolve_hub_revoke_args(args: argparse.Namespace) -> tuple[Path, str]:
    if args.token_id is None:
        return _resolve_hub_root(None), args.hub_root_or_token_id
    return _resolve_hub_root(Path(args.hub_root_or_token_id)), args.token_id


def _hub_init_hint(hub_root: Path) -> str:
    if hub_root == _resolve_hub_root(None):
        return "rightmemory hub init"
    return f"rightmemory hub init {hub_root}"


def _hub_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory hub")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("hub_root", nargs="?", type=Path)
    init.add_argument("--admin-token")
    init.add_argument("--public-base-url")
    status = subparsers.add_parser("status")
    status.add_argument("hub_root", nargs="?", type=Path)
    token = subparsers.add_parser("token")
    token_subparsers = token.add_subparsers(dest="token_command", required=True)
    token_list = token_subparsers.add_parser("list")
    token_list.add_argument("hub_root", nargs="?", type=Path)
    token_create = token_subparsers.add_parser("create")
    token_create.add_argument("hub_root", nargs="?", type=Path)
    token_create.add_argument("--provider", required=True)
    token_create.add_argument("--label")
    token_revoke = token_subparsers.add_parser("revoke")
    token_revoke.add_argument("hub_root_or_token_id")
    token_revoke.add_argument("token_id", nargs="?")
    serve = subparsers.add_parser("serve")
    serve.add_argument("hub_root", nargs="?", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    if args.command == "init":
        hub_root = _resolve_hub_root(args.hub_root)
        store = HubStore(hub_root)
        requested_public_base_url = args.public_base_url or DEFAULT_PUBLIC_BASE_URL
        initialized = _hub_initialized(store)
        if initialized:
            config = store.load_config()
            if args.public_base_url is not None and args.public_base_url != config.public_base_url:
                raise ValueError(
                    "hub is already initialized with public_base_url "
                    f"{config.public_base_url}; requested {args.public_base_url}. Use a new hub root."
                )
            if args.admin_token and not store.verify_token(args.admin_token, action="admin"):
                raise ValueError(
                    "hub is already initialized with a different bootstrap admin token; "
                    "token rotation is not supported by hub init"
                )
            admin_token = None
        else:
            admin_token = args.admin_token or secrets.token_urlsafe(32)
            store.initialize(admin_token=admin_token, public_base_url=requested_public_base_url)
            config = store.load_config()
            if not store.verify_token(admin_token, action="admin"):
                raise ValueError("admin token was not installed")
        print(f"hub_root\t{hub_root}")
        print("initialized\tyes")
        print(f"public_base_url\t{config.public_base_url}")
        if admin_token:
            print(f"admin_token\t{admin_token}")
        else:
            print("admin_token\tunchanged")
        return 0
    if args.command == "status":
        print(_format_hub_status(_resolve_hub_root(args.hub_root)))
        return 0
    if args.command == "token":
        return _hub_token_main(args)
    if args.command == "serve":
        hub_root = _resolve_hub_root(args.hub_root)
        if not _hub_initialized(HubStore(hub_root)):
            raise ValueError(f"hub is not initialized: {hub_root}. Run: {_hub_init_hint(hub_root)}")
        uvicorn.run(create_hub_app(hub_root), host=args.host, port=args.port)
        return 0
    raise ValueError(f"unknown hub command: {args.command}")


def _hub_token_main(args: argparse.Namespace) -> int:
    if args.token_command == "revoke":
        hub_root, token_id = _resolve_hub_revoke_args(args)
    else:
        hub_root = _resolve_hub_root(args.hub_root)
        token_id = ""
    store = HubStore(hub_root)
    if not _hub_initialized(store):
        raise ValueError(f"hub is not initialized: {hub_root}. Run: {_hub_init_hint(hub_root)}")
    if args.token_command == "create":
        token = store.create_provider_token(args.provider, label=args.label)
        print(f"token_id\t{token.token_id}")
        print(f"provider_id\t{token.provider_id}")
        print(f"action\t{token.action}")
        if token.label:
            print(f"label\t{token.label}")
        print(f"raw_token\t{token.raw_token}")
        return 0
    if args.token_command == "list":
        for token in store.list_tokens():
            revoked = token["revoked_at"] or "-"
            provider = token["provider_id"] or "-"
            view = token["view_id"] or "-"
            label = token["label"] or "-"
            print(
                "\t".join(
                    [
                        token["token_id"],
                        token["action"],
                        provider,
                        view,
                        label,
                        token["created_at"],
                        revoked,
                    ]
                )
        )
        return 0
    if args.token_command == "revoke":
        if store.revoke_token(token_id):
            print(f"revoked\t{token_id}")
            return 0
        print(f"not_found\t{token_id}")
        return 1
    raise ValueError(f"unknown hub token command: {args.token_command}")


def _format_hub_status(hub_root: Path) -> str:
    root = hub_root.expanduser().resolve()
    store = HubStore(root)
    initialized = _hub_initialized(store)
    lines = [
        f"hub_root\t{root}",
        f"initialized\t{'yes' if initialized else 'no'}",
        f"storage_root\t{store.storage_root}",
        f"storage_present\t{'yes' if store.storage_root.is_dir() else 'no'}",
    ]
    if initialized:
        config = store.load_config()
        tokens = store.list_tokens()
        lines.append(f"public_base_url\t{config.public_base_url}")
        lines.append(f"tokens\t{len(tokens)}")
        lines.append(f"active_tokens\t{sum(1 for token in tokens if not token['revoked_at'])}")
        lines.append(f"audit_events\t{len(store.list_audit_events())}")
    return "\n".join(lines)


def _hub_initialized(store: HubStore) -> bool:
    return store.db_path.is_file() and store.config_path.is_file()


def _shared_view_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory shared-view")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    build_file = subparsers.add_parser("build-file")
    build_file.add_argument("view_id")
    build_file.add_argument("intent", nargs="+")
    build_file.add_argument("--title", required=True)
    build_file.add_argument("--hub-url", required=True)
    build_file.add_argument("--credential-id", required=True)
    build_question = subparsers.add_parser("build-question")
    build_question.add_argument("view_id")
    build_question.add_argument("intent", nargs="+")
    build_question.add_argument("--title", required=True)
    refresh_file = subparsers.add_parser("refresh-file")
    refresh_file.add_argument("view_id")
    refresh_file.add_argument("--force", action="store_true")
    refresh_file.add_argument("--publish", action="store_true")
    publish_question = subparsers.add_parser("publish-question")
    publish_question.add_argument("view_id")
    publish_question.add_argument("--hub-url", required=True)
    publish_question.add_argument("--credential-id", required=True)
    publish_question.add_argument("--question-base-url", required=True)
    publish_question.add_argument("--label")
    publish_question.add_argument("--expires-at")
    invite = subparsers.add_parser("invite")
    invite.add_argument("view_id")
    invite.add_argument("--hub-url")
    invite.add_argument("--credential-id")
    invite.add_argument("--label")
    invite.add_argument("--expires-at")
    approve = subparsers.add_parser("approve")
    approve.add_argument("view_id")
    approve.add_argument("--type", choices=("file", "question"), required=True)
    pull = subparsers.add_parser("pull")
    pull.add_argument("heading_id", nargs="?")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("heading_id", nargs="?")
    ask = subparsers.add_parser("ask")
    ask.add_argument("heading_id")
    ask.add_argument("question", nargs=argparse.REMAINDER)
    credential = subparsers.add_parser("credential")
    credential_subparsers = credential.add_subparsers(dest="credential_command", required=True)
    credential_set = credential_subparsers.add_parser("set")
    credential_set.add_argument("credential_id")
    credential_set.add_argument("--kind", default="http-publish")
    token_source = credential_set.add_mutually_exclusive_group(required=True)
    token_source.add_argument("--token")
    token_source.add_argument("--token-stdin", action="store_true")
    token_source.add_argument("--token-prompt", action="store_true")
    credential_set.add_argument("--hub-url", required=True)
    credential_set.add_argument("--view-id")
    credential_set.add_argument("--provider")
    note = subparsers.add_parser("note")
    note.add_argument("heading_id")
    note.add_argument("--confirm", action="store_true")
    note.add_argument("--actor", default="user")
    note.add_argument("--task")
    note.add_argument("message", nargs="*")
    notes = subparsers.add_parser("notes")
    notes.add_argument("heading_id", nargs="?")
    inbox = subparsers.add_parser("inbox")
    inbox.add_argument("view_id", nargs="?")
    inbox_http = subparsers.add_parser("inbox-http")
    inbox_http.add_argument("--hub-url", required=True)
    inbox_http.add_argument("--credential-id", required=True)
    inbox_http.add_argument("--provider", required=True)
    accept_invite = subparsers.add_parser("accept-invite")
    accept_invite.add_argument("invitation")
    accept_invite.add_argument("--heading-id")
    accept_invite.add_argument("--title")
    accept_invite.add_argument("--body")
    accept_invite.add_argument("--relationship", choices=("human", "owned-agent", "team-space", "external"))
    if argv[:1] == ["note"]:
        args = note.parse_intermixed_args(argv[1:])
        args.command = "note"
    else:
        args = parser.parse_args(argv)
    if args.command == "list":
        for heading_id, connection in sorted(load_connections(memory_root).items()):
            maintainer = connection.maintainer or "-"
            description = connection.description or "-"
            print(f"{heading_id}\t{connection.view_type}\t{connection.relationship}\t{maintainer}\t{description}")
        return 0
    if args.command == "build-file":
        intent = " ".join(args.intent).strip()
        if not intent:
            raise ValueError("shared-view build-file requires an intent")
        print(
            run_file_view_builder(
                memory_root,
                view_id=args.view_id,
                title=args.title,
                intent=intent,
                hub_url=args.hub_url,
                credential_id=args.credential_id,
            )
        )
        return 0
    if args.command == "build-question":
        intent = " ".join(args.intent).strip()
        if not intent:
            raise ValueError("shared-view build-question requires an intent")
        print(
            run_question_view_builder(
                memory_root,
                view_id=args.view_id,
                title=args.title,
                intent=intent,
            )
        )
        return 0
    if args.command == "refresh-file":
        print(refresh_file_view(memory_root, args.view_id, force=args.force, publish=args.publish))
        return 0
    if args.command == "publish-question":
        print(
            publish_question_view(
                memory_root,
                args.view_id,
                hub_url=args.hub_url,
                credential_id=args.credential_id,
                question_base_url=args.question_base_url,
                label=args.label,
                expires_at=args.expires_at,
            )
        )
        return 0
    if args.command == "invite":
        print(
            invite_file_view(
                memory_root,
                args.view_id,
                hub_url=args.hub_url,
                credential_id=args.credential_id,
                label=args.label,
                expires_at=args.expires_at,
            )
        )
        return 0
    if args.command == "approve":
        if args.type == "file":
            print(approve_file_view(memory_root, args.view_id))
        else:
            print(approve_question_view(memory_root, args.view_id))
        return 0
    if args.command == "pull":
        if args.heading_id:
            result = pull_file_view(memory_root, args.heading_id)
            print(result.message)
            return 0 if result.status in {"pulled", "stale"} else 1
        exit_code = 0
        for result in pull_all_file_views(memory_root):
            print(f"{result.heading_id}\t{result.status}\t{result.message}")
            if result.status == "unavailable":
                exit_code = 1
        return exit_code
    if args.command == "status":
        if args.heading_id:
            item = shared_view_connection_status(memory_root, args.heading_id)
            print(_format_shared_view_status(item))
            return 0 if item.get("status") != "unavailable" else 1
        for heading_id in sorted(load_connections(memory_root)):
            print(_format_shared_view_status(shared_view_connection_status(memory_root, heading_id)))
        return 0
    if args.command == "ask":
        question = " ".join(args.question).strip()
        if not question:
            raise ValueError("shared-view ask requires a question")
        print(ask_question_view(memory_root, args.heading_id, question), end="")
        return 0
    if args.command == "credential":
        if args.credential_command == "set":
            save_shared_view_credential(
                memory_root,
                args.credential_id,
                kind=args.kind,
                token=_shared_view_credential_token(args),
                base_url=args.hub_url,
                view_id=args.view_id,
                provider_id=args.provider,
            )
            print(f"saved shared view credential {args.credential_id}")
            return 0
        raise ValueError(f"unknown shared-view credential command: {args.credential_command}")
    if args.command == "note":
        message = " ".join(args.message).strip()
        if not message:
            raise ValueError("shared-view note requires a message")
        print(
            record_shared_view_note(
                memory_root,
                args.heading_id,
                message,
                confirmed=args.confirm,
                actor=args.actor,
                task_context=args.task,
            )
        )
        return 0
    if args.command == "notes":
        for record in list_shared_view_notes(memory_root, args.heading_id):
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "inbox":
        for record in list_shared_view_inbox(memory_root, args.view_id):
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "inbox-http":
        for record in list_http_shared_view_inbox(
            memory_root,
            hub_url=args.hub_url,
            credential_id=args.credential_id,
            provider_id=args.provider,
        ):
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "accept-invite":
        with MemoryWriteLock(memory_root):
            print(
                _accept_shared_view_invitation_from_cli(
                    memory_root,
                    args.invitation,
                    heading_id=args.heading_id,
                    title=args.title,
                    body=args.body,
                    relationship=args.relationship,
                )
            )
        return 0
    raise ValueError(f"unknown shared-view command: {args.command}")


def _share_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory share")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("share_id")
    create.add_argument("--title")
    create.add_argument("--provider", required=True)
    create.add_argument("--hub-url")
    create.add_argument("--credential-id")
    create.add_argument("--git")
    create.add_argument("--branch")
    create.add_argument("--request")
    create.add_argument("--capability", choices=("auto", "file-context", "live-questions", "both"), default="auto")
    create.add_argument("--file")
    create.add_argument("--question")
    create.add_argument("--question-base-url")
    revise = subparsers.add_parser("revise")
    revise.add_argument("share_id")
    revise.add_argument("--capability", choices=("auto", "file-context", "live-questions", "both"))
    revise.add_argument("--question-base-url")
    revise.add_argument("revision", nargs="+")
    approve = subparsers.add_parser("approve")
    approve.add_argument("share_id")
    publish = subparsers.add_parser("publish")
    publish.add_argument("share_id")
    publish.add_argument("--label")
    publish.add_argument("--expires-at")
    publish.add_argument("--git")
    publish.add_argument("--branch")
    publish.add_argument("--no-push", action="store_true")
    join = subparsers.add_parser("join")
    join.add_argument("invitation_url")
    join.add_argument("--consumer-label")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("share_id", nargs="?")
    subparsers.add_parser("list")
    args = parser.parse_args(argv)

    if args.command == "create":
        if not args.request and not args.title:
            raise ValueError("share create requires --title unless --request is used")
        print(
            create_share(
                memory_root,
                args.share_id,
                title=args.title,
                provider_id=args.provider,
                hub_url=args.hub_url,
                credential_id=args.credential_id,
                request=args.request,
                capability=args.capability,
                file_intent=args.file,
                question_intent=args.question,
                question_base_url=args.question_base_url,
                git_url=args.git,
                git_branch=args.branch,
            )
        )
        return 0
    if args.command == "revise":
        print(
            format_share_operation_result(
                revise_share(
                    memory_root,
                    args.share_id,
                    " ".join(args.revision).strip(),
                    capability=args.capability,
                    question_base_url=args.question_base_url,
                )
            )
        )
        return 0
    if args.command == "approve":
        print(approve_share(memory_root, args.share_id))
        return 0
    if args.command == "publish":
        print(
            publish_share(
                memory_root,
                args.share_id,
                label=args.label,
                expires_at=args.expires_at,
                git_url=args.git,
                git_branch=args.branch,
                push=not args.no_push,
            )
        )
        return 0
    if args.command == "join":
        with MemoryWriteLock(memory_root):
            print(join_share(memory_root, args.invitation_url, consumer_label=args.consumer_label))
        return 0
    if args.command == "status":
        print(share_status(memory_root, args.share_id), end="")
        return 0
    if args.command == "list":
        print(list_shares(memory_root), end="")
        return 0
    raise ValueError(f"unknown share command: {args.command}")


def _accept_shared_view_invitation_from_cli(
    memory_root: Path,
    invitation: str,
    *,
    heading_id: str | None,
    title: str | None,
    body: str | None,
    relationship: str | None,
) -> str:
    if _is_http_url(invitation):
        return accept_http_shared_view_invitation(
            memory_root,
            invitation,
            heading_id=heading_id,
            title=title,
            body=body,
            relationship=relationship,
        )
    return accept_shared_view_invitation(memory_root, Path(invitation))


def _format_shared_view_status(item: dict[str, object]) -> str:
    fields = [
        str(item.get("heading_id") or "-"),
        str(item.get("type") or "-"),
        str(item.get("target") or "-"),
        str(item.get("status") or "-"),
        str(item.get("message") or "-"),
    ]
    return "\t".join(fields)


def _shared_view_credential_token(args: argparse.Namespace) -> str:
    if getattr(args, "token_prompt", False):
        token = getpass.getpass("Hub token: ").strip()
    elif getattr(args, "token_stdin", False):
        token = sys.stdin.read().strip()
    else:
        token = str(args.token or "").strip()
    if not token:
        raise ValueError("shared-view credential token must not be empty")
    return token


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _web_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory web")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8766)
    subparsers.add_parser("status")
    stop = subparsers.add_parser("stop")
    stop.add_argument("--timeout", type=int, default=30)
    restart = subparsers.add_parser("restart")
    restart.add_argument("--host", default="127.0.0.1")
    restart.add_argument("--port", type=int, default=8766)
    restart.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    if args.command == "start":
        print(format_web_status(start_web_service(memory_root, host=args.host, port=args.port)))
        return 0
    if args.command == "status":
        print(format_web_status(web_service_status(memory_root)))
        return 0
    if args.command == "stop":
        print(format_web_stop_result(stop_web_service(memory_root, args.timeout)))
        return 0
    if args.command == "restart":
        stop_result = stop_web_service(memory_root, args.timeout)
        if stop_result.state == "stopping":
            print(format_web_stop_result(stop_result))
            return 1
        print(format_web_status(start_web_service(memory_root, host=args.host, port=args.port)))
        return 0
    raise ValueError(f"unknown web command: {args.command}")


def cli_main(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ValueError, ProfileError, HubClientError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _is_help_request(args: list[str]) -> bool:
    return args == ["-h"] or args == ["--help"]


class _WatchStopToken:
    requested = False


@contextmanager
def _watch_stop_signal(label: str):
    token = _WatchStopToken()
    previous = signal.getsignal(signal.SIGTERM)

    def handle_stop(signum, frame):
        token.requested = True
        print(f"rightmemory {label} watch stopping after current work", file=sys.stderr, flush=True)

    signal.signal(signal.SIGTERM, handle_stop)
    try:
        yield token
    finally:
        signal.signal(signal.SIGTERM, previous)


def _watch_manager_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory watch")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "status", "restart"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("target", nargs="?", choices=("all", *MANAGED_WATCH_TARGETS), default="all")
    stop = subparsers.add_parser("stop")
    stop.add_argument("target", nargs="?", choices=("all", *MANAGED_WATCH_TARGETS), default="all")
    stop.add_argument("--timeout", type=int, default=30, help="seconds to wait for graceful stop")
    args = parser.parse_args(argv)

    if args.command == "start":
        return _watch_start(args.target, memory_root)
    if args.command == "status":
        return _watch_status(args.target, memory_root)
    if args.command == "stop":
        return _watch_stop(args.target, args.timeout, memory_root)
    if args.command == "restart":
        stop_result = _watch_stop(args.target, 30, memory_root)
        if stop_result:
            return stop_result
        return _watch_start(args.target, memory_root)
    raise ValueError(f"unknown watch command: {args.command}")


def _watch_start(target: str, memory_root: Path) -> int:
    failed = False
    for name in _watch_targets(target):
        try:
            if name == "sync":
                sync_config = load_sync_config(memory_root=memory_root)
                if not sync_config.enabled:
                    print("sync: disabled")
                    continue
                target_root = sync_config.memory_root
            else:
                config = load_config(_watch_role(name), memory_root=memory_root)
                target_root = config.memory_root
            status = start_managed_watch(target_root, name, sys.executable)
            print(_format_watch_status(status))
        except Exception as exc:
            failed = True
            print(f"{name}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _watch_stop(target: str, timeout: int, memory_root: Path) -> int:
    failed = False
    for name in _watch_targets(target):
        try:
            result = stop_managed_watch(memory_root, name, timeout)
            print(_format_stop_result(result))
            if result.state in {"external", "stopping"}:
                failed = True
        except Exception as exc:
            failed = True
            print(f"{name}: failed: {exc}", file=sys.stderr)
    return 1 if failed else 0


def _watch_status(target: str, memory_root: Path) -> int:
    for name in _watch_targets(target):
        print(_format_watch_status(managed_watch_status(memory_root, name)))
    return 0


def _watch_targets(target: str) -> list[str]:
    if target == "all":
        return list(MANAGED_WATCH_TARGETS)
    if target not in MANAGED_WATCH_TARGETS:
        joined = ", ".join(("all", *MANAGED_WATCH_TARGETS))
        raise ValueError(f"watch target must be one of: {joined}")
    return [target]


def _watch_role(name: str) -> str:
    if name == "review":
        return "reviewer"
    if name == "dreamer":
        return "dreamer"
    if name == "pruner":
        return "pruner"
    if name == "insight":
        return "insight"
    raise ValueError(f"unknown watch target: {name}")


def _format_watch_status(status: ManagedWatchStatus) -> str:
    if status.state == "running" and status.pid is not None:
        return f"{status.name}: running pid {status.pid}, log {status.log_path}"
    if status.state == "external":
        return f"{status.name}: running outside manager"
    if status.state == "stale" and status.pid is not None:
        return f"{status.name}: stale pid {status.pid}"
    return f"{status.name}: stopped"


def _format_stop_result(result: StopWatchResult) -> str:
    if result.state == "stopped" and result.pid is not None:
        return f"{result.name}: stopped pid {result.pid}"
    if result.state == "stopping" and result.pid is not None:
        return f"{result.name}: stopping pid {result.pid}"
    if result.state == "stale-removed" and result.pid is not None:
        return f"{result.name}: removed stale pid {result.pid}"
    if result.state == "external":
        return f"{result.name}: running outside manager; stop the foreground process directly"
    return f"{result.name}: stopped"


def _chat_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} chat")
    parser.add_argument("--session", help="persist Pydantic AI message history under this session id")
    return parser


def _daemon_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} daemon")
    parser.add_argument("--stdio-json", action="store_true", help="read JSON lines from stdin and write JSON lines to stdout")
    return parser


def _dreamer_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory dreamer watch")
    parser.add_argument(
        "--interval",
        type=int,
        help="override seconds between trigger checks",
    )
    parser.add_argument(
        "--session",
        default=DREAMER_WATCH_SESSION_ID,
        help="persist dreamer message history under this session id",
    )
    return parser


def _insight_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory insight watch")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="override trigger check interval in seconds",
    )
    parser.add_argument(
        "--session",
        default=INSIGHT_WATCH_SESSION_ID,
        help="persist insight message history under this session id",
    )
    return parser


def _turn_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role}")
    parser.add_argument("--session", required=True, help="persist Pydantic AI message history under this session id")
    parser.add_argument("message", nargs=argparse.REMAINDER)
    return parser


def _update_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rightmemory update",
        description="Run update turns or manage asynchronous memory update candidates.",
        epilog="Use `rightmemory update retry` to requeue sessions blocked in manual recovery.",
    )
    subparsers = parser.add_subparsers(metavar="command")
    subparsers.add_parser("submit", help="save an async memory update candidate")
    subparsers.add_parser("pull", help="read the latest async update state for one session")
    subparsers.add_parser("undo", help="cancel a pending candidate for one session")
    subparsers.add_parser("retry", help="requeue all sessions blocked in manual recovery")
    return parser


def _prune_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rightmemory prune",
        epilog="Use `rightmemory prune watch` to run periodic prune generation checks.",
    )
    parser.add_argument(
        "--session",
        default="pruner",
        help="persist pruner message history under this session id",
    )
    return parser


def _prune_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory prune watch")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_PRUNER_WATCH_INTERVAL_SECONDS,
        help="seconds between prune generation checks",
    )
    parser.add_argument(
        "--session",
        default=PRUNER_WATCH_SESSION_ID,
        help="persist pruner message history under this session id",
    )
    return parser


def _submit_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} submit")
    parser.add_argument("--session", required=True, help="persist Pydantic AI message history under this session id")
    parser.add_argument("message", nargs=argparse.REMAINDER)
    return parser


def _pull_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} pull")
    parser.add_argument("--session", required=True, help="read the latest async update state for this session id")
    return parser


def _undo_parser(role: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"rightmemory {role} undo")
    parser.add_argument("--session", required=True, help="cancel a pending candidate for this update session id")
    parser.add_argument("candidate_id", type=_candidate_id, help="pending candidate id to cancel")
    return parser


def _retry_parser(role: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=f"rightmemory {role} retry",
        description="Requeue all async update sessions blocked in manual recovery.",
        epilog="No --session is required; retry runs globally for manual-recovery sessions.",
    )


def _candidate_id(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("candidate id must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("candidate id must be a positive integer")
    return parsed


def _review_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan configured transcript sources")
    scan.add_argument("--once", action="store_true", help="review one eligible batch and exit")
    scan.add_argument("--since-days", type=int, help="only review transcript files modified within this many days")
    watch = subparsers.add_parser("watch", help="keep scanning configured transcript sources")
    watch.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_REVIEW_WATCH_INTERVAL_SECONDS,
        help="seconds to sleep when no eligible work is found",
    )
    watch.add_argument("--since-days", type=int, help="only review transcript files modified within this many days")
    normalize = subparsers.add_parser("normalize", help="print normalized transcript JSON without running reviewer")
    normalize.add_argument("--source", choices=("codex", "claude"), required=True, help="transcript provider format")
    normalize.add_argument("--path", required=True, help="path to one provider transcript file")
    args = parser.parse_args(argv)

    if args.command == "normalize":
        return _review_normalize(args.source, args.path)

    if args.command == "scan":
        if not args.once:
            raise ValueError("review scan currently requires --once")
        print(_run_review_scan(args.since_days, memory_root=memory_root).format())
        return 0
    if args.command == "watch":
        return _review_watch(args.interval, args.since_days, memory_root)
    raise ValueError(f"unknown review command: {args.command}")


def _status_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory status")
    parser.parse_args(argv)
    print(format_status_dashboard(collect_status(memory_root)))
    return 0


def _prune_main(argv: list[str], memory_root: Path) -> int:
    if argv and argv[0] == "watch":
        if _is_help_request(argv[1:]):
            _prune_watch_parser().parse_args(argv[1:])
            return 0
        watch_args = _prune_watch_parser().parse_args(argv[1:])
        return _prune_watch(watch_args.interval, watch_args.session, memory_root)
    args = _prune_parser().parse_args(argv)
    print(_run_prune_cycle(args.session, memory_root=memory_root))
    return 0


def _history_main(argv: list[str], memory_root: Path) -> int:
    args = _turn_parser("history").parse_args(argv)
    runtime = RightMemoryRuntime(load_config("historian", memory_root=memory_root))
    try:
        return _session_turn(runtime, args.session, args.message)
    finally:
        runtime.cleanup()


def _sync_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser("watch", help="keep local memory sync state alive")
    watch.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_SYNC_WATCH_INTERVAL_SECONDS,
        help="seconds between sync watch checks",
    )
    args = parser.parse_args(argv)

    if args.command == "watch":
        return _sync_watch(args.interval, memory_root)
    raise ValueError(f"unknown sync command: {args.command}")


def _doctor_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory doctor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("agent-cli", help="check Codex/Claude CLI-agent mode setup")
    args = parser.parse_args(argv)

    if args.command == "agent-cli":
        checks = run_agent_cli_doctor(memory_root=memory_root)
        print(format_doctor_report(checks))
        return 0 if all(check.ok for check in checks) else 1
    raise ValueError(f"unknown doctor command: {args.command}")


def _run_review_scan(
    since_days: int | None = None,
    *,
    require_full_batch: bool = False,
    memory_root: Path | None = None,
):
    reviewer_config = load_config("reviewer", memory_root=memory_root)
    return _run_review_scan_with_config(reviewer_config, since_days, require_full_batch=require_full_batch)


def _run_review_scan_with_config(
    reviewer_config: Any,
    since_days: int | None = None,
    *,
    require_full_batch: bool = False,
):
    review_config = load_review_config(memory_root=reviewer_config.memory_root)
    if since_days is not None:
        if since_days < 1:
            raise ValueError("--since-days must be a positive integer")
        review_config = replace(review_config, since_days=since_days)
    dreamer_watch_config = load_dreamer_watch_config(memory_root=reviewer_config.memory_root)
    insight_watch_config = load_insight_watch_config(memory_root=reviewer_config.memory_root)
    runtime = RightMemoryRuntime(reviewer_config)
    try:
        scanner = ReviewScanner(
            review_config,
            runtime.run_session_turn,
            on_review_success=_combined_trigger_incrementer(
                _dreamer_trigger_incrementer(
                    reviewer_config.memory_root,
                    dreamer_watch_config.review_session_points,
                ),
                _insight_trigger_incrementer(
                    reviewer_config.memory_root,
                    insight_watch_config.review_session_points,
                ),
            ),
        )
        return scanner.scan_once(require_full_batch=require_full_batch)
    finally:
        runtime.cleanup()


def _review_watch(interval: int, since_days: int | None, memory_root: Path) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    reviewer_config = load_config("reviewer", memory_root=memory_root)
    refresh = InstallStamp(reviewer_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("review") as stop, WatchLock(reviewer_config.memory_root, "review"):
            next_config: Any | None = reviewer_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory review scan", flush=True)
                if next_config is None:
                    result = _run_review_scan(since_days, require_full_batch=True)
                else:
                    result = _run_review_scan_with_config(next_config, since_days, require_full_batch=True)
                    next_config = None
                print(result.format(), flush=True)
                _reexec_if_install_changed(refresh, stop)
                if result.failed > 0:
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("review", consecutive_failures):
                        exit_code = 1
                        break
                    retry_seconds = min(interval, DEFAULT_REVIEW_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
                    continue
                consecutive_failures = 0
                if not stop.requested and result.reviewed > 0:
                    continue
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory review watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory review watch stopped", file=sys.stderr)
        return 130


def _run_dream_cycle(
    session_id: str,
    dreamer_config: Any | None = None,
    memory_root: Path | None = None,
) -> str:
    config = dreamer_config if dreamer_config is not None else load_config("dreamer", memory_root=memory_root)
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_cycle(session_id)
    finally:
        runtime.cleanup()


def _dreamer_watch_once(watch_config: Any, session_id: str, run_cycle: Callable[[str], str]) -> str:
    store = DreamerTriggerStore(watch_config.memory_root)
    state = store.read()
    if state.points < watch_config.trigger_points:
        return _DREAMER_WATCH_SKIPPED

    timestamp = datetime.now(UTC).isoformat()
    print(f"[{timestamp}] rightmemory dreamer cycle", flush=True)
    try:
        output = run_cycle(session_id)
    except Exception as exc:
        print(f"rightmemory dreamer cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return _DREAMER_WATCH_FAILED

    print(output, flush=True)
    store.consume_if_available(watch_config.trigger_points)
    return _DREAMER_WATCH_SUCCEEDED


def _dreamer_watch(interval: int | None, session_id: str, memory_root: Path) -> int:
    if interval is not None and interval < 1:
        raise ValueError("--interval must be a positive integer")
    watch_config = load_dreamer_watch_config(memory_root=memory_root)
    if interval is not None:
        watch_config = replace(watch_config, check_interval_seconds=interval)
    dreamer_config = load_config("dreamer", memory_root=memory_root)
    refresh = InstallStamp(watch_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("dreamer") as stop, WatchLock(watch_config.memory_root, "dreamer"):
            next_config: Any | None = dreamer_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)

                def run_cycle(current_session_id: str) -> str:
                    nonlocal next_config
                    if next_config is None:
                        return _run_dream_cycle(current_session_id, memory_root=memory_root)
                    output = _run_dream_cycle(current_session_id, next_config)
                    next_config = None
                    return output

                status = _dreamer_watch_once(watch_config, session_id, run_cycle)
                _reexec_if_install_changed(refresh, stop)
                if status == _DREAMER_WATCH_SKIPPED:
                    consecutive_failures = 0
                    if not _sleep_with_refresh_check(watch_config.check_interval_seconds, refresh, stop):
                        break
                elif status == _DREAMER_WATCH_FAILED:
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("dreamer", consecutive_failures):
                        exit_code = 1
                        break
                    retry_seconds = min(watch_config.check_interval_seconds, DEFAULT_DREAMER_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
                else:
                    consecutive_failures = 0
        print("rightmemory dreamer watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory dreamer watch stopped", file=sys.stderr)
        return 130


def _run_insight_cycle(
    session_id: str,
    insight_config: Any | None = None,
    memory_root: Path | None = None,
) -> str:
    config = insight_config if insight_config is not None else load_config("insight", memory_root=memory_root)
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_cycle(session_id)
    finally:
        runtime.cleanup()


def _insight_watch_once(watch_config: Any, session_id: str, run_cycle: Callable[[str], str]) -> str:
    store = InsightTriggerStore(watch_config.memory_root)
    state = store.read()
    if state.points < watch_config.trigger_points:
        return _INSIGHT_WATCH_SKIPPED

    before_logs = _insight_log_fingerprints(watch_config.memory_root)
    timestamp = datetime.now(UTC).isoformat()
    print(f"[{timestamp}] rightmemory insight cycle", flush=True)
    try:
        output = run_cycle(session_id)
    except Exception as exc:
        print(f"rightmemory insight cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return _INSIGHT_WATCH_FAILED

    print(output, flush=True)
    after_logs = _insight_log_fingerprints(watch_config.memory_root)
    result = "artifact" if after_logs != before_logs else "noop"
    store.consume_if_available(watch_config.trigger_points, result=result)
    return _INSIGHT_WATCH_SUCCEEDED


def _insight_log_fingerprints(memory_root: Path) -> dict[str, str]:
    root = Path(memory_root)
    fingerprints: dict[str, str] = {}
    for path in sorted((root / "insight_logs").glob("*.md")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root).as_posix()
        fingerprints[relative_path] = sha256(path.read_bytes()).hexdigest()
    return fingerprints


def _insight_watch(interval: int | None, session_id: str, memory_root: Path) -> int:
    if interval is not None and interval < 1:
        raise ValueError("--interval must be a positive integer")
    watch_config = load_insight_watch_config(memory_root=memory_root)
    if interval is not None:
        watch_config = replace(watch_config, check_interval_seconds=interval)
    insight_config = load_config("insight", memory_root=memory_root)
    refresh = InstallStamp(watch_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("insight") as stop, WatchLock(watch_config.memory_root, "insight"):
            next_config: Any | None = insight_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)

                def run_cycle(current_session_id: str) -> str:
                    nonlocal next_config
                    if next_config is None:
                        return _run_insight_cycle(current_session_id, memory_root=memory_root)
                    output = _run_insight_cycle(current_session_id, next_config)
                    next_config = None
                    return output

                status = _insight_watch_once(watch_config, session_id, run_cycle)
                _reexec_if_install_changed(refresh, stop)
                if status == _INSIGHT_WATCH_SKIPPED:
                    consecutive_failures = 0
                    if not _sleep_with_refresh_check(watch_config.check_interval_seconds, refresh, stop):
                        break
                elif status == _INSIGHT_WATCH_FAILED:
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("insight", consecutive_failures):
                        exit_code = 1
                        break
                    retry_seconds = min(watch_config.check_interval_seconds, DEFAULT_INSIGHT_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
                else:
                    consecutive_failures = 0
        print("rightmemory insight watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory insight watch stopped", file=sys.stderr)
        return 130


def _run_prune_cycle(
    session_id: str,
    pruner_config: Any | None = None,
    runtime_config: Any | None = None,
    memory_root: Path | None = None,
) -> str:
    config = pruner_config if pruner_config is not None else load_pruner_config(memory_root=memory_root)
    runtime = RightMemoryRuntime(
        runtime_config if runtime_config is not None else load_config("pruner", memory_root=memory_root)
    )
    try:
        return runtime.run_prune_turn(session_id, config)
    finally:
        runtime.cleanup()


def _prune_watch(interval: int, session_id: str, memory_root: Path) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    pruner_config = load_pruner_config(memory_root=memory_root)
    runtime_config = load_config("pruner", memory_root=memory_root)
    refresh = InstallStamp(pruner_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("pruner") as stop, WatchLock(pruner_config.memory_root, "pruner"):
            next_pruner_config: Any | None = pruner_config
            next_runtime_config: Any | None = runtime_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory prune check", flush=True)
                try:
                    output = _run_prune_cycle(
                        session_id,
                        next_pruner_config,
                        next_runtime_config,
                        memory_root=memory_root,
                    )
                    next_pruner_config = None
                    next_runtime_config = None
                except Exception as exc:
                    print(
                        f"rightmemory prune check failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("pruner", consecutive_failures):
                        exit_code = 1
                        break
                    retry_seconds = min(interval, DEFAULT_PRUNER_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
                    continue
                consecutive_failures = 0
                print(output, flush=True)
                _reexec_if_install_changed(refresh, stop)
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory pruner watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory pruner watch stopped", file=sys.stderr)
        return 130


def _sync_watch(interval: int, memory_root: Path) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    sync_config = load_sync_config(memory_root=memory_root)
    if not sync_config.enabled:
        print("rightmemory sync watch disabled", file=sys.stderr)
        return 0
    refresh = InstallStamp(sync_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("sync") as stop, WatchLock(sync_config.memory_root, "sync"):
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory sync check", flush=True)
                manager = SyncManager(sync_config)
                cycle_failed = False
                try:
                    with MemoryWriteLock(sync_config.memory_root):
                        result = manager.background_pull()
                except Exception as exc:
                    print(
                        f"rightmemory sync check failed: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    cycle_failed = True
                else:
                    print(result.message, flush=True)
                    if result.status in {"conflict", "dirty"}:
                        try:
                            _run_sync_reconciler(manager, result, sync_config.memory_root)
                        except Exception as exc:
                            print(
                                f"rightmemory sync reconciler failed: {type(exc).__name__}: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            cycle_failed = True
                if cycle_failed:
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("sync", consecutive_failures):
                        exit_code = 1
                        break
                else:
                    consecutive_failures = 0
                _reexec_if_install_changed(refresh, stop)
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory sync watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory sync watch stopped", file=sys.stderr)
        return 130


def _run_sync_reconciler(manager: SyncManager, result: Any, memory_root: Path | None = None) -> None:
    reconciler_config = load_config("sync-reconciler", memory_root=memory_root)
    reconciler_root = Path(reconciler_config.memory_root)
    if reconciler_root != manager.memory_root:
        raise ValueError(
            "sync-reconciler memory root mismatch: "
            f"sync watch uses {manager.memory_root}, sync-reconciler uses {reconciler_root}"
        )
    runtime = RightMemoryRuntime(reconciler_config)
    try:
        print(runtime.run_session_turn(SYNC_WATCH_SESSION_ID, manager.repair_message(result)), flush=True)
    finally:
        runtime.cleanup()


def _sleep_with_refresh_check(seconds: int, refresh: InstallStamp, stop: _WatchStopToken | None = None) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if stop is not None and stop.requested:
            return False
        _reexec_if_install_changed(refresh, stop)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(math.ceil(remaining), WATCH_REFRESH_POLL_SECONDS))


def _reexec_if_install_changed(refresh: InstallStamp, stop: _WatchStopToken | None = None) -> None:
    if stop is not None and stop.requested:
        return
    if not refresh.changed():
        return
    print("rightmemory install changed; restarting watch process", file=sys.stderr, flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "rightmemory.cli", *sys.argv[1:]])


def _review_normalize(source: str, path: str) -> int:
    normalized = normalize_transcript(source, Path(path).expanduser())
    if normalized is None:
        raise ValueError("no completed turns found in transcript")
    print(json.dumps(normalized.to_payload(), ensure_ascii=False, indent=2))
    return 0


def _chat(runtime: RightMemoryRuntime, session_id: str | None = None) -> int:
    print("RightMemory standalone chat. Type /exit to quit.", file=sys.stderr)
    while True:
        try:
            message = input("> ")
        except EOFError:
            print(file=sys.stderr)
            return 0
        if message.strip() in {"/exit", "/quit"}:
            return 0
        if not message.strip():
            continue
        if session_id is None:
            print(runtime.run_turn(message))
        else:
            print(runtime.run_session_turn(session_id, message))


def _daemon_stdio_json(runtime: RightMemoryRuntime) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = _handle_json_request(runtime, request)
        except Exception as exc:
            response = {"type": "error", "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


def _handle_json_request(runtime: RightMemoryRuntime, request: dict[str, Any]) -> dict[str, str]:
    message = request.get("message")
    if not isinstance(message, str):
        raise ValueError("JSON request must contain string field: message")
    return {"type": "assistant", "message": runtime.run_turn(message)}


def _session_turn(runtime: RightMemoryRuntime, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    print(runtime.run_session_turn(session_id, message))
    return 0


def _submit(memory_root, role: str, session_id: str, message_parts: list[str]) -> int:
    message = " ".join(message_parts).strip()
    if not message:
        raise ValueError("message must not be empty")
    state = AsyncUpdateStore(memory_root, role).submit(session_id, message)
    print(format_state(state))
    warning = manual_recovery_warning(state)
    if warning:
        print()
        print(warning)
    return 0


def _pull(memory_root, role: str, session_id: str) -> int:
    state = AsyncUpdateStore(memory_root, role).read(session_id)
    print(format_state(state))
    return 0


def _undo(memory_root, role: str, session_id: str, candidate_id: int) -> int:
    state, canceled = AsyncUpdateStore(memory_root, role).cancel_pending(session_id, candidate_id)
    if canceled:
        print(f"canceled pending candidate: {candidate_id}")
    else:
        print(f"candidate is not pending: {candidate_id}")
    print(format_state(state))
    return 0


def _retry(memory_root, role: str) -> int:
    result = AsyncUpdateStore(memory_root, role).retry_manual_recovery()
    print(format_retry_result(result))
    return 1 if result.worker_error else 0


def _async_worker(
    runtime: RightMemoryRuntime,
    memory_root,
    role: str,
) -> int:
    dreamer_watch_config = load_dreamer_watch_config(memory_root=memory_root)
    insight_watch_config = load_insight_watch_config(memory_root=memory_root)
    async_update_config = load_async_update_config(memory_root=memory_root)
    store = AsyncUpdateStore(memory_root, role)
    result = store.run_pending_batches(
        lambda batch_session_id, message: runtime.run_session_turn(batch_session_id, message),
        target_batch_candidates=async_update_config.target_batch_candidates,
        max_wait_seconds=async_update_config.max_wait_seconds,
        on_batch_success=_combined_trigger_incrementer(
            _dreamer_trigger_incrementer(
                memory_root,
                dreamer_watch_config.update_candidate_points,
            ),
            _insight_trigger_incrementer(
                memory_root,
                insight_watch_config.update_candidate_points,
            ),
        ),
    )
    if result.status == "failed":
        return 1
    return 0


def _combined_trigger_incrementer(*incrementers: Callable[[int], None]) -> Callable[[int], None]:
    def increment(count: int) -> None:
        for item in incrementers:
            item(count)

    return increment


def _dreamer_trigger_incrementer(memory_root: Path, points_per_item: float) -> Callable[[int], None]:
    store = DreamerTriggerStore(memory_root)

    def increment(count: int) -> None:
        try:
            store.increment(count * points_per_item)
        except OSError as exc:
            print(
                f"Warning: could not update dreamer trigger state: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return increment


def _insight_trigger_incrementer(memory_root: Path, points_per_item: float) -> Callable[[int], None]:
    store = InsightTriggerStore(memory_root)

    def increment(count: int) -> None:
        try:
            store.increment(count * points_per_item)
        except OSError as exc:
            print(
                f"Warning: could not update insight trigger state: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return increment


if __name__ == "__main__":
    raise SystemExit(cli_main())
