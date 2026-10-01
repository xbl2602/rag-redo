from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pymupdf  # noqa: E402

from official_extractor_pdf_text import extract as extract_module  # noqa: E402
from official_extractor_pdf_text.extract import FAST_MODE_MIN_PAGES, extract  # noqa: E402
from official_extractor_pdf_text.plugin import PdfTextExtractorPlugin  # noqa: E402


def _make_text_pdf(path: Path, text: str = "Hello RAG REDO test content") -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _make_mixed_pdf(path: Path) -> None:
    doc = pymupdf.open()
    text_page = doc.new_page()
    text_page.insert_text((72, 72), "This page has a valid text layer")
    doc.new_page()
    doc.save(path)
    doc.close()


def _make_threshold_pdf(path: Path, text: str) -> None:
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


def _make_pdf_with_page_texts(path: Path, texts: list[str]) -> None:
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 750), text)  # 自动折行，长文本不会跑出页面
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

    def test_a_blank_page_no_longer_sends_a_text_book_to_ocr(self):
        """2026-10-01 操作者确认改掉的旧规则（BC-01）：以前“有一页没字就整本当扫描件”，
        厚教材因为封面、空白页整本被送去识别、又撞上 200 页上限，一个字都没进索引。
        现在没字也没图的空白页直接跳过，这本书按文字层转。"""
        _make_mixed_pdf(self.tmp / "mixed.pdf")
        doc = extract("lib1", "mixed.pdf", self.tmp)
        self.assertIsNotNone(doc.text, doc.failure_reason)
        self.assertIn("valid text layer", doc.text)
        self.assertEqual(doc.image_pages, ())

    def test_exactly_ten_characters_is_a_text_page(self):
        _make_threshold_pdf(self.tmp / "ten.pdf", "1234567890")
        doc = extract("lib1", "ten.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_nine_characters_routes_to_scanned_failure(self):
        _make_threshold_pdf(self.tmp / "nine.pdf", "123456789")
        doc = extract("lib1", "nine.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

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


    # ---- 2026-09-29：扫描件水印冒充文字层（BC-01，操作者确认的偏离）----------
    # 真机 Y2S1 库里 7 个 CamScanner 扫描件每页都盖着 "CamScanner"（恰好 10 个字符），
    # 按“每页 >= 10 字符即有文字层”被判成文字 PDF，转出来只有 5 行水印，切块清洗后为空，
    # 记成终态 "empty"，OCR 根本没被调用。旧项目 extractors.py 的规则一模一样、会踩同一个坑；
    # 这是旧项目没有的新规则：每一页文字都是同一句短话 -> 那是水印，不是正文。

    def test_pages_that_all_carry_the_same_short_watermark_have_no_text_layer(self):
        _make_pdf_with_page_texts(self.tmp / "scan.pdf", ["CamScanner"] * 5)
        doc = extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_watermark_wording_and_spacing_differences_still_count_as_the_same(self):
        _make_pdf_with_page_texts(
            self.tmp / "scan2.pdf",
            ["Scanned with CamScanner", "Scanned  with   CamScanner ", "Scanned with CamScanner"],
        )
        doc = extract("lib1", "scan2.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")

    def test_pages_with_different_short_text_are_still_a_text_layer(self):
        _make_pdf_with_page_texts(self.tmp / "notes.pdf", ["Chapter one intro", "Chapter two intro"])
        doc = extract("lib1", "notes.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertIsNone(doc.failure_reason)

    def test_long_identical_pages_are_real_content_not_a_watermark(self):
        body = "This paragraph is intentionally long enough to be real body text, not a stamp. " * 3
        _make_pdf_with_page_texts(self.tmp / "long.pdf", [body] * 3)
        doc = extract("lib1", "long.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_a_single_page_is_never_judged_a_watermark(self):
        # 只有一页时没有“每一页都一样”的证据，沿用旧规则（恰好 10 字符算文字页）。
        _make_pdf_with_page_texts(self.tmp / "one.pdf", ["CamScanner"])
        doc = extract("lib1", "one.pdf", self.tmp)
        self.assertIsNotNone(doc.text)

    def test_watermark_on_top_of_real_page_text_is_still_a_text_layer(self):
        _make_pdf_with_page_texts(
            self.tmp / "stamped.pdf",
            ["CamScanner\nFirst page real body text", "CamScanner\nSecond page other real body text"],
        )
        doc = extract("lib1", "stamped.pdf", self.tmp)
        self.assertIsNotNone(doc.text)


def _make_pages_pdf(path: Path, pages: int) -> None:
    doc = pymupdf.open()
    for index in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {index} body text about bending and forming")
    doc.save(path)
    doc.close()


def _add_image(page, fraction: float) -> None:
    """在这一页放一张灰色图，占页面面积约 `fraction`（宽度撑满，按比例取高度）。"""
    rect = page.rect
    height = rect.height * fraction
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.clear_with(180)
    page.insert_image(pymupdf.Rect(rect.x0, rect.y1 - height, rect.x1, rect.y1), pixmap=pix)


def _make_layout_pdf(path: Path, spec: list[tuple[str, float]]) -> None:
    """按 (这一页的字, 图占页面比例) 逐页造 PDF；字为空就不写字，比例为 0 就不放图。"""
    doc = pymupdf.open()
    for text, fraction in spec:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 400), text)
        if fraction:
            _add_image(page, fraction)
    doc.save(path)
    doc.close()


def _plugin_with(settings):
    class _Ctx:
        logger = __import__("logging").getLogger("rag_redo.test.pdf_text")

    ctx = _Ctx()
    ctx.settings = settings
    plugin = PdfTextExtractorPlugin()
    plugin.on_load(ctx)
    return plugin


_LONG = "This page is mostly running text about heat transfer and fluid flow. " * 6  # 约 400 字
_SHORT = "Figure 3.2 Forging dies"  # 23 字：大图配一句图题


class TestPagesAreJudgedOneByOne(unittest.TestCase):
    """按页分流（2026-10-01 操作者确认，BC-01）：满足任一条就是图片页——①几乎没字（不到 10 个）
    但有图；②图占两成以上，并且（默认）这页字不到 200 个 / 或者（coverage-only）不管字多少。
    没字也没图的是空白页，跳过。只有图片页送识别，文字页直接转。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _make_layout_pdf(
            self.tmp / "book.pdf",
            [
                (_LONG, 0.0),  # 1 文字页
                ("", 0.9),  # 2 没字的整页图（扫描页、封面）→ 图片页
                (_SHORT, 0.6),  # 3 大图 + 一句图题 → 图片页（旧规则当它是文字页，图里的字全丢）
                (_LONG, 0.3),  # 4 一整页正文配一张插图 → 默认是文字页；“只看图”时是图片页
                ("", 0.0),  # 5 空白页 → 跳过
                (_LONG, 0.1),  # 6 小图标、页眉 logo → 文字页
            ],
        )

    def test_default_rule_needs_a_big_picture_and_little_text(self):
        doc = extract("lib1", "book.pdf", self.tmp)
        self.assertIsNotNone(doc.text, doc.failure_reason)
        self.assertEqual(doc.image_pages, (2, 3))
        self.assertIsNotNone(doc.page_texts)
        self.assertEqual(len(doc.page_texts), 6)
        self.assertEqual(doc.text, "".join(doc.page_texts), "整本正文就是逐页正文按顺序拼起来")
        self.assertIn("Forging dies", doc.page_texts[2], "图片页先带着它上面仅有的字，识别不了时不至于一无所有")

    def test_coverage_only_rule_ignores_how_much_text_the_page_has(self):
        doc = extract("lib1", "book.pdf", self.tmp, image_rule="coverage-only")
        self.assertEqual(doc.image_pages, (2, 3, 4))

    def test_a_pure_text_book_keeps_the_whole_document_conversion(self):
        _make_layout_pdf(self.tmp / "plain.pdf", [(_LONG, 0.0), ("", 0.0), (_LONG, 0.05)])
        doc = extract("lib1", "plain.pdf", self.tmp)
        self.assertIsNotNone(doc.text)
        self.assertEqual(doc.image_pages, ())
        self.assertIsNone(doc.page_texts, "纯文字的书不需要逐页正文")

    def test_a_book_without_any_real_text_is_still_a_whole_scan(self):
        _make_layout_pdf(self.tmp / "scan.pdf", [("", 1.0), ("", 1.0), ("", 0.0)])
        doc = extract("lib1", "scan.pdf", self.tmp)
        self.assertIsNone(doc.text)
        self.assertEqual(doc.failure_reason, "scanned")
        self.assertEqual(doc.image_pages, (1, 2, 3), "整本送识别：列出全书页码，编排层据此看页数上限")

    def test_the_rule_enters_the_capability_signature(self):
        plugin_default = _plugin_with({})
        plugin_only = _plugin_with({"pdf_image_page_rule": "coverage-only"})
        self.assertNotEqual(plugin_default.index_signature(), plugin_only.index_signature())
        self.assertEqual(_plugin_with({"pdf_image_page_rule": "nonsense"}).image_rule(),
                         "coverage-and-chars")

    def test_write_page_range_cuts_out_exactly_those_pages(self):
        plugin = _plugin_with({})
        plugin.write_page_range(self.tmp / "book.pdf", 2, 3, self.tmp / "part.pdf")
        part = pymupdf.open(str(self.tmp / "part.pdf"))
        try:
            self.assertEqual(part.page_count, 2)
            self.assertIn("Forging dies", part[1].get_text())
        finally:
            part.close()


class TestPdfTextModes(unittest.TestCase):
    """PDF 文字层转换三种方式（设置项 `pdf_text_mode`，2026-09-30 操作者确认，BC-01）：
    auto（默认，大文件用快速模式）/ layout（全部用 AI 版面分析，旧行为）/ fast（全部快速模式）。
    真机：AI 版面分析每页 0.25~0.5 秒且占满全部 CPU 核，快速模式讲义每页约 6~9 毫秒。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _make_pages_pdf(self.tmp / "small.pdf", 3)
        _make_pages_pdf(self.tmp / "big.pdf", FAST_MODE_MIN_PAGES + 1)

    def _routes(self, mode):
        """每份文件走了哪条转换：用包装器记调用。快速方式照常真跑；版面分析 201 页要几十秒，
        这里只验路由、替成一句占位正文（真实的版面分析转换由上面 TestExtractPdfText 覆盖）。"""
        calls = []
        real_fast = extract_module.pymupdf_rag.to_markdown

        def _layout(*args, **kwargs):
            calls.append(("layout", Path(str(args[0])).name))
            return "版面分析转出的正文（占位）"

        def _fast(*args, **kwargs):
            calls.append(("fast", Path(str(args[0])).name))
            return real_fast(*args, **kwargs)

        with patch.object(extract_module.pymupdf4llm, "to_markdown", _layout), patch.object(
            extract_module.pymupdf_rag, "to_markdown", _fast
        ):
            docs = [extract("lib1", name, self.tmp, mode=mode) for name in ("small.pdf", "big.pdf")]
        for doc in docs:
            self.assertIsNotNone(doc.text, doc.failure_reason)
        return calls, docs

    def test_auto_mode_converts_only_big_files_the_fast_way(self):
        calls, docs = self._routes("auto")
        self.assertEqual(calls, [("layout", "small.pdf"), ("fast", "big.pdf")])
        self.assertIn("Page 200 body text", docs[1].text, "快速模式也要把正文完整转出来")

    def test_layout_mode_uses_layout_analysis_for_every_file(self):
        calls, _ = self._routes("layout")
        self.assertEqual(calls, [("layout", "small.pdf"), ("layout", "big.pdf")])

    def test_fast_mode_uses_fast_conversion_for_every_file(self):
        calls, _ = self._routes("fast")
        self.assertEqual(calls, [("fast", "small.pdf"), ("fast", "big.pdf")])

    def test_both_ways_keep_the_same_extractor_version_so_cached_text_stays_valid(self):
        """提取缓存按“插件:版本”找正文；模式要是写进版本号，快速模式转好的正文下一轮就找不到了。"""
        _, docs = self._routes("auto")
        self.assertEqual({doc.extractor_version for doc in docs}, {extract_module.EXTRACTOR_VERSION})

    def test_a_scanned_pdf_is_still_scanned_in_every_mode(self):
        _make_blank_pdf(self.tmp / "scan.pdf")
        for mode in ("auto", "layout", "fast"):
            self.assertEqual(extract("lib1", "scan.pdf", self.tmp, mode=mode).failure_reason, "scanned")


class TestPdfTextModeSetting(unittest.TestCase):
    """插件读设置：未知值按默认 auto 处理；模式进能力签名（§8.5：设置变了，此前失败的文件下轮重试）。"""

    def _plugin(self, settings):
        class _Ctx:
            logger = __import__("logging").getLogger("rag_redo.test.pdf_text_mode")

        ctx = _Ctx()
        ctx.settings = settings
        plugin = PdfTextExtractorPlugin()
        plugin.on_load(ctx)
        return plugin

    def test_default_is_auto(self):
        self.assertEqual(self._plugin({}).mode(), "auto")

    def test_unknown_value_falls_back_to_auto(self):
        self.assertEqual(self._plugin({"pdf_text_mode": "turbo"}).mode(), "auto")

    def test_mode_enters_the_capability_signature(self):
        signatures = {self._plugin({"pdf_text_mode": m}).index_signature() for m in ("auto", "layout", "fast")}
        self.assertEqual(len(signatures), 3)

    def test_plugin_passes_the_configured_mode_to_the_extractor(self):
        plugin = self._plugin({"pdf_text_mode": "fast"})
        with patch("official_extractor_pdf_text.plugin.extract") as fake:
            fake.return_value.text = None  # 这里只看传给转换的参数；没转出正文，插件原样返回
            plugin.extract("lib1", "a.pdf", Path("."))
        self.assertEqual(fake.call_args.kwargs.get("mode"), "fast")

    def test_output_settings_name_both_settings_that_change_the_converted_text(self):
        """2026-10-01 操作者确认（BC-01）：改了转换方式或图片页判法，下一轮把已经转好的 PDF 按新设置
        重转——编排层拿这串字和清单里记的比；里面不能有 `:` 和 `+`（转换暂存路由用它们分段）。"""
        changed = self._plugin({"pdf_text_mode": "fast", "pdf_image_page_rule": "coverage-only"}).output_settings()
        default = self._plugin({}).output_settings()
        self.assertEqual(changed, "mode=fast;pages=coverage-only")
        self.assertEqual(default, "mode=auto;pages=coverage-and-chars")
        for text in (changed, default):
            self.assertNotIn(":", text)
            self.assertNotIn("+", text)

    def test_a_converted_book_carries_the_settings_it_was_converted_with(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _make_text_pdf(tmp / "a.pdf")
        _make_blank_pdf(tmp / "scan.pdf")
        plugin = self._plugin({"pdf_text_mode": "fast"})
        self.assertEqual(plugin.extract("lib1", "a.pdf", tmp).extractor_settings, "mode=fast;pages=coverage-and-chars")
        self.assertIsNone(plugin.extract("lib1", "scan.pdf", tmp).extractor_settings, "没转出来的不记")


class TestConvertingSeveralAtOnce(unittest.TestCase):
    """几本同时转（2026-10-01 操作者确认，BC-01）：快速方式只用一个核，编排层把接下来要转的几本
    交过来，插件在后台几个子进程里同时转；交出去的结果必须与当场转的一模一样。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _plugin(self, mode: str) -> PdfTextExtractorPlugin:
        plugin = _plugin_with({"pdf_text_mode": mode})
        self.addCleanup(plugin.on_unload, None)
        return plugin

    def test_a_book_converted_ahead_is_identical_to_converting_it_on_the_spot(self):
        _make_text_pdf(self.tmp / "a.pdf", "Alpha body text for converting ahead")
        _make_layout_pdf(self.tmp / "b.pdf", [(_LONG, 0.0), ("", 0.8), (_LONG, 0.0)])  # 中间一页是图片页
        _make_pages_pdf(self.tmp / "c.pdf", 3)
        names = ["a.pdf", "b.pdf", "c.pdf"]
        plugin = self._plugin("fast")
        self.assertEqual(plugin.prefetch("lib1", names, self.tmp), names)
        for name in names:
            self.assertTrue(plugin.prefetch_wait("lib1", name, 120))
        self.assertEqual(plugin.prefetch_ready("lib1"), 3)
        ahead = {name: plugin.extract("lib1", name, self.tmp) for name in names}
        self.assertEqual(plugin.prefetch_ready("lib1"), 0)
        on_the_spot = {name: self._plugin("fast").extract("lib1", name, self.tmp) for name in names}
        self.assertEqual(ahead, on_the_spot)
        self.assertEqual(ahead["b.pdf"].image_pages, (2,))

    def test_layout_conversion_is_not_done_ahead(self):
        """精细方式自己就占满所有核，几本同时转只会互相抢：不收，也不起子进程。"""
        _make_text_pdf(self.tmp / "a.pdf")
        plugin = self._plugin("layout")
        self.assertEqual(plugin.prefetch("lib1", ["a.pdf"], self.tmp), [])
        self.assertIsNone(plugin._ahead._pool)  # noqa: SLF001 - 确认没白起子进程

    def test_auto_mode_only_takes_books_that_go_the_fast_way(self):
        _make_pages_pdf(self.tmp / "long.pdf", FAST_MODE_MIN_PAGES + 1)
        _make_text_pdf(self.tmp / "short.pdf")
        plugin = self._plugin("auto")
        self.assertEqual(plugin.prefetch("lib1", ["short.pdf", "long.pdf"], self.tmp), ["long.pdf"])

    def test_at_most_twice_the_workers_are_queued_at_once(self):
        from concurrent.futures import Future

        from official_extractor_pdf_text.ahead import AheadConverter

        class _NeverFinishes:
            def submit(self, *args, **kwargs):
                return Future()

            def terminate_workers(self):
                pass

            def shutdown(self, **kwargs):
                pass

        names = [f"{i}.pdf" for i in range(5)]
        for name in names:
            _make_text_pdf(self.tmp / name)
        converter = AheadConverter(workers=1)
        converter._pool = _NeverFinishes()  # noqa: SLF001 - 让收下的都一直“在转”
        self.assertEqual(converter.submit("lib1", names, self.tmp, "fast", "coverage-and-chars"), names[:2])
        converter.close()

    def test_cancelling_drops_the_results_and_closes_the_workers(self):
        _make_text_pdf(self.tmp / "a.pdf")
        plugin = self._plugin("fast")
        plugin.prefetch("lib1", ["a.pdf"], self.tmp)
        plugin.prefetch_cancel("lib1")
        self.assertIsNone(plugin._ahead._pool)  # noqa: SLF001 - 一轮转完子进程要收掉，不常驻
        self.assertEqual(plugin.prefetch_ready("lib1"), 0)
        self.assertTrue(plugin.prefetch_wait("lib1", "a.pdf", 0.1), "没在提前转的不用等")
        self.assertIsNotNone(plugin.extract("lib1", "a.pdf", self.tmp).text, "丢掉之后照常当场转")

    def test_a_worker_that_died_falls_back_to_converting_here(self):
        from concurrent.futures import Future

        _make_text_pdf(self.tmp / "a.pdf", "Fallback body text")
        plugin = self._plugin("fast")
        broken: Future = Future()
        broken.set_exception(RuntimeError("worker died"))
        with patch.object(plugin._ahead, "take", return_value=broken):  # noqa: SLF001
            doc = plugin.extract("lib1", "a.pdf", self.tmp)
        self.assertIn("Fallback body text", doc.text)


if __name__ == "__main__":
    unittest.main()
