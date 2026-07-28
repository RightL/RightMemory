import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.update_queue import UpdateCandidate
from rightmemory.update_record import (
    UpdateRecord,
    UpdateRecordFormatError,
    UpdateRecordStore,
    validate_update_records,
)


class UpdateRecordTests(unittest.TestCase):
    def _candidate(
        self,
        *,
        uid: str = "a" * 32,
        session_id: str = "agent-session",
        display_id: int = 1,
        message: str = "retain this candidate",
    ) -> UpdateCandidate:
        return UpdateCandidate(
            uid=uid,
            session_id=session_id,
            display_id=display_id,
            message=message,
            submitted_at="2026-07-27T12:00:00+00:00",
        )

    def test_record_round_trips_exact_candidates_at_operation_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            candidates = (
                self._candidate(
                    uid="b" * 32,
                    session_id="second",
                    message="second exact message",
                ),
                self._candidate(message="first exact message"),
            )
            record = UpdateRecord.from_candidates(candidates)
            store = UpdateRecordStore(root)

            path = store.write(record)

            self.assertEqual(path, root / "update_records" / f"{record.operation_id}.json")
            self.assertEqual(store.read(record.operation_id), record)
            self.assertEqual(validate_update_records(root), [])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["operation_id"], record.operation_id)
            self.assertEqual(
                [item["message"] for item in data["candidates"]],
                ["first exact message", "second exact message"],
            )

    def test_record_path_is_immutable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = UpdateRecordStore(root)
            record = UpdateRecord.from_candidates((self._candidate(),))
            path = store.write(record)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["candidates"][0]["message"] = "conflicting evidence"
            path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaises(UpdateRecordFormatError):
                store.write(record)

    def test_validation_rejects_unrecognized_record_paths(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            records = root / "update_records"
            records.mkdir()
            (records / "notes.txt").write_text("not a record", encoding="utf-8")

            diagnostics = validate_update_records(root)

        self.assertEqual(
            diagnostics,
            ["update_records/notes.txt: path is not a canonical update record"],
        )


if __name__ == "__main__":
    unittest.main()
