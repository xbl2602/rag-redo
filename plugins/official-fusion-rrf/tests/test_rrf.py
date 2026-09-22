from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_fusion_rrf.rrf import reciprocal_rank_fusion  # noqa: E402


class TestReciprocalRankFusion(unittest.TestCase):
    def test_doc_top_in_both_lists_ranks_first(self):
        result = reciprocal_rank_fusion([["a", "b", "c"], ["a", "c", "b"]])
        self.assertEqual(result[0][0], "a")

    def test_doc_only_in_one_list_still_gets_score(self):
        result = reciprocal_rank_fusion([["a", "b"], ["c"]])
        doc_ids = {doc_id for doc_id, _ in result}
        self.assertEqual(doc_ids, {"a", "b", "c"})

    def test_appearing_in_more_lists_ranks_higher_than_single_list_top(self):
        # b 在两路都排第2，a 只在一路排第1——两路加权累积应该让 b 更靠前
        result = reciprocal_rank_fusion([["a", "b"], ["x", "b"]])
        ranking = [doc_id for doc_id, _ in result]
        self.assertEqual(ranking[0], "b")

    def test_weights_affect_ranking(self):
        result = reciprocal_rank_fusion([["a"], ["b"]], weights=[10.0, 0.1])
        self.assertEqual(result[0][0], "a")

    def test_mismatched_weights_length_raises(self):
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])

    def test_empty_lists_return_empty(self):
        self.assertEqual(reciprocal_rank_fusion([[], []]), [])

    def test_single_list_preserves_order(self):
        result = reciprocal_rank_fusion([["a", "b", "c"]])
        self.assertEqual([d for d, _ in result], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
