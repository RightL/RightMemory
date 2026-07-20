import tempfile
import unittest
from pathlib import Path

from rightmemory.watch_operation import WatchOperationStore


class WatchOperationStoreTests(unittest.TestCase):
    def test_claim_survives_restart_until_completed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = WatchOperationStore(root, "pruner").claim()
            recovered = WatchOperationStore(root, "pruner").claim()
            completed = WatchOperationStore(root, "pruner").complete(first)
            next_operation = WatchOperationStore(root, "pruner").claim()

        self.assertEqual(recovered, first)
        self.assertTrue(completed)
        self.assertNotEqual(next_operation, first)


if __name__ == "__main__":
    unittest.main()
