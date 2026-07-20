from __future__ import annotations

import io
import os
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from rightmemory.git_share_transport import import_git_file_package
from rightmemory.shared_view_files import (
    _replace_import_from_zip,
    export_file_view_package,
    render_file_view,
    write_extractive_file_view_recipe,
    write_generative_file_view,
)
from rightmemory.shared_view_package import (
    FileViewPackageError,
    extract_package_archive,
    validate_file_view_package,
)


class SharedViewPackageV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self._write_provider_graph()

    def test_extractive_package_copies_reachable_typed_resources(self) -> None:
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose typed auth context.",
            include_headings=["project"],
            approved=True,
        )

        render_file_view(self.root, "auth-api")
        package = self.root / "package"
        export_file_view_package(self.root, "auth-api", package)

        validated = validate_file_view_package(
            package,
            expected_view_id="auth-api",
            namespace_id="consumer-auth",
        )
        self.assertEqual(validated.manifest.namespace, "MF#consumer-auth")
        self.assertEqual(
            {path.name for path in (package / "dist").iterdir()},
            {
                "MEMORY.md",
                "MEMORY_detail.md",
                "MEMORY_notes.md",
                "MEMORY_SKILL_review.md",
                "manifest.toml",
            },
        )
        direct = (package / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("{F#detail}", direct)
        self.assertNotIn("Published Context", direct)

    def test_invalid_generative_document_keeps_previous_dist(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document=(
                "# Published Auth {#published-auth} → []\n\n"
                "- `token-expiry` Tokens expire hourly. → []\n"
            ),
            approved=True,
        )
        dist = self.root / "shared_views" / "auth-api" / "dist"
        recipe = self.root / "shared_views" / "auth-api" / "recipe.toml"
        previous_recipe = recipe.read_bytes()
        previous = {
            path.name: path.read_bytes()
            for path in dist.iterdir()
            if path.is_file()
        }

        with self.assertRaisesRegex(FileViewPackageError, "invalid MF Memory document"):
            write_generative_file_view(
                self.root,
                view_id="auth-api",
                title="Auth API",
                intent="Expose generated auth context.",
                memory_document="# Arbitrary wrapper\n\nUnaddressed prose.\n",
                approved=True,
            )

        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in dist.iterdir()
                if path.is_file()
            },
            previous,
        )
        self.assertEqual(recipe.read_bytes(), previous_recipe)

    def test_invalid_extractive_projection_keeps_previous_dist(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Stable {#stable} → []\n\nStable body.\n",
        )
        dist = self.root / "shared_views" / "auth-api" / "dist"
        previous = {path.name: path.read_bytes() for path in dist.iterdir()}
        (self.root / "MEMORY.md").write_text(
            "# Project {#project} → []\n\n"
            "## Public {#public} → [rel:private]\n\nPublic body.\n\n"
            "## Private {#private} → []\n\nPrivate body.\n",
            encoding="utf-8",
        )
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose only public context.",
            include_headings=["public"],
        )

        with self.assertRaisesRegex(FileViewPackageError, "dangling edge"):
            render_file_view(self.root, "auth-api")

        self.assertEqual(
            {path.name: path.read_bytes() for path in dist.iterdir()},
            previous,
        )

    def test_unreferenced_dist_resource_is_rejected(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Tokens {#tokens} → []\n\nToken body.\n",
        )
        package = self.root / "package"
        export_file_view_package(self.root, "auth-api", package)
        (package / "dist" / "MEMORY_unused.md").write_text("unused\n", encoding="utf-8")

        with self.assertRaisesRegex(FileViewPackageError, "unreferenced MF resource"):
            validate_file_view_package(package, expected_view_id="auth-api")

    def test_missing_extractive_selection_does_not_publish_a_narrower_view(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Stable {#stable} → []\n\nStable body.\n",
        )
        dist = self.root / "shared_views" / "auth-api" / "dist"
        previous = {path.name: path.read_bytes() for path in dist.iterdir()}
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose selected auth context.",
            include_headings=["removed-heading"],
        )

        with self.assertRaisesRegex(ValueError, "unknown heading"):
            render_file_view(self.root, "auth-api")

        self.assertEqual(
            {path.name: path.read_bytes() for path in dist.iterdir()},
            previous,
        )

    def test_http_candidate_validates_remote_id_but_uses_local_namespace(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="remote-auth",
            title="Remote Auth",
            intent="Expose generated auth context.",
            memory_document="# Tokens {#tokens} → []\n\nToken body.\n",
        )
        package = self.root / "package"
        export_file_view_package(self.root, "remote-auth", package)
        archive = _zip_package(package)
        consumer = self.root / "consumer"

        _replace_import_from_zip(consumer, "local-auth", "remote-auth", archive)

        imported = consumer / ".runtime" / "shared_views" / "imports" / "local-auth"
        validated = validate_file_view_package(
            imported,
            expected_view_id="remote-auth",
            namespace_id="local-auth",
        )
        self.assertEqual(validated.manifest.namespace, "MF#local-auth")

    def test_invalid_http_candidate_does_not_replace_previous_import(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Tokens {#tokens} → []\n\nToken body.\n",
        )
        package = self.root / "package"
        export_file_view_package(self.root, "auth-api", package)
        consumer = self.root / "consumer"
        _replace_import_from_zip(consumer, "auth-api", "auth-api", _zip_package(package))
        imported = consumer / ".runtime" / "shared_views" / "imports" / "auth-api"
        previous = (imported / "dist" / "MEMORY.md").read_bytes()
        invalid = _zip_entries(
            [
                ("view.md", b"# Auth\n"),
                ("recipe.toml", b'version = 1\nview_id = "auth-api"\nkind = "file"\n'),
                (
                    "rightmemory-shared-view.toml",
                    b'version = 1\nview_id = "auth-api"\nkind = "file"\n',
                ),
                ("dist/MEMORY.md", b"arbitrary text\n"),
                ("dist/manifest.toml", b'version = 1\nview_id = "auth-api"\n'),
            ]
        )

        with self.assertRaisesRegex(FileViewPackageError, "version must be 2"):
            _replace_import_from_zip(consumer, "auth-api", "auth-api", invalid)

        self.assertEqual((imported / "dist" / "MEMORY.md").read_bytes(), previous)

    def test_duplicate_zip_entry_is_rejected_before_extraction(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive = _zip_entries(
                [
                    ("view.md", b"first\n"),
                    ("view.md", b"second\n"),
                ]
            )
        target = self.root / "extracted"

        with self.assertRaisesRegex(FileViewPackageError, "duplicate"):
            extract_package_archive(archive, target)

        self.assertFalse(target.exists())

    def test_traversal_zip_entry_is_rejected_before_extraction(self) -> None:
        target = self.root / "extracted"

        with self.assertRaisesRegex(FileViewPackageError, "unsafe package path"):
            extract_package_archive(_zip_entries([("../outside.md", b"secret\n")]), target)

        self.assertFalse(target.exists())

    def test_symlink_zip_entry_is_rejected_before_extraction(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("dist/MEMORY.md")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "../../outside.md")
        target = self.root / "extracted"

        with self.assertRaisesRegex(FileViewPackageError, "regular file"):
            extract_package_archive(buffer.getvalue(), target)

        self.assertFalse(target.exists())

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks are unavailable")
    def test_git_import_does_not_launder_package_symlink(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Tokens {#tokens} → []\n\nToken body.\n",
        )
        package = self.root / "package"
        export_file_view_package(self.root, "auth-api", package)
        memory = package / "dist" / "MEMORY.md"
        outside = self.root / "outside.md"
        outside.write_text("# Outside {#outside} → []\n", encoding="utf-8")
        memory.unlink()
        try:
            memory.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation failed: {exc}")
        consumer = self.root / "consumer"

        with self.assertRaisesRegex(FileViewPackageError, "symlink"):
            import_git_file_package(consumer, "auth-api", package)

        self.assertFalse(
            (consumer / ".runtime" / "shared_views" / "imports" / "auth-api").exists()
        )

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks are unavailable")
    def test_git_import_rejects_symlinked_package_root(self) -> None:
        write_generative_file_view(
            self.root,
            view_id="auth-api",
            title="Auth API",
            intent="Expose generated auth context.",
            memory_document="# Tokens {#tokens} → []\n\nToken body.\n",
        )
        package = self.root / "package"
        export_file_view_package(self.root, "auth-api", package)
        link = self.root / "package-link"
        try:
            link.symlink_to(package, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink creation failed: {exc}")

        with self.assertRaisesRegex(ValueError, "regular directory"):
            import_git_file_package(self.root / "consumer", "auth-api", link)

    def _write_provider_graph(self) -> None:
        (self.root / "MEMORY.md").write_text(
            "# Project {#project} → []\n\n"
            "## Public Auth {#public-auth} → []\n\n"
            "- `token-expiry` Tokens expire hourly. → []\n\n"
            "## Auth Detail {F#detail} → []\n\n"
            "## Auth Notes {M#notes} → []\n\n"
            "## Review Instructions {S#review} → []\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_detail.md").write_text(
            "# Detail Topic {#detail-topic} → []\n\nDetail body.\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_notes.md").write_text("# Evidence\n\nRaw evidence.\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_review.md").write_text(
            "# Review\n\nUse every instruction.\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")


def _zip_package(package: Path) -> bytes:
    entries = [
        (path.relative_to(package).as_posix(), path.read_bytes())
        for path in sorted(package.rglob("*"))
        if path.is_file()
    ]
    return _zip_entries(entries)


def _zip_entries(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
