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


def _make_blank_pdf(path: Path) -> None:
    doc = pymupdf.open()
    doc.new_page()  # 完全空白，没有文字层
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


if __name__ == "__main__":
    unittest.main()
