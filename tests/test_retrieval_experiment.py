"""Numerical checks for the experiment's evaluation, not agent-facing content."""
import unittest

from experiments.retrieval_embeddings import fused_ranks, rank_indices, score_ids


class RetrievalExperimentMetricsTests(unittest.TestCase):
    def test_duplicate_delivery_does_not_inflate_recall(self):
        score = score_ids(["a", "a", "c"], ["a", "b"], ["c"])
        self.assertEqual(score["found"], 1)
        self.assertEqual(score["returned"], 2)
        self.assertEqual(score["recall"], 0.5)
        self.assertEqual(score["missing_ids"], ["b"])
        self.assertEqual(score["unlabelled"], 0)

    def test_optional_context_is_not_required_or_counted_as_unlabelled(self):
        score = score_ids(["a", "b", "x"], ["a"], ["b", "c"])
        self.assertTrue(score["all_required"])
        self.assertEqual(score["label_precision"], 2 / 3)
        self.assertEqual(score["unlabelled_ids"], ["x"])

    def test_no_answer_has_no_recall_denominator(self):
        empty = score_ids([], [])
        extra = score_ids(["x"], [])
        self.assertIsNone(empty["recall"])
        self.assertIsNone(extra["recall"])
        self.assertTrue(empty["empty"])
        self.assertFalse(extra["empty"])
        self.assertEqual(extra["unlabelled"], 1)

    def test_zero_lexical_scores_do_not_invent_rank_evidence(self):
        self.assertEqual(fused_ranks([2, 0, 1], [0, 1, 2], [0, 0, 0]), [2, 0, 1])

    def test_ties_are_stable(self):
        self.assertEqual(rank_indices([0.1, 0.8, 0.8]), [1, 2, 0])


if __name__ == "__main__":
    unittest.main()
