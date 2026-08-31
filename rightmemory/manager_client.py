"""Thin loopback client for local RightMemory Manager conversations.

The client deliberately calls the running Web Studio so configuration and
Pursuit edits pass through the same validation and transaction boundaries as
the browser.  It does not open the conversation database or construct a
second ConversationService.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .web.auth import SESSION_COOKIE, create_session_cookie
from .web.process import WebServiceStatus, validate_web_host, web_service_status


class ManagerClientError(RuntimeError):
    """A local Web Studio request could not be completed."""


class ManagerClient:
    def __init__(self, memory_root: Path, *, timeout_seconds: float = 30.0):
        self.root = Path(memory_root).expanduser().resolve()
        discovered = _running_web_service(self.root)
        if discovered is None:
            raise ManagerClientError(
                "Web Studio is not running for this root. Start it with `rightmemory pursuit --no-open`."
            )
        self.configured_root, status = discovered
        if status.host is None or status.port is None:
            raise ManagerClientError("The running Web Studio has no usable loopback address.")
        host = status.host.strip()
        validate_web_host(host)
        if status.port <= 0 or status.port > 65535:
            raise ManagerClientError("Web Studio must use a fixed loopback port.")
        url_host = f"[{host}]" if ":" in host else host
        self.base_url = f"http://{url_host}:{status.port}"
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cookie, session = create_session_cookie(
            self.configured_root, active_root=self.root
        )
        body = None
        headers = {
            "Accept": "application/json",
            "Cookie": f"{SESSION_COOKIE}={cookie}",
            "X-CSRF-Token": session.csrf_token,
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = None
            message = _error_message(detail) or f"Web Studio returned HTTP {exc.code}."
            raise ManagerClientError(message) from exc
        except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagerClientError(f"Could not call the local Web Studio: {exc}") from exc
        if not isinstance(result, dict):
            raise ManagerClientError("Web Studio returned an invalid response.")
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise ManagerClientError("Web Studio returned an invalid data object.")
        return data


def _running_web_service(
    memory_root: Path,
) -> tuple[Path, WebServiceStatus] | None:
    """Return the nearest running Web Studio registered at this root or an ancestor."""
    root = Path(memory_root).expanduser().resolve()
    for candidate in (root, *root.parents):
        try:
            status = web_service_status(candidate)
        except OSError:
            continue
        if status.state == "running":
            return candidate, status
    return None


def manager_main(argv: list[str], memory_root: Path) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    client = ManagerClient(memory_root)
    data = _dispatch(client, args)
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rightmemory manager",
        description="Call the running local Web Studio through its authenticated business API.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("workspace", help="read registered hosts, projects, and conversations")

    pursuit = commands.add_parser("pursuit", help="read or apply one validated map operation")
    pursuit_commands = pursuit.add_subparsers(dest="pursuit_command", required=True)
    pursuit_commands.add_parser("snapshot")
    pursuit_apply = pursuit_commands.add_parser("apply")
    pursuit_apply.add_argument("--expected-revision", required=True)
    pursuit_apply.add_argument("--operation-json", required=True)

    host = commands.add_parser("host", help="manage registered execution hosts")
    host_commands = host.add_subparsers(dest="host_command", required=True)
    host_commands.add_parser("list")
    host_add = host_commands.add_parser("add")
    host_add.add_argument("--display-name", required=True)
    host_add.add_argument("--ssh-alias", required=True)
    host_probe = host_commands.add_parser("probe")
    host_probe.add_argument("--host-id", required=True)
    host_update = host_commands.add_parser("update")
    host_update.add_argument("--host-id", required=True)
    host_update.add_argument("--display-name")
    host_update.add_argument("--ssh-alias")
    host_update.add_argument("--platform-hint")
    enabled = host_update.add_mutually_exclusive_group()
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")

    project = commands.add_parser("project", help="manage registered project working directories")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_commands.add_parser("list")
    project_add = project_commands.add_parser("add")
    project_add.add_argument("--host-id", required=True)
    project_add.add_argument("--label", required=True)
    project_add.add_argument("--cwd", required=True)
    project_update = project_commands.add_parser("update")
    project_update.add_argument("--project-id", required=True)
    project_update.add_argument("--label")
    project_update.add_argument("--cwd")
    return parser


def _dispatch(client: ManagerClient, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "workspace":
        return client.request("GET", "/api/conversation-workspace")
    if args.command == "pursuit":
        if args.pursuit_command == "snapshot":
            return client.request("GET", "/api/pursuit-map")
        try:
            operation = json.loads(args.operation_json)
        except json.JSONDecodeError as exc:
            raise ManagerClientError("--operation-json must be one JSON object.") from exc
        if not isinstance(operation, dict):
            raise ManagerClientError("--operation-json must be one JSON object.")
        return client.request(
            "POST",
            "/api/pursuit-map/operations",
            {"expected_revision": args.expected_revision, "operation": operation},
        )
    if args.command == "host":
        if args.host_command == "list":
            return {"hosts": client.request("GET", "/api/conversation-workspace").get("hosts", [])}
        if args.host_command == "add":
            return client.request(
                "POST",
                "/api/conversation-hosts",
                {"kind": "ssh", "display_name": args.display_name, "ssh_alias": args.ssh_alias},
            )
        if args.host_command == "probe":
            return client.request(
                "POST", f"/api/conversation-hosts/{quote(args.host_id, safe='')}/probe", {}
            )
        payload = {
            key: value
            for key, value in {
                "display_name": args.display_name,
                "ssh_alias": args.ssh_alias,
                "platform_hint": args.platform_hint,
                "enabled": True if args.enable else False if args.disable else None,
            }.items()
            if value is not None
        }
        if not payload:
            raise ManagerClientError("Host update requires at least one changed field.")
        return client.request(
            "PATCH", f"/api/conversation-hosts/{quote(args.host_id, safe='')}", payload
        )
    if args.command == "project":
        if args.project_command == "list":
            return {
                "projects": client.request("GET", "/api/conversation-workspace").get(
                    "projects", []
                )
            }
        if args.project_command == "add":
            return client.request(
                "POST",
                "/api/conversation-projects",
                {"host_id": args.host_id, "label": args.label, "cwd": args.cwd},
            )
        payload = {
            key: value
            for key, value in {"label": args.label, "cwd": args.cwd}.items()
            if value is not None
        }
        if not payload:
            raise ManagerClientError("Project update requires a label or working directory.")
        return client.request(
            "PATCH",
            f"/api/conversation-projects/{quote(args.project_id, safe='')}",
            payload,
        )
    raise ManagerClientError("Unknown Manager command.")


def _error_message(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    detail = value.get("detail")
    if isinstance(detail, dict):
        for key in ("message", "detail", "error"):
            candidate = detail.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    if isinstance(detail, str) and detail:
        return detail
    return None
