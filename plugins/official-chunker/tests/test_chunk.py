"""见 ../../../AGENTS.md 测试纪律：新功能必须带测试用例。

2026-09-25 对齐旧项目切块语义（问题8/9/18、审计F8）后更新的断言：
面包屑分隔符 " / "（旧 split_by_headings 输出格式）、无标题面包屑为空串
（旧："文件开头无标题部分 heading_path 为空串"）、无跨块 overlap（旧项目
没有重叠——重叠会把上一话题的尾巴混进下一话题）、缩写保护、围栏内 # 不算
标题、表格宁大勿断、列表按项边界切。"""
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
        self.assertEqual(pieces[-1].heading_breadcrumb, "一级 / 二级 / 三级")

    def test_sibling_heading_resets_deeper_level(self):
        text = "# 一级\n## 二A\n\n段落A\n\n## 二B\n\n段落B"
        pieces = chunk_document(text, max_chars=800)
        breadcrumbs = [p.heading_breadcrumb for p in pieces]
        self.assertEqual(breadcrumbs, ["一级 / 二A", "一级 / 二B"])

    def test_h4_and_deeper_are_body_not_headings(self):
        """对齐旧 heading_re #{1,3}：H4-H6 是正文，不是分节点。"""
        text = "# 一级\n#### 四级标题内容\n\n正文。"
        pieces = chunk_document(text, max_chars=800)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].heading_breadcrumb, "一级")
        self.assertIn("#### 四级标题内容", pieces[0].text)

    def test_hash_inside_code_fence_is_not_a_heading(self):
        """审计 F8：围栏代码块内的 # 是注释不是标题——否则代码注释会混进
        嵌入文本并成为其后真实小节的"父标题"。"""
        text = "# 真标题\n\n```python\n# 这是注释不是标题\nprint('x')\n```\n\n## 真二级\n\n正文。"
        pieces = chunk_document(text, max_chars=800)
        self.assertEqual([p.heading_breadcrumb for p in pieces], ["真标题", "真标题 / 真二级"])
        self.assertIn("# 这是注释不是标题", pieces[0].text)

    def test_tilde_fence_same_behavior(self):
        text = "# 标题\n\n~~~\n# 围栏内注释\n~~~\n\n正文。"
        pieces = chunk_document(text, max_chars=800)
        self.assertEqual(len(pieces), 1)

    def test_table_block_not_split_across_chunks(self):
        table = "\n".join(f"| 行{i} | 值{i} |" for i in range(20))
        text = f"# 表格\n\n{table}"
        pieces = chunk_document(text, max_chars=50)  # 故意设很小，逼切块
        table_lines = table.count("\n") + 1
        joined = "\n---CHUNK---\n".join(p.text for p in pieces)
        self.assertEqual(joined.count("| 行0 |"), 1)
        for piece in pieces:
            if "| 行0 |" in piece.text:
                self.assertEqual(piece.text.count("\n") + 1, table_lines)

    def test_table_binds_surrounding_context(self):
        """旧决策：表格与直接上文+直接下文整组绑定（宁大勿断）。"""
        table = "\n".join(f"| 行{i} |" for i in range(15))
        text = f"# 表格\n\n引导句在表格前面。\n\n{table}\n\n结论段在表格后面。"
        pieces = chunk_document(text, max_chars=60)
        table_piece = next(p for p in pieces if "| 行0 |" in p.text)
        self.assertIn("引导句", table_piece.text, "表格必须携带直接上文")
        self.assertIn("结论段", table_piece.text, "表格必须携带直接下文")

    def test_long_paragraph_splits_on_sentence_boundary_not_mid_sentence(self):
        sentences = [f"这是第{i}句话，内容随便写一点凑够长度。" for i in range(30)]
        text = "# 标题\n\n" + "".join(sentences)
        pieces = chunk_document(text, max_chars=100)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            body = piece.text.strip()
            self.assertTrue(
                body.endswith("。") or "。" not in body,
                f"chunk 疑似在句子中间被切断: {body!r}",
            )

    def test_abbreviation_not_treated_as_sentence_boundary(self):
        """旧 split_sentences 的缩写保护：Mr./e.g. 不是句界。"""
        # 旧算法保护的是带前导空格的缩写（" Mr."），句首的 Mr. 不在保护范围
        text = "# 缩写\n\n" + "开头 Mr. Smith 教授说了很长的一句话 " * 20 + "结束。"
        pieces = chunk_document(text, max_chars=100)
        for piece in pieces:
            body = piece.text.strip()
            # 缩写后的 "Smith" 不应成为新 chunk 的开头
            if body.startswith("Smith"):
                self.fail(f"缩写被误当句界: {body[:30]!r}")

    def test_list_block_split_on_item_boundary(self):
        """旧决策：超长列表按项边界切，永不从列表项中间剪断。"""
        items = "\n".join(f"- 列表项第{i}条，带一点内容让它变长。" for i in range(20))
        text = f"# 列表\n\n{items}"
        pieces = chunk_document(text, max_chars=80)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            lines = [line for line in piece.text.splitlines() if line.strip()]
            for line in lines:
                self.assertTrue(
                    line.lstrip().startswith(("-", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")),
                    f"列表块内出现被拦腰切断的行: {line!r}",
                )

    def test_no_overlap_between_chunks(self):
        """对齐旧项目：无跨块重叠——重叠把上一话题尾巴混进下一话题。"""
        para_a = "甲段内容" * 60
        para_b = "乙段内容" * 60
        text = f"# 标题\n\n{para_a}\n\n{para_b}"
        pieces = chunk_document(text, max_chars=100)
        self.assertGreater(len(pieces), 1)
        self.assertFalse(pieces[1].text.startswith(pieces[0].text[-20:]))

    def test_no_heading_uses_empty_breadcrumb(self):
        """旧 split_by_headings："文件开头无标题部分 heading_path 为空串"。"""
        pieces = chunk_document("没有标题，直接是正文。")
        self.assertEqual(pieces[0].heading_breadcrumb, "")

    def test_sibling_chunks_share_exact_parent_section(self):
        para = "父节内容" * 50
        pieces = chunk_document(f"# 标题\n\n{para}\n\n{para}", max_chars=100)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(len({piece.section_id for piece in pieces}), 1)
        self.assertEqual(len({piece.section_text for piece in pieces}), 1)
        self.assertIn("父节内容", pieces[0].section_text)

    def test_different_sections_have_different_ids(self):
        pieces = chunk_document("# 甲\n\n正文甲\n# 乙\n\n正文乙")
        self.assertEqual(len(pieces), 2)
        self.assertNotEqual(pieces[0].section_id, pieces[1].section_id)

    def test_empty_text_returns_no_pieces(self):
        self.assertEqual(chunk_document(""), [])


if __name__ == "__main__":
    unittest.main()
