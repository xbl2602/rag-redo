from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pymupdf  # noqa: E402

from official_extractor_pdf_text.extract import extract  # noqa: E402


def _make_text_pdf(path: Path, text: str = "Hello RAG REDO test content") -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_mixed_pdf(path: Path) -> None:
    doc = pymupdf.open()
    text_page = doc.new_page()
    text_page.insert_text((72, 72), "This page has a valid text layer")
    doc.new_page()
    doc.save(path)
    doc.close()


def _make_threshold_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_blank_pdf(path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page()  # 完全空白，没有文字层
    doc.save(path)
    doc.close()


def _make_pdf_with_page_texts(path: Path, texts: list[str]) -> None:
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 750), text)  # 自动折行，长文本不会跑出页面
    doc.save(path)
    doc.close()


class TestExtractPdfText(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_text_layer_pdf_extracts_content(self):
        _make_text_pdf(self.tmp / "a.pdf")
        doc = extract("lib1", "a.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertIn("Hello", doc.text)
        self.assertIsNone(doc.failure_reason)

    def test_no_text_layer_folds_to_scanned_failure_not_exception(self):
        _make_blank_pdf(self.tmp / "scanned.pdf")
        doc = extract("lib1", "scanned.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("scanned", doc.failure_reason)

    def test_mixed_pdf_routes_whole_document_to_scanned_failure(self):
        _make_mixed_pdf(self.tmp / "mixed.pdf")
        doc = extract("lib1", "mixed.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_exactly_ten_characters_is_a_text_page(self):
        _make_threshold_pdf(self.tmp / "ten.pdf", "1234567890")
        doc = extract("lib1", "ten.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_nine_characters_routes_to_scanned_failure(self):
        _make_threshold_pdf(self.tmp / "nine.pdf", "123456789")
        doc = extract("lib1", "nine.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_missing_file_folds_to_failure(self):
        doc = extract("lib1", "missing.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_corrupted_pdf_folds_to_failure_not_exception(self):
        (self.tmp / "corrupt.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf structure")
        doc = extract("lib1", "corrupt.pdf", self.tmp)  # 不应该抛异常
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_content_hash_is_stable_for_same_bytes(self):
        _make_text_pdf(self.tmp / "a.pdf", "same content")
        doc1 = extract("lib1", "a.pdf", self.tmp)
        doc2 = extract("lib1", "a.pdf", self.tmp)
        self.assertEqual(doc1.content_hash, doc2.content_hash)


    # ---- 2026-09-29：扫描件水印冒充文字层（BC-01，操作者确认的偏离）----------
    # 真机 Y2S1 库里 7 个 CamScanner 扫描件每页都盖着 "CamScanner"（恰好 10 个字符），
    # 按“每页 >= 10 字符即有文字层”被判成文字 PDF，转出来只有 5 行水印，切块清洗后为空，
    # 记成终态 "empty"，OCR 根本没被调用。旧项目 extractors.py 的规则一模一样、会踩同一个坑；
    # 这是旧项目没有的新规则：每一页文字都是同一句短话 -> 那是水印，不是正文。

    def test_pages_that_all_carry_the_same_short_watermark_have_no_text_layer(self):
        _make_pdf_with_page_texts(self.tmp / "scan.pdf", ["CamScanner"] * 5)
        doc = extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_watermark_wording_and_spacing_differences_still_count_as_the_same(self):
        _make_pdf_with_page_texts(
            self.tmp / "scan2.pdf",
            ["Scanned with CamScanner", "Scanned  with   CamScanner ", "Scanned with CamScanner"],
        )
        doc = extract("lib1", "scan2.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_pages_with_different_short_text_are_still_a_text_layer(self):
        _make_pdf_with_page_texts(self.tmp / "notes.pdf", ["Chapter one intro", "Chapter two intro"])
        doc = extract("lib1", "notes.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertIsNone(doc.failure_reason)

    def test_long_identical_pages_are_real_content_not_a_watermark(self):
        body = "This paragraph is intentionally long enough to be real body text, not a stamp. " * 3
        _make_pdf_with_page_texts(self.tmp / "long.pdf", [body] * 3)
        doc = extract("lib1", "long.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_a_single_page_is_never_judged_a_watermark(self):
        # 只有一页时没有“每一页都一样”的证据，沿用旧规则（恰好 10 字符算文字页）。
        _make_pdf_with_page_texts(self.tmp / "one.pdf", ["CamScanner"])
        doc = extract("lib1", "one.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_watermark_on_top_of_real_page_text_is_still_a_text_layer(self):
        _make_pdf_with_page_texts(
            self.tmp / "stamped.pdf",
            ["CamScanner\nFirst page real body text", "CamScanner\nSecond page other real body text"],
        )
        doc = extract("lib1", "stamped.pdf", self.tmp)
        self.assertIsNotNone(doc.text)


if __name__ == "__main__":
    unittest.main()
