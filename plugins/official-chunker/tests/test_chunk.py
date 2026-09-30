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


def _filler(chars: int, tag: str = "填充") -> str:
    """约 chars 个字的一段话，由若干完整的句子组成（每句以 。 结尾，句子内容各不相同）。"""
    out: list[str] = []
    i = 0
    while sum(len(s) for s in out) < chars:
        out.append(f"这是{tag}第{i}句。")
        i += 1
    return "".join(out)


class TestDisplayFormulaIsNeverCut(unittest.TestCase):
    """BC-07（2026-09-30 操作者批准）：`$$…$$` 公式块与表格一样"宁大勿断"。

    起因：本机 MinerU 识别结果的体检里，有 32 块只含半个公式（开头的 `$$` 与结尾的 `$$` 被切进了
    不同的块）——公式里的空行会被当成段落边界，超长公式又会被句子切分器在 `. ` 处切断。
    """

    def test_blank_line_inside_a_formula_does_not_split_it(self):
        formula = "$$\na = b\n\nc = d\n$$"
        text = f"# 节\n\n{_filler(300)}\n\n{formula}\n\n{_filler(300, '后文')}"
        pieces = chunk_document(text, max_chars=200)
        holders = [p for p in pieces if "a = b" in p.text]
        self.assertEqual(len(holders), 1)
        self.assertIn("c = d", holders[0].text, "公式中间的空行不能把它切成两块")

    def test_a_huge_formula_full_of_sentence_endings_stays_whole(self):
        formula = "$$" + " x. 1" * 400 + "$$"  # 每个 ". 1" 都像句界
        text = f"# 节\n\n{_filler(300)}\n\n{formula}\n\n{_filler(300, '后文')}"
        pieces = chunk_document(text, max_chars=200)
        self.assertEqual(sum(1 for p in pieces if formula in p.text), 1, "超长公式必须整块保留")

    def test_a_hash_line_inside_a_formula_is_not_a_heading(self):
        text = f"# 节\n\n$$\n# 不是标题\nx = 1\n$$\n\n{_filler(300)}"
        pieces = chunk_document(text, max_chars=200)
        self.assertEqual({p.heading_breadcrumb for p in pieces}, {"节"})

    def test_an_unmatched_double_dollar_does_not_swallow_the_rest(self):
        paragraphs = [f"$$ 没有配对的标记，{_filler(100, '首段')}"] + [
            _filler(150, f"第{i}段") for i in range(5)
        ]
        pieces = chunk_document("# 节\n\n" + "\n\n".join(paragraphs), max_chars=200)
        last = next(p for p in pieces if "第4段" in p.text)
        self.assertNotIn("没有配对", last.text, "找不到结尾 $$ 时当普通段落，不能把后文吞进去")
        self.assertGreater(len(pieces), 3)


class TestFormulaGetsItsContext(unittest.TestCase):
    """公式块不再单独成块：并到前面的引出句和后面的"式中…"说明（参考 RAGFlow 把上下文窗口并进
    表格/图片块、Docling 把标题与图注拼进嵌入文本的做法；不用大模型）。"""

    def _piece_with(self, pieces, needle):
        found = [p for p in pieces if needle in p.text]
        self.assertEqual(len(found), 1, f"{needle!r} 应恰好出现在一块里：{[p.text[:30] for p in pieces]}")
        return found[0]

    def test_formula_carries_the_sentence_that_introduces_it(self):
        text = f"# 节\n\n{_filler(300)}\n\n由式(3)得：\n\n$$\nx = y\n$$\n\n{_filler(300, '后文')}"
        piece = self._piece_with(chunk_document(text, max_chars=200), "x = y")
        self.assertIn("由式(3)得", piece.text)

    def test_formula_carries_the_where_clause_after_it(self):
        text = f"# 节\n\n{_filler(300)}\n\n$$\nx = y\n$$\n\n式中 x 为位移，y 为速度。\n\n{_filler(300, '后文')}"
        piece = self._piece_with(chunk_document(text, max_chars=200), "x = y")
        self.assertIn("式中 x 为位移", piece.text)

    def test_a_short_line_after_a_formula_goes_with_it(self):
        text = f"# 节\n\n{_filler(300)}\n\n$$\nx = y\n$$\n\n(7 marks)\n\n{_filler(300, '后文')}"
        piece = self._piece_with(chunk_document(text, max_chars=200), "x = y")
        self.assertIn("(7 marks)", piece.text)

    def test_a_long_unrelated_paragraph_after_a_formula_is_not_pulled_in(self):
        after = _filler(150, "无关")
        text = f"# 节\n\n{_filler(300)}\n\n$$\nx = y\n$$\n\n{after}\n\n{_filler(300, '后文')}"
        piece = self._piece_with(chunk_document(text, max_chars=200), "x = y")
        self.assertNotIn("无关", piece.text)

    def test_consecutive_formulas_stay_together_with_their_introduction(self):
        text = (
            f"# 节\n\n{_filler(300)}\n\n推导如下：\n\n$$\na = 1\n$$\n\n$$\nb = 2\n$$\n\n$$\nc = 3\n$$"
            f"\n\n{_filler(300, '后文')}"
        )
        piece = self._piece_with(chunk_document(text, max_chars=200), "a = 1")
        for needle in ("推导如下", "b = 2", "c = 3"):
            self.assertIn(needle, piece.text)

    def test_a_long_derivation_chain_is_capped(self):
        formulas = "\n\n".join(f"$$\nterm{i} = {'x' * 90}\n$$" for i in range(30))
        text = f"# 节\n\n{_filler(300)}\n\n{formulas}"
        pieces = chunk_document(text, max_chars=200)
        self.assertGreater(len(pieces), 3)
        self.assertLessEqual(max(len(p.text) for p in pieces), 2 * 200, "推导链最多拼到 2×上限")

    def test_no_chunk_is_a_bare_formula_when_an_introduction_exists(self):
        parts = []
        for i in range(6):
            parts.append(f"{_filler(120, f'引出{i}')}\n\n$$\ny{i} = f(x{i})\n$$")
        pieces = chunk_document("# 节\n\n" + "\n\n".join(parts), max_chars=200)
        for p in pieces:
            self.assertFalse(p.text.strip().startswith("$$"), f"出现了没有上文的孤立公式块：{p.text[:40]!r}")

    def test_a_long_introduction_only_lends_its_last_sentences(self):
        intro = "".join(f"这是引出第{i}句。" for i in range(40))  # 远超 max_chars
        text = f"# 节\n\n{intro}\n\n$$\nx = y\n$$"
        pieces = chunk_document(text, max_chars=200)
        piece = next(p for p in pieces if "x = y" in p.text)
        self.assertIn("这是引出第39句。", piece.text, "公式要带着紧挨它的那一句")
        self.assertNotIn("这是引出第0句。", piece.text, "整段超长的上文不能全拖进公式块")
        self.assertLess(len(piece.text), 2 * 200)

    def test_a_formula_right_after_a_table_is_taken_as_the_table_tail(self):
        table = "\n".join(f"| 行{i} | 值{i} |" for i in range(6))
        text = f"# 节\n\n{_filler(300)}\n\n{table}\n\n$$\nq = m c T\n$$\n\n{_filler(300, '后文')}"
        piece = next(p for p in chunk_document(text, max_chars=200) if "| 行0 |" in p.text)
        self.assertIn("q = m c T", piece.text)


class TestSmallParagraphsArePacked(unittest.TestCase):
    """BC-07（2026-09-30 操作者批准）：同一节里的碎小段落合并到接近上限再成块（参考 Docling 的
    merge_peers、Unstructured 的 combine_text_under_n_chars）。此前章节一长，每个自然段各自成块，
    "Figure 2"、页眉、孤立的一行字都成了单独的块（体检：短于 30 字的块占 21%）。"""

    def test_small_paragraphs_in_a_long_section_share_chunks(self):
        paragraphs = [f"第{i}段，很短。" for i in range(20)]
        pieces = chunk_document("# 节\n\n" + "\n\n".join(paragraphs), max_chars=100)
        self.assertLessEqual(len(pieces), 4, [p.text for p in pieces])
        # 有一侧是碎小段时允许略超上限（×1.25），免得在上限边缘留下孤零零的碎块
        self.assertLessEqual(max(len(p.text) for p in pieces), 125)

    def test_paragraph_order_is_preserved_after_packing(self):
        paragraphs = [f"第{i:02d}段，很短。" for i in range(20)]
        pieces = chunk_document("# 节\n\n" + "\n\n".join(paragraphs), max_chars=100)
        joined = "\n\n".join(p.text for p in pieces)
        self.assertEqual([m for m in paragraphs if m in joined], paragraphs)
        self.assertEqual(joined.replace("\n\n", ""), "".join(paragraphs))

    def test_a_tiny_caption_joins_a_neighbour_instead_of_standing_alone(self):
        text = f"# 节\n\n{_filler(90, '甲')}\n\nFigure 2\n\n{_filler(90, '乙')}\n\n{_filler(90, '丙')}"
        pieces = chunk_document(text, max_chars=120)
        self.assertNotIn("Figure 2", [p.text.strip() for p in pieces])
        self.assertTrue(any("Figure 2" in p.text and len(p.text) > 30 for p in pieces))

    def test_packed_chunks_stay_close_to_the_limit(self):
        paragraphs = [_filler(45, f"段{i}") for i in range(30)]
        pieces = chunk_document("# 节\n\n" + "\n\n".join(paragraphs), max_chars=200)
        self.assertLessEqual(max(len(p.text) for p in pieces), int(200 * 1.25))
        self.assertLess(len(pieces), 15, "30 个 45 字的段落应被合并成一半以下的块数")

    def test_packing_never_crosses_a_section_boundary(self):
        text = (
            "# 甲\n\n" + "\n\n".join(f"甲{i}段，很短。" for i in range(15))
            + "\n\n# 乙\n\n" + "\n\n".join(f"乙{i}段，很短。" for i in range(15))
        )
        for piece in chunk_document(text, max_chars=60):
            self.assertFalse("甲" in piece.text and "乙" in piece.text, piece.text)

    def test_a_short_section_is_still_one_chunk(self):
        pieces = chunk_document("# 节\n\n" + "\n\n".join(f"第{i}段。" for i in range(5)), max_chars=800)
        self.assertEqual(len(pieces), 1)

    def test_table_and_list_rules_still_hold_after_packing(self):
        table = "\n".join(f"| 行{i} | 值{i} |" for i in range(10))
        items = "\n".join(f"- 列表项第{i}条，带一点内容。" for i in range(10))
        text = f"# 节\n\n导语一句。\n\n{table}\n\n表后一句。\n\n{items}\n\n结尾一句。"
        pieces = chunk_document(text, max_chars=80)
        holder = next(p for p in pieces if "| 行0 |" in p.text)
        self.assertEqual(holder.text.count("| 行"), 10, "表格仍然整张不切")
        self.assertIn("导语一句", holder.text)
        self.assertIn("表后一句", holder.text)
        for p in pieces:
            for line in p.text.splitlines():
                if line.startswith("-"):
                    self.assertTrue(line.endswith("。"), f"列表项被拦腰切断：{line!r}")


if __name__ == "__main__":
    unittest.main()
