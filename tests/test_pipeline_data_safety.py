r"""索引数据安全回归（缺陷 1/2/3/4/5/6）——编排层最不该出错的几条线：

1. 库路径临时不可用 → 整库索引被清空（数据丢失，已实测复现）
2. meta 期望块数 ≠ 向量库实际块数 → 没有任何自愈（LEGACY 两条自愈全缺）
3. `full=True` 绕过提取缓存（改切块粒度会重复烧 MinerU 配额），且没有
   `fresh_extract` 等价物
4. 全局回收不清 Chroma 残留集合（LEGACY index.py:2404-2432 明确做这件事）
5. `re.sub(r"[^\w.-]", "_", library_id)` 无哈希后缀 → `a b` 与 `a_b` 共用
   同一份派生数据，其中一份还被全局回收当孤儿删掉
6. `library_freshness` 不比 manifest 签名 → 升级后不自动重建

**装置复用**：`tests/test_pipeline_e2e.py::TestEndToEndSearchPipeline` 的
`_build_runtime`（假 encoder + 假 reranker，绝不加载真实模型，见
AGENTS.md §9）用**组合**方式借用而不是继承——继承会把那 142 条用例一起
拖进本文件，本文件必须只跑自己这几条。
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

_E2E_PATH = REPO_ROOT / "tests" / "test_pipeline_e2e.py"
_spec = importlib.util.spec_from_file_location("data_safety__test_pipeline_e2e", _E2E_PATH)
assert _spec is not None and _spec.loader is not None
_e2e = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _e2e
_spec.loader.exec_module(_e2e)

_E2EBase = _e2e.TestEndToEndSearchPipeline  # 只借装置，不继承它的用例

from core import index_integrity  # noqa: E402
from core.index_failures import IndexFailuresStore  # noqa: E402
from core.index_generation import IndexGenerationStore, IndexManifestStore  # noqa: E402
from core.library_key import library_storage_key  # noqa: E402
from core.note_relations import NoteRelationsStore  # noqa: E402
from core.pipeline import SKIP_MISSING_ROOT  # noqa: E402


def _collection_name(library_id: str, segment: str) -> str:
    """官方 Chroma store 的集合名算法（正向复算，store.py::_collection）。"""
    return "libg_" + hashlib.sha256(f"{library_id}\0{segment}".encode("utf-8")).hexdigest()[:40]


def _embedded_chunk_count(embed_mock) -> int:
    """被送去向量化的块总数。向量化现在是跨文件的连续调用（旧项目顺序：先全部转换切块、
    再统一嵌入），所以不能再用“嵌入器被调用几次 == 文件数”来数——数块。"""
    return sum(len(call.args[0]) for call in embed_mock.call_args_list)


class _DataSafetyBase(unittest.TestCase):
    """借用 e2e 的 setUp（一个已注册、含两个 md 的库 + 完整插件运行时）。"""

    def setUp(self) -> None:
        harness = _E2EBase("test_index_then_search_finds_relevant_doc")
        harness.setUp()
        # e2e 的 setUp 把 tmp 清理与 runtime.close 都挂在它自己的 cleanup 栈上
        self.addCleanup(harness.doCleanups)
        self.tmp = harness.tmp
        self.vault = harness.vault
        self.data_dir = harness.data_dir
        self.runtime = harness.runtime
        self.pipeline = harness.pipeline
        self.lib_mgr = harness.lib_mgr

    def _text_extractor(self):
        return self.runtime.plugins["official-extractor-text"].instance

    def _embedder(self):
        return self.runtime.plugins["official-embedder-bge-m3"].instance

    def _vector_store(self):
        return self.pipeline._singleton("vector_store")


# ---------------------------------------------------------------- 缺陷 1

class TestMissingRootKeepsIndex(_DataSafetyBase):
    """缺陷 1：库路径临时不可用时 `index_library` 把整库索引清空（已实测复现）。

    对齐 obsidian-rag/index.py:1871-1873：
    `if not Path(vault).is_dir(): log("库路径不存在，跳过索引（保留现有索引）"); return`
    """

    def test_offline_root_keeps_generation_manifest_and_hits(self):
        first = self.pipeline.index_library("test-lib", generation_id="first")
        self.assertEqual(first.succeeded, 2)
        generation_before = self.pipeline._generations.active("test-lib")
        self.assertEqual(generation_before, "first")
        manifest_before = self.pipeline._manifest("test-lib", "first")
        self.assertEqual(len(manifest_before["files"]), 2)
        generations_before = set(self.pipeline._manifests.list_generations("test-lib"))
        hits_before = self.pipeline.search("test-lib", "插件 架构", top_k=10)
        self.assertTrue(hits_before)

        moved = self.tmp / "vault-offline"
        self.vault.rename(moved)
        try:
            self.assertFalse(self.vault.exists())
            report = self.pipeline.index_library("test-lib", generation_id="second")
            # 表达"本轮因库路径缺失而跳过"，且不冒充文件级 deferred
            self.assertTrue(report.skipped)
            self.assertEqual(report.skip_reason, SKIP_MISSING_ROOT)
            self.assertEqual(report.deferred, 0)
            self.assertEqual(report.files, [])
            self.assertEqual(report.added, 0)
            self.assertEqual(report.removed, 0)
            # 代数不动
            self.assertEqual(self.pipeline._generations.active("test-lib"), generation_before)
            self.assertEqual(
                set(self.pipeline._manifests.list_generations("test-lib")), generations_before
            )
            manifest_after = self.pipeline._manifest("test-lib", generation_before)
            self.assertEqual(len(manifest_after["files"]), len(manifest_before["files"]))
            self.assertEqual(manifest_after["vector_segments"], manifest_before["vector_segments"])
            # 检索命中一条不少
            hits_after = self.pipeline.search("test-lib", "插件 架构", top_k=10)
            self.assertEqual([r.path for r in hits_after], [r.path for r in hits_before])
        finally:
            moved.rename(self.vault)

    def test_missing_root_agrees_with_freshness_missing_flag(self):
        """两道闸必须说同一件事：freshness 判 missing（不自动同步），
        index_library 判 skip（就算被强行调用也不清空）。"""
        self.pipeline.index_library("test-lib", generation_id="first")
        moved = self.tmp / "vault-detached-2"
        self.vault.rename(moved)
        try:
            freshness = self.pipeline.library_freshness("test-lib")
            self.assertTrue(freshness["test-lib"].stale)
            self.assertTrue(freshness["test-lib"].missing)
            self.assertTrue(self.pipeline.stale_libraries("test-lib"))
            self.assertTrue(self.pipeline.index_library("test-lib").skipped)
        finally:
            moved.rename(self.vault)

    def test_index_resumes_incrementally_after_root_comes_back(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        moved = self.tmp / "vault-offline-3"
        self.vault.rename(moved)
        try:
            self.pipeline.index_library("test-lib", generation_id="second")
        finally:
            moved.rename(self.vault)
        with patch.object(self._text_extractor(), "extract", wraps=self._text_extractor().extract) as extract_mock:
            with patch.object(self._embedder(), "embed_chunks", wraps=self._embedder().embed_chunks) as embed_mock:
                report = self.pipeline.index_library("test-lib", generation_id="third")
        self.assertFalse(report.skipped)
        self.assertEqual(self.pipeline._generations.active("test-lib"), "third")
        self.assertEqual(report.unchanged, 2, report.files)
        self.assertEqual(extract_mock.call_count, 0)
        self.assertEqual(embed_mock.call_count, 0)
        self.assertTrue(self.pipeline.search("test-lib", "厨房 食谱", top_k=5))


# ---------------------------------------------------------------- 缺陷 2

class TestConsistencySelfHealing(_DataSafetyBase):
    """缺陷 2：一致性自愈整体缺失（LEGACY index.py:1903-1910 / 1592-1597）。"""

    def test_vector_store_damage_triggers_rebuild_and_is_reported_stale(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        manifest = self.pipeline._manifest("test-lib", "first")
        expected = index_integrity.expected_chunk_count(manifest)
        self.assertGreater(expected, 0)

        # 真实破坏：把 active manifest 引用的向量集合整本删掉（模拟外部清理
        # 工具/磁盘故障/进程被杀在清库窗口，LEGACY index.py:1899-1901 点名的
        # 两类场景）。用插件自己的 `delete_generation`——它按 store 的命名
        # 算法正算 `libg_<sha(库, 段)>`，测试里不再复算一遍哈希（复算就等于
        # 把"段名 → 集合名"这条规则抄成第二份，改一处就悄悄对不上）。
        store = self._vector_store()
        for segment in manifest["vector_segments"]:
            store.delete_generation("test-lib", segment)

        # 检索前扫描：判 stale（LEGACY index.py:1592-1597）
        self.assertTrue(self.pipeline.library_freshness("test-lib")["test-lib"].stale)

        with patch.object(self._embedder(), "embed_chunks", wraps=self._embedder().embed_chunks) as embed_mock:
            report = self.pipeline.index_library("test-lib", generation_id="second")
        # 增量修不了缺失的块（指纹全命中 → 没有新块可写），必须整库重建
        self.assertEqual(_embedded_chunk_count(embed_mock), 2, "缺块后必须整库重建而不是当无事发生")
        self.assertEqual(report.changed, 2, report.files)
        self.assertEqual(self.pipeline._generations.active("test-lib"), "second")
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))
        self.assertEqual(
            index_integrity.expected_chunk_count(
                self.pipeline._manifest("test-lib", "second")
            ),
            expected,
        )

    def test_all_terminal_library_zero_expected_is_not_treated_as_drift(self):
        """全终态库（只有 empty/tbd 条目，0 个非终态块）期望 0 块、
        实际 0 块，不能因为 0==0 触发重建（LEGACY index.py:1902 显式排除）。"""
        empty_vault = self.tmp / "terminal-vault"
        empty_vault.mkdir()
        (empty_vault / "empty.md").write_text("   \n", encoding="utf-8")
        (empty_vault / "draft.md").write_text("[TBD]\nTODO —\n占位", encoding="utf-8")
        self.lib_mgr.store.add_library("terminal-lib", "终态库", str(empty_vault))
        first = self.pipeline.index_library("terminal-lib", generation_id="first")
        self.assertEqual(first.failed, 2)
        manifest = self.pipeline._manifest("terminal-lib", "first")
        self.assertEqual(index_integrity.expected_chunk_count(manifest), 0)

        self.assertFalse(self.pipeline.library_freshness("terminal-lib")["terminal-lib"].stale)
        with patch.object(self._embedder(), "embed_chunks", wraps=self._embedder().embed_chunks) as embed_mock:
            with patch.object(self._text_extractor(), "extract", wraps=self._text_extractor().extract) as extract_mock:
                report = self.pipeline.index_library("terminal-lib", generation_id="second")
        self.assertEqual(embed_mock.call_count, 0, "全终态库不该被一致性自愈拖着重建")
        self.assertEqual(extract_mock.call_count, 0)
        self.assertEqual(report.retried, 0, report.files)

    def test_count_probe_failure_degrades_to_pass_through(self):
        """探测失败（count 抛异常）必须 fail-open：既不能把整轮索引搞崩，
        也不能反过来宣称索引是好的（AGENTS.md §7 探测失败 vs 所有权未知）。
        对齐 LEGACY `_chroma_count` 返回 None → kb_stale 不判漂移
        （obsidian-rag/index.py:1595）。"""
        self.pipeline.index_library("test-lib", generation_id="first")
        store = self._vector_store()
        with patch.object(store, "count", side_effect=RuntimeError("chroma count boom")):
            report = self.pipeline.index_library("test-lib", generation_id="second")
            freshness = self.pipeline.library_freshness("test-lib")
        self.assertEqual(report.skipped, False)
        self.assertEqual(report.unchanged, 2, report.files)
        self.assertFalse(freshness["test-lib"].stale)

    def test_ghosts_do_not_count_as_drift(self):
        """`actual > expected` 是压缩前的设计内中间态（旧块还在上一代集合里，
        靠 active_chunk_ids 过滤），不能当成损坏——否则每轮都误报漂移、
        每轮强制全量重建，正好制造 LEGACY 注释里要消灭的死循环。"""
        manifest = {
            "files": {"a.md": {"status": "indexed", "chunk_ids": ["c1", "c2", "c3"]}}
        }
        self.assertEqual(index_integrity.expected_chunk_count(manifest), 3)
        self.assertIsNone(index_integrity.consistency_drift(3, 5))
        self.assertIsNone(index_integrity.consistency_drift(3, 3))
        self.assertIsNone(index_integrity.consistency_drift(0, 0))
        self.assertIsNone(index_integrity.consistency_drift(3, None))
        self.assertEqual(index_integrity.consistency_drift(3, 2), index_integrity.REBUILD_MISSING_CHUNKS)
        self.assertEqual(index_integrity.count_store_chunks(lambda _s: (_ for _ in ()).throw(RuntimeError()), ["a"]), None)

    def test_expected_count_uses_real_entries_not_raw_dict(self):
        """判定基准是真实条目（files 里值是 dict 的那些），不是 files 这个
        dict 本身——LEGACY 特别强调过（meta 里混着 _version 这类非 dict 键）。"""
        manifest = {
            "_version": 11,
            "files": {
                "ok.md": {"status": "indexed", "chunk_ids": ["c1", "c2"]},
                "scan.pdf": {"status": "failed", "failure_state": "scanned", "chunk_ids": []},
                "tbd.md": {"status": "failed", "failure_state": "tbd"},
            },
        }
        self.assertEqual(index_integrity.expected_chunk_count(manifest), 2)
        self.assertIsNone(index_integrity.rebuild_reason(manifest, actual_chunk_count=2))
        self.assertEqual(
            index_integrity.rebuild_reason(manifest, actual_chunk_count=1),
            index_integrity.REBUILD_MISSING_CHUNKS,
        )
        # 从未索引过 = 没有可判定的东西
        self.assertIsNone(index_integrity.rebuild_reason(None, actual_chunk_count=0))


# ---------------------------------------------------------------- 缺陷 3

class TestFullReusesExtractCache(_DataSafetyBase):
    """缺陷 3：`full=True` 完全绕过提取缓存，且没有"强制重提"的等价物。

    LEGACY `--full`（index.py:1891）只是 `meta = {}`，每个文件仍走
    `extract_to_markdown` → `_extract_full` 先查提取缓存
    （extractors.py:341-343 命中即秒回）；真正强制重提是 `--fresh-extract`
    （index.py:2450-2455）。
    """

    def test_full_reembeds_without_reextracting(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        extractor, embedder = self._text_extractor(), self._embedder()
        with patch.object(extractor, "extract", wraps=extractor.extract) as extract_mock:
            with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
                report = self.pipeline.index_library("test-lib", generation_id="second", full=True)
        self.assertEqual(extract_mock.call_count, 0, "full 不该重新解析正文（否则改切块粒度要重烧 MinerU 配额）")
        self.assertEqual(_embedded_chunk_count(embed_mock), 2, "full 必须重切块 + 重嵌")
        self.assertEqual(report.changed, 2, report.files)
        self.assertEqual(report.unchanged, 0)
        self.assertEqual(self.pipeline._generations.active("test-lib"), "second")
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))

    def test_fresh_extract_forces_re_extraction_and_updates_cache(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        extractor = self._text_extractor()
        with patch.object(extractor, "extract", wraps=extractor.extract) as extract_mock:
            report = self.pipeline.index_library(
                "test-lib", generation_id="second", fresh_extract=True
            )
        self.assertEqual(extract_mock.call_count, 2, "fresh_extract 必须真的重新解析")
        self.assertEqual(report.changed, 2, report.files)
        manifest = self.pipeline._manifest("test-lib", "second")
        # 提取段名**不是**写死的 "second"：压缩段会把本轮有效正文搬进
        # `{generation}-compact` 再让 extract_segments 指向它（pipeline.py 的
        # compaction 分支），所以这一代落在 "second-compact" 上是完全正确的。
        # 真正要钉住的是"这代正文由本轮拥有 + 真的写进去了"：
        self.assertTrue(manifest["extract_segments"], manifest["extract_segments"])
        for segment in manifest["extract_segments"]:
            self.assertTrue(
                segment.startswith("second"),
                f"提取段必须属于本轮 generation 的血脉：{manifest['extract_segments']}",
            )
        # 新 generation 的提取缓存里真的有正文（read_document 的来源）：
        # 目录存在还不够，得逐个文件读得到内容。
        self.assertTrue((self.data_dir / "extracted" / "test-lib" / "second").is_dir())
        for path in ("plugin-notes.md", "cooking.md"):
            cached = self.pipeline._extract_cache.read_any("test-lib", path, "second")
            self.assertIsNotNone(cached, f"{path} 的本轮提取缓存丢了")
            self.assertEqual(
                cached,
                (self.vault / path).read_text(encoding="utf-8"),
                f"{path} 的提取缓存内容与源文件不一致",
            )
        self.assertTrue(self.pipeline.search("test-lib", "厨房 食谱", top_k=5))

    def test_fresh_extract_default_is_false_and_plain_run_skips_extraction(self):
        first = self.pipeline.index_library("test-lib", generation_id="first")
        self.assertFalse(first.skipped)
        extractor = self._text_extractor()
        with patch.object(extractor, "extract", wraps=extractor.extract) as extract_mock:
            self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(extract_mock.call_count, 0)

    def test_forced_rebuild_keeps_deferred_file_searchable(self):
        """强制重建（fresh_extract / 一致性自愈 / 签名变化都会走 force_embed）
        + 某个文件本轮 deferred = 最容易丢数据的一格：那个文件一个新块都没
        写，它的块只在**老**集合里；压缩若只从"本轮写盘链"搬（force_embed 会
        把那条链清空到只剩本轮 generation），它的块就静默从检索里消失，而
        manifest 里的记录还写着 indexed——用户表现为"刚建的库搜不到了"。

        对齐 obsidian-rag/index.py:2142-2148：deferred 文件的旧块原样保留
        继续服务，本轮不动它。
        """
        from core.contracts import ExtractedDocument

        self.pipeline.index_library("test-lib", generation_id="first")
        before = [r.path for r in self.pipeline.search("test-lib", "厨房 食谱", top_k=5)]
        self.assertIn("cooking.md", before)

        real_extract = self._text_extractor().extract

        def defer_cooking(library_id, path, root):
            if path == "cooking.md":
                return ExtractedDocument(
                    library_id=library_id, path=path, text=None,
                    failure_reason="deferred", extracted_by="official-extractor-text",
                    extractor_version="test", content_hash="unused", failure_state="deferred",
                )
            return real_extract(library_id, path, root)

        with patch.object(self._text_extractor(), "extract", side_effect=defer_cooking):
            report = self.pipeline.index_library(
                "test-lib", generation_id="second", fresh_extract=True
            )
        self.assertEqual(report.deferred, 1, report.files)
        manifest = self.pipeline._manifest("test-lib", "second")
        self.assertEqual(manifest["files"]["cooking.md"]["status"], "indexed")
        after = [r.path for r in self.pipeline.search("test-lib", "厨房 食谱", top_k=5)]
        self.assertIn("cooking.md", after, "deferred 文件的旧块在强制重建后被丢了")
        # 它的提取正文也必须还在（read_document 的来源），否则等于静默删档
        for segment in manifest["extract_segments"]:
            self.assertIsNotNone(
                self.pipeline._extract_cache.read_any("test-lib", "cooking.md", segment),
                f"deferred 文件的提取正文在压缩段 {segment} 里丢了",
            )


# ---------------------------------------------------------------- 缺陷 4

class TestCollectionReclaim(_DataSafetyBase):
    """缺陷 4：全局回收不清 Chroma 残留集合（LEGACY index.py:2404-2432）。"""

    def test_removed_library_collections_are_reclaimed_live_ones_survive(self):
        other = self.tmp / "other-vault"
        other.mkdir()
        (other / "other.md").write_text("# 其他库\n\n插件 架构 内容。", encoding="utf-8")
        self.lib_mgr.store.add_library("other-lib", "other-lib", str(other))
        self.pipeline.index_library("other-lib", generation_id="only")
        other_manifest = self.pipeline._manifest("other-lib", "only")
        other_collections = {
            _collection_name("other-lib", segment)
            for segment in other_manifest["vector_segments"]
        }
        self.assertTrue(other_collections)
        store = self._vector_store()
        self.assertTrue(other_collections <= set(store.list_collection_names()))

        self.lib_mgr.store.remove_library("other-lib")
        self.pipeline.index_library("test-lib")
        remaining = set(store.list_collection_names())
        self.assertFalse(other_collections & remaining, "已删库的集合必须被回收")

        # 活库当前 generation 的集合绝不能被删，且仍可检索
        live_manifest = self.pipeline._manifest("test-lib", self.pipeline._generations.active("test-lib"))
        live_collections = {
            _collection_name("test-lib", segment) for segment in live_manifest["vector_segments"]
        }
        self.assertTrue(live_collections)
        self.assertTrue(live_collections <= set(store.list_collection_names()))
        self.assertTrue(self.pipeline.search("test-lib", "厨房 食谱", top_k=5))

    def test_deferred_previous_generation_collection_survives_reclaim(self):
        """deferred 语义刻意保留旧 generation 的集合（被跳过文件的旧块只在
        旧集合里），按任何"近似 keep 集"清扫都会打断它。"""
        from core.contracts import ExtractedDocument

        self.pipeline.index_library("test-lib", generation_id="first")
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n重写后的正文。", encoding="utf-8"
        )

        def deferred_extract(library_id, path, root):
            return ExtractedDocument(
                library_id=library_id, path=path, text=None,
                failure_reason="deferred", extracted_by="official-extractor-text",
                extractor_version="test", content_hash="unused", failure_state="deferred",
            )

        with patch.object(self._text_extractor(), "extract", side_effect=deferred_extract):
            report = self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(report.deferred, 1)
        self.pipeline.prune_unreferenced_data()
        remaining = set(self._vector_store().list_collection_names())
        self.assertIn(
            _collection_name("test-lib", "first"),
            remaining,
            "上一代集合仍被 manifest/history 引用，不能被回收",
        )
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))

    def test_unreferenced_stale_collection_of_live_library_is_reclaimed(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        store = self._vector_store()
        # 造一个"没有任何 manifest 引用过"的残留段集合：崩溃那一轮写了一半、
        # 进程被杀、manifest 从未原子发布——正是 AGENTS.md §5 要求回收的
        # 那一类。走插件的命名空间维护接口（`ensure_collection_by_name`），
        # 不伸手进 chromadb 私有属性。
        stale = store.ensure_collection_by_name(
            _collection_name("test-lib", "ancient-never-referenced")
        )
        self.assertIn(stale, set(store.list_collection_names()))
        removed = self.pipeline.prune_unreferenced_data()
        self.assertGreaterEqual(removed[1], 1)
        self.assertNotIn(stale, set(store.list_collection_names()))

    def test_foreign_namespace_collections_are_never_touched(self):
        """共享同一个 Chroma 目录时，别人的集合不属于我们的回收职责。"""
        self.pipeline.index_library("test-lib", generation_id="first")
        store = self._vector_store()
        foreign = store.ensure_collection_by_name("some_other_plugin_index")
        self.assertIn(foreign, set(store.list_collection_names()))
        try:
            self.pipeline.prune_unreferenced_data()
            self.assertIn(foreign, set(store.list_collection_names()))
            # 对照组：同名空间（lib_/libg_ 前缀）里不属于任何在册库的那个，
            # 必须被删——否则"只动自己命名空间"就变成了"什么都不删"。
            orphan = store.ensure_collection_by_name("lib_not-a-registered-library")
            self.assertIn(orphan, set(store.list_collection_names()))
            self.pipeline.prune_unreferenced_data()
            self.assertNotIn(orphan, set(store.list_collection_names()))
            self.assertIn(foreign, set(store.list_collection_names()))
            self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))
        finally:
            store.delete_collection_by_name(foreign)

    def test_cjk_library_live_collections_survive_reclaim(self):
        """中文库 id 的集合必须活过全局回收。

        这是"集合名合法化"改动的直接对偶风险：插件侧为了让 `lib_<中文id>`
        能被 Chroma 接受，把名字换成了哈希化的 `libk_<sha>`；如果回收侧的
        存活白名单还在自己复算 `f"lib_{library_id}"`，它就会把一个**正在
        使用**的集合当成残留删掉——用户可观察的数据丢失，而且要等到下一轮
        索引收尾才发生，极难归因。所以这条钉住白名单问插件要名字。
        """
        vault = self.tmp / "中文库"
        vault.mkdir()
        (vault / "笔记.md").write_text("# 标题\n\n插件 架构 内容。", encoding="utf-8")
        self.lib_mgr.store.add_library("中文 库 id", "中文库", str(vault))
        self.pipeline.index_library("中文 库 id", generation_id="gen-cjk")
        store = self._vector_store()
        names = set(store.list_collection_names())
        self.assertTrue(names, "中文库索引后应当留下集合")

        self.pipeline.prune_unreferenced_data()
        self.assertEqual(
            set(store.list_collection_names()),
            names,
            "中文库 id 的在用集合被全局回收误删（白名单与插件命名算法不一致）",
        )
        # 数据真的还在，不只是集合名还在
        self.assertTrue(self.pipeline.search("中文 库 id", "插件 架构", top_k=5))

    def test_hashed_namespace_collections_are_reclaimed(self):
        """对照组：`libk_` 前缀确实是我们的命名空间，残留的必须被清掉——
        否则上面那条的保护会退化成"什么都不删"。"""
        store = self._vector_store()
        orphan = store.ensure_collection_by_name("libk_" + "0" * 32)
        self.assertIn(orphan, set(store.list_collection_names()))
        self.pipeline.prune_unreferenced_data()
        self.assertNotIn(orphan, set(store.list_collection_names()))

    def test_live_compact_segment_collection_survives(self):
        """压缩段（`<generation>-compact`）的集合必须活过全局回收。

        压缩只在"上一代 manifest 存在且没压缩过"时触发（第一次索引没有旧段
        可搬，本来就是单段），所以这里先跑两轮——`pipeline.py` 的
        `compaction_due` 就是这个条件。
        """
        self.pipeline.index_library("test-lib", generation_id="first")
        self.pipeline.index_library("test-lib", generation_id="second")
        manifest = self.pipeline._manifest("test-lib", "second")
        self.assertTrue(manifest["compacted"], manifest)
        self.assertTrue(manifest["vector_segments"], manifest)
        remaining = set(self._vector_store().list_collection_names())
        for segment in manifest["vector_segments"]:
            self.assertIn(_collection_name("test-lib", segment), remaining)
        # 回收跑完之后（索引每轮收尾都会自己跑一遍，这里再显式跑一次确保
        # 幂等且不会误删）压缩段集合仍然在，而且还能检索。
        self.pipeline.prune_unreferenced_data()
        remaining = set(self._vector_store().list_collection_names())
        for segment in manifest["vector_segments"]:
            self.assertIn(
                _collection_name("test-lib", segment), remaining, "活库当前代的压缩段集合被误删"
            )
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))


# ---------------------------------------------------------------- 缺陷 5

class TestLibraryKeyCollision(_DataSafetyBase):
    r"""缺陷 5：`re.sub(r"[^\w.-]", "_", library_id)` 无哈希后缀 → key 碰撞。"""

    def test_ids_that_collide_after_sanitization_do_not_share_storage(self):
        vault_a = self.tmp / "vault-a"
        vault_b = self.tmp / "vault-b"
        vault_a.mkdir()
        vault_b.mkdir()
        # 两个库各自都有一份 b.md，出链图不同（甲库的 a.md 链到本库的 b.md，
        # 乙库的 b.md 是孤立笔记）。`[[b]]` 必须解析到**本库**的 b.md——
        # 跨库解析 wikilink 是不存在的语义，所以隔离性只能靠"两库各写各的
        # 目录、互不覆盖"来证明：一旦共用目录，generation 同名时后写的会
        # 直接盖掉先写的。
        (vault_a / "a.md").write_text("# 甲\n\n[[b]]", encoding="utf-8")
        (vault_a / "b.md").write_text("# 乙\n\n甲库里的 b", encoding="utf-8")
        (vault_b / "b.md").write_text("# 乙\n\n孤立笔记", encoding="utf-8")
        self.lib_mgr.store.add_library("a b", "带空格", str(vault_a))
        self.lib_mgr.store.add_library("a_b", "带下划线", str(vault_b))
        # 故意让两个库用**同名 generation**——真实的 generation 通常是 uuid
        # 不会撞，但共用目录时只要撞上就是一份数据覆盖另一份，这里把那个
        # 最坏情况固定下来。
        self.pipeline.index_library("a b", generation_id="shared-gen")
        self.pipeline.index_library("a_b", generation_id="shared-gen")

        # 1) 失败诊断各写各的（文件数不同 = 2 vs 1，本来就能看出串了）
        failures_a = self.pipeline.index_failures("a b")
        failures_b = self.pipeline.index_failures("a_b")
        self.assertIsNotNone(failures_a)
        self.assertIsNotNone(failures_b)
        self.assertEqual(failures_a["succeeded"], 2)
        self.assertEqual(failures_b["succeeded"], 1)
        # 2) 双链关系各是各的
        relations_a = self.pipeline.note_relations("a b", "a.md")
        relations_b = self.pipeline.note_relations("a_b", "b.md")
        self.assertEqual(relations_a["outlinks"], ["b.md"])
        self.assertTrue(relations_a["resolved"])
        self.assertTrue(relations_b["resolved"])
        self.assertEqual(relations_b["outlinks"], [])
        # 3) 两个 key = 两个物理目录，各自只装自己的 generation；共用目录
        #    的话下面两条 assertFalse 立刻红（这正是"孤儿判定/回收会误伤
        #    另一个库"和"两份数据互相覆盖"的根源）。
        self.pipeline.index_library("a b", generation_id="only-a-gen")
        self.pipeline.index_library("a_b", generation_id="only-b-gen")
        relations_dir = self.data_dir / "note_relations" / "generations"
        key_a, key_b = library_storage_key("a b"), library_storage_key("a_b")
        self.assertNotEqual(key_a, key_b)
        self.assertTrue((relations_dir / key_a / "only-a-gen.json").is_file())
        self.assertTrue((relations_dir / key_b / "only-b-gen.json").is_file())
        self.assertFalse(
            (relations_dir / key_a / "only-b-gen.json").exists(),
            "a b 的目录里出现了 a_b 的 generation——两个库共用了一份派生数据",
        )
        self.assertFalse(
            (relations_dir / key_b / "only-a-gen.json").exists(),
            "a_b 的目录里出现了 a b 的 generation——两个库共用了一份派生数据",
        )
        self.assertEqual(self.pipeline.note_relations("a b", "a.md")["outlinks"], ["b.md"])
        self.assertEqual(self.pipeline.note_relations("a_b", "b.md")["outlinks"], [])
        # 4) 全局回收不许把其中一份当孤儿删掉：删掉一个库之后另一个库的
        #    目录与数据必须原样还在（共用目录时这一步才会出事）。
        self.lib_mgr.store.remove_library("a_b")
        self.pipeline.index_library("a b", generation_id="shared-gen3")
        self.pipeline.prune_unreferenced_data()
        self.assertEqual(self.pipeline.note_relations("a b", "a.md")["outlinks"], ["b.md"])
        self.assertTrue(self.pipeline.index_failures("a b")["succeeded"] == 2)

    def test_cjk_and_space_library_ids_are_usable(self):
        vault = self.tmp / "中文 库"
        vault.mkdir()
        (vault / "笔记.md").write_text("# 标题\n\n插件 架构 内容。", encoding="utf-8")
        self.lib_mgr.store.add_library("中文 库 id", "中文库", str(vault))
        report = self.pipeline.index_library("中文 库 id", generation_id="gen-cjk")
        self.assertEqual(report.succeeded, 1)
        self.assertTrue(self.pipeline.search("中文 库 id", "插件 架构", top_k=5))

    def test_shared_key_matches_index_generation_implementation(self):
        """`core/index_generation.py` 的两个 `_key` 是本函数的逐字拷贝（文件
        所有权限制下没动它们）。两份实现必须永远一致，任何一边单方面改动都
        要被这条用例立刻抓住，否则全库目录会被判成孤儿删掉。"""
        store = IndexGenerationStore(self.tmp / "gens")
        manifests = IndexManifestStore(self.tmp / "manifests")
        for library_id in ("a b", "a_b", "中文 库 id", "", "x" * 200, "with/slash"):
            self.assertEqual(library_storage_key(library_id), store._key(library_id))
            self.assertEqual(library_storage_key(library_id), manifests._key(library_id))
        self.assertNotEqual(library_storage_key("a b"), library_storage_key("a_b"))

    def test_index_failures_and_relations_use_the_shared_key(self):
        failures = IndexFailuresStore(self.tmp / "failures")
        relations = NoteRelationsStore(self.tmp / "relations")
        key = library_storage_key("a b")
        self.assertEqual(failures._path_for("a b", "gen").parent.name, key)
        self.assertEqual(relations._path_for("a b", "gen").parent.name, key)
        failures.write_library("a b", succeeded=1, failures=[], generation="gen")
        self.assertEqual(failures.read("a b", "gen")["succeeded"], 1)
        relations.write_library("a b", {"a.md": ["b.md"]}, "gen")
        self.assertEqual(relations.read_library("a b", "gen"), {"a.md": ["b.md"]})


class TestManifestSegmentsAreRememberedNotReparsed(unittest.TestCase):
    """回收旧数据时只需要清单里“还在用哪几段”的三个字段，同一份没变的清单不再整份重读
    （2026-10-01 真机：每个库每轮收尾把所有库的全部清单各读一遍，Y2S1 一份 15MB）。
    清单一变（换了文件）必须读到新内容，删了就是没有——记错一段就会误删正在用的数据。"""

    def setUp(self) -> None:
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = IndexManifestStore(self.tmp / "manifests")

    def _manifest(self, generation: str, vector: list[str], extract: list[str], lexical: list[str]) -> dict:
        from core.index_generation import INDEX_MANIFEST_VERSION

        return {
            "format_version": INDEX_MANIFEST_VERSION,
            "library_id": "库 a",
            "generation": generation,
            "files": {"a.md": {"status": "indexed", "chunk_ids": ["c1"]}},
            "vector_segments": vector,
            "extract_segments": extract,
            "lexical_segments": lexical,
        }

    def test_segments_are_the_union_of_the_three_fields(self):
        self.assertTrue(self.store.write(self._manifest("g2", ["g1", "g2"], ["g0"], ["g2"])))
        self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g0", "g1", "g2"}))
        self.assertEqual(self.store.referenced_generations("库 a", ["g2"]), {"g0", "g1", "g2"})

    def test_unchanged_manifest_is_not_parsed_again(self):
        self.assertTrue(self.store.write(self._manifest("g2", ["g1"], [], [])))
        self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g1"}))
        with patch.object(IndexManifestStore, "read", side_effect=AssertionError("没变的清单不该再整份读")):
            self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g1"}))
            # 另一个 store 对象（下一个库的那一轮、另一个 Pipeline）同样认得
            self.assertEqual(IndexManifestStore(self.tmp / "manifests").segments("库 a", "g2"), frozenset({"g1"}))

    def test_a_rewritten_manifest_is_read_again(self):
        self.assertTrue(self.store.write(self._manifest("g2", ["g1"], [], [])))
        self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g1"}))
        # 同样长短的新内容：只靠“大小 + 修改时间”在同一时刻重写时会认错，换了文件就一定重读
        self.assertTrue(self.store.write(self._manifest("g2", ["g9"], [], [])))
        self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g9"}))

    def test_missing_cleared_or_unreadable_manifest_has_no_segments(self):
        self.assertIsNone(self.store.segments("库 a", "nope"))
        self.assertIsNone(self.store.segments("库 a", ""))
        self.assertTrue(self.store.write(self._manifest("g2", ["g1"], [], [])))
        self.assertEqual(self.store.segments("库 a", "g2"), frozenset({"g1"}))
        self.store.clear("库 a", "g2")
        self.assertIsNone(self.store.segments("库 a", "g2"))
        broken = self.store._path_for("库 a", "g3")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{半截", encoding="utf-8")
        self.assertIsNone(self.store.segments("库 a", "g3"))


# ---------------------------------------------------------------- 缺陷 6

class TestFreshnessSignatureCheck(_DataSafetyBase):
    """缺陷 6：`library_freshness` 不比较 manifest 签名，升级后不自动重建。

    对齐 LEGACY `kb_stale` 的 `version_upgrade` 自愈
    （obsidian-rag/index.py:1518-1519）：切块/嵌入/提取插件的
    `index_signature()` 变了，检索前的自动同步必须自己发现并重建。
    """

    def test_signature_upgrade_marks_freshness_stale_and_rebuilds(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        self.assertFalse(self.pipeline.library_freshness("test-lib")["test-lib"].stale)
        chunker = self.pipeline._singleton("chunker")
        self.assertTrue(callable(getattr(chunker, "index_signature", None)))
        with patch.object(chunker, "index_signature", return_value="chunking-v999"):
            self.assertTrue(self.pipeline.library_freshness("test-lib")["test-lib"].stale)
            self.assertEqual(self.pipeline.stale_libraries("test-lib"), ["test-lib"])
            with patch.object(self._embedder(), "embed_chunks", wraps=self._embedder().embed_chunks) as embed_mock:
                report = self.pipeline.index_library("test-lib", generation_id="second")
            # 收敛判定必须在**同一个签名下**做：新 manifest 记的签名已经跟上
            # 当前切块器，所以同一签名下不该再判 stale（早先这条断言被写在
            # patch 之外，等于在问"把切块器签名改回去之后算不算升级"——那是
            # 另一个问题，签名回退同样是升级，答案是必须重建）。
            self.assertEqual(report.changed, 2, report.files)
            self.assertEqual(self.pipeline._generations.active("test-lib"), "second")
            self.assertFalse(self.pipeline.library_freshness("test-lib")["test-lib"].stale)
        self.assertEqual(_embedded_chunk_count(embed_mock), 2, "签名升级后必须整库重切块重嵌")
        # 签名回退（换回旧切块器）同样是能力变化 → 又一次 stale（LEGACY
        # index.py:1518-1519 `version_upgrade` 对"任一签名变了"的判定是
        # 无方向的）：这条钉住漂移判定的两个方向，防止实现偷偷写成单边比较。
        self.assertTrue(self.pipeline.library_freshness("test-lib")["test-lib"].stale)
        self.assertEqual(self.pipeline.stale_libraries("test-lib"), ["test-lib"])
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))

    def test_unchanged_signature_stays_converged(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        chunker = self.pipeline._singleton("chunker")
        original = chunker.index_signature()
        with patch.object(chunker, "index_signature", return_value=original):
            self.assertFalse(self.pipeline.library_freshness("test-lib")["test-lib"].stale)


if __name__ == "__main__":
    unittest.main()
