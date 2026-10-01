"""core/conversion_cache.py 的行为测试（BC-19：转换缓存看得见）。

只测“读出来、摆在一起”这一层：给定清单记录、正文缓存位置、页库状态，清单里每份文件的
状态/原因/位置/大小、每个库的汇总数、每轮日志那一行、缓存目录.md 的内容都要对。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.contracts import VisualPageState  # noqa: E402
from core.conversion_cache import (  # noqa: E402
    CATALOG_FILE_NAME,
    PLAIN_TEXT_EXTENSIONS,
    build_library_report,
    format_round_summary,
    human_bytes,
    missing_pages,
    needs_attention,
    needs_conversion,
    page_ranges,
    reason_text,
    render_catalog,
    round_summary,
)


def _state(path: str, status: str, pages: tuple[int, ...], *, page_count=None, built_in=None, reason=None):
    return VisualPageState(
        library_id="lib",
        path=path,
        provider_id="official-visual-wemm",
        status=status,
        failure_reason=reason,
        pages=pages,
        page_count=page_count,
        built_in=built_in,
    )


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.text_dir = Path(self._tmp.name) / "extracted" / "lib"
        (self.text_dir / "gen-2").mkdir(parents=True)
        self.cached = self.text_dir / "gen-2" / "abc.official-ocr-mineru-local%3A1.2.0.txt"
        self.cached.write_text("转出来的正文" * 10, encoding="utf-8")
        os.utime(self.cached, (1_790_000_000, 1_790_000_000))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def build(self, *, records, states=(), pages_enabled=True, included=None, locate=None, **extra):
        included = included if included is not None else [(path, True, "") for path in records]
        return build_library_report(
            library_id="lib",
            name="论文阅读",
            included_files=included,
            records=records,
            locate_text=locate or (lambda path: self.cached if path == "papers/a.pdf" else None),
            route_names={"official-ocr-mineru-local": "MinerU 本地解析"},
            page_states=states,
            pages_enabled=pages_enabled,
            generation="gen-2",
            text_dir=self.text_dir,
            **extra,
        )


class TestWhichFilesAreListed(_Fixture):
    def test_only_included_files_that_need_conversion_are_listed(self) -> None:
        report = self.build(
            records={},
            included=[
                ("notes/a.md", True, ""),
                ("notes/b.txt", True, ""),
                ("papers/a.pdf", True, ""),
                ("papers/skip.pdf", False, "用户排除"),
                ("reports/r.docx", True, ""),
            ],
        )
        self.assertEqual([item.path for item in report.files], ["papers/a.pdf", "reports/r.docx"])
        self.assertTrue(needs_conversion("x/y.PDF"))
        self.assertFalse(needs_conversion("x/y.markdown"))
        self.assertFalse(needs_conversion("README"))
        self.assertEqual(PLAIN_TEXT_EXTENSIONS, frozenset({"md", "txt", "markdown"}))


class TestTextState(_Fixture):
    def test_indexed_file_with_a_cache_file_is_done_with_route_size_and_time(self) -> None:
        report = self.build(
            records={
                "papers/a.pdf": {
                    "status": "indexed",
                    "extractor_id": "official-ocr-mineru-local",
                    "extractor_version": "1.2.0",
                }
            }
        )
        item = report.files[0]
        self.assertEqual(item.text_state, "done")
        self.assertIsNone(item.text_reason)
        self.assertEqual(item.text_route, "official-ocr-mineru-local")
        self.assertEqual(item.text_route_version, "1.2.0")
        self.assertEqual(item.text_route_name, "MinerU 本地解析")
        self.assertEqual(item.text_file, str(self.cached))
        self.assertEqual(item.text_bytes, self.cached.stat().st_size)
        self.assertEqual(item.text_updated, 1_790_000_000)
        self.assertEqual((report.text_done, report.text_total, report.text_bytes), (1, 1, item.text_bytes))

    def test_indexed_file_whose_cache_file_vanished_is_reported_missing(self) -> None:
        report = self.build(records={"papers/b.pdf": {"status": "indexed", "extractor_id": "x"}})
        item = report.files[0]
        self.assertEqual((item.text_state, item.text_reason), ("missing", "cache-missing"))
        self.assertIsNone(item.text_file)
        self.assertEqual(report.text_done, 0)

    def test_file_not_in_the_manifest_yet_is_pending_not_indexed(self) -> None:
        report = self.build(records={}, included=[("papers/new.pdf", True, "")])
        self.assertEqual((report.files[0].text_state, report.files[0].text_reason), ("pending", "not-indexed"))

    def test_scanned_and_deferred_are_pending_other_failures_are_failed(self) -> None:
        report = self.build(
            records={
                "a.pdf": {"status": "failed", "failure_state": "scanned"},
                "b.pdf": {"status": "failed", "failure_state": "deferred"},
                "c.docx": {"status": "terminal", "failure_state": "unreadable"},
                "d.pdf": {"status": "failed", "failure_state": None, "failure_reason": "empty"},
                "e.pdf": {"status": "failed", "failure_state": "奇怪的原因"},
            }
        )
        got = {item.path: (item.text_state, item.text_reason) for item in report.files}
        self.assertEqual(
            got,
            {
                "a.pdf": ("pending", "scanned"),
                "b.pdf": ("pending", "deferred"),
                "c.docx": ("failed", "unreadable"),
                "d.pdf": ("failed", "empty"),
                "e.pdf": ("failed", "extract-failed"),
            },
        )

    def test_every_reason_code_has_a_label_and_a_next_step(self) -> None:
        for code in (
            "not-indexed", "scanned", "deferred", "extract-failed", "unreadable", "empty", "tbd",
            "cache-missing", "pages-off", "pages-not-built", "pages-partial", "pages-failed",
            "ocr-off", "too-many-pages", "ocr-deferred", "ocr-failed",
        ):
            label, step = reason_text(code)
            self.assertTrue(label and step, code)
        self.assertEqual(reason_text("没见过的"), ("没见过的", ""))
        self.assertEqual(reason_text(None), ("", ""))

    # ---- PDF 按页分流（2026-10-01，BC-01）---------------------------------------------------

    def test_a_pdf_with_unrecognized_picture_pages_is_partial_with_the_pages_and_why(self) -> None:
        report = self.build(
            records={
                "papers/a.pdf": {
                    "status": "indexed",
                    "extractor_id": "official-extractor-pdf-text",
                    "extractor_version": "0.4.0",
                    "missing_pages": [12, 743, 744],
                    "missing_reason": "too-many-pages",
                }
            }
        )
        item = report.files[0]
        self.assertEqual((item.text_state, item.text_reason), ("partial", "too-many-pages"))
        self.assertEqual(item.text_missing_pages, (12, 743, 744))
        self.assertEqual(item.text_file, str(self.cached), "正文照样转好了，缓存文件照样报出来")
        self.assertTrue(needs_attention(item), "有图片页没识别要标黄")
        self.assertEqual(report.text_done, 0)
        text = render_catalog(report, library_root="D:/vault", now=0)
        self.assertIn("⚠️ 第 12、743–744 页没识别（要识别的页数超过本机上限）", text)
        self.assertIn("[打开](", text)

    def test_recognized_picture_pages_are_reported_on_a_done_pdf(self) -> None:
        report = self.build(
            records={
                "papers/a.pdf": {
                    "status": "indexed",
                    "extractor_id": "official-extractor-pdf-text",
                    "ocr_pages": [2, 3],
                    "ocr_by": "official-ocr-mineru-local",
                }
            },
            pages_enabled=False,
        )
        item = report.files[0]
        self.assertEqual(item.text_state, "done")
        self.assertEqual((item.text_ocr_pages, item.text_ocr_by), ((2, 3), "official-ocr-mineru-local"))
        self.assertFalse(needs_attention(item))

    def test_a_whole_scan_over_the_page_limit_says_so_instead_of_asking_to_turn_mineru_on(self) -> None:
        report = self.build(
            records={
                "big.pdf": {
                    "status": "terminal",
                    "failure_state": "scanned",
                    "failure_detail": "scanned:too-many-pages: 901 页超过本机识别上限 200 页",
                }
            }
        )
        self.assertEqual((report.files[0].text_state, report.files[0].text_reason), ("failed", "too-many-pages"))


class TestPagesState(_Fixture):
    def test_word_files_are_not_applicable_to_the_page_library(self) -> None:
        report = self.build(records={"r.docx": {"status": "indexed"}})
        self.assertEqual(report.files[0].pages_state, "n/a")
        self.assertEqual(report.pdf_total, 0)

    def test_page_states_map_to_done_partial_failed_and_not_built(self) -> None:
        report = self.build(
            records={name: {"status": "indexed"} for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf")},
            states=[
                _state("a.pdf", "indexed", (1, 2, 3), page_count=3, built_in="gen-2"),
                _state("b.pdf", "partial", (1, 3), page_count=4, built_in="gen-1", reason="部分页面编码失败"),
                _state("c.pdf", "failed", (), reason="WEMM子进程未运行"),
            ],
            bytes_per_page=1000,
        )
        got = {item.path: item for item in report.files}
        self.assertEqual((got["a.pdf"].pages_state, got["a.pdf"].pages, got["a.pdf"].page_count), ("done", (1, 2, 3), 3))
        self.assertTrue(got["a.pdf"].pages_rebuilt)
        self.assertEqual((got["b.pdf"].pages_state, got["b.pdf"].pages_reason), ("partial", "pages-partial"))
        self.assertEqual(got["b.pdf"].pages_detail, "部分页面编码失败")
        self.assertFalse(got["b.pdf"].pages_rebuilt)
        self.assertEqual(missing_pages(got["b.pdf"]), (2, 4))
        self.assertEqual((got["c.pdf"].pages_state, got["c.pdf"].pages_reason), ("failed", "pages-failed"))
        self.assertEqual(got["c.pdf"].pages_detail, "WEMM子进程未运行")
        self.assertEqual((got["d.pdf"].pages_state, got["d.pdf"].pages_reason), ("none", "pages-not-built"))
        self.assertEqual((report.pdf_total, report.pages_done, report.page_vectors), (4, 1, 5))
        self.assertEqual(report.page_bytes_estimate, 5000)

    def test_page_library_switched_off_reports_off_but_keeps_what_is_on_disk(self) -> None:
        report = self.build(
            records={"a.pdf": {"status": "indexed"}},
            states=[_state("a.pdf", "indexed", (1, 2), page_count=2)],
            pages_enabled=False,
            bytes_per_page=10,
        )
        item = report.files[0]
        self.assertEqual((item.pages_state, item.pages_reason, item.pages), ("off", "pages-off", (1, 2)))
        self.assertEqual(report.pages_done, 0)
        self.assertEqual(report.page_bytes_estimate, 20)

    def test_old_records_without_a_page_count_do_not_guess_missing_pages(self) -> None:
        report = self.build(records={"a.pdf": {"status": "indexed"}}, states=[_state("a.pdf", "indexed", (1, 2))])
        self.assertIsNone(report.files[0].page_count)
        self.assertEqual(missing_pages(report.files[0]), ())


class TestRoundSummary(_Fixture):
    def test_counts_reused_new_and_missing_for_both_caches(self) -> None:
        report = self.build(
            records={
                "papers/a.pdf": {"status": "indexed", "extractor_id": "official-ocr-mineru-local"},
                "b.pdf": {"status": "failed", "failure_state": "scanned"},
            },
            locate=lambda path: self.cached,
            states=[_state("papers/a.pdf", "indexed", (1,), built_in="gen-2")],
            included=[("papers/a.pdf", True, ""), ("b.pdf", True, ""), ("c.docx", True, "")],
        )
        summary = round_summary(report, fresh_paths={"papers/a.pdf"})
        self.assertEqual((summary.text_reused, summary.text_new, summary.text_missing), (0, 1, 2))
        self.assertEqual((summary.pages_reused, summary.pages_new, summary.pages_missing), (0, 1, 1))
        self.assertEqual(
            format_round_summary(summary),
            "转换缓存：转文字 复用 0 / 新转 1（MinerU 本地解析 1）/ 缺 2；图片页 本轮没有送识别的；"
            "页库 复用 0 / 新建 1 / 缺 1",
        )

    def test_reused_cache_is_not_counted_as_new_and_page_library_off_says_so(self) -> None:
        report = self.build(records={"papers/a.pdf": {"status": "indexed"}}, pages_enabled=False)
        summary = round_summary(report, fresh_paths=set())
        self.assertEqual((summary.text_reused, summary.text_new, summary.text_missing), (1, 0, 0))
        self.assertEqual((summary.pages_reused, summary.pages_new, summary.pages_missing), (0, 0, 0))
        self.assertTrue(format_round_summary(summary).endswith("；页库 没开"))

    def test_the_line_says_who_recognized_how_many_picture_pages_and_how_many_are_still_missing(self) -> None:
        """2026-10-01 操作者反馈：改了识别相关的设置，看不出是没生效还是静默失败，连送没送 MinerU
        云端都不知道。每轮这一行写明本轮谁识别了几页、还有几页没识别；转好了只差几页图的书
        正文已经进了索引，不算“缺”（BC-19/BC-01）。"""
        report = self.build(
            records={
                "papers/a.pdf": {
                    "status": "indexed",
                    "extractor_id": "official-extractor-pdf-text",
                    "ocr_pages": [3, 4],
                    "ocr_by": "official-ocr-mineru-local",
                    "missing_pages": [9],
                    "missing_reason": "too-many-pages",
                },
            },
            pages_enabled=False,
        )
        summary = round_summary(report, fresh_paths={"papers/a.pdf"})
        self.assertEqual((summary.text_reused, summary.text_new, summary.text_missing), (0, 1, 0))
        self.assertEqual(summary.ocr_pages_by, (("MinerU 本地解析", 2),))
        self.assertEqual((summary.pages_unrecognized, summary.files_unrecognized), (1, 1))
        self.assertEqual(
            format_round_summary(summary),
            "转换缓存：转文字 复用 0 / 新转 1（official-extractor-pdf-text 1）/ 缺 0；"
            "图片页 本轮 MinerU 本地解析 识别 2 页，还有 1 页没识别（1 份，原因见诊断页）；页库 没开",
        )

    def test_a_library_without_pdfs_has_no_picture_page_part(self) -> None:
        report = self.build(
            records={"c.docx": {"status": "indexed", "extractor_id": "official-extractor-docx"}},
            locate=lambda path: self.cached,
            pages_enabled=False,
        )
        line = format_round_summary(round_summary(report, fresh_paths=set()))
        self.assertNotIn("图片页", line)


class TestHumanReadableHelpers(unittest.TestCase):
    def test_sizes_and_page_ranges_read_naturally(self) -> None:
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(86016), "84 KB")
        self.assertEqual(human_bytes(int(3.4 * 1024 * 1024)), "3.4 MB")
        self.assertEqual(page_ranges([3, 1, 2, 7, 9, 8, 12]), "1–3、7–9、12")
        self.assertEqual(page_ranges([]), "")


class TestCatalog(_Fixture):
    def test_catalog_maps_each_original_file_to_its_cache_file_with_a_working_link(self) -> None:
        report = self.build(
            records={
                "papers/a.pdf": {
                    "status": "indexed",
                    "extractor_id": "official-ocr-mineru-local",
                    "extractor_version": "1.2.0",
                },
                "papers/x|y.pdf": {"status": "failed", "failure_state": "scanned"},
            },
            states=[_state("papers/a.pdf", "indexed", (1, 2), page_count=2)],
            pages_dir="D:/data/visual_wemm",
            bytes_per_page=4096,
        )
        text = render_catalog(report, library_root="D:/vault", now=1_790_000_000)
        self.assertTrue(report.catalog_file.endswith(CATALOG_FILE_NAME))
        self.assertIn("# 转换缓存目录 · 论文阅读", text)
        self.assertIn("转文字：1/2 份已转好", text)
        self.assertIn("页库：1/2 份 PDF 已建，共 2 页", text)
        self.assertIn("D:/data/visual_wemm", text)
        self.assertIn("MinerU 本地解析 1.2.0", text)
        # 文件名里的 %3A 要编码成 %253A，否则阅读器会把它解成冒号、链接就断了
        self.assertIn("[打开](<gen-2/abc.official-ocr-mineru-local%253A1.2.0.txt>)", text)
        self.assertIn("✅ 2/2 页", text)
        self.assertIn("papers/x\\|y.pdf", text)
        self.assertIn("⚠️ 扫描件，等文字识别", text)

    def test_catalog_says_when_the_page_library_is_off_or_nothing_needs_conversion(self) -> None:
        report = self.build(records={}, included=[], pages_enabled=False)
        text = render_catalog(report, library_root="D:/vault", now=0)
        self.assertIn("页库：没开", text)
        self.assertIn("这个库里没有需要转换的文件", text)


if __name__ == "__main__":
    unittest.main()
