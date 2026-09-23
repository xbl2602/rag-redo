"""core/index_failures.py 的单元测试：真实写盘、真实读回。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.index_failures import IndexFailuresStore  # noqa: E402


class TestIndexFailuresStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = IndexFailuresStore(self.tmp / "index_failures")

    def test_read_before_any_write_is_none(self):
        self.assertIsNone(self.store.read("lib1"))

    def test_write_then_read_round_trips(self):
        failures = [{"path": "scan.pdf", "reason": "提取失败：不支持的编码"}]
        self.store.write_library("lib1", succeeded=3, failures=failures)
        result = self.store.read("lib1")
        self.assertEqual(result["succeeded"], 3)
        self.assertEqual(result["failures"], failures)

    def test_all_succeeded_reports_empty_failures_not_none(self):
        """全部成功和从没跑过是两种不同的状态——全部成功要能明确区分于
        "从没索引过"，不能都返回 None 让调用方猜。"""
        self.store.write_library("lib1", succeeded=5, failures=[])
        result = self.store.read("lib1")
        self.assertIsNotNone(result)
        self.assertEqual(result["failures"], [])

    def test_rewriting_library_replaces_stale_failures(self):
        self.store.write_library("lib1", succeeded=1, failures=[{"path": "a.pdf", "reason": "x"}])
        self.store.write_library("lib1", succeeded=2, failures=[])
        result = self.store.read("lib1")
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failures"], [])

    def test_different_libraries_are_isolated(self):
        self.store.write_library("lib1", succeeded=1, failures=[{"path": "a.pdf", "reason": "x"}])
        self.store.write_library("lib2", succeeded=9, failures=[])
        self.assertEqual(self.store.read("lib1")["failures"], [{"path": "a.pdf", "reason": "x"}])
        self.assertEqual(self.store.read("lib2")["failures"], [])

    def test_unknown_library_after_others_written_is_still_none(self):
        self.store.write_library("lib1", succeeded=1, failures=[])
        self.assertIsNone(self.store.read("lib-never-indexed"))


if __name__ == "__main__":
    unittest.main()
