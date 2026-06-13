import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
