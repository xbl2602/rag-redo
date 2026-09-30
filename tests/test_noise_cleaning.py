# -*- coding: utf-8 -*-
"""见 ./AGENTS.md 测试纪律。提取噪声清洗四件套（问题48 v10/v11）——用例
镜像旧 obsidian-rag/index.py strip_* 函数的 docstring 行为约定。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.text_cleaning import (  # noqa: E402
    TEXT_PIPELINE_VERSION,
    flatten_html_tables,
    strip_boilerplate_lines,
    strip_dead_image_refs,
    strip_page_number_lines,
    strip_sidecar_noise,
)


class TestStripPageNumbers(unittest.TestCase):
    def test_marked_and_decorated_lines_always_removed(self):
        text = "正文\n第 12 页\nPage 3\n- 45 -\n尾部"
        self.assertEqual(strip_page_number_lines(text), "正文\n尾部")

    def test_bare_numbers_removed_only_with_pagination_signal(self):
        # ≥2 个不同裸数字行 = 分页信号 → 删；单个孤立数字行是正文，留
        self.assertEqual(strip_page_number_lines("12\n13\n正文"), "正文")
        self.assertEqual(strip_page_number_lines("2026\n正文"), "2026\n正文")


class TestStripBoilerplate(unittest.TestCase):
    def test_repeated_paragraph_lines_dropped(self):
        text = "机密内部资料\n正文一\n机密内部资料\n正文二\n机密内部资料\n结尾"
        self.assertEqual(
            strip_boilerplate_lines(text),
            "正文一\n正文二\n结尾",
        )

    def test_protected_lines_never_dropped(self):
        # 标题/表格/列表项/短行/分隔线：即使逐字重复 3 次也不动
        line = "# 标题\n| 表格 |\n- 项目\n短\n---\n"
        text = line + line + line
        self.assertEqual(strip_boilerplate_lines(text), text)

    def test_fence_content_exempt(self):
        text = "```python\nprint(1)\n```\nprint(1)\nprint(1)\nprint(1)"
        # 围栏内 print(1) 是代码；围栏外重复 3 次会删
        out = strip_boilerplate_lines(text)
        self.assertIn("```python\nprint(1)\n```", out)


class TestStripDeadImages(unittest.TestCase):
    def test_local_image_replaced_by_alt_or_removed(self):
        self.assertEqual(
            strip_dead_image_refs("前![截图](./img/a.png)后"),
            "前截图后",
        )
        self.assertEqual(strip_dead_image_refs("前![](./img/a.png)后"), "前后")

    def test_remote_and_data_images_kept(self):
        text = "![](https://x/a.png) ![](data:image/png;base64,AAA)"
        self.assertEqual(strip_dead_image_refs(text), text)

    def test_html_img_takes_alt(self):
        self.assertEqual(
            strip_dead_image_refs('前<img src="a.png" alt="示意图">后'),
            "前示意图后",
        )


class TestStripSidecarNoise(unittest.TestCase):
    def test_official_noise_lines_removed_exact_match(self):
        sidecar = [
            {"type": "header", "text": "第 1 章 流体力学"},
            {"type": "footer", "text": "公司机密"},
            {"type": "page_number", "text": "12"},
            {"type": "text", "text": "正文保留"},
            "垃圾元素",
        ]
        body = "第 1 章 流体力学\n# 标题\n正文保留\n公司机密\n12\n结尾"
        out = strip_sidecar_noise(body, sidecar)
        self.assertEqual(out, "# 标题\n正文保留\n结尾")

    def test_heading_line_never_removed_even_if_text_matches(self):
        sidecar = [{"type": "header", "text": "标题"}]
        self.assertEqual(strip_sidecar_noise("# 标题", sidecar), "# 标题")

    def test_non_list_sidecar_returns_body_unchanged(self):
        self.assertEqual(strip_sidecar_noise("正文", None), "正文")
        self.assertEqual(strip_sidecar_noise("正文", []), "正文")
        self.assertEqual(strip_sidecar_noise("正文", "垃圾"), "正文")


#: 真实 MinerU 输出的样子：整张表一行，单元格带 rowspan/colspan（几乎都是 1）。
_MINERU_TABLE = (
    "<table><tr><td rowspan=1 colspan=2>TABLE A-3</td></tr>"
    "<tr><td rowspan=1 colspan=1>Water</td><td rowspan=1 colspan=1>4.18</td></tr></table>"
)


class TestFlattenHtmlTables(unittest.TestCase):
    """BC-07（2026-09-30 操作者批准）：HTML 表格在**索引文本**里摊平成竖线表格。

    起因：本机 196 份 MinerU 识别结果里表格全是 HTML，切块器只认 `|` 开头的表格，于是 HTML 表格被
    当普通文字按句号切断（96 块被切断），标签占篇幅 40% 以上的块有 471 个，`td`/`tr`/`rowspan` 还
    成了 BM25 词表里的高频词。摊平之后，切块器"整张不切、并入上下文"的既有设计原样适用；提取缓存
    和 `read_document` 交付的原文不动。
    """

    def test_mineru_table_becomes_a_pipe_table_without_any_tag(self):
        out = flatten_html_tables(_MINERU_TABLE)
        self.assertEqual(
            out.strip().splitlines(),
            ["| TABLE A-3 |  |", "| --- | --- |", "| Water | 4.18 |"],
            "第一行当表头；colspan=2 的标题格后面补一个空格子保持列数",
        )
        for noise in ("<", ">", "colspan", "rowspan", "td"):
            self.assertNotIn(noise, out, f"摊平后不能残留标签噪声：{noise}")

    def test_rowspan_text_is_repeated_so_each_row_stands_alone(self):
        html = "<table><tr><td rowspan=2>水</td><td>4.18</td></tr><tr><td>4.19</td></tr></table>"
        self.assertEqual(
            flatten_html_tables(html).strip().splitlines(),
            ["| 水 | 4.18 |", "| --- | --- |", "| 水 | 4.19 |"],
        )

    def test_rows_are_padded_to_the_same_width(self):
        html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td></tr></table>"
        rows = flatten_html_tables(html).strip().splitlines()
        self.assertEqual({r.count("|") for r in rows}, {4}, rows)

    def test_sup_sub_and_line_breaks_inside_cells(self):
        html = "<table><tr><td>m<sup>2</sup> H<sub>2</sub>O</td><td>第一行<br>第二行</td></tr></table>"
        out = flatten_html_tables(html)
        self.assertIn("m^2 H2O", out)
        self.assertIn("第一行 第二行", out)

    def test_pipe_inside_a_cell_is_escaped(self):
        out = flatten_html_tables("<table><tr><td>a|b</td><td>c</td></tr></table>")
        self.assertIn("a\\|b", out)
        self.assertEqual(out.strip().splitlines()[0].replace("\\|", "").count("|"), 3)

    def test_text_around_the_table_is_kept_and_the_table_is_its_own_paragraph(self):
        out = flatten_html_tables("表 3 给出参数：\n" + _MINERU_TABLE + "\n注：单位为 kJ/kg。")
        self.assertIn("表 3 给出参数：\n\n| TABLE A-3 |", out, "表格前要有空行，才是独立的表格段")
        self.assertIn("| Water | 4.18 |\n\n注：单位为 kJ/kg。", out, "表格后要有空行")

    def test_table_cut_in_the_middle_still_flattens(self):
        """旧索引里被切断的表格块只剩后半截（开头是 `Sat.</td></tr><tr>…`）。"""
        out = flatten_html_tables("Sat.</td></tr><tr><td>1</td><td>2</td></tr></table>")
        self.assertIn("| 1 | 2 |", out)
        self.assertNotIn("<", out)

    def test_table_spanning_several_lines(self):
        out = flatten_html_tables("<table>\n<tr>\n<td>甲</td>\n<td>乙</td>\n</tr>\n</table>")
        self.assertIn("| 甲 | 乙 |", out)

    def test_html_inside_a_code_fence_is_left_alone(self):
        text = "示例：\n\n```html\n" + _MINERU_TABLE + "\n```\n"
        self.assertEqual(flatten_html_tables(text), text)

    def test_prose_that_merely_mentions_a_tag_is_left_alone(self):
        text = "HTML 里用 <td> 表示单元格，用 <table> 表示表格。"
        self.assertEqual(flatten_html_tables(text), text)

    def test_text_without_tables_is_returned_unchanged(self):
        text = "# 标题\n\n正文。\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        self.assertEqual(flatten_html_tables(text), text)

    def test_flattening_twice_changes_nothing(self):
        once = flatten_html_tables("前文\n" + _MINERU_TABLE + "\n后文")
        self.assertEqual(flatten_html_tables(once), once)

    def test_no_script_or_attribute_survives(self):
        evil = '<table onclick="x()"><tr><td onmouseover="y()"><script>alert(1)</script>hi</td></tr></table>'
        out = flatten_html_tables(evil)
        for bad in ("<script", "onclick", "onmouseover"):
            self.assertNotIn(bad, out)
        self.assertIn("hi", out)

    def test_text_pipeline_version_was_bumped_so_old_indexes_are_rebuilt(self):
        self.assertGreaterEqual(TEXT_PIPELINE_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
