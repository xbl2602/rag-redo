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


if __name__ == "__main__":
    unittest.main()
