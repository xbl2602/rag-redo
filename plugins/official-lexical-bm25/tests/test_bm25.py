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

from official_lexical_bm25.bm25 import BM25Index, tokenize  # noqa: E402


class TestTokenize(unittest.TestCase):
    def test_chinese_is_segmented_not_char_by_char(self):
        tokens = tokenize("插件化架构设计")
        # jieba 应该切出词而不是退化成逐字——至少要比总字数少（说明真的
        # 分了词，不是纯 2-gram/逐字兜底）
        self.assertLess(len(tokens), len("插件化架构设计"))

    def test_english_lowercased(self):
        tokens = tokenize("RAG Redo Plugin")
        self.assertIn("rag", tokens)
        self.assertIn("redo", tokens)

    def test_punctuation_dropped(self):
        tokens = tokenize("你好，世界！")
        self.assertNotIn("，", tokens)
        self.assertNotIn("！", tokens)


class TestBM25Index(unittest.TestCase):
    def test_empty_index_returns_no_results(self):
        idx = BM25Index()
        self.assertEqual(idx.search("任何查询"), [])

    def test_doc_with_term_ranks_above_doc_without(self):
        idx = BM25Index()
        idx.add("d1", "插件系统的核心是运行时和数据流管理器")
        idx.add("d2", "今天天气不错，适合出去散步")
        results = idx.search("插件系统")
        self.assertEqual(results[0][0], "d1")

    def test_add_twice_replaces_not_duplicates(self):
        idx = BM25Index()
        idx.add("d1", "第一版内容")
        idx.add("d1", "第二版内容，插件系统")
        self.assertEqual(idx.doc_count, 1)
        results = idx.search("插件系统")
        self.assertEqual(results[0][0], "d1")

    def test_remove_makes_doc_unsearchable(self):
        idx = BM25Index()
        idx.add("d1", "插件系统架构")
        idx.remove("d1")
        self.assertEqual(idx.doc_count, 0)
        self.assertEqual(idx.search("插件系统"), [])

    def test_remove_nonexistent_is_noop(self):
        idx = BM25Index()
        idx.remove("nope")  # 不该抛异常

    def test_top_k_limits_results(self):
        idx = BM25Index()
        for i in range(20):
            idx.add(f"d{i}", "插件系统架构设计")
        results = idx.search("插件系统", top_k=5)
        self.assertEqual(len(results), 5)

    def test_query_with_no_matching_terms_returns_empty(self):
        idx = BM25Index()
        idx.add("d1", "插件系统架构")
        self.assertEqual(idx.search("完全无关的英文查询xyz"), [])

    def test_mixed_chinese_english_query(self):
        idx = BM25Index()
        idx.add("d1", "RAG插件系统的embedder扩展点")
        idx.add("d2", "今天天气")
        results = idx.search("embedder 扩展点")
        self.assertEqual(results[0][0], "d1")


class TestBM25IndexPersistence(unittest.TestCase):
    """索引重启后不该悄悄清空——这是端到端场景里才会暴露的真实缺口
    （Chroma 向量数据持久化，BM25 原本完全不落盘），见
    official_lexical_bm25/plugin.py 模块 docstring。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "lib1.json"

    def test_load_missing_file_returns_empty_index(self):
        idx = BM25Index.load(self.path)
        self.assertEqual(idx.doc_count, 0)

    def test_save_then_load_preserves_search_results(self):
        idx = BM25Index()
        idx.add("d1", "插件系统的架构设计")
        idx.add("d2", "今天天气不错")
        idx.save(self.path)

        loaded = BM25Index.load(self.path)
        self.assertEqual(loaded.doc_count, 2)
        results = loaded.search("插件系统")
        self.assertEqual(results[0][0], "d1")

    def test_save_then_load_preserves_k1_and_b(self):
        idx = BM25Index(k1=2.0, b=0.5)
        idx.add("d1", "内容")
        idx.save(self.path)
        loaded = BM25Index.load(self.path)
        self.assertEqual(loaded.k1, 2.0)
        self.assertEqual(loaded.b, 0.5)

    def test_corrupted_file_degrades_to_empty_not_crash(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        idx = BM25Index.load(self.path)
        self.assertEqual(idx.doc_count, 0)

    def test_loaded_index_supports_further_add_and_remove(self):
        idx = BM25Index()
        idx.add("d1", "插件系统")
        idx.save(self.path)

        loaded = BM25Index.load(self.path)
        loaded.add("d2", "又一篇插件笔记")
        loaded.remove("d1")
        self.assertEqual(loaded.doc_count, 1)
        results = loaded.search("插件")
        self.assertEqual(results[0][0], "d2")

    def test_to_dict_from_dict_round_trip(self):
        """export_import 插件走这两个方法（不经过文件），确认内存里
        直接来回转换也保真，不是只测过'写文件再读文件'这一条路径。"""
        idx = BM25Index(k1=1.8, b=0.6)
        idx.add("d1", "插件系统的架构设计")
        restored = BM25Index.from_dict(idx.to_dict())
        self.assertEqual(restored.k1, 1.8)
        self.assertEqual(restored.b, 0.6)
        self.assertEqual(restored.doc_count, 1)
        self.assertEqual(restored.search("插件系统")[0][0], "d1")


if __name__ == "__main__":
    unittest.main()
