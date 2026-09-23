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


if __name__ == "__main__":
    unittest.main()
