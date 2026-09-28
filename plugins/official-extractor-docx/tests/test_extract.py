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

import docx  # noqa: E402

from official_extractor_docx.extract import extract  # noqa: E402


def _make_docx(path: Path) -> None:
    document = docx.Document()
    document.add_heading("标题一", level=1)
    document.add_paragraph("第一段正文内容。")
    document.add_heading("子标题", level=2)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "列A"
    table.cell(0, 1).text = "列B"
    table.cell(1, 0).text = "值1"
    table.cell(1, 1).text = "值2"
    document.add_paragraph("表格后面的段落。")
    document.save(path)


def _make_heading_styles_docx(path: Path) -> None:
    """造一个把各种标题样式名都摆出来的真 docx。

    LEGACY `obsidian-rag/extractors.py:420-435 _heading_level` 的完整规则：
      - `Title`（不分大小写）-> H1；
      - `Heading N` 或中文 `标题 N`（不分大小写，标题与数字间允许空白）-> N，
        再**钳到 3 级**；
      - 其余样式名 -> 0（当普通正文）。

    注意：python-docx 的默认模板只有英文样式名，中文 Word 模板里的
    `标题 1` 是**本地化后的样式名**，必须自己 add_style 出来才能复现真实
    中文文档——直接 `add_paragraph(style='标题 1')` 会 KeyError。
    """
    from docx.enum.style import WD_STYLE_TYPE

    document = docx.Document()
    localized = {
        name: document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH, builtin=False)
        for name in ("标题 1", "标题 2")
    }
    document.add_paragraph("Title 样式的段落", style="Title")
    document.add_paragraph("Heading 1 样式的段落", style="Heading 1")
    document.add_paragraph("Heading 2 样式的段落", style="Heading 2")
    document.add_paragraph("Heading 3 样式的段落", style="Heading 3")
    document.add_paragraph("Heading 4 样式的段落", style="Heading 4")
    document.add_paragraph("Heading 5 样式的段落", style="Heading 5")
    document.add_paragraph("Heading 6 样式的段落", style="Heading 6")
    document.add_paragraph("标题 1 样式的段落", style=localized["标题 1"])
    document.add_paragraph("标题 2 样式的段落", style=localized["标题 2"])
    document.add_paragraph("正文样式的段落", style="Normal")
    document.save(path)


class TestHeadingLevelLegacyCompat(unittest.TestCase):
    """缺陷 2 复现：DOCX 标题样式识别必须逐条对齐 LEGACY `_heading_level`。

    修复前 REDO 只有 `^Heading (\\d+)$` 且输出 `min(level, 6)`，后果是：
      1. 中文 Word 模板（`标题 1`）完全不识别 -> 整篇被当正文 ->
         heading_breadcrumb 为空，块文本失去最强语义锚点；
      2. `Heading 4/5/6` 产出 `####`，而切块器
         `official-chunker/chunk.py:31 _HEADING_RE = ^(#{1,3})\\s+` 不认 ->
         H4-H6 被当普通正文行。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _make_heading_styles_docx(self.tmp / "styles.docx")
        self.doc = extract("lib1", "styles.docx", self.tmp)
        self.assertIsNone(self.doc.failure_reason)

    def _line_of(self, needle: str) -> str:
        for line in (self.doc.text or "").splitlines():
            if needle in line:
                return line
        self.fail(f"输出里找不到含 {needle!r} 的行：\n{self.doc.text}")

    def test_title_style_becomes_h1(self):
        self.assertEqual(self._line_of("Title 样式"), "# Title 样式的段落")

    def test_chinese_heading_styles_recognized(self):
        self.assertEqual(self._line_of("标题 1 样式"), "# 标题 1 样式的段落")
        self.assertEqual(self._line_of("标题 2 样式"), "## 标题 2 样式的段落")

    def test_heading_levels_clamped_to_three(self):
        self.assertEqual(self._line_of("Heading 1 样式"), "# Heading 1 样式的段落")
        self.assertEqual(self._line_of("Heading 2 样式"), "## Heading 2 样式的段落")
        self.assertEqual(self._line_of("Heading 3 样式"), "### Heading 3 样式的段落")
        # >3 级必须钳到 ###：切块器只认 H1-H3，输出 #### 会被当普通正文
        self.assertEqual(self._line_of("Heading 4 样式"), "### Heading 4 样式的段落")
        self.assertEqual(self._line_of("Heading 5 样式"), "### Heading 5 样式的段落")
        self.assertEqual(self._line_of("Heading 6 样式"), "### Heading 6 样式的段落")

    def test_non_heading_style_stays_plain_text(self):
        self.assertEqual(self._line_of("正文样式"), "正文样式的段落")

    def test_no_h4_or_deeper_hashes_emitted(self):
        """切块器 `^(#{1,3})\\s+` 只认三级，输出里绝不能出现 #### 及更深。"""
        import re

        for line in (self.doc.text or "").splitlines():
            hashes = re.match(r"^(#+) ", line)
            if hashes:
                self.assertLessEqual(len(hashes.group(1)), 3, line)


class TestHeadingLevelUnit(unittest.TestCase):
    """直接钉住 `_heading_level` 的每一条规则（含 0 与异常输入）。"""

    def test_rules_match_legacy(self):
        from official_extractor_docx.extract import _heading_level

        cases = {
            "Title": 1,
            "title": 1,
            "TITLE": 1,
            "  Title  ": 1,
            "Heading 1": 1,
            "Heading 2": 2,
            "Heading 3": 3,
            "Heading 4": 3,
            "Heading 6": 3,
            "Heading 9": 3,
            "heading 2": 2,
            "标题 1": 1,
            "标题 2": 2,
            "标题 3": 3,
            "标题 4": 3,
            "标题1": 1,
            "标题  2": 2,
            "Normal": 0,
            "Heading One": 0,
            "标题": 0,
            "": 0,
            None: 0,
        }
        for style_name, expected in cases.items():
            with self.subTest(style=style_name):
                self.assertEqual(_heading_level(style_name), expected)


class TestExtractDocx(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_extracts_headings_paragraphs_and_table(self):
        _make_docx(self.tmp / "a.docx")
        doc = extract("lib1", "a.docx", self.tmp)
        self.assertIsNone(doc.failure_reason)
        self.assertIn("# 标题一", doc.text)
        self.assertIn("## 子标题", doc.text)
        self.assertIn("第一段正文内容。", doc.text)
        self.assertIn("| 列A | 列B |", doc.text)
        self.assertIn("值1", doc.text)

    def test_order_preserved_paragraph_table_paragraph(self):
        _make_docx(self.tmp / "a.docx")
        doc = extract("lib1", "a.docx", self.tmp)
        before_table = doc.text.index("子标题")
        table_pos = doc.text.index("列A")
        after_pos = doc.text.index("表格后面的段落")
        self.assertLess(before_table, table_pos)
        self.assertLess(table_pos, after_pos)

    def test_empty_document_folds_to_failure(self):
        docx.Document().save(self.tmp / "empty.docx")
        doc = extract("lib1", "empty.docx", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIn("空", doc.failure_reason)

    def test_missing_file_folds_to_failure(self):
        doc = extract("lib1", "missing.docx", self.tmp)
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)

    def test_failure_states_stay_distinguishable(self):
        """AGENTS.md §5：终态不能合并成一个无法诊断的 failed。

        LEGACY 分别落 `unreadable`（obsidian-rag/index.py:118）/
        `extract-failed`（obsidian-rag/extractors.py:475）/
        `empty`（obsidian-rag/extractors.py:501）。REDO 曾让三种 reason
        全部被 `core/pipeline.py::normalize_failure_state` 归成
        `extract-failed`，损坏文件与空文档在诊断里长得一模一样。
        """
        missing = extract("lib1", "missing.docx", self.tmp)
        self.assertEqual(missing.failure_state, "unreadable")

        (self.tmp / "bad.docx").write_bytes(b"not a real docx/zip")
        broken = extract("lib1", "bad.docx", self.tmp)
        self.assertEqual(broken.failure_state, "extract-failed")

        docx.Document().save(self.tmp / "empty.docx")
        empty = extract("lib1", "empty.docx", self.tmp)
        self.assertEqual(empty.failure_state, "empty")

    def test_corrupted_docx_folds_to_failure_not_exception(self):
        (self.tmp / "bad.docx").write_bytes(b"not a real docx/zip")
        doc = extract("lib1", "bad.docx", self.tmp)  # 不应该抛异常
        self.assertIsNone(doc.text)
        self.assertIsNotNone(doc.failure_reason)


if __name__ == "__main__":
    unittest.main()
