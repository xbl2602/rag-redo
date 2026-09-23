from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_dedup.dedup import DedupIndex  # noqa: E402
from official_dedup.plugin import DedupPlugin  # noqa: E402

BASE_TEXT = (
    "插件化架构的核心思想是把功能拆分成互相独立的模块，每个模块通过统一的契约和核心交互，"
    "而不是模块之间直接互相调用。这样做的好处是任何一个模块都可以被单独替换或者移除，"
    "而不影响其他模块的正常工作。"
)
NEAR_DUP_TEXT = BASE_TEXT.replace("互相独立的模块", "彼此独立的模块").replace("单独替换", "自由替换")
UNRELATED_TEXT = (
    "今天天气很好，适合出门散步。路边的花都开了，公园里有很多人在锻炼身体，"
    "还有小朋友在草地上放风筝，场面十分热闹。"
)


class TestDedupIndex(unittest.TestCase):
    def test_near_identical_text_detected_as_similar(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        idx.add("b", NEAR_DUP_TEXT)
        self.assertIn("b", idx.similar_to("a"))

    def test_unrelated_text_not_similar(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        idx.add("c", UNRELATED_TEXT)
        self.assertNotIn("c", idx.similar_to("a"))

    def test_similar_to_excludes_self(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        self.assertNotIn("a", idx.similar_to("a"))

    def test_find_duplicate_groups_excludes_singletons(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        idx.add("b", NEAR_DUP_TEXT)
        idx.add("c", UNRELATED_TEXT)
        groups = idx.find_duplicate_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0], ["a", "b"])

    def test_remove_takes_document_out_of_index(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        idx.add("b", NEAR_DUP_TEXT)
        idx.remove("a")
        self.assertEqual(idx.count(), 1)
        self.assertEqual(idx.similar_to("b"), [])

    def test_readd_same_id_replaces_not_duplicates(self):
        idx = DedupIndex(threshold=0.7)
        idx.add("a", BASE_TEXT)
        idx.add("a", UNRELATED_TEXT)
        self.assertEqual(idx.count(), 1)

    def test_empty_index_has_no_groups(self):
        idx = DedupIndex()
        self.assertEqual(idx.find_duplicate_groups(), [])


class TestDedupPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = DedupPlugin()
        self.plugin.indexes = {}

    def test_libraries_are_isolated(self):
        """一个库的重复检测绝不该和另一个库的内容比对——继承
        official-lexical-bm25 补过的"一库一索引"隔离教训，这次一次性
        做对。"""
        self.plugin.add_document("lib1", "a", BASE_TEXT)
        self.plugin.add_document("lib2", "b", NEAR_DUP_TEXT)
        self.assertEqual(self.plugin.find_duplicate_groups("lib1"), [])
        self.assertEqual(self.plugin.find_duplicate_groups("lib2"), [])

    def test_duplicates_found_within_same_library(self):
        self.plugin.add_document("lib1", "a", BASE_TEXT)
        self.plugin.add_document("lib1", "b", NEAR_DUP_TEXT)
        groups = self.plugin.find_duplicate_groups("lib1")
        self.assertEqual(groups, [["a", "b"]])

    def test_unknown_library_returns_empty_not_error(self):
        self.assertEqual(self.plugin.find_duplicate_groups("no-such-lib"), [])


if __name__ == "__main__":
    unittest.main()
