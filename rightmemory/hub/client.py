from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


class HubClientError(RuntimeError):
    pass


class HubClient:
    def __init__(self, base_url: str, token: str | None = None, *, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get_invitation_view(self, invite_token: str) -> dict[str, Any]:
        return self._request("GET", f"/api/invitations/{urllib.parse.quote(invite_token)}/view")

    def accept_invitation(self, invite_token: str, *, consumer_label: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if consumer_label:
            payload["consumer_label"] = consumer_label
        return self._request("POST", f"/api/invitations/{urllib.parse.quote(invite_token)}/accept", json_body=payload)

    def download_package(self, view_id: str) -> bytes:
        return self._request_bytes(
            "GET",
            f"/api/views/{urllib.parse.quote(view_id)}/package",
            bearer=True,
        )

    def ask_question(self, view_id: str, question: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/share/questions/{urllib.parse.quote(view_id)}/ask",
            json_body={"question": question},
            bearer=True,
        )

    def post_interaction(self, view_id: str, payload: dict[str, object]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/views/{urllib.parse.quote(view_id)}/interactions",
            json_body=payload,
            bearer=True,
        )

    def publish_package(self, view_id: str, package_root: Path) -> dict[str, Any]:
        with TemporaryDirectory() as tempdir:
            archive = Path(tempdir) / "package.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
                for path in sorted(Path(package_root).rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        zip_file.write(path, path.relative_to(package_root).as_posix())
            return self._request(
                "POST",
                f"/api/views/{urllib.parse.quote(view_id)}/versions",
                data=archive.read_bytes(),
                content_type="application/zip",
                bearer=True,
            )

    def create_invitation(
        self,
        view_id: str,
        *,
        label: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if label:
            payload["label"] = label
        if expires_at:
            payload["expires_at"] = expires_at
        return self._request(
            "POST",
            f"/api/views/{urllib.parse.quote(view_id)}/invitations",
            json_body=payload,
            bearer=True,
        )

    def provider_inbox(self, provider_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/providers/{urllib.parse.quote(provider_id)}/inbox",
            bearer=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        bearer: bool = False,
    ) -> dict[str, Any]:
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if bearer:
            if not self.token:
                raise HubClientError("hub token is required")
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubClientError(f"hub request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HubClientError(f"hub request failed: {exc.reason}") from exc
        if not payload:
            return {}
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise HubClientError("hub response must be a JSON object")
        return decoded

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        bearer: bool = False,
    ) -> bytes:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if bearer:
            if not self.token:
                raise HubClientError("hub token is required")
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubClientError(f"hub request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HubClientError(f"hub request failed: {exc.reason}") from exc
