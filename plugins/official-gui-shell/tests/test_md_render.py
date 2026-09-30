"""BC-15：GUI 的 Markdown 渲染器（检索命中 / 正文查看 / 提取试验台三处共用）对表格、公式、
图片的呈现。

**这些用例来自真实数据**（2026-09-30 对本机 `data-real/extracted` 里 196 份 MinerU 本机识别
结果的体检）：MinerU 输出的表格是 HTML（`<table><tr><td rowspan=1 colspan=1>…`），公式是
`$$ … $$` / `$ … $` 的 LaTeX，图片是 `![](images/xxx.jpg)`；旧渲染器把 HTML 表格当成一串带
尖括号的文字显示、把公式里的星号当斜体标记吃掉。

渲染器必须始终 XSS-safe：不信任任何原文，HTML 表格只放行白名单里的表格标签和整数的
`colspan`/`rowspan`，其余标签一律丢弃（文字保留并转义）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent
REPO_ROOT = PLUGIN_DIR.parent.parent
for _p in (REPO_ROOT, PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from official_gui_shell.md_render import md_to_html  # noqa: E402

#: 真实 MinerU 输出的样子：整张表一行，单元格带 rowspan/colspan（几乎都是 1）。
MINERU_TABLE = (
    '<table><tr><td rowspan=1 colspan=2>TABLE A-3</td></tr>'
    '<tr><td rowspan=1 colspan=1>Water</td><td rowspan=1 colspan=1>4.18</td></tr></table>'
)


class TestHtmlTable(unittest.TestCase):
    def test_mineru_html_table_becomes_a_real_table(self) -> None:
        out = md_to_html(MINERU_TABLE)
        self.assertIn("<table>", out)
        self.assertIn("<td>Water</td>", out)
        self.assertIn("<td>4.18</td>", out)
        self.assertNotIn("&lt;td", out, "表格标签不能被当成文字显示出来")

    def test_colspan_and_rowspan_are_kept_only_when_greater_than_one(self) -> None:
        out = md_to_html(MINERU_TABLE)
        self.assertIn('colspan="2"', out)
        self.assertNotIn("rowspan", out, "rowspan=1 是默认值，不必输出")

    def test_span_attributes_cannot_smuggle_anything(self) -> None:
        evil = '<table><tr><td colspan="2 onclick=alert(1)" rowspan=99999>x</td></tr></table>'
        out = md_to_html(evil)
        self.assertNotIn("onclick", out)
        self.assertNotIn("alert", out)
        self.assertNotIn("99999", out, "跨度要有上限，防止恶意撑爆布局")

    def test_scripts_and_event_handlers_never_survive(self) -> None:
        evil = (
            '<table onclick="x()"><tr><td onmouseover="y()">'
            '<script>alert(1)</script><img src=x onerror="z()">hi</td></tr></table>'
        )
        out = md_to_html(evil)
        for bad in ("<script", "onclick", "onmouseover", "onerror", "<img"):
            self.assertNotIn(bad, out, f"{bad} 不应出现在输出里")
        self.assertIn("hi", out)

    def test_sup_and_sub_inside_cells_are_kept(self) -> None:
        out = md_to_html("<table><tr><td>m<sup>2</sup> H<sub>2</sub>O</td></tr></table>")
        self.assertIn("<sup>2</sup>", out)
        self.assertIn("<sub>2</sub>", out)

    def test_image_inside_cell_becomes_the_placeholder(self) -> None:
        out = md_to_html('<table><tr><td><img src="images/a.jpg" alt="曲线图"></td></tr></table>')
        self.assertIn('class="md-img"', out)
        self.assertIn("曲线图", out)
        self.assertNotIn("<img", out)

    def test_chunk_that_starts_in_the_middle_of_a_table_still_renders(self) -> None:
        """旧索引里有被切断的表格块：只有后半截，开头是 `Sat.</td></tr><tr>…`。"""
        out = md_to_html("Sat.</td></tr><tr><td>1</td><td>2</td></tr></table>")
        self.assertIn("<table>", out)
        self.assertIn("<td>1</td>", out)
        self.assertIn("<td>2</td>", out)

    def test_unclosed_table_still_renders(self) -> None:
        out = md_to_html("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td>")
        self.assertIn("<td>a</td>", out)
        self.assertIn("<td>c</td>", out)
        self.assertNotIn("&lt;", out)

    def test_table_spanning_several_lines(self) -> None:
        out = md_to_html("<table>\n<tr>\n<td>甲</td>\n<td>乙</td>\n</tr>\n</table>")
        self.assertEqual(out.count("<table>"), 1)
        self.assertIn("<td>甲</td>", out)
        self.assertIn("<td>乙</td>", out)

    def test_text_around_a_table_is_kept_in_order(self) -> None:
        out = md_to_html("表 3 给出参数：\n\n" + MINERU_TABLE + "\n\n注：单位为 kJ/kg。")
        self.assertLess(out.index("表 3 给出参数"), out.index("<table>"))
        self.assertLess(out.index("</table>"), out.index("单位为 kJ/kg"))

    def test_inline_formula_inside_a_cell_is_not_mangled(self) -> None:
        out = md_to_html("<table><tr><td>$a_1 * b_2$</td></tr></table>")
        self.assertIn('<code class="md-math">$a_1 * b_2$</code>', out)
        self.assertNotIn("<i>", out)

    def test_prose_that_merely_mentions_a_tag_is_not_turned_into_a_table(self) -> None:
        out = md_to_html("HTML 里用 <td> 表示单元格，用 <table> 表示表格。")
        self.assertNotIn("<table>", out)
        self.assertIn("&lt;td&gt;", out)


class TestFormula(unittest.TestCase):
    def test_display_formula_is_shown_verbatim_in_a_math_block(self) -> None:
        out = md_to_html("$$\n\\frac{a_1}{b_2} + c_3 * d_4\n$$")
        self.assertIn('<pre class="md-math">', out)
        self.assertIn("\\frac{a_1}{b_2} + c_3 * d_4", out, "公式原文不能被改动")
        self.assertNotIn("<i>", out)
        self.assertNotIn("<br/>", out)

    def test_single_line_display_formula(self) -> None:
        out = md_to_html("$$ E = mc^2 $$")
        self.assertIn('<pre class="md-math">', out)
        self.assertIn("E = mc^2", out)

    def test_display_formula_content_is_escaped(self) -> None:
        out = md_to_html("$$ a < b & c > d $$")
        self.assertIn("a &lt; b &amp; c &gt; d", out)

    def test_display_formula_between_paragraphs_keeps_the_paragraphs(self) -> None:
        out = md_to_html("由式(3)得\n\n$$\nx = y\n$$\n\n式中 x 为位移。")
        self.assertLess(out.index("由式(3)得"), out.index("md-math"))
        self.assertLess(out.index("md-math"), out.index("式中 x 为位移"))

    def test_unclosed_display_formula_marker_does_not_swallow_the_document(self) -> None:
        out = md_to_html("$$ 这个标记没有配对\n\n后面还有正常的段落。\n\n- 列表项")
        self.assertIn("后面还有正常的段落", out)
        self.assertIn("<li>列表项</li>", out)

    def test_inline_formula_is_not_mangled_by_italic_markers(self) -> None:
        out = md_to_html("已知 $x_1 * y_2 * z$ 与 $a_i + b_i$ 成立")
        self.assertNotIn("<i>", out)
        self.assertIn('<code class="md-math">$x_1 * y_2 * z$</code>', out)
        self.assertIn('<code class="md-math">$a_i + b_i$</code>', out)

    def test_inline_double_dollar_inside_a_sentence(self) -> None:
        out = md_to_html("式 $$a*b*c$$ 成立")
        self.assertIn('<code class="md-math">$$a*b*c$$</code>', out)
        self.assertNotIn("<i>", out)

    def test_currency_amounts_are_not_taken_for_formulas(self) -> None:
        out = md_to_html("价格从 $5 涨到 $10，再到 $20。")
        self.assertNotIn("md-math", out)
        self.assertIn("$5", out)

    def test_dollar_inside_code_span_is_left_alone(self) -> None:
        out = md_to_html("环境变量 `$PATH` 和 `$HOME` 都要检查")
        self.assertEqual(out.count("<code>"), 2)
        self.assertNotIn("md-math", out)

    def test_formula_markup_cannot_inject_html(self) -> None:
        out = md_to_html("$<script>alert(1)</script>$")
        self.assertNotIn("<script", out)


class TestPipeTable(unittest.TestCase):
    def test_simple_pipe_table_is_unchanged(self) -> None:
        out = md_to_html("| a | b |\n| --- | --- |\n| 1 | 2 |")
        self.assertIn("<th>a</th><th>b</th>", out)
        self.assertIn("<td>1</td><td>2</td>", out)

    def test_bold_and_line_break_inside_cells(self) -> None:
        out = md_to_html("| 名称 | 说明 |\n| --- | --- |\n| **甲** | 第一行<br>第二行 |")
        self.assertIn("<td><b>甲</b></td>", out)
        self.assertIn("第一行<br/>第二行", out)
        self.assertNotIn("&lt;br", out)

    def test_escaped_pipe_stays_inside_one_cell(self) -> None:
        out = md_to_html("| 表达式 | 含义 |\n| --- | --- |\n| a \\| b | 或 |")
        self.assertEqual(len(re.findall(r"<td>", out)), 2, "转义的竖线不能多切出一列")
        self.assertIn("<td>a | b</td>", out)

    def test_inline_formula_inside_a_pipe_cell(self) -> None:
        out = md_to_html("| 量 | 式 |\n| --- | --- |\n| 速度 | $v_1 * t_2$ |")
        self.assertIn('<code class="md-math">$v_1 * t_2$</code>', out)


class TestUnchangedBehaviour(unittest.TestCase):
    """扩展不能改坏原有的呈现。"""

    def test_paragraph_html_is_still_escaped(self) -> None:
        out = md_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script", out)
        self.assertIn("&lt;script&gt;", out)

    def test_images_still_become_the_placeholder(self) -> None:
        self.assertIn('class="md-img"', md_to_html("![](images/abc.jpg)"))

    def test_headings_lists_code_fences_quotes(self) -> None:
        md = "# 标题\n\n- 甲\n- 乙\n\n1. 一\n2. 二\n\n```py\nx = 1\n```\n\n> 引用"
        out = md_to_html(md)
        for piece in ("<h1>标题</h1>", "<ul>", "<ol>", '<pre><code class="py">', "<blockquote>"):
            self.assertIn(piece, out)

    def test_empty_input(self) -> None:
        self.assertEqual(md_to_html(""), "")
        self.assertEqual(md_to_html(None), "")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
