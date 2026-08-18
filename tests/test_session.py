import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.session import MessageSessionStore


class MessageSessionStoreTests(unittest.TestCase):
    def test_ordinary_session_ids_keep_existing_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MessageSessionStore(Path(tempdir), "update")
            session_id = "s" * 48
            paths = store.paths(session_id)

        self.assertEqual(paths.history.name, f"{session_id}.json")
        self.assertEqual(paths.lock.name, f"{session_id}.lock")
        self.assertEqual(paths.history.parent, store.root)

    def test_session_id_beyond_plain_limit_uses_hashed_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MessageSessionStore(Path(tempdir), "update")
            paths = store.paths("s" * 49)

        self.assertEqual(paths.history.parent.name, "hashed")

    def test_existing_long_history_keeps_legacy_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MessageSessionStore(Path(tempdir), "update")
            session_id = "legacy-" + "s" * 64
            legacy_history = store.root / f"{session_id}.json"
            legacy_history.parent.mkdir(parents=True)
            legacy_history.write_bytes(b'[{"message":"existing"}]')

            paths = store.paths(session_id)
            with store.locked(session_id) as locked:
                saved = locked.load_json()

        self.assertEqual(paths.history, legacy_history)
        self.assertEqual(saved, b'[{"message":"existing"}]')

    def test_long_session_ids_use_stable_hashed_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MessageSessionStore(Path(tempdir), "update")
            first_id = f"update-batch-{'a' * 64}"
            second_id = f"update-batch-{'b' * 64}"

            first = store.paths(first_id)
            repeated = store.paths(first_id)
            second = store.paths(second_id)

        self.assertEqual(first, repeated)
        self.assertEqual(first.history.parent.name, "hashed")
        self.assertEqual(first.history.parent.parent, store.root)
        self.assertRegex(first.history.name, r"^[0-9a-f]{32}\.json$")
        self.assertRegex(first.lock.name, r"^[0-9a-f]{32}\.lock$")
        self.assertEqual(first.history.stem, first.lock.stem)
        self.assertNotEqual(first.history, second.history)
        self.assertNotIn(first_id, str(first.history))

    def test_long_session_history_round_trips_with_short_atomic_temp_name(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = MessageSessionStore(Path(tempdir), "update")
            session_id = f"update-batch-{'a' * 64}"
            replacements = []
            real_replace = os.replace

            def capture_replace(source, destination):
                replacements.append((Path(source), Path(destination)))
                real_replace(source, destination)

            with (
                store.locked(session_id) as locked,
                patch("rightmemory.session.os.replace", side_effect=capture_replace),
            ):
                locked.save_json(b'[{"message":"remember"}]')
                saved = locked.load_json()

            source, destination = replacements[-1]
            expected = store.paths(session_id).history

        self.assertEqual(saved, b'[{"message":"remember"}]')
        self.assertEqual(destination, expected)
        self.assertEqual(source.parent, destination.parent)
        self.assertNotIn(destination.name, source.name)
        self.assertTrue(re.fullmatch(r"\.\d+\.[0-9a-f]{32}\.tmp", source.name))


if __name__ == "__main__":
    unittest.main()
