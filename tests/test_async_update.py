import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.async_update import AsyncUpdateStore


class AsyncUpdateStateTests(unittest.TestCase):
    def test_read_rejects_state_missing_required_identity_fields(self):
        with tempfile.TemporaryDirectory() as tempdir:
            store = AsyncUpdateStore(Path(tempdir), "update")
            state_path = store._state_path("agent-1")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"status": "succeeded", "result": "done"}), encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                store.read("agent-1")

        self.assertIn("async update state must contain string field: session_id", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
