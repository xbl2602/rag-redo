from __future__ import annotations

import hashlib
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    def test_non_utf8_never_raises_and_is_never_extract_failed_terminal(self):
        """非 UTF-8 字节流不能变成 extract-failed 终态。

        LEGACY 侧证据：`obsidian-rag/index.py:100` 是
        `text = raw.decode("utf-8", errors="replace")`——**永不抛错**，
        坏字节被换成 U+FFFD 后照常入库（"可搜、不会消失"）。REDO 曾经的
        严格 `utf-8-sig` 解码 + `failure_state="extract-failed"` 终态，
        让任何非 UTF-8 编码的纯文本笔记（GBK 存档、跨设备同步的老库）
        彻底不进索引且**不自愈**（`core/pipeline.py:687-701` 的
        `stable_terminal` 会把 extract-failed 判成稳定终态）。
        """
        (self.tmp / "bad.md").write_bytes(b"\xff\xfe\x00\x01not utf8 \x80\x81")
        doc = extract("lib1", "bad.md", self.tmp)  # 不应该抛异常
        self.assertIsNotNone(doc.text)
        self.assertIsNone(doc.failure_reason)
        self.assertNotEqual(doc.failure_state, "extract-failed")
        self.assertTrue(doc.content_hash)

    def test_gbk_encoded_note_is_indexed_with_replacement_chars(self):
        """GBK 编码的 .md 仍然进索引，内容里带 U+FFFD（对齐 LEGACY）。"""
        raw = "# 中文标题\n\n这是一段 GBK 编码的正文内容。".encode("gbk")
        (self.tmp / "gbk.md").write_bytes(raw)
        doc = extract("lib1", "gbk.md", self.tmp)
        self.assertIsNone(doc.failure_reason)
        self.assertIsNone(doc.failure_state)
        self.assertIsNotNone(doc.text)
        self.assertIn("\ufffd", doc.text)
        # 内容哈希必须仍然是「原始字节」的哈希（LEGACY index.py:97 的 bhash
        # 语义），不能因为降级解码而变化。
        self.assertEqual(doc.content_hash, _sha256(raw))

    def test_shift_jis_note_is_indexed(self):
        """另一类非 UTF-8 CJK 编码同样不得落终态。"""
        (self.tmp / "sjis.txt").write_bytes("日本語のメモ本文。".encode("shift_jis"))
        doc = extract("lib1", "sjis.txt", self.tmp)
        self.assertIsNone(doc.failure_reason)
        self.assertIsNone(doc.failure_state)
        self.assertIsNotNone(doc.text)

    def test_gbk_and_utf8_of_same_text_are_both_indexed(self):
        """同一段内容两种编码都必须可索引（LEGACY 从不因编码丢文件）。"""
        body = "知识库检索笔记，第一段。"
        (self.tmp / "u.md").write_bytes(body.encode("utf-8"))
        (self.tmp / "g.md").write_bytes(body.encode("gbk"))
        for name in ("u.md", "g.md"):
            doc = extract("lib1", name, self.tmp)
            self.assertIsNone(doc.failure_reason, name)
            self.assertIsNotNone(doc.text, name)

    def test_crlf_and_lf_produce_identical_markdown(self):
        """CRLF/LF 归一化不变量：同一内容、不同换行符 -> 相同 markdown。

        这是相对 LEGACY 的**有意改进**（LEGACY index.py:100 不做归一化，
        Windows 上写出的 .md 每行都带看不见的 \\r，切块按行切分时会把它
        带进块文本，同一篇笔记在 Windows/Linux 之间还会被误判成内容变了）。
        """
        (self.tmp / "crlf.md").write_bytes(b"# Title\r\n\r\nbody line\r\n")
        (self.tmp / "lf.md").write_bytes(b"# Title\n\nbody line\n")
        crlf_doc = extract("lib1", "crlf.md", self.tmp)
        lf_doc = extract("lib1", "lf.md", self.tmp)
        self.assertEqual(crlf_doc.text, lf_doc.text)
        self.assertNotIn("\r", crlf_doc.text)
        self.assertEqual(crlf_doc.text, "# Title\n\nbody line\n")

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
