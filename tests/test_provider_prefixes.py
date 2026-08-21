import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.provider_prefixes import ProviderPrefixRecord, ProviderPrefixStore


PREFIX_KEY = "a" * 64
CREATED_AT = "2026-08-21T01:02:03+00:00"
UPDATED_AT = "2026-08-21T02:03:04+00:00"


def _wait_for_prefix_lock(memory_root: str, ready, acquired) -> None:
    store = ProviderPrefixStore(Path(memory_root))
    ready.set()
    with store.locked("codex", PREFIX_KEY):
        acquired.set()


class ProviderPrefixStoreTests(unittest.TestCase):
    def test_round_trips_exact_schema_at_validated_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ProviderPrefixStore(root)
            record = ProviderPrefixRecord(
                provider="codex",
                prefix_key=PREFIX_KEY,
                provider_session_id="thread-1",
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )

            store.save(record)
            loaded = store.load("codex", PREFIX_KEY)
            path = store.path("codex", PREFIX_KEY)
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(loaded, record)
            self.assertEqual(
                path,
                root / ".runtime" / "agent_cli_prefixes" / "codex" / f"{PREFIX_KEY}.json",
            )
            self.assertEqual(
                set(data),
                {
                    "provider",
                    "prefix_key",
                    "provider_session_id",
                    "created_at",
                    "updated_at",
                    "schema_version",
                },
            )
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual((root / ".runtime" / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_save_uses_unique_atomic_temp_and_fsyncs_file_and_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderPrefixStore(Path(tempdir))
            record = ProviderPrefixRecord(
                provider="codex",
                prefix_key=PREFIX_KEY,
                provider_session_id="thread-1",
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )
            replacements: list[tuple[Path, Path]] = []
            real_replace = os.replace

            def capture_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                real_replace(source, destination)

            with store.locked("codex", PREFIX_KEY):
                pass
            with (
                patch("rightmemory.provider_prefixes.os.replace", side_effect=capture_replace),
                patch("rightmemory.provider_prefixes.os.fsync") as fsync,
                patch("rightmemory.provider_prefixes._fsync_directory") as fsync_directory,
            ):
                store.save(record)

            source, destination = replacements[-1]
            self.assertEqual(destination, store.path("codex", PREFIX_KEY))
            self.assertEqual(source.parent, destination.parent)
            self.assertRegex(source.name, r"^\.\d+\.[0-9a-f]{32}\.tmp$")
            self.assertNotIn(destination.name, source.name)
            fsync.assert_called_once()
            fsync_directory.assert_called_with(destination.parent)

    def test_rejects_unsafe_provider_and_noncanonical_prefix_keys_before_creating_state(self):
        invalid_providers = ("../codex", "codex/child", "codex\\child", "Codex", ".", "")
        invalid_keys = (
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            "../" + "a" * 61,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ProviderPrefixStore(root)

            for provider in invalid_providers:
                with self.subTest(provider=provider), self.assertRaises(ValueError):
                    store.path(provider, PREFIX_KEY)
            for prefix_key in invalid_keys:
                with self.subTest(prefix_key=prefix_key), self.assertRaises(ValueError):
                    store.path("codex", prefix_key)

            self.assertFalse((root / ".runtime").exists())

    def test_load_rejects_unknown_fields_wrong_schema_and_invalid_timestamps(self):
        valid = {
            "provider": "codex",
            "prefix_key": PREFIX_KEY,
            "provider_session_id": "thread-1",
            "created_at": CREATED_AT,
            "updated_at": UPDATED_AT,
            "schema_version": 1,
        }
        mutations = (
            {**valid, "unexpected": True},
            {key: value for key, value in valid.items() if key != "updated_at"},
            {**valid, "schema_version": 2},
            {**valid, "schema_version": True},
            {**valid, "created_at": "not-a-time"},
            {**valid, "updated_at": "2026-08-21T02:03:04"},
        )
        for index, data in enumerate(mutations):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tempdir:
                store = ProviderPrefixStore(Path(tempdir))
                path = store.path("codex", PREFIX_KEY)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(data), encoding="utf-8")

                with self.assertRaises(ValueError):
                    store.load("codex", PREFIX_KEY)

    def test_load_rejects_identity_that_does_not_correspond_to_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderPrefixStore(Path(tempdir))
            path = store.path("codex", PREFIX_KEY)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "provider": "claude",
                        "prefix_key": PREFIX_KEY,
                        "provider_session_id": "thread-1",
                        "created_at": CREATED_AT,
                        "updated_at": UPDATED_AT,
                        "schema_version": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                store.load("codex", PREFIX_KEY)

    def test_delete_only_removes_matching_provider_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderPrefixStore(Path(tempdir))
            record = ProviderPrefixRecord(
                provider="codex",
                prefix_key=PREFIX_KEY,
                provider_session_id="thread-1",
                created_at=CREATED_AT,
                updated_at=UPDATED_AT,
            )
            store.save(record)

            self.assertFalse(store.delete_if_matches("codex", PREFIX_KEY, "thread-2"))
            self.assertEqual(store.load("codex", PREFIX_KEY), record)
            self.assertTrue(store.delete_if_matches("codex", PREFIX_KEY, "thread-1"))
            self.assertIsNone(store.load("codex", PREFIX_KEY))
            self.assertFalse(store.delete_if_matches("codex", PREFIX_KEY, "thread-1"))

    def test_same_prefix_lock_blocks_another_process(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tempdir:
            store = ProviderPrefixStore(Path(tempdir))
            ready = context.Event()
            acquired = context.Event()
            process = context.Process(target=_wait_for_prefix_lock, args=(tempdir, ready, acquired))
            try:
                with store.locked("codex", PREFIX_KEY):
                    process.start()
                    self.assertTrue(ready.wait(10))
                    self.assertFalse(acquired.wait(0.5))
                self.assertTrue(acquired.wait(10))
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(10)


if __name__ == "__main__":
    unittest.main()
