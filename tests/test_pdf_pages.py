"""PDF 按页分流的纯规则（core/pdf_pages.py，BC-01，2026-10-01 操作者确认）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pdf_pages import (  # noqa: E402
    MISSING_OCR_DEFERRED,
    MISSING_OCR_FAILED,
    RETRY_EVERY_ROUND,
    missing_reason_for,
    page_runs,
    run_page_count,
    splice_pages,
    split_runs,
)


class TestPageRuns(unittest.TestCase):
    def test_consecutive_pages_become_one_run(self):
        self.assertEqual(page_runs([3, 4, 5]), [(3, 5)])

    def test_runs_separated_by_a_couple_of_text_pages_are_merged(self):
        """中间只隔 1～2 页文字页的并成一段送识别：少发几次请求，识别结果不会比文字层少。"""
        self.assertEqual(page_runs([2, 4, 7]), [(2, 7)])
        self.assertEqual(page_runs([2, 6]), [(2, 2), (6, 6)])

    def test_order_duplicates_and_nonsense_are_tolerated(self):
        self.assertEqual(page_runs([9, 1, 1, 0, -3]), [(1, 1), (9, 9)])
        self.assertEqual(page_runs([]), [])

    def test_long_runs_are_cut_to_the_per_request_limit(self):
        """云端每份最多 200 页：长段按顺序切开。"""
        self.assertEqual(split_runs([(1, 450)], 200), [(1, 200), (201, 400), (401, 450)])
        self.assertEqual(split_runs([(3, 4)], None), [(3, 4)])
        self.assertEqual(run_page_count(split_runs([(1, 450), (500, 501)], 200)), 452)


class TestSplicePages(unittest.TestCase):
    TEXTS = ("p1\n\n", "", "p3 caption\n\n", "p4\n\n", "p5\n\n")

    def test_recognized_runs_replace_their_pages_in_place(self):
        text, recognized, missing = splice_pages(self.TEXTS, (2, 3), {(2, 3): "OCR two-three"})
        self.assertEqual(text, "p1\n\nOCR two-three\n\np4\n\np5\n\n")
        self.assertEqual(recognized, (2, 3))
        self.assertEqual(missing, ())

    def test_a_failed_run_keeps_the_text_layer_and_lists_only_its_picture_pages(self):
        """段里顺带并进来的文字页不算缺：它的字文字层已经有了。"""
        text, recognized, missing = splice_pages(self.TEXTS, (2, 4), {(2, 4): None})
        self.assertEqual(text, "".join(self.TEXTS))
        self.assertEqual(recognized, ())
        self.assertEqual(missing, (2, 4))

    def test_mixed_success_and_failure(self):
        text, recognized, missing = splice_pages(self.TEXTS, (1, 5), {(1, 1): "OCR one", (5, 5): None})
        self.assertTrue(text.startswith("OCR one\n\n"))
        self.assertTrue(text.endswith("p5\n\n"))
        self.assertEqual((recognized, missing), ((1,), (5,)))

    def test_blank_recognition_counts_as_not_recognized(self):
        _, recognized, missing = splice_pages(self.TEXTS, (2,), {(2, 2): "   \n"})
        self.assertEqual((recognized, missing), ((), (2,)))


class TestMissingReason(unittest.TestCase):
    def test_any_temporarily_unavailable_piece_means_try_again_next_round(self):
        self.assertEqual(missing_reason_for(["extract-failed", "deferred"]), MISSING_OCR_DEFERRED)
        self.assertIn(MISSING_OCR_DEFERRED, RETRY_EVERY_ROUND)

    def test_otherwise_it_is_a_failure_that_waits_for_conditions_to_change(self):
        self.assertEqual(missing_reason_for(["extract-failed"]), MISSING_OCR_FAILED)
        self.assertNotIn(MISSING_OCR_FAILED, RETRY_EVERY_ROUND)


if __name__ == "__main__":
    unittest.main()
