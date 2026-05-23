import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from rightmemory.dreamer_trigger import DreamerTriggerStore


class DreamerTriggerStoreTests(unittest.TestCase):
    def test_missing_state_reads_zero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = DreamerTriggerStore(Path(tempdir))

            state = store.load()

        self.assertEqual(state.points, 0)
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_successful_dream_at)
        self.assertIsNone(state.last_recovery_at)

    def test_minimal_valid_state_loads_points_with_empty_timestamps(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "dreamer" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"points": 2.5}), encoding="utf-8")
            store = DreamerTriggerStore(root)

            state = store.load()
            backups = list(state_path.parent.glob("trigger-state.corrupt-*.json"))

        self.assertAlmostEqual(state.points, 2.5)
        self.assertIsNone(state.updated_at)
        self.assertIsNone(state.last_successful_dream_at)
        self.assertIsNone(state.last_recovery_at)
        self.assertEqual(backups, [])

    def test_increment_preserves_fractional_points(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = DreamerTriggerStore(root)

            first = store.increment(1.25)
            second = store.increment(0.5)

            state = store.load()
            gitignore = (root / ".runtime" / ".gitignore").read_text(encoding="utf-8")

        self.assertAlmostEqual(first.points, 1.25)
        self.assertAlmostEqual(second.points, 1.75)
        self.assertAlmostEqual(state.points, 1.75)
        self.assertIsNotNone(state.updated_at)
        self.assertIsNone(state.last_successful_dream_at)
        self.assertEqual(gitignore, "*\n")

    def test_consume_threshold_keeps_excess(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = DreamerTriggerStore(Path(tempdir))
            store.increment(2.5)

            consumed = store.consume_if_available(1.0)
            state = store.load()

        self.assertTrue(consumed)
        self.assertAlmostEqual(state.points, 1.5)
        self.assertIsNotNone(state.updated_at)
        self.assertEqual(state.last_successful_dream_at, state.updated_at)

    def test_consume_below_threshold_returns_false_and_keeps_balance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = DreamerTriggerStore(Path(tempdir))
            store.increment(0.75)
            before = store.load()

            consumed = store.consume_if_available(1.0)
            after = store.load()

        self.assertFalse(consumed)
        self.assertAlmostEqual(after.points, 0.75)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertIsNone(after.last_successful_dream_at)

    def test_corrupt_state_is_backed_up_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "dreamer" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not json", encoding="utf-8")
            store = DreamerTriggerStore(root)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                state = store.load()

            backups = list(state_path.parent.glob("trigger-state.corrupt-*.json"))
            self.assertEqual(len(backups), 1)
            backup_content = backups[0].read_text(encoding="utf-8")

        self.assertEqual(state.points, 0)
        self.assertIsNotNone(state.last_recovery_at)
        self.assertEqual(backup_content, "{not json")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("recovered corrupt dreamer trigger state", stderr.getvalue())

    def test_invalid_stored_points_is_backed_up_and_rebuilt(self):
        invalid_states = [
            '{"points": NaN, "updated_at": null, "last_successful_dream_at": null, "last_recovery_at": null}\n',
            json.dumps(
                {
                    "points": -0.1,
                    "updated_at": None,
                    "last_successful_dream_at": None,
                    "last_recovery_at": None,
                }
            )
            + "\n",
            json.dumps(
                {
                    "points": 10**400,
                    "updated_at": None,
                    "last_successful_dream_at": None,
                    "last_recovery_at": None,
                }
            )
            + "\n",
        ]

        for content in invalid_states:
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as tempdir:
                    root = Path(tempdir)
                    state_path = root / ".runtime" / "dreamer" / "trigger-state.json"
                    state_path.parent.mkdir(parents=True)
                    state_path.write_text(content, encoding="utf-8")
                    store = DreamerTriggerStore(root)
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        state = store.load()

                    backups = list(state_path.parent.glob("trigger-state.corrupt-*.json"))
                    self.assertEqual(len(backups), 1)
                    backup_content = backups[0].read_text(encoding="utf-8")

                self.assertEqual(state.points, 0)
                self.assertIsNotNone(state.last_recovery_at)
                self.assertEqual(backup_content, content)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("recovered corrupt dreamer trigger state", stderr.getvalue())

    def test_invalid_increment_and_threshold_inputs_raise_value_error(self):
        invalid_values = [0, -1, math.nan, math.inf, -math.inf]

        with tempfile.TemporaryDirectory() as tempdir:
            store = DreamerTriggerStore(Path(tempdir))

            for value in invalid_values:
                with self.subTest(operation="increment", value=value):
                    with self.assertRaises(ValueError):
                        store.increment(value)

            for value in invalid_values:
                with self.subTest(operation="consume", value=value):
                    with self.assertRaises(ValueError):
                        store.consume_if_available(value)


if __name__ == "__main__":
    unittest.main()
