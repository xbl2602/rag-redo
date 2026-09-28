from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from official_vector_store_chroma.store import (  # noqa: E402
    ChromaVectorStore,
    chroma_collection_name,
)


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

    def test_collection_namespace_maintenance_round_trip(self):
        """按名建 → 按名列 → 按名删（全局回收那一族接口的闭环）。

        `ensure_collection_by_name` 存在的理由就是前两条造不出的集合：崩溃
        那一轮留下的 `libg_<sha(库, 未发布 generation)>` 残留，以及共用同一
        个 Chroma 目录的别的插件建的集合（`upsert` 只会写 `lib_/libg_`）。
        """
        self.assertEqual(
            self.store.ensure_collection_by_name("some_other_plugin_index"),
            "some_other_plugin_index",
        )
        self.assertIn("some_other_plugin_index", self.store.list_collection_names())
        # 幂等：再建一次不报错、也不产生第二份
        self.store.ensure_collection_by_name("some_other_plugin_index")
        self.assertEqual(
            self.store.list_collection_names().count("some_other_plugin_index"), 1
        )
        self.store.delete_collection_by_name("some_other_plugin_index")
        self.assertNotIn("some_other_plugin_index", self.store.list_collection_names())
        # 删不存在的名字静默吞掉（回收绝不能拖垮索引主流程）
        self.store.delete_collection_by_name("never-existed")

    def test_ensure_collection_by_name_never_overwrites_another_collection_data(self):
        """建空集合不得碰到同名集合里已有的数据（否则"回收测试台"变成
        "静默清库"）。"""
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]], documents=["原文"])
        self.store.ensure_collection_by_name("lib1")
        self.assertEqual(self.store.get_all("lib1")["c1"]["document"], "原文")

    # ------------------------------------------------------------------
    # 读路径绝不建集合（2026-09-27 全量回归随机 flake 的根因修复）
    #
    # 症状：`chromadb.errors.InternalError: Error executing plan: Internal
    # error: Error creating hnsw segment reader: Nothing found on disk`，
    # 随机落在不同用例上，只有全量单进程串行跑才复现（单独跑该用例必绿）。
    # 根因：`_collection()` 对读和写都用 `get_or_create_collection`，
    # 于是"探测某段有没有数据"的 `count()` 会凭空造出一个**空集合**；
    # 空集合在 Chroma 的 Rust 实现里没有落盘的 HNSW 段文件，之后任何一次
    # 真正查它都会抛上面那个 InternalError——一次无害的探测把用户侧的
    # 检索搞崩。修法：读路径走 `get_collection`，不存在就是"这段没数据"。
    # ------------------------------------------------------------------

    def test_count_on_missing_collection_does_not_create_it(self):
        """count 一段不存在的数据 = 0，且**不得留下空集合**。"""
        self.assertEqual(self.store.count("ghost-lib", "ghost-generation"), 0)
        self.assertEqual(
            [n for n in self.store.list_collection_names() if "ghost" in n or "lib_ghost" in n],
            [],
            "count() 是读操作，绝不能凭空建出集合",
        )

    def test_query_on_missing_collection_creates_nothing(self):
        self.assertEqual(
            self.store.query("ghost-lib", [1.0, 0.0, 0.0], generation="ghost-generation"),
            [],
        )
        self.assertEqual(
            [n for n in self.store.list_collection_names() if "ghost" in n or "lib_ghost" in n],
            [],
        )

    def test_get_by_ids_and_get_all_and_sample_create_nothing(self):
        self.assertEqual(self.store.get_by_ids("ghost-lib", ["c1"], "ghost-generation"), {})
        self.assertEqual(self.store.get_all("ghost-lib", "ghost-generation"), {})
        self.assertEqual(self.store.sample("ghost-lib", generation="ghost-generation"), [])
        self.assertEqual(
            [n for n in self.store.list_collection_names() if "ghost" in n or "lib_ghost" in n],
            [],
        )

    def test_delete_on_missing_collection_creates_nothing(self):
        """删除也是"读"——删一个不存在的集合应该是无操作，不是先建再删。"""
        self.store.delete("ghost-lib", ["c1"], "ghost-generation")
        self.assertEqual(
            [n for n in self.store.list_collection_names() if "ghost" in n or "lib_ghost" in n],
            [],
        )

    def test_probing_a_missing_generation_then_querying_does_not_crash(self):
        """完整复现链：先探测（count）再查询同一段，必须干净返回空。"""
        self.assertEqual(self.store.count("lib1", "no-such-generation"), 0)
        self.assertEqual(
            self.store.query("lib1", [1.0, 0.0, 0.0], generation="no-such-generation"),
            [],
        )

    def test_query_degrades_when_the_collection_is_corrupt(self):
        """段文件损坏/被外部动过时，本路降级为空 + warning，不能让整轮检索崩。"""
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]], documents=["原文"])

        class _Corrupt:
            def count(self):
                raise RuntimeError(
                    "Error creating hnsw segment reader: Nothing found on disk"
                )

            def query(self, **_kwargs):
                raise AssertionError("count 失败后不该走到 query")

        with mock.patch.object(self.store, "_existing_collection", return_value=_Corrupt()):
            with self.assertLogs("rag_redo.vector_store_chroma", level="WARNING") as logs:
                self.assertEqual(self.store.query("lib1", [1.0, 0.0, 0.0]), [])
        self.assertTrue(
            any("降级为空" in line for line in logs.output),
            f"必须留下可定位的降级告警，实际：{logs.output}",
        )

    def test_count_still_propagates_real_errors_for_fail_open(self):
        """count 的真实读取异常必须**继续往上抛**。

        `core/index_integrity.py::count_store_chunks` 靠捕获异常来 fail-open
        （返回 None = "数不出来，别据此判 stale"）。如果这里把异常也吞成 0，
        一致性自愈会把"探测失败"误读成"块丢了"，进而触发一次本不该发生
        的全量重建。"""
        self.store.upsert("lib1", ["c1"], [[1.0, 0.0, 0.0]])

        class _Broken:
            def count(self):
                raise RuntimeError("disk on fire")

        with mock.patch.object(self.store, "_existing_collection", return_value=_Broken()):
            with self.assertRaises(RuntimeError):
                self.store.count("lib1")

    def test_existing_collection_helper_is_used_by_every_read_path(self):
        """结构性护栏：读路径不许再直接用 get_or_create_collection。

        这条是"读路径绝不建集合"的元约束——将来有人图省事把某个读方法改回
        `_ensure_collection`，这里会立刻红。"""
        import inspect

        source = inspect.getsource(ChromaVectorStore)
        for name in ("get_by_ids", "get_all", "query", "count", "sample", "delete"):
            body = source.split(f"def {name}(", 1)[1].split("\n    def ", 1)[0]
            self.assertNotIn(
                "_ensure_collection(", body,
                f"{name}() 是读路径，不允许建集合",
            )

    def test_infrastructure_error_is_not_reported_as_zero_chunks(self):
        """查集合本身失败（客户端连不上/目录损坏/权限）必须**上抛**。

        这条是本轮修的一个隐蔽风险：`_existing_collection` 最初 catch 的是
        裸 `Exception`，于是任何基础设施故障都会被 `count()` 报成"这段 0 块"，
        `core/index_integrity.py` 的一致性自愈据此判定"块丢了"→ 触发一次
        本不该发生的全量重建，把一次可恢复的探测故障放大成数据事件。"""
        boom = RuntimeError("chroma client unavailable")

        with mock.patch.object(self.store._client, "get_collection", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.store.count("lib1")
            with self.assertRaises(RuntimeError):
                self.store.query("lib1", [1.0, 0.0, 0.0])
            with self.assertRaises(RuntimeError):
                self.store.get_all("lib1")


class TestChromaCollectionNaming(unittest.TestCase):
    r"""集合名合法化。

    Chroma 对集合名有硬约束（实测 chromadb 1.5：`Expected a name containing
    3-512 characters from [a-zA-Z0-9._-], starting and ending with a character
    in [a-zA-Z0-9]`），而 `library_id` 是用户可见自由文本，`add_library` 只挡
    `/\:*?"<>|` 与控制字符——中文、空格都是**合法**库 id。原先直接
    `f"lib_{library_id}"`，于是"用中文名建库"必然在第一次写向量时抛
    `InvalidArgumentError`，而错误信息完全指不到真因。

    下面每条都用**真 chromadb 客户端**验收，不自己实现一遍规则（自己实现
    的正则和 Chroma 的实现漂移了，这个测试就会变成"验证我的正则"而不是
    "验证能建集合"）。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = ChromaVectorStore(self.tmp / "chroma")

    #: 每一条都是 add_library 允许、但拼不出合法 Chroma 集合名的库 id。
    ILLEGAL_IDS = (
        "我的知识库",
        "my vault",
        "笔记/收藏",          # 斜杠会被 add_library 挡，但归档里可能有
        "trailing_",
        "x" * 600,
        "库.名",
    )

    def test_chinese_and_space_library_ids_can_actually_upsert(self):
        """回归钉子：修复前 `lib_我的知识库` 直接抛 InvalidArgumentError。"""
        for index, library_id in enumerate(self.ILLEGAL_IDS):
            with self.subTest(library_id=library_id[:20]):
                self.store.upsert(
                    library_id,
                    [f"c{index}"],
                    [[1.0, 0.0, 0.0]],
                    documents=[f"doc for {library_id[:12]}"],
                )
                self.assertEqual(self.store.count(library_id), 1)
                results = self.store.query(library_id, [1.0, 0.0, 0.0], top_k=1)
                self.assertEqual([cid for cid, _ in results], [f"c{index}"])

    def test_legal_ascii_ids_keep_their_existing_collection_name(self):
        """向后兼容：原本合法的 id 必须仍然拼出一字不差的名字，
        否则所有已有用户的向量集合会集体'消失'（读不到 = 索引没了）。"""
        for library_id in ("notes", "my-vault", "a_b", "lib1"):
            with self.subTest(library_id=library_id):
                self.assertEqual(
                    chroma_collection_name(library_id),
                    f"lib_{library_id}",
                )

    def test_names_are_injective_across_legal_and_hashed(self):
        """`a b` 与 `a_b` 这种无哈希后缀的字符替换不单射，两个库共用一份
        数据（core/library_key.py 模块 docstring 记的是同一个坑）。"""
        ids = [
            "notes", "my-vault", "a_b", "a b", "a.b", "a-b",
            "我的知识库", "我的 知识库", "我的-知识库", "我的_知识库",
        ]
        names = [chroma_collection_name(i) for i in ids]
        self.assertEqual(len(set(names)), len(ids), f"集合名撞车了：{list(zip(ids, names))}")

    def test_generation_names_unchanged(self):
        """带 generation 的命名（`libg_<sha>`）本来就是纯十六进制，
        这条钉住它没被"顺手一起合法化"改掉。"""
        for library_id in ("notes", "我的知识库", "my vault"):
            with self.subTest(library_id=library_id):
                name = chroma_collection_name(library_id, "gen-1")
                self.assertTrue(name.startswith("libg_"))
                self.assertEqual(len(name), 45)
                self.assertEqual(name, chroma_collection_name(library_id, "gen-1"))

    def test_public_collection_name_for_matches_internal(self):
        """回收白名单走公开出口，必须和内部算出来的一字不差。"""
        for library_id in ("notes", "我的知识库"):
            for generation in (None, "gen-1"):
                with self.subTest(library_id=library_id, generation=generation):
                    self.assertEqual(
                        self.store.collection_name_for(library_id, generation),
                        self.store._collection_name(library_id, generation),
                    )


if __name__ == "__main__":
    unittest.main()
