# -*- coding: utf-8 -*-
"""见 ./AGENTS.md 测试纪律。提取噪声清洗四件套（问题48 v10/v11）——用例
镜像旧 obsidian-rag/index.py strip_* 函数的 docstring 行为约定。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.text_cleaning import (  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
