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

from official_extractor_text.extract import extract  # noqa: E402


class TestExtract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_reads_utf8_text(self):
        (self.tmp / "a.md").write_text("# 你好\n\n世界", encoding="utf-8")
        doc = extract("lib1", "a.md", self.tmp)
        self.assertEqual(doc.text, "# 你好\n\n世界")
        self.assertIsNone(doc.failure_reason)
        self.assertTrue(doc.content_hash)

    def test_missing_file_folds_to_failure_not_exception(self):
        doc = extract("lib1", "missing.md", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_empty_file_folds_to_failure(self):
        (self.tmp / "empty.md").write_text("   \n\n  ", encoding="utf-8")
        doc = extract("lib1", "empty.md", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("空", doc.failure_reason)

    def test_non_utf8_folds_to_failure_not_exception(self):
        (self.tmp / "bad.md").write_bytes(b"\xff\xfe\x00\x01not utf8 \x80\x81")
        doc = extract("lib1", "bad.md", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_utf8_bom_is_handled(self):
        (self.tmp / "bom.md").write_bytes(b"\xef\xbb\xbf# has BOM")
        doc = extract("lib1", "bom.md", self.tmp)
        self.assertEqual(doc.text, "# has BOM")

    def test_same_content_same_hash(self):
        (self.tmp / "a.md").write_text("同样内容", encoding="utf-8")
        (self.tmp / "b.md").write_text("同样内容", encoding="utf-8")
        doc_a = extract("lib1", "a.md", self.tmp)
        doc_b = extract("lib1", "b.md", self.tmp)
        self.assertEqual(doc_a.content_hash, doc_b.content_hash)


if __name__ == "__main__":
    unittest.main()
