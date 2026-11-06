import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rightmemory import retained_text
from rightmemory.retained_text import count_retained_text


class RetainedTextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative: str, data: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_counts_only_domain_owned_files_including_untracked_text_assets(self):
        self.write("MEMORY.md", b"hello\n")
        self.write("MEMORY_context.md", b" world")
        self.write("MEMORY_SKILL_examples.md", b"asset\n")
        self.write("MEMORY_agent-corrections-design.md", b"hello\n")
        self.write("learning/guidance/nested/example.md", b" world")
        self.write("learning/guidance/ignored.txt", b"\xff")
        self.write("learning/experiences/example.md", b"hello\n")
        self.write("learning/skills/example/SKILL.md", b"hello\n")
        self.write("learning/skills/example/check.py", b'print("ok")\n')
        self.write("learning/skills/example/.runtime/notes.txt", b"asset\n")
        self.write("learning/skills/example/.git/objects/file", b"\xff")
        self.write(f"learning/candidates/{'a' * 64}.json", b"\xff")
        self.write("learning/claims/memory.json", b"\xff")
        self.write(".runtime/MEMORY_cache.md", b"\xff")
        self.write(".git/MEMORY_index.md", b"\xff")
        self.write("PURSUITS.md", b"\xff")
        self.write("corrections.md", b"\xff")

        expected = {
            "memory": {"MEMORY.md": 2, "MEMORY_context.md": 1},
            "guidance": {
                "MEMORY_agent-corrections-design.md": 2,
                "learning/guidance/nested/example.md": 1,
            },
            "skills": {
                "MEMORY_SKILL_examples.md": 2,
                "learning/experiences/example.md": 2,
                "learning/skills/example/SKILL.md": 2,
                "learning/skills/example/check.py": 4,
                "learning/skills/example/.runtime/notes.txt": 2,
            },
        }
        for domain, by_file in expected.items():
            with self.subTest(domain=domain):
                self.assertEqual(count_retained_text(self.root, domain), {
                    "domain": domain,
                    "encoding": "o200k_base",
                    "retained_text_tokens": sum(by_file.values()),
                    "text_file_count": len(by_file),
                    "tokens_by_text_file": by_file,
                    "binary_assets": [],
                })

    def test_counts_files_separately_and_normalizes_newlines(self):
        self.write("MEMORY_first.md", b"hel")
        self.write("MEMORY_second.md", b"lo")
        self.write("MEMORY_lf.md", b"one\ntwo\nthree\n")
        self.write("MEMORY_mixed.md", b"one\r\ntwo\rthree\r\n")
        self.write("MEMORY_special.md", b"# Title\n\n<|endoftext|>\n")
        result = count_retained_text(self.root, "memory")
        self.assertEqual(result["tokens_by_text_file"], {
            "MEMORY_first.md": 1, "MEMORY_second.md": 1, "MEMORY_lf.md": 6,
            "MEMORY_mixed.md": 6, "MEMORY_special.md": 10,
        })
        self.assertEqual(result["retained_text_tokens"], 24)

    def test_reports_recognized_binary_assets_and_counts_text_with_a_binary_extension(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7ioAAAAASUVORK5CYII="
        )
        name = "learning/skills/example/screenshot.png"
        self.write(name, png)
        self.write("learning/skills/example/notes.png", b"asset\n")
        result = count_retained_text(self.root, "skills")
        self.assertEqual(result["tokens_by_text_file"], {"learning/skills/example/notes.png": 2})
        self.assertEqual(result["binary_assets"], [{"path": name, "bytes": len(png)}])
        self.assertEqual(result["text_file_count"], 1)

    def test_rejects_invalid_utf8_even_with_a_binary_extension(self):
        name = "learning/skills/example/bad.png"
        self.write(name, b"bad\xff")
        with self.assertRaisesRegex(ValueError, r"Invalid UTF-8.*bad\.png at byte 3"):
            count_retained_text(self.root, "skills")

    def test_reports_opaque_bin_assets_and_counts_utf8_bin_text(self):
        prefix = "learning/skills/example/"
        self.write(prefix + "fixture.bin", b"\x00\x01")
        self.write(prefix + "sample.bin", b"\xff\x01")
        self.write(prefix + "notes.bin", b"asset\n")
        result = count_retained_text(self.root, "skills")
        self.assertEqual(result["tokens_by_text_file"], {prefix + "notes.bin": 2})
        self.assertEqual(result["binary_assets"], [
            {"path": prefix + "fixture.bin", "bytes": 2},
            {"path": prefix + "sample.bin", "bytes": 2},
        ])

    def test_rejects_invalid_utf8_in_owned_markdown(self):
        self.write("MEMORY.md", b"\x00\x01\xff")
        with self.assertRaisesRegex(ValueError, r"Invalid UTF-8.*MEMORY\.md at byte 2"):
            count_retained_text(self.root, "memory")

    def test_rejects_owned_non_file(self):
        (self.root / "MEMORY.md").mkdir()
        with self.assertRaisesRegex(ValueError, r"regular file: MEMORY\.md"):
            count_retained_text(self.root, "memory")

    def test_rejects_unreadable_owned_file(self):
        path = self.write("MEMORY.md", b"hello")
        real_open = os.open

        def denied(candidate, *args, **kwargs):
            if Path(candidate) == path:
                raise PermissionError("unreadable fixture")
            return real_open(candidate, *args, **kwargs)

        with patch.object(retained_text.os, "open", side_effect=denied):
            with self.assertRaisesRegex(ValueError, r"Could not read owned file MEMORY\.md"):
                count_retained_text(self.root, "memory")

    def test_rejects_owned_file_symlink(self):
        target = self.write("unowned.md", b"hello")
        try:
            (self.root / "MEMORY.md").symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, r"link or reparse point: MEMORY\.md"):
            count_retained_text(self.root, "memory")

    def test_rejects_reparse_directories_that_can_contain_owned_files(self):
        path = self.write("learning/skills/example/.runtime/notes.txt", b"hello")
        real_lstat = Path.lstat
        for linked in (self.root / "learning", path.parent.parent, path.parent):
            def junction(candidate):
                status = real_lstat(candidate)
                if candidate == linked:
                    return SimpleNamespace(
                        st_mode=status.st_mode,
                        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                    )
                return status

            relative = linked.relative_to(self.root).as_posix()
            with self.subTest(relative=relative), patch.object(Path, "lstat", new=junction):
                with self.assertRaises(ValueError) as exc:
                    count_retained_text(self.root, "skills")
                self.assertIn(f"link or reparse point: {relative}", str(exc.exception))

    def test_bundled_encoding_loads_offline_once_and_leaves_cache_untouched(self):
        self.write("MEMORY.md", b"hello\n")
        cache_dir = self.root / "token-cache"
        retained_text._encoding.cache_clear()
        self.addCleanup(retained_text._encoding.cache_clear)
        with (
            patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": str(cache_dir)}),
            patch("tiktoken.get_encoding", side_effect=AssertionError("registry use")),
            patch("tiktoken.load.read_file_cached", side_effect=AssertionError("cache use")),
            patch.object(retained_text.resources, "files", wraps=retained_text.resources.files) as files,
        ):
            self.assertEqual(count_retained_text(self.root, "guidance")["retained_text_tokens"], 0)
            files.assert_not_called()
            for _ in range(2):
                self.assertEqual(count_retained_text(self.root, "memory")["retained_text_tokens"], 2)
            files.assert_called_once_with("rightmemory")
        self.assertFalse(cache_dir.exists())

    def test_rejects_corrupted_bundled_ranks(self):
        self.write("MEMORY.md", b"hello\n")
        package = self.root / "package"
        self.write("package/data/o200k_base.tiktoken", b"corrupted resource")
        retained_text._encoding.cache_clear()
        self.addCleanup(retained_text._encoding.cache_clear)
        with patch.object(retained_text.resources, "files", return_value=package):
            with self.assertRaisesRegex(ValueError, "expected SHA-256"):
                count_retained_text(self.root, "memory")
