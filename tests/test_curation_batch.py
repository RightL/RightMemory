import unittest
from datetime import UTC, datetime, timedelta

from rightmemory.config import CurationBatchConfig
from rightmemory.curation_batch import select_curation_batch
from rightmemory.learning import LearningCandidate


START = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(index, *, session="session", at=START, evidence="evidence"):
    return LearningCandidate(str(index), session, None, at.isoformat(), evidence)


class CurationBatchTests(unittest.TestCase):
    def test_no_pending_has_no_deadline(self):
        selection = select_curation_batch([], CurationBatchConfig(), now=START)
        self.assertEqual(selection.candidates, ())
        self.assertIsNone(selection.next_ready_at)

    def test_count_trigger_boundary_bypasses_quiet_period(self):
        pending = [candidate(index) for index in range(15)]
        waiting = select_curation_batch(pending[:14], CurationBatchConfig(), now=START)
        self.assertEqual(waiting.candidates, ())
        self.assertEqual(waiting.next_ready_at, START + timedelta(hours=25))
        ready = select_curation_batch(pending, CurationBatchConfig(), now=START)
        self.assertEqual(len(ready.candidates), 15)
        self.assertIsNone(ready.next_ready_at)

    def test_fallback_is_24_hours_after_quiet_completion(self):
        pending = [candidate(1)]
        deadline = START + timedelta(hours=25)
        waiting = select_curation_batch(pending, CurationBatchConfig(), now=deadline - timedelta(microseconds=1))
        self.assertEqual(waiting.candidates, ())
        self.assertEqual(waiting.next_ready_at, deadline)
        ready = select_curation_batch(pending, CurationBatchConfig(), now=deadline)
        self.assertEqual(ready.candidates, tuple(pending))
        self.assertIsNone(ready.next_ready_at)

    def test_new_submission_resets_same_session_quiet_time(self):
        pending = [candidate(1), candidate(2, at=START + timedelta(minutes=30))]
        selection = select_curation_batch(pending, CurationBatchConfig(), now=START + timedelta(hours=25))
        self.assertEqual(selection.candidates, ())
        self.assertEqual(selection.next_ready_at, START + timedelta(hours=25, minutes=30))

    def test_new_session_does_not_delay_old_session(self):
        old = candidate(1, session="old")
        new = candidate(2, session="new", at=START + timedelta(hours=25))
        selection = select_curation_batch([new, old], CurationBatchConfig(), now=START + timedelta(hours=25))
        self.assertEqual(selection.candidates, (old,))

    def test_target_splits_sessions_and_orders_by_actual_timestamp(self):
        pending = [candidate(3, session="z", at=START - timedelta(minutes=1)),
                   candidate(2, session="a"), candidate(1, session="a"),
                   candidate(4, session="b")]
        config = CurationBatchConfig(trigger_candidates=2, target_batch_candidates=3)
        selection = select_curation_batch(reversed(pending), config, now=START)
        self.assertEqual([item.candidate_id for item in selection.candidates], ["3", "1", "2"])

    def test_evidence_limit_and_first_oversized_candidate_make_progress(self):
        config = CurationBatchConfig(trigger_candidates=1)
        for sizes, expected in (((12_000, 12_000, 1), 2), ((12_000, 12_001), 1), ((24_001, 1), 1)):
            with self.subTest(sizes=sizes):
                pending = [candidate(index, evidence="x" * size) for index, size in enumerate(sizes)]
                selection = select_curation_batch(pending, config, now=START)
                self.assertEqual(len(selection.candidates), expected)

    def test_timezone_offsets_are_normalized_for_ordering_and_deadlines(self):
        old = LearningCandidate("old", "old", None, "2026-01-01T07:00:00+08:00", "old")
        new = LearningCandidate("new", "new", None, "2026-01-01T00:00:00Z", "new")
        config = CurationBatchConfig(trigger_candidates=2)
        selection = select_curation_batch([new, old], config, now=START)
        self.assertEqual(selection.candidates, (old, new))
        waiting = select_curation_batch([old], CurationBatchConfig(), now=START)
        self.assertEqual(waiting.next_ready_at, START + timedelta(hours=24))
        self.assertIs(waiting.next_ready_at.tzinfo, UTC)

    def test_naive_or_invalid_timestamps_are_rejected(self):
        for timestamp in ("2026-01-01T00:00:00", "invalid"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                select_curation_batch([LearningCandidate("1", "s", None, timestamp, "e")], CurationBatchConfig(), now=START)
        with self.assertRaisesRegex(ValueError, "timezone"):
            select_curation_batch([], CurationBatchConfig(), now=START.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
