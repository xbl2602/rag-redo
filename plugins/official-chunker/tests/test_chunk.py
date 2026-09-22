"""见 ../../../AGENTS.md 测试纪律：新功能必须带测试用例。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_chunker.chunk import chunk_document  # noqa: E402


class TestChunkDocument(unittest.TestCase):
    def test_single_short_section_is_one_chunk(self):
        text = "# 标题\n\n这是正文。"
        pieces = chunk_document(text, max_chars=800)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].heading_breadcrumb, "标题")
        self.assertIn("这是正文。", pieces[0].text)

    def test_nested_headings_produce_full_breadcrumb(self):
        text = "# 一级\n## 二级\n### 三级\n\n正文。"
        pieces = chunk_document(text, max_chars=800)
        self.assertEqual(pieces[-1].heading_breadcrumb, "一级 > 二级 > 三级")

    def test_sibling_heading_resets_deeper_level(self):
        text = "# 一级\n## 二A\n\n段落A\n\n## 二B\n\n段落B"
        pieces = chunk_document(text, max_chars=800)
        breadcrumbs = [p.heading_breadcrumb for p in pieces]
        self.assertEqual(breadcrumbs, ["一级 > 二A", "一级 > 二B"])

    def test_table_block_not_split_across_chunks(self):
        table = "\n".join(f"| 行{i} | 值{i} |" for i in range(20))
        text = f"# 表格\n\n{table}"
        pieces = chunk_document(text, max_chars=50)  # 故意设很小，逼切块
        table_lines = table.count("\n") + 1
        # 找出包含表格行的那个chunk，表格的全部行必须在同一个chunk里，
        # 不能被切成两半
        joined = "\n---CHUNK---\n".join(p.text for p in pieces)
        self.assertEqual(joined.count("| 行0 |"), 1)
        for piece in pieces:
            if "| 行0 |" in piece.text:
                self.assertEqual(piece.text.count("\n") + 1, table_lines)

    def test_long_paragraph_splits_on_sentence_boundary_not_mid_sentence(self):
        sentences = [f"这是第{i}句话，内容随便写一点凑够长度。" for i in range(30)]
        text = "# 标题\n\n" + "".join(sentences)
        pieces = chunk_document(text, max_chars=100)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            body = piece.text
            # 每个chunk要么整体不含句号，要么必须以句末标点结尾——
            # 不允许"半句话"结尾（除非它就是最后一个chunk里剩下的完整句子）
            self.assertTrue(
                body.endswith("。") or body.endswith("话。") or "。" not in body,
                f"chunk 疑似在句子中间被切断: {body!r}",
            )

    def test_overlap_within_same_section(self):
        para = "段落" * 60
        text = f"# 标题\n\n{para}\n\n{para}"
        pieces = chunk_document(text, max_chars=100, overlap_chars=20)
        self.assertGreater(len(pieces), 1)
        # 第二个chunk的开头应该包含第一个chunk结尾的重叠片段
        self.assertTrue(pieces[1].text.startswith(pieces[0].text[-20:]))

    def test_no_heading_uses_placeholder_breadcrumb(self):
        pieces = chunk_document("没有标题，直接是正文。")
        self.assertEqual(pieces[0].heading_breadcrumb, "(无标题)")

    def test_empty_text_returns_no_pieces(self):
        self.assertEqual(chunk_document(""), [])


if __name__ == "__main__":
    unittest.main()
