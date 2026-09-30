"""core/extract_cache.py 的单元测试：真实写盘、真实反查，不是纸面设计。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.extract_cache import ExtractCache  # noqa: E402
from core.index_generation import INDEX_MANIFEST_VERSION, IndexManifestStore  # noqa: E402


class TestExtractCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = ExtractCache(self.tmp / "extracted")

    def test_read_missing_returns_none(self):
        self.assertIsNone(self.cache.read("lib1", "notes.md"))

    def test_write_then_read_round_trips(self):
        self.cache.write("lib1", "notes.md", "正文内容")
        self.assertEqual(self.cache.read("lib1", "notes.md"), "正文内容")

    def test_write_overwrites_existing(self):
        self.cache.write("lib1", "notes.md", "旧内容")
        self.cache.write("lib1", "notes.md", "新内容")
        self.assertEqual(self.cache.read("lib1", "notes.md"), "新内容")

    def test_different_libraries_are_isolated(self):
        self.cache.write("lib1", "notes.md", "库1的内容")
        self.cache.write("lib2", "notes.md", "库2的内容")
        self.assertEqual(self.cache.read("lib1", "notes.md"), "库1的内容")
        self.assertEqual(self.cache.read("lib2", "notes.md"), "库2的内容")

    def test_long_nested_chinese_path_does_not_crash(self):
        """真实的失败模式：早期实现用"转义后的原始路径"当文件名，长的
        中文嵌套路径转义后可能超过 Windows 路径长度限制——这里用固定长度
        的哈希当文件名，不该再有这个问题。"""
        rel_path = "20-Projects/" + "机器学习论文合集完整详细笔记与心得体会总结" * 5 + "/2024年最新进展详细笔记正文.md"
        self.cache.write("lib1", rel_path, "内容")
        self.assertEqual(self.cache.read("lib1", rel_path), "内容")

    def test_route_caches_keep_cloud_local_and_text_results_with_legacy_priority(self):
        self.cache.write("lib1", "mixed.pdf", "text", generation="g1", route="official-extractor-pdf-text:0.2.0")
        self.cache.write("lib1", "mixed.pdf", "local", generation="g1", route="official-ocr-mineru-local:0.2.0")
        self.cache.write("lib1", "mixed.pdf", "cloud", generation="g1", route="official-ocr-mineru-cloud:0.2.0")
        routes = (
            "official-ocr-mineru-cloud:0.2.0",
            "official-ocr-mineru-local:0.2.0",
            "official-extractor-pdf-text:0.2.0",
        )
        self.assertEqual(self.cache.read_preferred("lib1", "mixed.pdf", routes, generation="g1"), "cloud")
        self.assertEqual(
            self.cache.read_preferred("lib1", "mixed.pdf", routes[1:], generation="g1"),
            "local",
        )

    def test_route_cache_and_legacy_cache_are_isolated(self):
        self.cache.write("lib1", "a.pdf", "legacy")
        self.cache.write("lib1", "a.pdf", "cloud", route="official-ocr-mineru-cloud:0.2.0")
        self.assertEqual(
            self.cache.read_preferred(
                "lib1",
                "a.pdf",
                ("official-ocr-mineru-cloud:0.2.0",),
            ),
            "cloud",
        )
        self.assertEqual(self.cache.read("lib1", "a.pdf"), "legacy")

    def test_clear_library_removes_all_entries(self):
        self.cache.write("lib1", "a.md", "a")
        self.cache.write("lib1", "b.md", "b")
        self.cache.clear_library("lib1")
        self.assertIsNone(self.cache.read("lib1", "a.md"))
        self.assertIsNone(self.cache.read("lib1", "b.md"))
        self.assertEqual(self.cache.list_relative_paths("lib1"), [])

    def test_clear_library_does_not_affect_other_libraries(self):
        self.cache.write("lib1", "a.md", "a")
        self.cache.write("lib2", "a.md", "a2")
        self.cache.clear_library("lib1")
        self.assertIsNone(self.cache.read("lib1", "a.md"))
        self.assertEqual(self.cache.read("lib2", "a.md"), "a2")

    def test_clear_nonexistent_library_is_noop(self):
        self.cache.clear_library("never-existed")  # 不该抛异常

    def test_list_relative_paths_returns_sorted_unique(self):
        self.cache.write("lib1", "b.md", "b")
        self.cache.write("lib1", "a.md", "a")
        self.cache.write("lib1", "a.md", "a2")  # 覆盖写，不该出现两次
        self.assertEqual(self.cache.list_relative_paths("lib1"), ["a.md", "b.md"])

    def test_list_relative_paths_empty_library_returns_empty_list(self):
        self.assertEqual(self.cache.list_relative_paths("never-existed"), [])


class TestConversionStash(unittest.TestCase):
    """转换暂存：按“内容指纹 + 转换器与版本”存，跟轮次（generation）无关——一轮被停掉、这一轮
    的缓存段被丢弃，已经转好的正文还在（2026-09-29 操作者确认）。"""

    H1 = "a" * 64
    H2 = "b" * 64
    LOCAL = "official-ocr-mineru-local:0.2.0"
    TEXT = "official-extractor-pdf-text:0.3.0"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = ExtractCache(self.tmp / "extracted")

    def test_round_trip_reports_which_converter_produced_it(self):
        self.cache.write_stash("lib1", self.H1, self.LOCAL, "识别出的正文")
        self.assertEqual(self.cache.read_stash("lib1", self.H1, [self.TEXT, self.LOCAL]), ("识别出的正文", self.LOCAL))

    def test_a_different_converter_version_does_not_match(self):
        self.cache.write_stash("lib1", self.H1, "official-extractor-pdf-text:0.2.0", "只有水印")
        self.assertIsNone(self.cache.read_stash("lib1", self.H1, [self.TEXT]))

    def test_a_different_content_does_not_match(self):
        self.cache.write_stash("lib1", self.H1, self.LOCAL, "旧内容的识别结果")
        self.assertIsNone(self.cache.read_stash("lib1", self.H2, [self.LOCAL]))

    def test_discarding_a_generation_keeps_the_stash(self):
        self.cache.write("lib1", "scan.pdf", "本轮缓存", generation="g-cancelled", route=self.LOCAL)
        self.cache.write_stash("lib1", self.H1, self.LOCAL, "识别出的正文")
        self.cache.clear_library("lib1", "g-cancelled")
        self.assertEqual(self.cache.read_stash("lib1", self.H1, [self.LOCAL]), ("识别出的正文", self.LOCAL))

    def test_removing_the_library_removes_its_stash(self):
        self.cache.write_stash("lib1", self.H1, self.LOCAL, "识别出的正文")
        self.cache.clear_library("lib1")
        self.assertIsNone(self.cache.read_stash("lib1", self.H1, [self.LOCAL]))

    def test_prune_drops_settled_and_expired_entries_and_keeps_the_rest(self):
        import os
        import time

        self.cache.write_stash("lib1", self.H1, self.LOCAL, "已入库")
        self.cache.write_stash("lib1", self.H2, self.LOCAL, "还没轮到")
        self.cache.write_stash("lib1", "c" * 64, self.LOCAL, "放了很久")
        old = time.time() - 40 * 24 * 3600
        os.utime(self.cache._stash_path("lib1", "c" * 64, self.LOCAL), (old, old))
        removed = self.cache.prune_stash("lib1", settled_hashes={self.H1})
        self.assertEqual(removed, 2)
        self.assertIsNone(self.cache.read_stash("lib1", self.H1, [self.LOCAL]))
        self.assertIsNotNone(self.cache.read_stash("lib1", self.H2, [self.LOCAL]))

    def test_reading_refreshes_the_last_used_time(self):
        import os
        import time

        self.cache.write_stash("lib1", self.H1, self.LOCAL, "还被需要")
        path = self.cache._stash_path("lib1", self.H1, self.LOCAL)
        old = time.time() - 40 * 24 * 3600
        os.utime(path, (old, old))
        self.assertIsNotNone(self.cache.read_stash("lib1", self.H1, [self.LOCAL]))
        self.assertEqual(self.cache.prune_stash("lib1", settled_hashes=set()), 0)

    def test_a_non_hex_content_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cache.write_stash("lib1", "../../escape", self.LOCAL, "x")
        self.assertIsNone(self.cache.read_stash("lib1", "../../escape", [self.LOCAL]))

    def test_stash_does_not_show_up_as_a_library_entry(self):
        self.cache.write_stash("lib1", self.H1, self.LOCAL, "识别出的正文")
        self.assertEqual(self.cache.list_relative_paths("lib1"), [])


class TestIndexManifestStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = IndexManifestStore(self.tmp / "manifests")

    def test_write_read_and_reference_segments(self):
        manifest = {
            "format_version": INDEX_MANIFEST_VERSION,
            "library_id": "lib1",
            "generation": "g2",
            "vector_segments": ["g1", "g2"],
            "extract_segments": ["g1"],
            "lexical_segments": ["g2"],
        }
        self.assertTrue(self.store.write(manifest))
        self.assertEqual(self.store.read("lib1", "g2"), manifest)
        self.assertEqual(
            self.store.referenced_generations("lib1", ["g2"]),
            {"g1", "g2"},
        )

    def test_corrupt_or_missing_manifest_returns_none(self):
        self.assertIsNone(self.store.read("lib1", "missing"))
        self.store.write(
            {
                "format_version": INDEX_MANIFEST_VERSION,
                "library_id": "lib1",
                "generation": "g1",
            }
        )
        self.assertEqual(self.store.list_generations("lib1"), ["g1"])
        self.store.clear("lib1", "g1")
        self.assertIsNone(self.store.read("lib1", "g1"))


if __name__ == "__main__":
    unittest.main()
