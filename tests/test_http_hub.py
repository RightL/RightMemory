import sqlite3
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from rightmemory.hub.app import create_hub_app
from rightmemory.hub.models import HubPackageManifest
from rightmemory.hub.packages import (
    PackageValidationError,
    copy_package_version,
    load_package_manifest,
)
from rightmemory.hub.store import HubStore


class HubStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_initialize_creates_database_storage_runtime_and_config(self):
        store = HubStore(self.root)

        store.initialize(admin_token="admin-secret")

        self.assertTrue((self.root / "hub.db").is_file())
        self.assertTrue((self.root / "storage").is_dir())
        self.assertTrue((self.root / "storage" / "views").is_dir())
        self.assertTrue((self.root / ".runtime").is_dir())
        self.assertTrue((self.root / "hub.toml").is_file())

    def test_provider_token_verifies_by_hash_and_records_audit_event(self):
        store = HubStore(self.root)
        store.initialize(admin_token="admin-secret")

        provider_token = store.create_provider_token("alice", label="publish")

        self.assertTrue(store.verify_token(provider_token.raw_token, action="publish", provider_id="alice"))
        self.assertFalse(store.verify_token("wrong", action="publish", provider_id="alice"))
        self.assertIn("token.created", [event.kind for event in store.list_audit_events()])

        with sqlite3.connect(store.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM tokens WHERE id = ?",
                (provider_token.token_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        stored_values = " ".join(str(value) for value in dict(row).values() if value is not None)
        self.assertNotIn(provider_token.raw_token, stored_values)
        self.assertEqual(len(row["token_hash"]), 64)
        self.assertNotEqual(row["nonce"], row["token_hash"])

    def test_revoked_provider_token_fails_verification(self):
        store = HubStore(self.root)
        store.initialize(admin_token="admin-secret")
        provider_token = store.create_provider_token("alice", label="publish")

        store.revoke_token(provider_token.token_id)

        self.assertFalse(store.verify_token(provider_token.raw_token, action="publish", provider_id="alice"))

    def test_list_tokens_exposes_revocation_handles_without_secret_material(self):
        store = HubStore(self.root)
        store.initialize(admin_token="admin-secret")
        provider_token = store.create_provider_token("alice", label="publish")
        store.revoke_token(provider_token.token_id)

        tokens = store.list_tokens()
        token_ids = {token["token_id"] for token in tokens}
        rendered = " ".join(str(token) for token in tokens)

        self.assertIn(provider_token.token_id, token_ids)
        self.assertIn("revoked_at", tokens[0])
        self.assertNotIn(provider_token.raw_token, rendered)
        self.assertNotIn("admin-secret", rendered)


class HubPackageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_valid_package_loads_manifest_and_copies_immutable_version(self):
        package = self.root / "package"
        _write_package(package)

        manifest = load_package_manifest(
            package,
            expected_view_id="alice-auth-api",
            max_package_bytes=8192,
        )
        copied = copy_package_version(
            package,
            self.root / "hub" / "storage",
            view_id="alice-auth-api",
            version_id="v1",
            max_package_bytes=8192,
        )

        self.assertEqual(manifest.view_id, "alice-auth-api")
        self.assertEqual(manifest.title, "Alice Auth API")
        self.assertIn("dist/MEMORY.md", manifest.files)
        self.assertEqual(len(manifest.package_hash), 64)
        self.assertTrue((copied.path / "dist" / "MEMORY.md").is_file())
        self.assertEqual(copied.manifest.package_hash, manifest.package_hash)

    def test_missing_required_package_file_is_rejected(self):
        package = self.root / "package"
        _write_package(package)
        (package / "dist" / "MEMORY.md").unlink()

        with self.assertRaises(PackageValidationError) as caught:
            load_package_manifest(package, expected_view_id="alice-auth-api")

        self.assertIn("missing required package file", str(caught.exception))

    def test_copy_rejects_path_traversal_target_ids(self):
        package = self.root / "package"
        _write_package(package)

        with self.assertRaises(PackageValidationError) as caught:
            copy_package_version(
                package,
                self.root / "hub" / "storage",
                view_id="alice-auth-api",
                version_id="../escape",
            )

        self.assertIn("path traversal", str(caught.exception))
        self.assertFalse((self.root / "hub" / "storage" / "views" / "alice-auth-api" / "escape").exists())

    def test_copy_rejects_path_traversal_package_entries(self):
        package = self.root / "package"
        _write_package(package)
        malicious_manifest = HubPackageManifest(
            source_root=package,
            view_id="alice-auth-api",
            title="Alice Auth API",
            ref="rightmemory://view/alice-auth-api",
            files=("../escape.md",),
            size_bytes=1,
            package_hash="0" * 64,
        )

        malicious_snapshot = SimpleNamespace(
            manifest=malicious_manifest,
            files=(("../escape.md", b"escape"),),
        )

        with patch("rightmemory.hub.packages._package_snapshot", return_value=malicious_snapshot):
            with self.assertRaises(PackageValidationError) as caught:
                copy_package_version(
                    package,
                    self.root / "hub" / "storage",
                    view_id="alice-auth-api",
                    version_id="v1",
                )

        self.assertIn("package path traversal entry", str(caught.exception))
        self.assertFalse((self.root / "hub" / "storage" / "escape.md").exists())

    def test_copy_writes_the_validated_package_byte_snapshot(self):
        package = self.root / "package"
        _write_package(package)
        snapshot_manifest = HubPackageManifest(
            source_root=package,
            view_id="alice-auth-api",
            title="Alice Auth API",
            ref="rightmemory://view/alice-auth-api",
            files=("dist/MEMORY.md",),
            size_bytes=len(b"snapshot bytes\n"),
            package_hash="1" * 64,
        )
        snapshot = SimpleNamespace(
            manifest=snapshot_manifest,
            files=(("dist/MEMORY.md", b"snapshot bytes\n"),),
        )
        (package / "dist" / "MEMORY.md").write_text("changed after snapshot\n", encoding="utf-8")

        with patch("rightmemory.hub.packages._package_snapshot", return_value=snapshot):
            copied = copy_package_version(
                package,
                self.root / "hub" / "storage",
                view_id="alice-auth-api",
                version_id="v1",
            )

        self.assertEqual((copied.path / "dist" / "MEMORY.md").read_bytes(), b"snapshot bytes\n")
        self.assertEqual(copied.manifest.package_hash, "1" * 64)

    def test_symlinked_file_that_escapes_package_root_is_rejected(self):
        package = self.root / "package"
        _write_package(package)
        outside = self.root / "outside.md"
        outside.write_text("outside secret\n", encoding="utf-8")
        (package / "dist" / "MEMORY.md").unlink()
        try:
            (package / "dist" / "MEMORY.md").symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        with self.assertRaises(PackageValidationError) as caught:
            load_package_manifest(package, expected_view_id="alice-auth-api")

        self.assertIn("symlink escapes package root", str(caught.exception))

    def test_package_over_size_limit_is_rejected(self):
        package = self.root / "package"
        _write_package(package, memory_text="x" * 128)

        with self.assertRaises(PackageValidationError) as caught:
            load_package_manifest(
                package,
                expected_view_id="alice-auth-api",
                max_package_bytes=64,
            )

        self.assertIn("exceeds package size limit", str(caught.exception))

    def test_publish_target_must_match_package_view_id(self):
        package = self.root / "package"
        _write_package(package, view_id="alice-auth-api")

        with self.assertRaises(PackageValidationError) as caught:
            load_package_manifest(package, expected_view_id="bob-auth-api")

        self.assertIn("does not match publish target", str(caught.exception))


def _write_package(package: Path, *, view_id: str = "alice-auth-api", memory_text: str | None = None) -> None:
    package.mkdir(parents=True)
    (package / "dist").mkdir()
    title = "Alice Auth API" if view_id == "alice-auth-api" else view_id
    (package / "view.md").write_text(f"# {title}\n", encoding="utf-8")
    (package / "export.toml").write_text(
        f"""
        version = 1
        view_id = "{view_id}"
        ref = "rightmemory://view/{view_id}"
        title = "{title}"
        """,
        encoding="utf-8",
    )
    (package / "rightmemory-shared-view.toml").write_text(
        f"""
        version = 1
        view_id = "{view_id}"
        ref = "rightmemory://view/{view_id}"
        title = "{title}"

        [transport]
        kind = "package"
        path = "."
        view_id = "{view_id}"
        """,
        encoding="utf-8",
    )
    (package / "dist" / "MEMORY.md").write_text(
        memory_text or "# Alice Auth API Shared View\n\nTokens rotate monthly.\n",
        encoding="utf-8",
    )


class HubApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.store = HubStore(self.root)
        self.store.initialize(admin_token="admin-secret")
        self.provider_token = self.store.create_provider_token("alice", label="publish")
        self.client = TestClient(create_hub_app(self.root))

    def test_health_reports_initialized_hub(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["initialized"])

    def test_publish_invite_accept_retrieve_interact_and_inbox_flow(self):
        first_package = self.root / "package-v1"
        _write_package(
            first_package,
            memory_text="# Alice Auth API Shared View\n\nSigned tokens expire after fifteen minutes.\n",
        )
        second_package = self.root / "package-v2"
        _write_package(
            second_package,
            memory_text="# Alice Auth API Shared View\n\nRefresh tokens rotate monthly.\n",
        )

        first_publish = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(first_package),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )
        second_publish = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(second_package),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )

        self.assertEqual(first_publish.status_code, 201)
        self.assertEqual(second_publish.status_code, 201)
        first_version_id = first_publish.json()["version_id"]
        second_version_id = second_publish.json()["version_id"]
        self.assertNotEqual(first_version_id, second_version_id)
        self.assertTrue((self.root / "storage" / "views" / "alice-auth-api" / "versions" / first_version_id).is_dir())
        self.assertTrue((self.root / "storage" / "views" / "alice-auth-api" / "versions" / second_version_id).is_dir())
        self.assertIn(
            "Signed tokens expire",
            (
                self.root
                / "storage"
                / "views"
                / "alice-auth-api"
                / "versions"
                / first_version_id
                / "dist"
                / "MEMORY.md"
            ).read_text(encoding="utf-8"),
        )
        with sqlite3.connect(self.store.db_path) as connection:
            current_version_id = connection.execute(
                "SELECT current_version_id FROM views WHERE id = ?",
                ("alice-auth-api",),
            ).fetchone()[0]
        self.assertEqual(current_version_id, second_version_id)

        invitation = self.client.post(
            "/api/views/alice-auth-api/invitations",
            headers=_auth(self.provider_token.raw_token),
            json={"label": "frontend agent"},
        )

        self.assertEqual(invitation.status_code, 201)
        invitation_url = invitation.json()["invitation_url"]
        self.assertIn("/i/", invitation_url)
        invitation_token = invitation_url.rsplit("/i/", 1)[1]

        invitation_view = self.client.get(f"/api/invitations/{invitation_token}/view")

        self.assertEqual(invitation_view.status_code, 200)
        self.assertEqual(invitation_view.json()["view_id"], "alice-auth-api")
        self.assertEqual(invitation_view.json()["title"], "Alice Auth API")
        self.assertEqual(invitation_view.json()["current_version_id"], second_version_id)

        accepted = self.client.post(
            f"/api/invitations/{invitation_token}/accept",
            json={"consumer_label": "frontend"},
        )

        self.assertEqual(accepted.status_code, 201)
        accepted_body = accepted.json()
        self.assertEqual(accepted_body["view_id"], "alice-auth-api")
        self.assertTrue(accepted_body["connection_id"])
        self.assertTrue(accepted_body["token_id"])
        connection_token = accepted_body["connection_token"]
        self.assertTrue(connection_token)

        retrieval = self.client.post(
            "/api/views/alice-auth-api/retrieve",
            headers=_auth(connection_token),
            json={"query": "refresh token rotation", "limit": 4},
        )

        self.assertEqual(retrieval.status_code, 200)
        retrieval_body = retrieval.json()
        self.assertEqual(retrieval_body["view_id"], "alice-auth-api")
        self.assertEqual(retrieval_body["version_id"], second_version_id)
        self.assertTrue(retrieval_body["freshness"])
        self.assertEqual(retrieval_body["provenance"]["title"], "Alice Auth API")
        self.assertIn("Refresh tokens rotate monthly", retrieval_body["snippets"][0]["text"])

        other_package = self.root / "package-other"
        _write_package(other_package, view_id="alice-billing-api", memory_text="# Billing\n\nInvoices are separate.\n")
        other_publish = self.client.post(
            "/api/views/alice-billing-api/versions",
            content=_zip_package(other_package),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )
        self.assertEqual(other_publish.status_code, 201)

        wrong_view = self.client.post(
            "/api/views/alice-billing-api/retrieve",
            headers=_auth(connection_token),
            json={"query": "invoices"},
        )

        self.assertEqual(wrong_view.status_code, 403)

        interaction = self.client.post(
            "/api/views/alice-auth-api/interactions",
            headers=_auth(connection_token),
            json={
                "actor": "assistant",
                "message": "Docs are missing token_expires_at.",
                "task_context": "frontend login migration",
            },
        )

        self.assertEqual(interaction.status_code, 201)
        self.assertEqual(interaction.json()["status"], "recorded")
        self.assertTrue(interaction.json()["interaction_id"])

        provider_inbox = self.client.get(
            "/api/providers/alice/inbox",
            headers=_auth(self.provider_token.raw_token),
        )
        admin_inbox = self.client.get(
            "/api/providers/alice/inbox",
            headers=_auth("admin-secret"),
        )

        for response in (provider_inbox, admin_inbox):
            self.assertEqual(response.status_code, 200)
            records = response.json()["interactions"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["view_id"], "alice-auth-api")
            self.assertEqual(records[0]["payload"]["message"], "Docs are missing token_expires_at.")
            self.assertEqual(records[0]["payload"]["task_context"], "frontend login migration")
            self.assertEqual(records[0]["payload"]["actor"], "assistant")

    def test_failed_auth_is_audited_without_raw_token_material(self):
        package = self.root / "package"
        _write_package(package)

        response = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(package),
            headers={**_auth("wrong-secret-token"), "content-type": "application/zip"},
        )

        self.assertEqual(response.status_code, 401)
        audit_blob = "\n".join(str(event.details) for event in self.store.list_audit_events())
        self.assertIn("publish", audit_blob)
        self.assertNotIn("wrong-secret-token", audit_blob)

    def test_publish_rejects_server_side_package_paths(self):
        package = self.root / "package"
        _write_package(package)

        response = self.client.post(
            "/api/views/alice-auth-api/versions",
            headers=_auth(self.provider_token.raw_token),
            json={"package_path": str(package)},
        )

        self.assertEqual(response.status_code, 415)

    def test_publish_rejects_large_upload_before_snapshot(self):
        small_root = self.root / "small-hub"
        small_store = HubStore(small_root)
        small_store.initialize(admin_token="admin-secret", max_package_bytes=512)
        provider_token = small_store.create_provider_token("alice", label="publish")
        client = TestClient(create_hub_app(small_root))
        package = self.root / "large-package"
        _write_package(package, memory_text="# Alice Auth API Shared View\n\n" + ("A" * 4096))

        response = client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(package),
            headers={**_auth(provider_token.raw_token), "content-type": "application/zip"},
        )

        self.assertEqual(response.status_code, 413)

    def test_publish_rejects_zip_with_too_many_entries(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index in range(2100):
                archive.writestr(f"empty-{index}.txt", "")

        response = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=buffer.getvalue(),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )

        self.assertEqual(response.status_code, 413)

    def test_invitation_expiry_must_be_iso_datetime(self):
        package = self.root / "package"
        _write_package(package)
        publish = self.client.post(
            "/api/views/alice-auth-api/versions",
            content=_zip_package(package),
            headers={**_auth(self.provider_token.raw_token), "content-type": "application/zip"},
        )
        self.assertEqual(publish.status_code, 201)

        response = self.client.post(
            "/api/views/alice-auth-api/invitations",
            headers=_auth(self.provider_token.raw_token),
            json={"expires_at": "tomorrow"},
        )

        self.assertEqual(response.status_code, 400)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _zip_package(package: Path) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package).as_posix())
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
