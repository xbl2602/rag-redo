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

from official_vector_store_chroma.store import ChromaVectorStore  # noqa: E402


class TestChromaVectorStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = ChromaVectorStore(self.tmp / "chroma")

    def test_upsert_and_query_returns_closest_first(self):
        self.store.upsert(
            "lib1",
            ["c1", "c2"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            documents=["about cats", "about dogs"],
        )
        results = self.store.query("lib1", [1.0, 0.0, 0.0], top_k=2)
        self.assertEqual(results[0][0], "c1")

    def test_empty_collection_query_returns_empty(self):
        self.assertEqual(self.store.query("lib1", [1.0, 0.0, 0.0]), [])

    def test_count_reflects_upserts(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]])
        self.assertEqual(self.store.count("lib1"), 1)

    def test_upsert_same_id_updates_not_duplicates(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]])
        self.store.upsert("lib1", ["c1"], [[0.0, 0.0, 1.0]])
        self.assertEqual(self.store.count("lib1"), 1)

    def test_delete_removes_entry(self):
        self.store.upsert("lib1", ["c1", "c2"], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        self.store.delete("lib1", ["c1"])
        self.assertEqual(self.store.count("lib1"), 1)

    def test_libraries_are_isolated(self):
        """一个库的向量查询绝不能命中另一个库的数据——隔离性由 collection
        边界保证，不是靠 library_id 字段过滤（DATA_FLOW.md 库隔离精神的
        向量存储层体现）。"""
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]])
        self.store.upsert("lib2", ["c2"], [[1.0, 0.0, 0.0]])
        results = self.store.query("lib1", [1.0, 0.0, 0.0])
        ids = [chunk_id for chunk_id, _ in results]
        self.assertIn("c1", ids)
        self.assertNotIn("c2", ids)

    def test_empty_upsert_is_noop(self):
        self.store.upsert("lib1", [], [])  # 不应该抛异常

    def test_persists_across_instances(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]])
        store2 = ChromaVectorStore(self.tmp / "chroma")
        self.assertEqual(store2.count("lib1"), 1)

    def test_get_by_ids_returns_documents_and_metadata(self):
        self.store.upsert(
            "lib1",
            ["c1", "c2"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            documents=["文本一", "文本二"],
            metadatas=[{"path": "a.md"}, {"path": "b.md"}],
        )
        records = self.store.get_by_ids("lib1", ["c1", "c2"])
        self.assertEqual(records["c1"]["document"], "文本一")
        self.assertEqual(records["c1"]["metadata"]["path"], "a.md")

    def test_get_by_ids_empty_list_returns_empty_dict(self):
        self.assertEqual(self.store.get_by_ids("lib1", []), {})

    def test_get_by_ids_missing_id_simply_absent(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]], documents=["文本"])
        records = self.store.get_by_ids("lib1", ["c1", "does-not-exist"])
        self.assertIn("c1", records)
        self.assertNotIn("does-not-exist", records)

    def test_get_all_returns_every_record_with_embeddings(self):
        self.store.upsert(
            "lib1",
            ["c1", "c2"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            documents=["文本一", "文本二"],
            metadatas=[{"path": "a.md"}, {"path": "b.md"}],
        )
        records = self.store.get_all("lib1")
        self.assertEqual(set(records), {"c1", "c2"})
        self.assertEqual(records["c1"]["document"], "文本一")
        self.assertEqual(records["c1"]["embedding"], [1.0, 0.0, 0.0])

    def test_get_all_empty_collection_returns_empty_dict(self):
        self.assertEqual(self.store.get_all("lib1"), {})

    def test_get_all_only_returns_requested_library(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]], documents=["文本一"])
        self.store.upsert("lib2", ["c2"], [[0.0, 1.0, 0.0]], documents=["文本二"])
        records = self.store.get_all("lib1")
        self.assertEqual(set(records), {"c1"})

    def test_sample_empty_collection_returns_empty_list(self):
        self.assertEqual(self.store.sample("lib1"), [])

    def test_sample_returns_at_most_k_rows(self):
        self.store.upsert(
            "lib1",
            ["c1", "c2", "c3"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            documents=["文本一", "文本二", "文本三"],
        )
        rows = self.store.sample("lib1", k=2)
        self.assertEqual(len(rows), 2)

    def test_sample_covers_semantically_spread_points_not_duplicates(self):
        """最远点采样应该挑出彼此分散的点，不是恰好挑到一堆重复/相邻的——
        三个正交方向各放一个块，k=3 应该三个都选中（互相最远）。"""
        self.store.upsert(
            "lib1",
            ["c1", "c2", "c3"],
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            documents=["讲天文", "讲地理", "讲历史"],
            metadatas=[{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}],
        )
        rows = self.store.sample("lib1", k=3)
        self.assertEqual({r["path"] for r in rows}, {"a.md", "b.md", "c.md"})

    def test_sample_degenerate_all_identical_vectors_does_not_crash(self):
        """全部向量重合的退化情形（比如库只有一份内容被切成很多相同的块）
        应该提前收手，不抛异常、不死循环。"""
        self.store.upsert(
            "lib1",
            ["c1", "c2", "c3"],
            [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
            documents=["同一段文本"] * 3,
        )
        rows = self.store.sample("lib1", k=5)
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), 3)

    def test_sample_truncates_long_text_to_400_chars(self):
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]], documents=["字" * 1000])
        rows = self.store.sample("lib1", k=1)
        self.assertEqual(len(rows[0]["text"]), 400)


if __name__ == "__main__":
    unittest.main()
