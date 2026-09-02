import tempfile
import unittest
from pathlib import Path

from rightmemory.web.app import create_web_app
from rightmemory.web.auth import SESSION_COOKIE, create_session_cookie, read_session_cookie
from tests.asgi_client import ASGITestClient as TestClient


class WebStudioAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        self.app = create_web_app(self.root)
        self.client = TestClient(self.app)

    def _bootstrap(self, client=None):
        response = (client or self.client).get("/api/session")
        self.assertEqual(response.status_code, 200)
        return response

    def test_session_bootstraps_automatically_with_cookie_and_csrf(self):
        response = self._bootstrap()

        body = response.json()
        self.assertEqual(body["active_root"], str(self.root.resolve()))
        self.assertTrue(body["csrf_token"])
        self.assertTrue(self.client.cookies.get(SESSION_COOKIE))
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertTrue((self.root / ".runtime" / "web" / "session-secret").exists())
        self.assertFalse((self.root / ".runtime" / "web" / "operator-token.sha256").exists())

    def test_protected_read_uses_the_bootstrapped_session(self):
        before_bootstrap = self.client.get("/api/overview")
        self._bootstrap()
        after_bootstrap = self.client.get("/api/overview")

        self.assertEqual(before_bootstrap.status_code, 401)
        self.assertEqual(after_bootstrap.status_code, 200)

    def test_login_route_and_operator_token_artifact_are_removed(self):
        response = self.client.post("/api/login", json={"token": "obsolete"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse((self.root / ".runtime" / "web" / "operator-token.sha256").exists())

    def test_non_loopback_host_header_is_rejected(self):
        client = TestClient(self.app)

        response = client.get("/api/session", headers={"host": "malicious.example"})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(client.cookies.get(SESSION_COOKIE))

    def test_mutation_rejects_a_nonmatching_origin(self):
        csrf = self._bootstrap().json()["csrf_token"]

        response = self.client.post(
            "/api/active-root",
            json={"root": str(self.root)},
            headers={
                "origin": "http://malicious.example",
                "x-csrf-token": csrf,
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_write_requires_csrf_after_bootstrap(self):
        csrf = self._bootstrap().json()["csrf_token"]

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
        self.assertEqual(accepted.json()["data"]["active_root"], str(self.root.resolve()))

    def test_logout_requires_csrf_and_next_session_bootstraps_automatically(self):
        csrf = self._bootstrap().json()["csrf_token"]
        previous_cookie = self.client.cookies.get(SESSION_COOKIE)

        missing = self.client.post("/api/logout")
        accepted = self.client.post("/api/logout", headers={"x-csrf-token": csrf})
        protected = self.client.get("/api/overview")
        next_session = self._bootstrap()

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(protected.status_code, 401)
        self.assertEqual(next_session.json()["active_root"], str(self.root.resolve()))
        self.assertTrue(next_session.json()["csrf_token"])
        self.assertNotEqual(self.client.cookies.get(SESSION_COOKIE), previous_cookie)

    def test_active_root_switch_is_scoped_to_browser_session(self):
        other_root = self.root / "other"
        other_root.mkdir()
        (other_root / "MEMORY.md").write_text("# Other {#other}\n", encoding="utf-8")
        first = TestClient(self.app)
        second = TestClient(self.app)
        first_bootstrap = self._bootstrap(first)
        second_bootstrap = self._bootstrap(second)

        switched = first.post(
            "/api/active-root",
            json={"root": str(other_root)},
            headers={"x-csrf-token": first_bootstrap.json()["csrf_token"]},
        )
        first_session = first.get("/api/session")
        second_session = second.get("/api/session")

        self.assertEqual(switched.status_code, 200)
        self.assertEqual(first_session.json()["active_root"], str(other_root.resolve()))
        self.assertEqual(second_session.json()["active_root"], str(self.root.resolve()))
        self.assertTrue(switched.json()["data"]["csrf_token"])
        self.assertTrue(second_bootstrap.json()["csrf_token"])

    def test_active_root_switch_rejects_paths_outside_configured_root(self):
        with tempfile.TemporaryDirectory() as outside_temp:
            outside_root = Path(outside_temp)
            (outside_root / "MEMORY.md").write_text("# Outside {#outside}\n", encoding="utf-8")
            csrf = self._bootstrap().json()["csrf_token"]

            response = self.client.post(
                "/api/active-root",
                json={"root": str(outside_root)},
                headers={"x-csrf-token": csrf},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("outside", response.json()["detail"]["technical"])

    def test_expired_session_cookie_is_rejected_then_replaced_by_bootstrap(self):
        cookie, _session = create_session_cookie(
            self.root,
            active_root=self.root,
            created_at="2000-01-01T00:00:00+00:00",
        )
        self.client.cookies.set(SESSION_COOKIE, cookie)

        protected = self.client.get("/api/overview")
        replacement = self._bootstrap()

        self.assertEqual(protected.status_code, 401)
        self.assertEqual(replacement.json()["active_root"], str(self.root.resolve()))
        self.assertTrue(replacement.json()["csrf_token"])
        self.assertNotEqual(self.client.cookies.get(SESSION_COOKIE), cookie)

    def test_tampered_session_cookie_is_rejected(self):
        self._bootstrap()
        cookie = self.client.cookies.get(SESSION_COOKIE)
        self.client.cookies.set(SESSION_COOKIE, f"{cookie[:-1]}{'a' if cookie[-1] != 'a' else 'b'}")

        response = self.client.get("/api/overview")

        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_copied_session_cookie(self):
        bootstrap = self._bootstrap()
        copied_cookie = self.client.cookies.get(SESSION_COOKIE)

        logout = self.client.post(
            "/api/logout",
            headers={"x-csrf-token": bootstrap.json()["csrf_token"]},
        )
        copied_session = TestClient(self.app)
        copied_session.cookies.set(SESSION_COOKIE, copied_cookie)
        response = copied_session.get("/api/overview")

        self.assertEqual(logout.status_code, 200)
        self.assertEqual(response.status_code, 401)

    def test_logout_revokes_and_clears_when_the_active_root_was_deleted(self):
        other_root = self.root / "other"
        other_root.mkdir()
        memory_file = other_root / "MEMORY.md"
        memory_file.write_text("# Other {#other}\n", encoding="utf-8")
        csrf = self._bootstrap().json()["csrf_token"]
        switched = self.client.post(
            "/api/active-root", json={"root": str(other_root)}, headers={"x-csrf-token": csrf},
        )
        self.assertEqual(switched.status_code, 200)
        signed_cookie = self.client.cookies.get(SESSION_COOKIE)
        memory_file.unlink()
        other_root.rmdir()

        response = self.client.post(
            "/api/logout", headers={"x-csrf-token": switched.json()["data"]["csrf_token"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(read_session_cookie(self.root, signed_cookie))
        replacement = self._bootstrap()
        self.assertEqual(replacement.json()["active_root"], str(self.root.resolve()))
        self.assertNotEqual(self.client.cookies.get(SESSION_COOKIE), signed_cookie)

    def test_cors_headers_are_closed_by_default(self):
        response = self._bootstrap()

        self.assertNotIn("access-control-allow-origin", {key.lower() for key in response.headers})
