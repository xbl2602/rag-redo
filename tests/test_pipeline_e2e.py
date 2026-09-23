"""端到端集成测试：真实通过 PluginRuntime 扫描 plugins/ 目录，加载/启用
全部 Phase 1 官方插件，索引一个临时小库，搜索验证结果——这是证明"插件化
架构本身能交付真实检索能力"的关键测试，不是把各插件的单元测试简单拼起来
就算数。

embedder/reranker 注入确定性假实现（避免下载真实模型，理由同
plugins/official-embedder-bge-m3/tests/test_embed.py），其余（extractor/
chunker/library-manager/bm25/chroma/rrf）全部走真实代码，不打折扣。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
for plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if plugin_dir.is_dir():
        sys.path.insert(0, str(plugin_dir))

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime, PluginState  # noqa: E402

OFFICIAL_PHASE1_PLUGINS = [
    "official-extractor-text",
    "official-extractor-pdf-text",
    "official-extractor-docx",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
]


class _DeterministicFakeEncoder:
    """给 embedder 用的假编码器：把关键词出现次数映射进固定维度，保证
    "包含相同关键词的文本"在向量空间里更接近——端到端测试才能验证"真的
    搜到了语义相关的内容"，不是随机分数凑巧排对。"""

    KEYWORDS = ["插件", "架构", "厨房", "食谱"]

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(text.count(k)) for k in self.KEYWORDS] for text in texts]


class _DeterministicFakeReranker:
    def score(self, query: str, texts: list[str]) -> list[float]:
        # query.split() 按空白切词，不要写成 [t for t in query]——那是逐
        # 字符遍历，在内容更长/更真实的语料里容易被无关字符噪声干扰
        # （demo-vault 那份测试真实踩到过，见 tests/test_demo_vault.py）。
        query_terms = query.split()
        return [sum(text.count(term) for term in query_terms) for text in texts]


class TestEndToEndSearchPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n这篇笔记讲插件系统的架构设计，核心只有两个组件。",
            encoding="utf-8",
        )
        (self.vault / "cooking.md").write_text(
            "# 厨房笔记\n\n这篇笔记记录了几个食谱，包括家常菜做法。",
            encoding="utf-8",
        )

        self.data_dir = self.tmp / "data"
        self.runtime, self.pipeline = self._build_runtime()
        self.lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.lib_mgr.store.add_library("test-lib", "测试库", str(self.vault))

    def _build_runtime(self) -> tuple[PluginRuntime, Pipeline]:
        """启动一套完整的插件运行时+Pipeline，指向 self.data_dir。独立成
        方法是为了能在同一个 data_dir 上模拟"进程重启"——新建一个运行时
        实例、重新扫描/加载/启用，验证数据是不是真的从磁盘活过来了，而
        不是只活在第一个运行时实例的内存里（见
        test_bm25_search_survives_process_restart）。"""
        runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.data_dir,
        )
        runtime.scan()
        for plugin_id in OFFICIAL_PHASE1_PLUGINS:
            self.assertIn(plugin_id, runtime.plugins, f"{plugin_id} 应该被发现")
            # scan() 会自动恢复"重启前就是启用状态"的插件（Phase 0 验证过
            # 的行为：核心重启后插件自己回到之前的状态，不需要用户每次都
            # 手动重新启用）。模拟重启时这里会正好命中这条路径——插件在
            # scan() 这一步就已经是 ENABLED 了，不需要（也不能）再走一遍
            # load()，那样什么都不做（load 只处理 DISCOVERED 状态）。
            if runtime.plugins[plugin_id].state != PluginState.ENABLED:
                runtime.load(plugin_id)
                self.assertEqual(
                    runtime.plugins[plugin_id].state,
                    PluginState.LOADED,
                    f"{plugin_id} 加载失败: {runtime.plugins[plugin_id].error}",
                )
                runtime.enable(plugin_id)
            self.assertEqual(
                runtime.plugins[plugin_id].state,
                PluginState.ENABLED,
                f"{plugin_id} 启用失败: {runtime.plugins[plugin_id].error}",
            )

        # 注入假 encoder/reranker，避免端到端测试下载真实模型。
        from official_embedder_bge_m3.embed import BGEM3Embedder

        embedder_instance = runtime.plugins["official-embedder-bge-m3"].instance
        embedder_instance.embedder = BGEM3Embedder(encoder=_DeterministicFakeEncoder())

        from official_reranker.rerank import RerankerEngine

        reranker_instance = runtime.plugins["official-reranker"].instance
        reranker_instance.engine = RerankerEngine(reranker=_DeterministicFakeReranker())

        return runtime, Pipeline(runtime)

    def test_data_dir_is_isolated_tmp_dir_not_hardcoded_relative_path(self):
        """插件的数据落在测试传入的隔离 data_dir 下，而不是插件自己硬编码
        的相对路径——这是本次重写补的 ctx.data_dir 机制要解决的问题（见
        core/context.py），顺手验证一下真的接对了，没有走回头路。"""
        self.assertTrue((self.data_dir / "libraries.json").exists())
        self.assertTrue((self.data_dir / "chroma").exists())
        self.assertFalse((REPO_ROOT / "data" / "libraries.json").exists())

    def test_index_then_search_finds_relevant_doc(self):
        report = self.pipeline.index_library("test-lib")
        self.assertEqual(report.succeeded, 2, f"应该两个文件都索引成功: {report.files}")
        self.assertEqual(report.failed, 0)

        results = self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].path, "plugin-notes.md")
        self.assertIn("插件", results[0].text)

    def test_search_different_query_finds_different_doc(self):
        self.pipeline.index_library("test-lib")
        results = self.pipeline.search("test-lib", "食谱 厨房", top_k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].path, "cooking.md")

    def test_excluded_file_never_appears_in_any_search_result(self):
        excluded_dir = self.vault / "excluded"
        excluded_dir.mkdir()
        # 故意把假 encoder 认识的全部关键词都塞进这个文件——如果排除逻辑
        # 有漏洞让它被意外索引，它会是几乎任何查询的最强命中，测试会立刻
        # 抓到。
        (excluded_dir / "secret.md").write_text(
            "# 秘密\n\n插件 插件 架构 架构 厨房 厨房 食谱 食谱 全部关键词各来两遍。",
            encoding="utf-8",
        )
        self.lib_mgr.store.set_selection("test-lib", selection_out=["excluded"])

        report = self.pipeline.index_library("test-lib")
        excluded_entries = [f for f in report.files if f.path.startswith("excluded/")]
        self.assertTrue(excluded_entries)
        self.assertFalse(excluded_entries[0].included)

        for query in ("插件 架构", "厨房 食谱", "秘密"):
            results = self.pipeline.search("test-lib", query, top_k=10)
            paths = [r.path for r in results]
            self.assertNotIn("excluded/secret.md", paths, f"查询 {query!r} 不该命中被排除的文件")

    def test_confidence_normalized_between_0_and_1(self):
        self.pipeline.index_library("test-lib")
        results = self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertTrue(results)
        for r in results:
            self.assertGreaterEqual(r.confidence, 0.0)
            self.assertLessEqual(r.confidence, 1.0)

    def test_bm25_search_survives_process_restart(self):
        """真实缺口回归测试：早期实现里 BM25 词法索引完全只活在内存里，
        Chroma 向量数据落盘了但 BM25 没有——进程一重启，词法这一路会悄悄
        变空，检索质量在用户不知情的情况下退化（不报错，只是排名/召回
        变差），比直接崩溃更难发现。用 self._build_runtime() 新建一个
        运行时实例模拟"重启"，只走 search，不重新 index_library，如果
        BM25 索引真的从磁盘活过来了，检索质量应该和重启前一样。"""
        self.pipeline.index_library("test-lib")
        before = self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertTrue(before)

        _restarted_runtime, restarted_pipeline = self._build_runtime()
        after = restarted_pipeline.search("test-lib", "插件 架构", top_k=5)

        self.assertTrue(after, "重启后应该还能搜到结果，不该因为BM25索引丢失而变空")
        self.assertEqual([r.path for r in before], [r.path for r in after])

    def test_lexical_search_is_isolated_per_library(self):
        """回归测试：official-lexical-bm25 早期实现只有一个全局 BM25Index，
        没有按库分开，会导致"搜库B却搜到库A内容"的跨库数据泄漏——具体
        经过见 plugins/official-lexical-bm25/official_lexical_bm25/plugin.py
        模块 docstring。这里用两个库、内容互不重叠，确认搜库B绝对搜不到
        库A的东西（哪怕BM25那一路单独命中了也不该泄漏进最终结果）。"""
        vault2 = self.tmp / "vault2"
        vault2.mkdir()
        (vault2 / "other.md").write_text(
            "# 完全不相关的内容\n\n插件 架构 这两个词特意也塞进来，试图从词法层面泄漏。",
            encoding="utf-8",
        )
        self.lib_mgr.store.add_library("other-lib", "另一个库", str(vault2))

        self.pipeline.index_library("test-lib")
        self.pipeline.index_library("other-lib")

        results = self.pipeline.search("test-lib", "插件 架构", top_k=10)
        paths = [r.path for r in results]
        self.assertNotIn("other.md", paths)
        self.assertIn("plugin-notes.md", paths)

    def test_multi_library_search_pools_results_across_libraries(self):
        """真正的多库并查——不是"每库各搜一遍简单拼接"，而是每库先融合、
        候选池跨库合并、重排器统一精排给出全局排序（见
        core/pipeline.py::search 的说明），对齐 obsidian-rag/retriever.py
        ::hybrid_search 的"libraries='A,B' 多库并查"语义。用两个内容不
        重叠的库、查一个只在其中一个库里出现的词，确认结果真的来自两个
        不同的 library_id（而不是只搜了第一个库）。"""
        vault2 = self.tmp / "vault2"
        vault2.mkdir()
        (vault2 / "other.md").write_text(
            "# 另一个库的笔记\n\n这篇也讲插件 架构，但是是完全不同的一篇。",
            encoding="utf-8",
        )
        self.lib_mgr.store.add_library("other-lib", "另一个库", str(vault2))
        self.pipeline.index_library("test-lib")
        self.pipeline.index_library("other-lib")

        results = self.pipeline.search("test-lib,other-lib", "插件 架构", top_k=10)
        library_ids = {r.library_id for r in results}
        self.assertEqual(library_ids, {"test-lib", "other-lib"})
        paths = {r.path for r in results}
        self.assertIn("plugin-notes.md", paths)
        self.assertIn("other.md", paths)

    def test_multi_library_search_empty_libraries_defaults_to_all(self):
        """libraries 留空="全部已注册库"——对齐 obsidian-rag 在没有配置
        default_libraries 时的最终回退行为（rag-redo 目前没有通用配置
        存储，直接以全部库为默认，见 resolve_libraries 的说明）。"""
        vault2 = self.tmp / "vault2"
        vault2.mkdir()
        (vault2 / "other.md").write_text("# 另一个库\n\n插件 架构 也出现在这里。", encoding="utf-8")
        self.lib_mgr.store.add_library("other-lib", "另一个库", str(vault2))
        self.pipeline.index_library("test-lib")
        self.pipeline.index_library("other-lib")

        results = self.pipeline.search("", "插件 架构", top_k=10)
        self.assertEqual({r.library_id for r in results}, {"test-lib", "other-lib"})

        all_results = self.pipeline.search("all", "插件 架构", top_k=10)
        self.assertEqual({r.library_id for r in all_results}, {"test-lib", "other-lib"})

    def test_multi_library_search_exclude_removes_library_from_pool(self):
        vault2 = self.tmp / "vault2"
        vault2.mkdir()
        (vault2 / "other.md").write_text("# 另一个库\n\n插件 架构 也出现在这里。", encoding="utf-8")
        self.lib_mgr.store.add_library("other-lib", "另一个库", str(vault2))
        self.pipeline.index_library("test-lib")
        self.pipeline.index_library("other-lib")

        results = self.pipeline.search("all", "插件 架构", top_k=10, exclude="other-lib")
        self.assertEqual({r.library_id for r in results}, {"test-lib"})

    def test_search_unknown_library_name_raises_value_error_listing_available(self):
        with self.assertRaises(ValueError) as ctx:
            self.pipeline.search("no-such-lib", "随便什么查询")
        self.assertIn("test-lib", str(ctx.exception))

    def test_search_exclude_everything_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.pipeline.search("test-lib", "随便什么查询", exclude="test-lib")

    def test_search_folder_filter_scopes_to_subdirectory(self):
        """folder 按库内子目录过滤——须是完整目录名，"docs" 匹配
        "docs/x.md" 不匹配 "docs2/x.md"，对齐 obsidian-rag/retriever.py
        ::_in_folder 的前缀+边界规则。"""
        (self.vault / "docs").mkdir()
        (self.vault / "docs" / "inside.md").write_text(
            "# docs内的笔记\n\n插件 架构 相关内容。", encoding="utf-8"
        )
        (self.vault / "docs2").mkdir()
        (self.vault / "docs2" / "outside.md").write_text(
            "# docs2的笔记（不该被docs前缀误匹配）\n\n插件 架构 相关内容。", encoding="utf-8"
        )
        self.pipeline.index_library("test-lib")

        results = self.pipeline.search("test-lib", "插件 架构", top_k=10, folder="docs")
        paths = {r.path for r in results}
        self.assertIn("docs/inside.md", paths)
        self.assertNotIn("docs2/outside.md", paths)

    def test_reindex_after_disabling_gui_style_optional_plugin_still_works(self):
        """插件之间真的没有硬编码依赖——即使不装/不启用任何 gui_panel 类
        插件（Phase 1 目前还没有这类插件），核心检索链路完全不受影响，
        对应 docs/ROADMAP.md Phase 1 验收标准"关掉任意一个非必需插件，
        核心+MCP 仍能正常工作"。scan() 会发现 plugins/ 目录下的全部插件
        （包括 official-gui-shell），但这里的 REQUIRED_PLUGINS 列表从不
        加载/启用它——只检查"没被启用"，不是"没被发现"，这两件事不一样。"""
        gui_plugin = self.runtime.plugins.get("official-gui-shell")
        self.assertIsNotNone(gui_plugin, "gui-shell应该能被scan()发现")
        self.assertNotEqual(gui_plugin.state.value, "enabled")
        report = self.pipeline.index_library("test-lib")
        self.assertEqual(report.succeeded, 2)


class TestExportImportLibrary(TestEndToEndSearchPipeline):
    """导出/导入是"把已建索引的库搬到另一台机器，不用重新跑一遍索引"的
    能力——见 core/pipeline.py 的 export_library/import_library 模块内
    注释。复用 TestEndToEndSearchPipeline 的 setUp（已经装好一个索引好
    的 test-lib），不重新拼一遍 fixture。"""

    def test_export_returns_real_zip_with_vectors_and_bm25(self):
        import zipfile
        from io import BytesIO

        self.pipeline.index_library("test-lib")
        data = self.pipeline.export_library("test-lib")
        zf = zipfile.ZipFile(BytesIO(data))
        self.assertEqual(set(zf.namelist()), {"manifest.json", "vectors.json", "bm25.json"})

    def test_export_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.pipeline.export_library("no-such-library")

    def test_import_creates_library_with_same_search_results(self):
        """核心承诺：导入之后不重新索引，搜索质量应该和导出前完全一样——
        这就是这个功能存在的理由，不是随便验证"能跑不报错"。"""
        self.pipeline.index_library("test-lib")
        before = self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertTrue(before)

        archive = self.pipeline.export_library("test-lib")
        new_id = self.pipeline.import_library(archive, root_path="/new/machine/vault", library_id="test-lib-restored")
        self.assertEqual(new_id, "test-lib-restored")

        after = self.pipeline.search("test-lib-restored", "插件 架构", top_k=5)
        self.assertEqual([r.path for r in before], [r.path for r in after])
        self.assertEqual([r.text for r in before], [r.text for r in after])

    def test_import_without_explicit_library_id_reuses_original(self):
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")
        self.lib_mgr.store.remove_library("test-lib")

        new_id = self.pipeline.import_library(archive, root_path="/new/machine/vault")
        self.assertEqual(new_id, "test-lib")
        self.assertIsNotNone(self.lib_mgr.store.get("test-lib"))

    def test_import_carries_over_selection_and_policy(self):
        self.lib_mgr.store.set_selection("test-lib", selection_out=["cooking.md"])
        self.lib_mgr.store.set_policy("test-lib", new_file_default="exclude", enabled_extensions=[".md"])
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")

        self.pipeline.import_library(archive, root_path="/new/machine/vault", library_id="test-lib-2")
        cfg = self.lib_mgr.store.get("test-lib-2")
        self.assertEqual(cfg.selection_out, ["cooking.md"])
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".md"])
        self.assertEqual(cfg.root_path, "/new/machine/vault")

    def test_import_rejects_existing_library_id(self):
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")
        with self.assertRaises(ValueError):
            self.pipeline.import_library(archive, root_path="/new/machine/vault", library_id="test-lib")

    def test_import_survives_process_restart(self):
        """导入进去的数据也得真的落盘，不是只活在导入那一次的内存里——
        同 test_bm25_search_survives_process_restart 的精神，这里额外
        确认"导入"这条路径本身也遵守同一条纪律，不是导出/导入两条路径
        里只有一条测过持久化。"""
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")
        self.pipeline.import_library(archive, root_path="/new/machine/vault", library_id="test-lib-restored")
        before = self.pipeline.search("test-lib-restored", "插件 架构", top_k=5)

        _restarted_runtime, restarted_pipeline = self._build_runtime()
        after = restarted_pipeline.search("test-lib-restored", "插件 架构", top_k=5)
        self.assertTrue(after)
        self.assertEqual([r.path for r in before], [r.path for r in after])


class TestOcrChainTryFallback(unittest.TestCase):
    """证明 Phase 2 的 OCR chain-try 设计真的接进了主管道，不是插件单测
    自证自话：一份没有文字层的"扫描版"PDF，先被 official-extractor-pdf-text
    诚实地判定"scanned:no-text-layer"放弃，再被 official-ocr-mineru-cloud
    尝试（真实客户端，没配 MINERU_API_KEY，真的会失败），最后被
    official-ocr-mineru-local 接住（子进程+RAG_REDO_FAKE_OCR=1 注入的
    确定性假结果）——三层链式尝试全部走真实代码，只有"识别出的文字内容"
    是假的（因为沙盒环境刻意不下载真实OCR模型，见
    official_ocr_mineru_local/plugin.py 模块docstring），链路本身、
    core/pipeline.py 的 provider 链式尝试逻辑、chunker/BM25/Chroma/
    Pipeline.search 全部是真代码。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._fake_ocr_env_backup = os.environ.get("RAG_REDO_FAKE_OCR")
        os.environ["RAG_REDO_FAKE_OCR"] = "1"
        self._api_key_backup = os.environ.pop("MINERU_API_KEY", None)
        self.addCleanup(self._restore_env)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        import pymupdf

        doc = pymupdf.open()
        doc.new_page()  # 完全空白，没有文字层——逼 extractor-pdf-text 认输
        doc.save(str(self.vault / "scanned-contract.pdf"))
        doc.close()

        self.data_dir = self.tmp / "data"
        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.data_dir,
        )
        self.runtime.scan()
        plugin_ids = OFFICIAL_PHASE1_PLUGINS + ["official-ocr-mineru-cloud", "official-ocr-mineru-local"]
        for plugin_id in plugin_ids:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)
            state = self.runtime.plugins[plugin_id]
            self.assertEqual(state.state, PluginState.ENABLED, f"{plugin_id}: {state.error}")
        self.addCleanup(lambda: self.runtime.disable("official-ocr-mineru-local"))

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_DeterministicFakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_DeterministicFakeReranker())

        self.pipeline = Pipeline(self.runtime)
        self.lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.lib_mgr.store.add_library("scan-lib", "扫描件库", str(self.vault))
        # library-manager 的默认启用格式是 [.md, .txt]（见
        # official_library_manager/config.py），不包含 .pdf——PDF 检索
        # 场景要显式打开，这是库层面的选择，不是extractor/OCR这一侧该
        # 关心的事。
        self.lib_mgr.store.set_policy("scan-lib", enabled_extensions=[".md", ".txt", ".pdf"])

    def _restore_env(self) -> None:
        if self._fake_ocr_env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._fake_ocr_env_backup
        if self._api_key_backup is not None:
            os.environ["MINERU_API_KEY"] = self._api_key_backup

    def test_scanned_pdf_falls_through_to_local_ocr_and_gets_indexed(self):
        report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.succeeded, 1, f"扫描版PDF应该经OCR链式尝试后索引成功: {report.files}")
        self.assertEqual(report.failed, 0)
        self.assertGreater(report.files[0].chunk_count, 0)

    def test_ocr_recovered_content_is_actually_searchable(self):
        self.pipeline.index_library("scan-lib")
        results = self.pipeline.search("scan-lib", "fake-ocr scanned-contract", top_k=5)
        self.assertTrue(results, "OCR恢复出的内容应该能被搜到，不是索引了但实际检索不到的死数据")
        self.assertIn("fake-ocr", results[0].text)


class _FakeLlmHttpClient:
    """给 official-llm-openai-compatible 用的假客户端——不碰真实网络，
    验证的是"pipeline 真的把采样片段拼进了prompt、真的按provider链式
    尝试调用、真的把结果传回给库摘要插件写盘"这条编排逻辑本身，不是
    "LLM 写得好不好"。"""

    def __init__(self, response: str = "这是一个关于插件架构和厨房食谱的个人知识库，适合查询系统设计与家常菜做法。") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user})
        return self.response


class TestLibrarySummaryPipeline(unittest.TestCase):
    """证明 Phase 3 的库摘要设计真的接进了主管道，不是插件单测自证自话：
    vector_store.sample() 真的从索引好的库里采样、llm_provider 链式尝试
    真的被调用、official-library-summary 的写权限门禁真的挡住了对用户
    手写简介的覆盖——采样/chunker/BM25/Chroma/Pipeline.search 全部是真
    代码，只有"调用真实云端/本地LLM"这一步注入假客户端（同
    official-embedder-bge-m3 假编码器的理由：不为了验证编排逻辑对不对
    就强绑一次真实网络调用）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n这篇笔记讲插件系统的架构设计，核心只有两个组件。",
            encoding="utf-8",
        )
        (self.vault / "cooking.md").write_text(
            "# 厨房笔记\n\n这篇笔记记录了几个食谱，包括家常菜做法。",
            encoding="utf-8",
        )

        self.data_dir = self.tmp / "data"
        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.data_dir,
        )
        self.runtime.scan()
        plugin_ids = OFFICIAL_PHASE1_PLUGINS + ["official-library-summary", "official-llm-openai-compatible"]
        for plugin_id in plugin_ids:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)
            state = self.runtime.plugins[plugin_id]
            self.assertEqual(state.state, PluginState.ENABLED, f"{plugin_id}: {state.error}")

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(encoder=_DeterministicFakeEncoder())
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_DeterministicFakeReranker())

        from official_llm_openai_compatible.llm import OpenAiCompatibleClient

        self.fake_llm = _FakeLlmHttpClient()
        self.runtime.plugins["official-llm-openai-compatible"].instance._client = OpenAiCompatibleClient(  # noqa: SLF001
            http_client=self.fake_llm
        )

        self.pipeline = Pipeline(self.runtime)
        self.lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.lib_mgr.store.add_library("test-lib", "测试库", str(self.vault))
        self.pipeline.index_library("test-lib")

    def test_sample_library_returns_representative_chunks_from_real_index(self):
        samples = self.pipeline.sample_library("test-lib", k=5)
        self.assertTrue(samples)
        paths = {s.path for s in samples}
        self.assertEqual(paths, {"plugin-notes.md", "cooking.md"})

    def test_sample_library_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.pipeline.sample_library("no-such-lib")

    def test_generate_library_summary_calls_llm_provider_chain_and_returns_text(self):
        text, provider_id = self.pipeline.generate_library_summary("test-lib")
        self.assertEqual(provider_id, "official-llm-openai-compatible")
        self.assertEqual(text, self.fake_llm.response)
        self.assertEqual(len(self.fake_llm.calls), 1)
        # prompt 里应该真的带上了采样到的文件名，不是空壳调用
        self.assertTrue(any(name in self.fake_llm.calls[0]["user"] for name in ("plugin-notes.md", "cooking.md")))

    def test_generate_library_summary_on_unindexed_library_raises_clear_error(self):
        self.lib_mgr.store.add_library("empty-lib", "空库", str(self.tmp / "empty"))
        (self.tmp / "empty").mkdir()
        with self.assertRaises(Exception):
            self.pipeline.generate_library_summary("empty-lib")

    def test_propose_then_apply_full_roundtrip_via_pipeline(self):
        result = self.pipeline.propose_library_summary("test-lib", "一段AI生成的简介")
        self.assertTrue(result["applied"])
        summary = self.pipeline.get_library_summary("test-lib")
        self.assertEqual(summary.text, "一段AI生成的简介")
        self.assertEqual(summary.source, "ai")

    def test_propose_over_user_summary_requires_gate_confirmation(self):
        """完整端到端验证写权限门禁真的挡住了 AI 覆盖用户手写内容——
        这是 core/write_gate.py 第一次被真实插件+真实Pipeline调用链路
        验证过，不只是插件自己的单元测试。"""
        summary_plugin = self.runtime.plugins["official-library-summary"].instance
        summary_plugin._store.set("test-lib", "用户手写的简介", source="user")  # noqa: SLF001

        propose_result = self.pipeline.propose_library_summary("test-lib", "AI想覆盖的新简介")
        self.assertFalse(propose_result["applied"])
        self.assertEqual(self.pipeline.get_library_summary("test-lib").text, "用户手写的简介")

        apply_result = self.pipeline.apply_library_summary(
            "test-lib", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertTrue(apply_result["ok"])
        self.assertEqual(self.pipeline.get_library_summary("test-lib").text, "AI想覆盖的新简介")


if __name__ == "__main__":
    unittest.main()
