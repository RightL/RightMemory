from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rightmemory.pursuit_web import create_pursuit_app
from tests.asgi_client import ASGITestClient as TestClient


class FakeRunner:
    def run_turn(self, **kwargs):
        callback = kwargs.get("on_thread_started")
        if callback:
            callback("web-thread")
        return SimpleNamespace(provider_session_id="web-thread", text="Web task done.")

    def close(self):
        pass


class PursuitWebTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Focus\n\nNo Pursuit is focused yet.\n\n"
            "## Product {#product}\n\nBuild the product.\n\n"
            "**Next:**\n- `do` Implement the map.\n",
            encoding="utf-8",
        )
        self.client = TestClient(
            create_pursuit_app(self.root, access_token="test-token", runner_factory=FakeRunner)
        )
        self.headers = {"authorization": "Bearer test-token"}

    def test_static_shell_and_auth(self):
        index = self.client.get("/")
        script = self.client.get("/static/pursuit.js")
        unauthorized = self.client.get("/api/workspace")
        authorized = self.client.get("/api/workspace", headers=self.headers)

        self.assertEqual(index.status_code, 200)
        self.assertIn("Pursuit Map", index.text)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()["data"]["workspace"]["roots"], ["product"])

    def test_preview_apply_task_and_run_endpoints(self):
        workspace = self.client.get("/api/workspace", headers=self.headers).json()["data"]["workspace"]
        operation = {"op": "update", "id": "product", "state": "Map implementation started."}
        preview = self.client.post(
            "/api/preview",
            headers=self.headers,
            json={"revision": workspace["revision"], "operations": [operation]},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Map implementation started", preview.json()["data"]["diff"])

        applied = self.client.post(
            "/api/apply",
            headers=self.headers,
            json={"revision": workspace["revision"], "operations": [operation]},
        )
        self.assertEqual(applied.status_code, 200)

        planned = self.client.post(
            "/api/tasks/plan",
            headers=self.headers,
            json={"pursuit_id": "product", "project": str(self.root)},
        )
        self.assertEqual(planned.status_code, 200)
        task_id = planned.json()["data"]["task_id"]

        ran = self.client.post(
            f"/api/tasks/{task_id}/run",
            headers=self.headers,
            json={"project": str(self.root)},
        )
        self.assertEqual(ran.status_code, 200)
        self.assertEqual(ran.json()["data"]["thread_id"], "web-thread")
        self.assertEqual(ran.json()["data"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
