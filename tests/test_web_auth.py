import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from rightmemory.web.app import create_web_app
from rightmemory.web.auth import operator_token_hash_path


class WebStudioAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        self.client = TestClient(create_web_app(self.root, operator_token="secret-token"))

    def test_session_starts_unauthenticated(self):
        response = self.client.get("/api/session")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["authenticated"], False)

    def test_protected_read_requires_login(self):
        response = self.client.get("/api/overview")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["message"], "login required")

    def test_login_sets_http_only_session_cookie_and_returns_csrf(self):
        response = self.client.post("/api/login", json={"token": "secret-token"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["csrf_token"])
        self.assertTrue(operator_token_hash_path(self.root).exists())
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)

    def test_login_rejects_wrong_token(self):
        response = self.client.post("/api/login", json={"token": "wrong"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["message"], "invalid operator token")

    def test_write_requires_csrf_after_login(self):
        login = self.client.post("/api/login", json={"token": "secret-token"})
        csrf = login.json()["data"]["csrf_token"]

        missing = self.client.post("/api/active-root", json={"root": str(self.root)})
        wrong = self.client.post(
            "/api/active-root",
            json={"root": str(self.root)},
            headers={"x-csrf-token": "wrong"},
        )
        accepted = self.client.post(
            "/api/active-root",
            json={"root": str(self.root)},
            headers={"x-csrf-token": csrf},
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["data"]["active_root"], str(self.root))

    def test_active_root_switch_is_scoped_to_browser_session(self):
        other_root = self.root / "other"
        other_root.mkdir()
        (other_root / "MEMORY.md").write_text("# Other {#other}\n", encoding="utf-8")
        app = create_web_app(self.root, operator_token="secret-token")
        first = TestClient(app)
        second = TestClient(app)
        first_login = first.post("/api/login", json={"token": "secret-token"})
        second_login = second.post("/api/login", json={"token": "secret-token"})

        switched = first.post(
            "/api/active-root",
            json={"root": str(other_root)},
            headers={"x-csrf-token": first_login.json()["data"]["csrf_token"]},
        )
        first_session = first.get("/api/session")
        second_session = second.get("/api/session")

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(first_session.json()["active_root"], str(other_root))
        self.assertEqual(second_session.json()["active_root"], str(self.root))
        self.assertTrue(switched.json()["data"]["csrf_token"])
        self.assertTrue(second_login.json()["data"]["csrf_token"])

    def test_cors_headers_are_closed_by_default(self):
        response = self.client.get("/api/session")

        self.assertNotIn("access-control-allow-origin", {key.lower() for key in response.headers})
