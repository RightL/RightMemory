import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from rightmemory.manager_client import (
    ManagerClient,
    ManagerClientError,
    _dispatch,
    _parser,
)
from rightmemory.web.process import WebServiceStatus


class _Response:
    def __init__(self, value: object):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.body


class ManagerClientTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _status(self, *, state: str = "running", host: str = "127.0.0.1", port: int = 8766):
        return WebServiceStatus(state, 123, host, port, self.root / "web.log")

    def test_request_uses_authenticated_loopback_api_with_json_body(self):
        captured = {}

        def open_request(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response({"data": {"updated": True}})

        session = SimpleNamespace(csrf_token="csrf-token")
        with (
            patch("rightmemory.manager_client.web_service_status", return_value=self._status()),
            patch("rightmemory.manager_client.validate_web_host") as validate_host,
            patch(
                "rightmemory.manager_client.create_session_cookie",
                return_value=("signed-cookie", session),
            ) as create_cookie,
            patch("rightmemory.manager_client.urlopen", side_effect=open_request),
        ):
            client = ManagerClient(self.root, timeout_seconds=4.5)
            result = client.request("PATCH", "/api/conversation-projects/project-1", {"cwd": "D:/repo"})

        request = captured["request"]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(result, {"updated": True})
        self.assertEqual(request.full_url, "http://127.0.0.1:8766/api/conversation-projects/project-1")
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"cwd": "D:/repo"})
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["cookie"], "rightmemory_session=signed-cookie")
        self.assertEqual(headers["x-csrf-token"], "csrf-token")
        self.assertEqual(captured["timeout"], 4.5)
        validate_host.assert_called_once_with("127.0.0.1")
        create_cookie.assert_called_once_with(self.root.resolve(), active_root=self.root.resolve())

    def test_get_has_no_body_and_still_uses_session_authentication(self):
        captured = {}

        def open_request(request, *, timeout):
            captured["request"] = request
            return _Response({"data": {"hosts": []}})

        with (
            patch("rightmemory.manager_client.web_service_status", return_value=self._status()),
            patch(
                "rightmemory.manager_client.create_session_cookie",
                return_value=("cookie", SimpleNamespace(csrf_token="csrf")),
            ),
            patch("rightmemory.manager_client.urlopen", side_effect=open_request),
        ):
            result = ManagerClient(self.root).request("GET", "/api/conversation-workspace")

        request = captured["request"]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(result, {"hosts": []})
        self.assertIsNone(request.data)
        self.assertNotIn("content-type", headers)
        self.assertEqual(headers["cookie"], "rightmemory_session=cookie")
        self.assertEqual(headers["x-csrf-token"], "csrf")

    def test_nested_active_root_uses_ancestor_web_registration_and_signing_root(self):
        configured_root = self.root.resolve()
        active_root = configured_root / "nested" / "memory"
        active_root.mkdir(parents=True)
        checked: list[Path] = []

        def status(candidate: Path):
            resolved = Path(candidate).resolve()
            checked.append(resolved)
            if resolved == configured_root:
                return self._status()
            return self._status(state="stopped")

        with (
            patch("rightmemory.manager_client.web_service_status", side_effect=status),
            patch(
                "rightmemory.manager_client.create_session_cookie",
                return_value=("cookie", SimpleNamespace(csrf_token="csrf")),
            ) as create_cookie,
            patch(
                "rightmemory.manager_client.urlopen",
                return_value=_Response({"data": {"conversations": []}}),
            ),
        ):
            client = ManagerClient(active_root)
            result = client.request("GET", "/api/conversation-workspace")

        self.assertEqual(result, {"conversations": []})
        self.assertEqual(client.root, active_root.resolve())
        self.assertEqual(client.configured_root, configured_root)
        self.assertEqual(
            checked,
            [active_root.resolve(), active_root.parent.resolve(), configured_root],
        )
        create_cookie.assert_called_once_with(
            configured_root, active_root=active_root.resolve()
        )

    def test_ipv6_loopback_is_bracketed_in_api_url(self):
        with patch(
            "rightmemory.manager_client.web_service_status",
            return_value=self._status(host="::1"),
        ):
            client = ManagerClient(self.root)

        self.assertEqual(client.base_url, "http://[::1]:8766")

    def test_stopped_service_is_rejected_before_authentication_or_http(self):
        with (
            patch(
                "rightmemory.manager_client.web_service_status",
                return_value=self._status(state="stopped"),
            ),
            patch("rightmemory.manager_client.create_session_cookie") as create_cookie,
            patch("rightmemory.manager_client.urlopen") as open_url,
            self.assertRaisesRegex(ManagerClientError, "not running"),
        ):
            ManagerClient(self.root)

        create_cookie.assert_not_called()
        open_url.assert_not_called()

    def test_non_loopback_service_is_rejected_before_authentication_or_http(self):
        with (
            patch(
                "rightmemory.manager_client.web_service_status",
                return_value=self._status(host="192.0.2.10"),
            ),
            patch("rightmemory.manager_client.create_session_cookie") as create_cookie,
            patch("rightmemory.manager_client.urlopen") as open_url,
            self.assertRaisesRegex(ValueError, "loopback"),
        ):
            ManagerClient(self.root)

        create_cookie.assert_not_called()
        open_url.assert_not_called()

    def test_invalid_fixed_port_is_rejected(self):
        for port in (0, 65536):
            with self.subTest(port=port), patch(
                "rightmemory.manager_client.web_service_status",
                return_value=self._status(port=port),
            ), self.assertRaisesRegex(ManagerClientError, "fixed loopback port"):
                ManagerClient(self.root)

    def test_http_error_surfaces_api_detail(self):
        error = HTTPError(
            "http://127.0.0.1:8766/api/conversation-hosts",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps({"detail": {"message": "alias has history"}}).encode("utf-8")),
        )
        self.addCleanup(error.close)
        with (
            patch("rightmemory.manager_client.web_service_status", return_value=self._status()),
            patch(
                "rightmemory.manager_client.create_session_cookie",
                return_value=("cookie", SimpleNamespace(csrf_token="csrf")),
            ),
            patch("rightmemory.manager_client.urlopen", side_effect=error),
            self.assertRaisesRegex(ManagerClientError, "alias has history"),
        ):
            ManagerClient(self.root).request("POST", "/api/conversation-hosts", {})

    def test_transport_and_invalid_response_errors_are_wrapped(self):
        with patch("rightmemory.manager_client.web_service_status", return_value=self._status()):
            client = ManagerClient(self.root)

        session_patch = patch(
            "rightmemory.manager_client.create_session_cookie",
            return_value=("cookie", SimpleNamespace(csrf_token="csrf")),
        )
        with session_patch, patch(
            "rightmemory.manager_client.urlopen", side_effect=URLError("connection refused")
        ), self.assertRaisesRegex(ManagerClientError, "Could not call the local Web Studio"):
            client.request("GET", "/api/conversation-workspace")

        with patch(
            "rightmemory.manager_client.create_session_cookie",
            return_value=("cookie", SimpleNamespace(csrf_token="csrf")),
        ), patch(
            "rightmemory.manager_client.urlopen", return_value=_Response({"data": []})
        ), self.assertRaisesRegex(ManagerClientError, "invalid data object"):
            client.request("GET", "/api/conversation-workspace")


class ManagerCommandDispatchTests(unittest.TestCase):
    def _dispatch(self, argv: list[str], response: dict | None = None):
        client = Mock()
        client.request.return_value = {} if response is None else response
        result = _dispatch(client, _parser().parse_args(argv))
        return client, result

    def test_read_commands_use_workspace_and_pursuit_endpoints(self):
        client, result = self._dispatch(["workspace"], {"projects": []})
        self.assertEqual(result, {"projects": []})
        client.request.assert_called_once_with("GET", "/api/conversation-workspace")

        client, result = self._dispatch(["pursuit", "snapshot"], {"revision": "abc"})
        self.assertEqual(result, {"revision": "abc"})
        client.request.assert_called_once_with("GET", "/api/pursuit-map")

    def test_pursuit_apply_uses_validated_operation_endpoint(self):
        client, _ = self._dispatch(
            [
                "pursuit",
                "apply",
                "--expected-revision",
                "revision-1",
                "--operation-json",
                '{"op":"rename","id":"P1","title":"Next"}',
            ]
        )

        client.request.assert_called_once_with(
            "POST",
            "/api/pursuit-map/operations",
            {
                "expected_revision": "revision-1",
                "operation": {"op": "rename", "id": "P1", "title": "Next"},
            },
        )

    def test_host_commands_use_business_api_and_quote_identity(self):
        client, _ = self._dispatch(
            ["host", "add", "--display-name", "Build", "--ssh-alias", "build-box"]
        )
        client.request.assert_called_once_with(
            "POST",
            "/api/conversation-hosts",
            {"kind": "ssh", "display_name": "Build", "ssh_alias": "build-box"},
        )

        client, _ = self._dispatch(["host", "probe", "--host-id", "host/one"])
        client.request.assert_called_once_with(
            "POST", "/api/conversation-hosts/host%2Fone/probe", {}
        )

        client, _ = self._dispatch(
            [
                "host",
                "update",
                "--host-id",
                "host/one",
                "--display-name",
                "Builder",
                "--platform-hint",
                "linux",
                "--disable",
            ]
        )
        client.request.assert_called_once_with(
            "PATCH",
            "/api/conversation-hosts/host%2Fone",
            {"display_name": "Builder", "platform_hint": "linux", "enabled": False},
        )

    def test_project_commands_use_business_api_and_quote_identity(self):
        client, _ = self._dispatch(
            [
                "project",
                "add",
                "--host-id",
                "host-1",
                "--label",
                "RightMemory",
                "--cwd",
                "/srv/rightmemory",
            ]
        )
        client.request.assert_called_once_with(
            "POST",
            "/api/conversation-projects",
            {"host_id": "host-1", "label": "RightMemory", "cwd": "/srv/rightmemory"},
        )

        client, _ = self._dispatch(
            [
                "project",
                "update",
                "--project-id",
                "project/one",
                "--label",
                "Renamed",
                "--cwd",
                "/srv/new",
            ]
        )
        client.request.assert_called_once_with(
            "PATCH",
            "/api/conversation-projects/project%2Fone",
            {"label": "Renamed", "cwd": "/srv/new"},
        )

    def test_invalid_operation_and_empty_updates_stop_before_http(self):
        client = Mock()
        with self.assertRaisesRegex(ManagerClientError, "one JSON object"):
            _dispatch(
                client,
                _parser().parse_args(
                    [
                        "pursuit",
                        "apply",
                        "--expected-revision",
                        "revision-1",
                        "--operation-json",
                        "[]",
                    ]
                ),
            )
        client.request.assert_not_called()

        with self.assertRaisesRegex(ManagerClientError, "Host update requires"):
            _dispatch(client, _parser().parse_args(["host", "update", "--host-id", "host-1"]))
        client.request.assert_not_called()

        with self.assertRaisesRegex(ManagerClientError, "Project update requires"):
            _dispatch(
                client,
                _parser().parse_args(["project", "update", "--project-id", "project-1"]),
            )
        client.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
