import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.insight_trigger import InsightTriggerStore


class InsightTriggerStoreTests(unittest.TestCase):
    def test_minimal_valid_state_loads_with_no_operation_receipts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "insight" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"points": 2.5}), encoding="utf-8")

            state = InsightTriggerStore(root).load()

        self.assertEqual(state.points, 2.5)
        self.assertEqual(state.applied_operation_ids, ())

    def test_increment_and_consume_use_insight_runtime_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)

            state = store.increment(12.5)
            consumed = store.consume_if_available(10.0)
            after = store.read()
            state_file_exists = (root / ".runtime" / "insight" / "trigger-state.json").exists()

        self.assertEqual(state.points, 12.5)
        self.assertTrue(consumed)
        self.assertEqual(after.points, 2.5)
        self.assertIsNotNone(after.last_successful_insight_at)
        self.assertTrue(state_file_exists)

    def test_increment_once_persists_receipt_and_does_not_repeat_points(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)

            first = store.increment_once("update-op-1", 2.5)
            repeated = InsightTriggerStore(root).increment_once("update-op-1", 2.5)
            second = store.increment_once("update-op-2", 1.0)
            store.forget_operation("update-op-1")
            persisted = store.load()

        self.assertEqual(first.points, 2.5)
        self.assertEqual(repeated, first)
        self.assertEqual(second.points, 3.5)
        self.assertEqual(persisted.points, 3.5)
        self.assertEqual(persisted.applied_operation_ids, ("update-op-2",))

    def test_claimed_watch_operation_survives_restart_until_completed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(155.0)

            first = store.claim_operation(150.0)
            recovered = InsightTriggerStore(root).claim_operation(150.0)
            completed = InsightTriggerStore(root).complete_operation(first, 150.0, result="artifact")
            state = store.read()

        self.assertIsNotNone(first)
        self.assertEqual(recovered, first)
        self.assertTrue(completed)
        self.assertEqual(state.points, 5.0)
        self.assertIsNone(state.active_operation_id)
        self.assertIsNone(state.active_operation_points)
        self.assertEqual(state.last_successful_insight_result, "artifact")

    def test_active_claim_keeps_its_original_threshold_after_config_change(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(155.0)

            operation_id = store.claim_operation(150.0)
            recovered = InsightTriggerStore(root).claim_operation(200.0)
            completed = InsightTriggerStore(root).complete_operation(
                operation_id,
                200.0,
                result="artifact",
            )
            state = store.read()

        self.assertEqual(recovered, operation_id)
        self.assertTrue(completed)
        self.assertEqual(state.points, 5.0)

    def test_consume_below_threshold_preserves_points(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(3.0)

            consumed = store.consume_if_available(5.0)
            after = store.read()

        self.assertFalse(consumed)
        self.assertEqual(after.points, 3.0)
        self.assertIsNone(after.last_successful_insight_at)

    def test_consume_records_last_successful_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(12.5)

            consumed = store.consume_if_available(10.0, result="artifact")
            after = store.read()

        self.assertTrue(consumed)
        self.assertEqual(after.last_successful_insight_result, "artifact")

    def test_consume_rejects_invalid_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(12.5)

            with self.assertRaises(ValueError):
                store.consume_if_available(10.0, result="maybe")

    def test_corrupt_state_is_recovered_under_insight_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "insight" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not json", encoding="utf-8")

            state = InsightTriggerStore(root).read()
            backups = list(state_path.parent.glob("trigger-state.corrupt-*.json"))
            backup_content = backups[0].read_text(encoding="utf-8") if backups else None

        self.assertEqual(state.points, 0.0)
        self.assertIsNotNone(state.last_recovery_at)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backup_content, "{not json")

    def test_rejects_invalid_state_values(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "insight" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"points": -1}), encoding="utf-8")

            state = InsightTriggerStore(root).read()

        self.assertEqual(state.points, 0.0)
        self.assertIsNotNone(state.last_recovery_at)


if __name__ == "__main__":
    unittest.main()
