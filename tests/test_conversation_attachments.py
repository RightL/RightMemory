import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path

from rightmemory.conversations.attachments import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    cleanup_orphaned_attachment_files,
    resolve_attachment_path,
    validate_upload,
    write_upload,
)
from rightmemory.conversations.models import ConversationError


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + b"\x00" * 4


def _jpeg(width: int, height: int) -> bytes:
    components = b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    frame = b"\x08" + struct.pack(">HH", height, width) + components
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame + b"\xff\xd9"


class ConversationAttachmentTests(unittest.TestCase):
    def test_generic_file_retains_safe_extension_and_text_can_be_forced_to_file(self):
        pdf = validate_upload(
            b"%PDF-1.7\nfixture",
            "application/pdf",
            r"C:\downloads\Quarterly Report.PDF",
            "a" * 32,
        )
        selected_text = validate_upload(
            b"plain selected file",
            "text/plain; charset=utf-8",
            "selected.TXT",
            "b" * 32,
            "file",
        )
        unsafe_extension = validate_upload(
            b"opaque",
            "application/octet-stream",
            "archive.not-safe!",
            "c" * 32,
            "file",
        )

        self.assertEqual((pdf.kind, pdf.suffix), ("file", ".pdf"))
        self.assertEqual(pdf.display_name, "Quarterly Report.PDF")
        self.assertEqual((selected_text.kind, selected_text.suffix), ("file", ".txt"))
        self.assertEqual(unsafe_extension.suffix, ".bin")

    def test_managed_generic_file_keeps_fixed_identity_hash_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            upload = validate_upload(
                b"PK\x03\x04fixture",
                "application/zip",
                "bundle.ZIP",
                "d" * 32,
                "file",
            )
            relative_path = write_upload(root, upload)
            attachment = {
                "relative_path": relative_path,
                "byte_size": upload.byte_size,
                "sha256": upload.sha256,
            }

            resolved = resolve_attachment_path(root, attachment)

            self.assertEqual(resolved.name, "d" * 32 + ".zip")
            self.assertEqual(resolved.read_bytes(), upload.content)
            self.assertEqual(hashlib.sha256(resolved.read_bytes()).hexdigest(), upload.sha256)
            attachment["relative_path"] = ".runtime/web/attachments/not-managed.zip"
            with self.assertRaises(ConversationError):
                resolve_attachment_path(root, attachment)

    def test_png_and_jpeg_dimensions_are_bounded_before_preview(self):
        safe_png = validate_upload(_png(1200, 900), "image/png", "safe.png")
        safe_jpeg = validate_upload(_jpeg(1200, 900), "image/jpeg", "safe.jpg")

        self.assertEqual(safe_png.kind, "image")
        self.assertEqual(safe_jpeg.kind, "image")
        for content, media_type in (
            (_png(MAX_IMAGE_DIMENSION + 1, 1), "image/png"),
            (_jpeg(10_000, MAX_IMAGE_PIXELS // 10_000 + 1), "image/jpeg"),
        ):
            with self.subTest(media_type=media_type), self.assertRaises(
                ConversationError
            ) as caught:
                validate_upload(content, media_type, None)
            self.assertEqual(caught.exception.code, "image_dimensions_too_large")
            self.assertEqual(caught.exception.status, 413)

    def test_orphan_cleanup_recognizes_safe_generic_managed_names_only(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            base = root / ".runtime" / "web" / "attachments"
            base.mkdir(parents=True)
            generic = base / ("e" * 32 + ".docx")
            unrelated = base / "not-managed.docx"
            generic.write_bytes(b"generic")
            unrelated.write_bytes(b"unrelated")
            os.utime(generic, (1, 1))
            os.utime(unrelated, (1, 1))

            removed = cleanup_orphaned_attachment_files(root, [], now=100_000)

            self.assertEqual(removed, 1)
            self.assertFalse(generic.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
