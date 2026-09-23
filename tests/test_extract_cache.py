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


if __name__ == "__main__":
    unittest.main()
