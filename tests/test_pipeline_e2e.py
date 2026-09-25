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
from unittest.mock import patch

from docx import Document

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
for plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if plugin_dir.is_dir():
        sys.path.insert(0, str(plugin_dir))

from core.contracts import ExtractedDocument, SearchResult
from core.index_progress import IndexStartResult
from core.pipeline import Pipeline, confidence_tier  # noqa: E402
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
    "official-query-enhancer-hyde",
    "official-result-advisor",
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


class _FakeHydeClient:
    def __init__(self, response: str = "假设文档正文") -> None:
        self.response = response
        self.calls: list[dict] = []

    def complete(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.response


class _FailingHydeClient:
    def complete(self, prompt: str, **kwargs):
        raise RuntimeError("LLM unavailable")


class _PathRankedReranker:
    def rerank(self, query, chunk_id_text_pairs, top_k=10):
        long_chunks = [pair for pair in chunk_id_text_pairs if ":long.md:" in pair[0]]
        other_chunks = [pair for pair in chunk_id_text_pairs if ":long.md:" not in pair[0]]
        ranked = [(pair[0], 0.99 - index * 0.01) for index, pair in enumerate(long_chunks)]
        ranked.extend((pair[0], 0.60 - index * 0.01) for index, pair in enumerate(other_chunks))
        return sorted(ranked, key=lambda item: item[1], reverse=True)[:top_k]


class TestConfidenceTier(unittest.TestCase):
    """纯函数，不需要真实索引/检索基础设施——对齐 obsidian-rag
    retriever.py::_conf_tier 的分档线（真分尺度，高相关线固定0.75）。"""

    def test_at_or_above_strong_threshold_is_high(self):
        self.assertEqual(confidence_tier(0.75, warn_threshold=0.30), "高相关")
        self.assertEqual(confidence_tier(0.98, warn_threshold=0.30), "高相关")

    def test_between_warn_and_strong_is_medium(self):
        self.assertEqual(confidence_tier(0.50, warn_threshold=0.30), "中相关")

    def test_below_warn_threshold_is_weak(self):
        self.assertEqual(confidence_tier(0.10, warn_threshold=0.30), "弱相关")

    def test_at_warn_threshold_is_medium_not_weak(self):
        self.assertEqual(confidence_tier(0.30, warn_threshold=0.30), "中相关")

    def test_different_warn_threshold_shifts_boundary(self):
        self.assertEqual(confidence_tier(0.40, warn_threshold=0.50), "弱相关")


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
        self.addCleanup(self.runtime.close)
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

    def test_agent_format_allowlist_freezes_unapproved_docx_until_authorized(self):
        document = Document()
        document.add_paragraph("Agent binary authorization test content")
        document.save(str(self.vault / "agent.docx"))
        self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt"))
        generation = self.pipeline._generations.active("test-lib")
        manifest = self.pipeline._manifests.read("test-lib", generation)
        assert manifest is not None
        self.assertNotIn("agent.docx", manifest["files"])
        self.pipeline.index_library(
            "test-lib",
            full=True,
            format_allowlist=(".md", ".txt", ".docx"),
        )
        generation = self.pipeline._generations.active("test-lib")
        manifest = self.pipeline._manifests.read("test-lib", generation)
        assert manifest is not None
        self.assertEqual(manifest["files"]["agent.docx"]["extractor_id"], "official-extractor-docx")
        blocked = self.pipeline.search(
            "test-lib",
            "Agent binary authorization test content",
            format_allowlist=(".md", ".txt"),
        )
        self.assertTrue(all(result.path != "agent.docx" for result in blocked))
        allowed = self.pipeline.search(
            "test-lib",
            "Agent binary authorization test content",
            format_allowlist=(".md", ".txt", ".docx"),
        )
        self.assertTrue(any(result.path == "agent.docx" for result in allowed))

    def test_stale_libraries_detects_first_run_changes_additions_and_removals(self):
        allowed = {"test-lib": (".md", ".txt")}
        self.assertEqual(self.pipeline.stale_libraries("all", format_allowlist=allowed), ["test-lib"])
        self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt"))
        self.assertEqual(self.pipeline.stale_libraries("all", format_allowlist=allowed), [])
        (self.vault / "new.md").write_text("# 新文件\n\n新增内容", encoding="utf-8")
        self.assertEqual(self.pipeline.stale_libraries("all", format_allowlist=allowed), ["test-lib"])
        self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt"))
        (self.vault / "new.md").unlink()
        self.assertEqual(self.pipeline.stale_libraries("all", format_allowlist=allowed), ["test-lib"])

    def test_missing_root_reports_missing_and_keeps_old_index_searchable(self):
        """对齐 obsidian-rag/index.py::kb_stale 的 missing 标志（1506-1508）与
        server.py::ensure_fresh 的跳过同步（264-266）：库路径消失（临时挂载
        失败的典型形态）必须报 missing——"本轮看不到"不等于"确认删除"，
        旧索引清空是不可逆代价。旧块继续可检索，代数不变。"""
        self.pipeline.index_library("test-lib")
        generation_before = self.pipeline._generations.active("test-lib")
        moved = self.vault.parent / "vault-detached"
        self.vault.rename(moved)
        try:
            freshness = self.pipeline.library_freshness("test-lib")
            self.assertTrue(freshness["test-lib"].stale)
            self.assertTrue(freshness["test-lib"].missing)
            self.assertFalse(freshness["test-lib"].emptied)
            self.assertEqual(self.pipeline.stale_libraries("test-lib"), ["test-lib"])
            self.assertTrue(self.pipeline.search("test-lib", "插件 架构"))
        finally:
            moved.rename(self.vault)
        self.assertEqual(self.pipeline._generations.active("test-lib"), generation_before)

    def test_emptied_dir_reports_emptied_and_keeps_old_index_searchable(self):
        """对齐 obsidian-rag 2026-08-14 审计 F16（index.py:1511-1517）："目录
        还在但一个文件都扫不到"几乎总是"源文件没放回去"而不是"用户真的删
        光"——报 emptied，自动同步必须跳过以免清空索引，旧结果继续可检索。"""
        self.pipeline.index_library("test-lib")
        for md in self.vault.glob("*.md"):
            md.unlink()
        freshness = self.pipeline.library_freshness("test-lib")
        self.assertTrue(freshness["test-lib"].stale)
        self.assertTrue(freshness["test-lib"].emptied)
        self.assertFalse(freshness["test-lib"].missing)
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构"))

    def test_never_indexed_empty_library_is_converged_not_stale(self):
        """对齐 obsidian-rag/index.py:1522-1529：从未索引过且扫不到任何文件 =
        收敛态，不得每轮误报 stale 触发无效重建。"""
        empty_vault = self.tmp / "empty-vault"
        empty_vault.mkdir()
        self.lib_mgr.store.add_library("empty-lib", "空库", str(empty_vault))
        freshness = self.pipeline.library_freshness("empty-lib")
        self.assertFalse(freshness["empty-lib"].stale)
        self.assertFalse(freshness["empty-lib"].missing)
        self.assertFalse(freshness["empty-lib"].emptied)
        self.assertEqual(self.pipeline.stale_libraries("empty-lib"), [])

    def test_deferred_keeps_previous_record_and_old_chunks_searchable(self):
        """对齐 obsidian-rag/index.py:2142-2148（R3b）：本地服务瞬态不可用 →
        本轮跳过——不落终态、不动 manifest 记录、不计 changed；旧条目与旧块
        原样保留继续服务（检索不受影响），文件 stat 与旧记录的差异驱动下一轮
        stale → 自动重试；服务恢复后重试成功、新内容可检索。"""
        self.pipeline.index_library("test-lib")
        old_record = self.pipeline._manifests.read(
            "test-lib", self.pipeline._generations.active("test-lib")
        )["files"]["plugin-notes.md"]
        self.assertTrue(old_record["chunk_ids"])
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n重写后的正文，内容完全不同。", encoding="utf-8"
        )
        text_extractor = self.runtime.plugins["official-extractor-text"].instance

        def deferred_extract(library_id, path, root):
            return ExtractedDocument(
                library_id=library_id,
                path=path,
                text=None,
                failure_reason="deferred",
                extracted_by="official-extractor-text",
                extractor_version="test",
                content_hash="unused",
                failure_state="deferred",
            )

        with patch.object(text_extractor, "extract", side_effect=deferred_extract):
            report = self.pipeline.index_library("test-lib")
        self.assertEqual(report.deferred, 1)
        manifest = self.pipeline._manifests.read(
            "test-lib", self.pipeline._generations.active("test-lib")
        )
        record = manifest["files"]["plugin-notes.md"]
        self.assertEqual(record["chunk_ids"], old_record["chunk_ids"])
        self.assertEqual(record["status"], "indexed")
        self.assertEqual(record["mtime_ns"], old_record["mtime_ns"])
        self.assertTrue(self.pipeline.stale_libraries("test-lib"))
        results = self.pipeline.search("test-lib", "插件 架构")
        self.assertTrue(results)
        self.assertEqual(results[0].path, "plugin-notes.md")
        report2 = self.pipeline.index_library("test-lib")
        self.assertEqual(report2.changed, 1)
        self.assertEqual(report2.succeeded, 2)
        results2 = self.pipeline.search("test-lib", "插件 架构")
        top = next(r for r in results2 if r.path == "plugin-notes.md")
        self.assertIn("重写后的正文", top.text)

    def test_agent_format_revocation_freezes_files_and_keeps_old_chunks(self):
        """对齐 obsidian-rag/index.py:2058-2066：Agent 未授权格式 = 冻结——
        保留既有条目与块（不裁剪不清理），零 I/O、不计变更；撤销授权不得把
        未授权文件当"已删除"清掉旧索引，重新授权后原块立即可用，无需重新提取。"""
        document = Document()
        document.add_paragraph("Revocation freeze retention test body")
        document.save(str(self.vault / "agent.docx"))
        self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt", ".docx"))
        indexed_record = self.pipeline._manifests.read(
            "test-lib", self.pipeline._generations.active("test-lib")
        )["files"]["agent.docx"]
        self.assertEqual(indexed_record["status"], "indexed")

        report = self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt"))
        self.assertEqual(report.removed, 0)
        frozen_record = self.pipeline._manifests.read(
            "test-lib", self.pipeline._generations.active("test-lib")
        )["files"]["agent.docx"]
        self.assertEqual(frozen_record["chunk_ids"], indexed_record["chunk_ids"])
        self.assertEqual(frozen_record["status"], "indexed")
        results = self.pipeline.search("test-lib", "Revocation freeze retention")
        self.assertTrue(any(r.path == "agent.docx" for r in results))

        report2 = self.pipeline.index_library("test-lib", format_allowlist=(".md", ".txt", ".docx"))
        self.assertEqual(report2.unchanged, 3, report2.files)
        self.assertTrue(
            self.pipeline.search("test-lib", "Revocation freeze retention")
        )

    def test_agent_format_revocation_freezes_pdf_visual_pages(self):
        """对齐 obsidian-rag/wemm_indexer.py:190（"仅处理这些格式的 PDF，其余
        冻结"）：被撤销授权的 PDF 保留在有效页集合里（不重渲染、不屏蔽），
        重新授权后页级导航立即可用。"""
        import pymupdf

        pdf_doc = pymupdf.open()
        page = pdf_doc.new_page()
        page.insert_text((72, 72), "visual freeze pdf body")
        pdf_doc.save(str(self.vault / "visual.pdf"))
        pdf_doc.close()
        visual_calls: list[tuple[list[str], list[str]]] = []

        class _RecordingVisual:
            def index_library(self, library_id, root, pdf_paths, *, generation,
                              changed_paths, previous_generation):
                visual_calls.append((list(pdf_paths), list(changed_paths)))

        for allowlist in ((".md", ".txt", ".pdf"), (".md", ".txt")):
            real_plugin = self.pipeline._plugin
            real_providers_of = self.runtime.registry.providers_of

            def _fake_providers(point, _real=real_providers_of):
                # active_of 内部也会调 providers_of——只劫持 visual_index，
                # 其他扩展点必须委托真实实现，否则 _singleton 全部拿到 fake
                if point == "visual_index":
                    return ["fake-visual"]
                return _real(point)

            def _fake_plugin(plugin_id, _recorder=_RecordingVisual(), _real=real_plugin):
                if plugin_id == "fake-visual":
                    return _recorder
                return _real(plugin_id)

            with (
                patch.object(self.runtime.registry, "providers_of", side_effect=_fake_providers),
                patch.object(self.pipeline, "_plugin", side_effect=_fake_plugin),
            ):
                self.pipeline.index_library("test-lib", format_allowlist=allowlist)
        self.assertEqual(visual_calls[0], (["visual.pdf"], ["visual.pdf"]))
        self.assertEqual(visual_calls[1], (["visual.pdf"], []))

    def test_index_then_search_finds_relevant_doc(self):
        report = self.pipeline.index_library("test-lib")
        self.assertEqual(report.succeeded, 2, f"应该两个文件都索引成功: {report.files}")
        self.assertEqual(report.failed, 0)

        results = self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].path, "plugin-notes.md")
        self.assertIn("插件", results[0].text)

    def test_rerank_disabled_falls_back_to_pure_fusion(self):
        """对齐旧 retriever.py:627（rerank_enabled=False → 纯融合继续出结果）
        与 _merge_normalized 降级：置信度退回 RRF 双路一致度，检索绝不因
        重排器缺席整体失败。"""
        self.pipeline.index_library("test-lib")
        self.runtime.settings.set("rerank_enabled", False)
        try:
            results = self.pipeline.search("test-lib", "插件 架构", top_k=5)
            self.assertTrue(results, "纯融合降级必须仍能出结果")
            self.assertTrue(all(0.0 <= r.confidence <= 1.0 for r in results))
        finally:
            self.runtime.settings.unset("rerank_enabled")

    def test_dense_candidate_pool_respects_old_floor(self):
        """对齐旧 retriever.py:640：候选池 = max(top_k×8, 200)——小 top_k 时
        候选池不得静默缩水（此前 top_k*3 把 top_k=5 的池子缩到 15）。"""
        self.pipeline.index_library("test-lib")
        with patch.object(
            self.pipeline._singleton("lexical_index"),
            "search",
            wraps=self.pipeline._singleton("lexical_index").search,
        ) as spy:
            self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertGreaterEqual(spy.call_args.kwargs.get("top_k", spy.call_args.args[-1] if spy.call_args.args else 0), 200)

    def test_wikilink_cleaning_and_frontmatter_anchors_enter_index_text(self):
        """问题15/审计F9 + 问题18/审计F20 的两条索引文本决策：
        ①wikilink 清洗——别名保留进索引文本、![[嵌入]]删除、原文链接抽取不受影响；
        ②frontmatter title/tags + 文件名锚点拼进嵌入/BM25 文本、交付输出剥离前缀。
        """
        (self.vault / "anchor-notes.md").write_text(
            "---\ntitle: 火箭发动机笔记\ntags: 航天\n---\n"
            "# 火箭发动机笔记\n\n"
            "这台机器参考了 [[概念/推进系统|推进原理]] 的设计。\n"
            "![[图片.png]]\n"
            "结构图见 [[推进系统# overview]]。\n",
            encoding="utf-8",
        )
        report = self.pipeline.index_library("test-lib")
        self.assertEqual(report.succeeded, 3, report.files)
        generation = self.pipeline._generations.active("test-lib")
        manifest = self.pipeline._manifests.read("test-lib", generation)
        record = manifest["files"]["anchor-notes.md"]
        self.assertEqual(
            record["links"],
            ["推进系统"],
            "链接抽取取「目标」（剥路径/锚点/别名、跳过嵌入），且基于清洗前的原文",
        )
        # 向量库里存的文本带锚点前缀（文件名+title+tags+标题链）
        vector_records = self.pipeline._vector_records(
            "test-lib", list(record["chunk_ids"]),
            self.pipeline._manifest_segments(manifest, "vector_segments", generation),
        )
        self.assertTrue(vector_records)
        stored = next(iter(vector_records.values()))
        self.assertIn("火箭发动机笔记", stored["document"])
        self.assertIn("航天", stored["document"])
        self.assertIn("anchor-notes", stored["document"])
        self.assertIn("推进原理", stored["document"], "别名必须保留进索引文本")
        self.assertNotIn("![[", stored["document"], "嵌入语法必须删除")
        # 检索交付的正文剥离锚点前缀
        results = self.pipeline.search("test-lib", "推进原理", top_k=5)
        hit = next(r for r in results if r.path == "anchor-notes.md")
        self.assertTrue(hit.text)
        # 原文里的 [[...]] 已被清洗为可读文字：交付正文含别名、不含双链/嵌入语法
        self.assertNotIn("[[", hit.text)
        self.assertNotIn("![[", hit.text)
        self.assertIn("推进原理", hit.text)
        self.assertNotIn("图片.png", hit.text, "嵌入语法已从索引文本删除")

    def test_graph_reads_active_manifest_and_relations_without_loading_embedder(self):
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n插件系统连接到 [[cooking]]。",
            encoding="utf-8",
        )
        self.pipeline.index_library("test-lib")
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(embedder, "embed_texts", wraps=embedder.embed_texts) as embed_texts:
            response = self.pipeline.graph("all")
        embed_texts.assert_not_called()
        self.assertEqual(response.library_ids, ("test-lib",))
        self.assertEqual({node.path for node in response.nodes}, {"plugin-notes.md", "cooking.md"})
        self.assertIn(
            ("test-lib|cooking.md", "test-lib|plugin-notes.md", "link"),
            {(edge.source, edge.target, edge.kind) for edge in response.edges},
        )

    def test_graph_semantic_edges_are_separate_and_use_existing_embedder(self):
        (self.vault / "third.md").write_text("# 第三篇\n\n独立内容。", encoding="utf-8")
        self.pipeline.index_library("test-lib")
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(
            embedder,
            "embed_texts",
            return_value=[(1.0, 0.0), (0.99, 0.01), (0.0, 1.0)],
        ) as embed_texts:
            response = self.pipeline.graph_semantic_edges("all", threshold=0.9)
        embed_texts.assert_called_once()
        self.assertTrue(response.error is None)
        self.assertTrue(any(edge.source != edge.target for edge in response.edges))

    def test_graph_semantic_cache_invalidates_when_document_metadata_changes(self):
        (self.vault / "third.md").write_text("# 第三篇\n\n独立内容。", encoding="utf-8")
        self.pipeline.index_library("test-lib")
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        vectors = [(1.0, 0.0), (0.99, 0.01), (0.0, 1.0)]
        with patch.object(embedder, "embed_texts", return_value=vectors) as embed_texts:
            self.pipeline.graph_semantic_edges("all", threshold=0.9)
            self.pipeline.graph_semantic_edges("all", threshold=0.9)
            self.assertEqual(embed_texts.call_count, 1)
            generation = self.pipeline._generations.active("test-lib")
            manifest = self.pipeline._manifests.read("test-lib", generation)
            assert manifest is not None
            manifest["files"]["third.md"]["mtime_ns"] += 1
            self.assertTrue(self.pipeline._manifests.write(manifest))
            self.pipeline.graph_semantic_edges("all", threshold=0.9)
        self.assertEqual(embed_texts.call_count, 2)

    def test_index_progress_callback_reports_phases_and_completion_after_work(self):
        events = []
        report = self.pipeline.index_library("test-lib", progress_callback=events.append)

        phases = []
        for event in events:
            if event.phase not in phases:
                phases.append(event.phase)
        self.assertEqual(
            phases,
            ["scanning", "extracting", "embedding", "writing", "file_complete", "visual", "finalizing"],
        )

        extracting = [event for event in events if event.phase == "extracting"]
        self.assertEqual([event.files_done for event in extracting], [0, 1])
        self.assertTrue(all(event.stall_grace_s == 300.0 for event in extracting))
        self.assertTrue(all(event.chunks_total is None for event in events if event.phase != "visual" and event.phase != "finalizing"))

        completed = [event for event in events if event.phase == "file_complete"]
        self.assertEqual([event.files_done for event in completed], [1, 2])
        expected_chunks = [report.files[0].chunk_count, sum(f.chunk_count for f in report.files)]
        self.assertEqual([event.chunks_done for event in completed], expected_chunks)

        embedding = [event for event in events if event.phase == "embedding"]
        writing = [event for event in events if event.phase == "writing"]
        self.assertTrue(all(event.stall_grace_s == 300.0 for event in embedding))
        self.assertTrue(all(event.stall_grace_s == 180.0 for event in writing))

        visual = next(event for event in events if event.phase == "visual")
        self.assertEqual(visual.chunks_total, visual.chunks_done)
        self.assertEqual(visual.stall_grace_s, 300.0)
        finalizing = next(event for event in events if event.phase == "finalizing")
        self.assertEqual(finalizing.chunks_total, finalizing.chunks_done)

    def test_background_index_start_and_stop_api(self):
        result = IndexStartResult(True, "started", "run-1", 123)
        with patch.object(self.pipeline._index_progress, "start", return_value=result) as start_mock:
            with patch.object(
                self.pipeline._index_progress,
                "stop",
                return_value=(True, "cancelled"),
            ) as stop_mock:
                started, message = self.pipeline.start_index_library("test-lib", source="e2e")
                stopped, stop_message = self.pipeline.stop_index_library("test-lib", result.run_id)
        self.assertTrue(started)
        self.assertEqual(message, "started")
        self.assertTrue(stopped)
        self.assertEqual(stop_message, "cancelled")
        start_mock.assert_called_once_with("test-lib", "e2e")
        stop_mock.assert_called_once_with("test-lib", result.run_id)

    def test_search_advice_uses_separate_response_channel(self):
        result = SearchResult("c1", "test-lib", "low.md", "低", "正文", 0.1)
        with patch.object(self.pipeline, "_search_once", return_value=[result]):
            response = self.pipeline.search_with_advice("test-lib", "低")
        self.assertIsInstance(response.results, tuple)
        self.assertTrue(response.advice)
        self.assertLessEqual(len(response.advice), 2)
        self.assertEqual(response.results[0].advice, ())

    def test_hyde_disabled_uses_one_search(self):
        first = [SearchResult("c1", "test-lib", "first.md", "first", "first", 0.2)]
        enhancer = self.runtime.plugins["official-query-enhancer-hyde"].instance
        enhancer._client = _FakeHydeClient()
        with patch.object(self.pipeline, "_search_once", return_value=first) as search_mock:
            results = self.pipeline.search("test-lib", "能力")
        self.assertEqual(results, first)
        self.assertEqual(search_mock.call_count, 1)
        self.assertEqual(enhancer._client.calls, [])

    def test_hyde_rechecks_and_keeps_strictly_higher_confidence(self):
        self.runtime.settings.set("hyde_enabled", True)
        self.runtime.settings.set("hyde_min_confidence", 0.5)
        enhancer = self.runtime.plugins["official-query-enhancer-hyde"].instance
        enhancer._client = _FakeHydeClient("假设文档正文")
        first = [SearchResult("c1", "test-lib", "first.md", "first", "first", 0.2)]
        second = [SearchResult("c2", "test-lib", "second.md", "second", "second", 0.8)]
        with patch.object(self.pipeline, "_search_once", side_effect=[first, second]) as search_mock:
            results = self.pipeline.search("test-lib", "能力")
        self.assertEqual(results, second)
        self.assertEqual(search_mock.call_count, 2)
        self.assertEqual(search_mock.call_args_list[1].args[1], "假设文档正文")
        self.assertEqual(len(enhancer._client.calls), 1)

    def test_hyde_failure_or_worse_result_keeps_first(self):
        self.runtime.settings.set("hyde_enabled", True)
        enhancer = self.runtime.plugins["official-query-enhancer-hyde"].instance
        first = [SearchResult("c1", "test-lib", "first.md", "first", "first", 0.4)]
        enhancer._client = _FailingHydeClient()
        with self.assertLogs(level="WARNING"):
            with patch.object(self.pipeline, "_search_once", return_value=first) as search_mock:
                self.assertEqual(self.pipeline.search("test-lib", "能力"), first)
        self.assertEqual(search_mock.call_count, 1)

        enhancer._client = _FakeHydeClient("更差的查询")
        worse = [SearchResult("c2", "test-lib", "worse.md", "worse", "worse", 0.3)]
        with patch.object(self.pipeline, "_search_once", side_effect=[first, worse]) as search_mock:
            self.assertEqual(self.pipeline.search("test-lib", "能力"), first)
        self.assertEqual(search_mock.call_count, 2)

    def test_second_unchanged_index_reuses_extraction_and_embeddings(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        manifest_before = self.pipeline._manifest("test-lib", "first")
        extractor = self.runtime.plugins["official-extractor-text"].instance
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(extractor, "extract", wraps=extractor.extract) as extract_mock:
            with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
                report = self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(extract_mock.call_count, 0)
        self.assertEqual(embed_mock.call_count, 0)
        self.assertEqual(report.unchanged, 2)
        manifest_after = self.pipeline._manifest("test-lib", "second")
        self.assertIsNotNone(manifest_before)
        self.assertIsNotNone(manifest_after)
        self.assertEqual(len(manifest_before["vector_segments"]), 1)
        self.assertEqual(len(manifest_after["vector_segments"]), 1)
        self.assertEqual(len(manifest_after["extract_segments"]), 1)
        self.assertTrue(manifest_after["compacted"])
        self.assertTrue(self.pipeline.search("test-lib", "插件 架构", top_k=5))

    def test_changed_file_reembeds_only_that_file(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        (self.vault / "plugin-notes.md").write_text(
            "# 插件架构笔记\n\n新增内容只讨论厨房食谱。", encoding="utf-8"
        )
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
            report = self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(embed_mock.call_count, 1)
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.unchanged, 1)
        results = self.pipeline.search("test-lib", "厨房 食谱", top_k=5)
        self.assertEqual(results[0].path, "plugin-notes.md")

    def test_touch_without_content_change_reuses_embeddings(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        path = self.vault / "plugin-notes.md"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000))
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
            report = self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(embed_mock.call_count, 0)
        self.assertEqual(report.unchanged, 2)

    def test_failed_file_is_retried_and_can_recover(self):
        path = self.vault / "recover.md"
        path.write_text("", encoding="utf-8")
        first = self.pipeline.index_library("test-lib", generation_id="first")
        failed = next(item for item in first.files if item.path == "recover.md")
        self.assertFalse(failed.extracted)
        self.assertEqual(first.failed, 1)
        path.write_text("# 恢复成功\n\n插件 架构 已经可以检索。", encoding="utf-8")
        second = self.pipeline.index_library("test-lib", generation_id="second")
        retried = next(item for item in second.files if item.path == "recover.md")
        self.assertTrue(retried.extracted)
        self.assertEqual(second.retried, 1)
        self.assertTrue(any(result.path == "recover.md" for result in self.pipeline.search("test-lib", "插件 架构")))

    def test_embedder_signature_change_reembeds_all_without_reextracting(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        manifest = self.pipeline._manifest("test-lib", "first")
        manifest["signatures"]["embedder"] = [["official-embedder-bge-m3", "old"]]
        self.assertTrue(self.pipeline._manifests.write(manifest))
        extractor = self.runtime.plugins["official-extractor-text"].instance
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(extractor, "extract", wraps=extractor.extract) as extract_mock:
            with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
                report = self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(extract_mock.call_count, 0)
        self.assertEqual(embed_mock.call_count, 2)
        self.assertEqual(report.changed, 2)
        updated_manifest = self.pipeline._manifest("test-lib", "second")
        self.assertIsNotNone(updated_manifest)
        self.assertEqual(len(updated_manifest["vector_segments"]), 1)
        self.assertTrue(updated_manifest["vector_segments"][0].startswith("second"))

    def test_full_index_forces_unchanged_files_to_rebuild(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(embedder, "embed_chunks", wraps=embedder.embed_chunks) as embed_mock:
            report = self.pipeline.index_library("test-lib", generation_id="second", full=True)
        self.assertEqual(embed_mock.call_count, 2)
        self.assertEqual(report.unchanged, 0)
        self.assertEqual(report.changed, 2)

    def test_failed_generation_keeps_previous_searchable_index(self):
        self.pipeline.index_library("test-lib", generation_id="good")
        old_paths = {r.path for r in self.pipeline.search("test-lib", "插件 架构", top_k=10)}
        (self.vault / "new.md").write_text("# 插件新增\n\n新一代插件索引。", encoding="utf-8")
        embedder = self.runtime.plugins["official-embedder-bge-m3"].instance
        with patch.object(embedder, "embed_chunks", side_effect=RuntimeError("generation failed")):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                self.pipeline.index_library("test-lib", generation_id="bad")
        self.assertEqual(self.pipeline._generations.active("test-lib"), "good")
        current_paths = {r.path for r in self.pipeline.search("test-lib", "插件 架构", top_k=10)}
        self.assertEqual(current_paths, old_paths)
        self.assertNotIn("new.md", current_paths)

    def test_successful_generation_drops_removed_files(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        self.assertTrue(self.pipeline.search("test-lib", "厨房 食谱", top_k=10))
        (self.vault / "cooking.md").unlink()
        self.pipeline.index_library("test-lib", generation_id="second")
        self.assertEqual(self.pipeline._generations.active("test-lib"), "second")
        self.assertFalse(
            any(r.path == "cooking.md" for r in self.pipeline.search("test-lib", "厨房 食谱", top_k=10))
        )

    def test_emptying_library_compacts_away_old_segments(self):
        self.pipeline.index_library("test-lib", generation_id="first")
        (self.vault / "plugin-notes.md").unlink()
        (self.vault / "cooking.md").unlink()
        report = self.pipeline.index_library("test-lib", generation_id="second")
        manifest = self.pipeline._manifest("test-lib", "second")
        self.assertEqual(report.removed, 2)
        self.assertEqual(self.pipeline.search("test-lib", "插件 架构", top_k=10), [])
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["vector_segments"], [])
        self.assertEqual(manifest["extract_segments"], [])
        self.assertTrue(manifest["compacted"])

    def test_old_generation_is_cleaned_after_next_commit(self):
        for generation in ("first", "second", "third"):
            self.pipeline.index_library("test-lib", generation_id=generation)
        self.assertEqual(self.pipeline._generations.active("test-lib"), "third")
        self.assertEqual(self.pipeline._generations.history("test-lib"), ["second"])
        self.assertFalse((self.data_dir / "extracted" / "test-lib" / "first").exists())
        self.assertFalse((self.data_dir / "bm25" / "generations" / "test-lib" / "first.json").exists())
        self.assertFalse(
            (self.data_dir / "note_relations" / "generations" / "test-lib" / "first.json").exists()
        )

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

    def test_max_chunks_per_file_caps_same_file_results(self):
        """同篇结果封顶（对齐 obsidian-rag 的 max_chunks_per_file，2026-09-23
        全面功能审计B类）：单篇文档命中很多块也不该挤占整个结果列表，
        默认每篇最多3条。用5个独立小节都强命中同一关键词的文件构造。"""
        big_vault = self.tmp / "big-vault"
        big_vault.mkdir()
        sections = "\n\n".join(f"## 第{i}节\n\n插件 插件 插件 架构相关内容第{i}节。" for i in range(1, 6))
        (big_vault / "big.md").write_text(f"# 大文件\n\n{sections}", encoding="utf-8")
        self.lib_mgr.store.add_library("big-lib", "大文件库", str(big_vault))

        self.pipeline.index_library("big-lib")
        results = self.pipeline.search("big-lib", "插件", top_k=10)
        same_file_hits = [r for r in results if r.path == "big.md"]
        self.assertGreaterEqual(len(same_file_hits), 1)
        self.assertLessEqual(len(same_file_hits), 3, "默认 max_chunks_per_file=3，不该超过这个数")

    def test_max_chunks_per_file_setting_overrides_default(self):
        big_vault = self.tmp / "big-vault2"
        big_vault.mkdir()
        sections = "\n\n".join(f"## 第{i}节\n\n插件 插件 插件 架构相关内容第{i}节。" for i in range(1, 6))
        (big_vault / "big.md").write_text(f"# 大文件\n\n{sections}", encoding="utf-8")
        self.lib_mgr.store.add_library("big-lib2", "大文件库2", str(big_vault))
        self.pipeline.index_library("big-lib2")

        self.pipeline.runtime.settings.set("max_chunks_per_file", 1)
        results = self.pipeline.search("big-lib2", "插件", top_k=10)
        same_file_hits = [r for r in results if r.path == "big.md"]
        self.assertEqual(len(same_file_hits), 1)

    def test_small_to_big_backfills_and_folds_parent_section(self):
        parent_paragraphs = "\n\n".join(f"测试父节第{i}段，包含足够长且不会重复的工程内容。" + "细节" * 80 for i in range(8))
        (self.vault / "long.md").write_text(f"# 长父节\n\n{parent_paragraphs}", encoding="utf-8")
        (self.vault / "other.md").write_text("# 其它\n\n测试其它甲。\n\n## 其它乙\n\n测试其它乙。", encoding="utf-8")
        self.runtime.plugins["official-reranker"].instance.engine = _PathRankedReranker()
        self.pipeline.index_library("test-lib")
        manifest = self.pipeline._manifest("test-lib", self.pipeline._generations.active("test-lib"))
        section = manifest["files"]["long.md"]["sections"]["s0"]
        self.assertGreater(section["chunk_count"], 1)
        self.assertGreater(len(section["text"]), 300)
        self.runtime.settings.set("max_chunks_per_file", 1)
        results = self.pipeline.search("test-lib", "测试", top_k=4)
        long_results = [result for result in results if result.path == "long.md"]
        self.assertEqual(len(long_results), 1)
        self.assertTrue(long_results[0].backfilled)
        self.assertEqual(long_results[0].text, section["text"])
        self.assertTrue(any(result.path == "other.md" for result in results))

    def test_small_to_big_list_mode_keeps_small_chunks(self):
        parent_paragraphs = "\n\n".join(f"测试父节第{i}段。" + "细节" * 80 for i in range(8))
        (self.vault / "long.md").write_text(f"# 长父节\n\n{parent_paragraphs}", encoding="utf-8")
        self.runtime.plugins["official-reranker"].instance.engine = _PathRankedReranker()
        self.pipeline.index_library("test-lib")
        self.runtime.settings.set("max_chunks_per_file", 1)
        results = self.pipeline.search("test-lib", "测试", top_k=4, include_body=False)
        long_results = [result for result in results if result.path == "long.md"]
        self.assertEqual(len(results), 4)
        self.assertGreaterEqual(len(long_results), 2)
        self.assertTrue(all(not result.backfilled for result in results))

    def test_small_to_big_does_not_backfill_single_chunk_section(self):
        (self.vault / "long.md").write_text(
            "# 长父节\n\n" + "测试单块父节。" + "细节" * 80,
            encoding="utf-8",
        )
        self.runtime.plugins["official-reranker"].instance.engine = _PathRankedReranker()
        self.pipeline.index_library("test-lib")
        results = self.pipeline.search("test-lib", "测试", top_k=3)
        long_results = [result for result in results if result.path == "long.md"]
        self.assertTrue(long_results)
        self.assertTrue(all(not result.backfilled for result in long_results))

    def test_small_to_big_can_be_disabled(self):
        parent_paragraphs = "\n\n".join(f"测试父节第{i}段。" + "细节" * 80 for i in range(8))
        (self.vault / "long.md").write_text(f"# 长父节\n\n{parent_paragraphs}", encoding="utf-8")
        self.runtime.plugins["official-reranker"].instance.engine = _PathRankedReranker()
        self.pipeline.index_library("test-lib")
        self.runtime.settings.set("small_to_big", False)
        self.runtime.settings.set("max_chunks_per_file", 1)
        results = self.pipeline.search("test-lib", "测试", top_k=3)
        self.assertTrue(all(not result.backfilled for result in results))
        self.assertLessEqual(sum(result.path == "long.md" for result in results), 1)

    def test_confidence_drop_threshold_filters_low_confidence_results(self):
        """置信度丢弃护栏默认关闭（0.0），显式调高后应该真的把低于阈值的
        命中丢掉——对齐 obsidian-rag 的 confidence_drop_threshold。"""
        self.pipeline.index_library("test-lib")
        baseline = self.pipeline.search("test-lib", "插件 架构", top_k=10)
        self.assertTrue(baseline)

        self.pipeline.runtime.settings.set("confidence_drop_threshold", 1.1)  # 高于任何可能的置信度，全部丢弃
        dropped = self.pipeline.search("test-lib", "插件 架构", top_k=10)
        self.assertEqual(dropped, [])

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

    def test_search_reads_fusion_weights_from_settings(self):
        """RRF 两路权重可调（2026-09-23 全面功能审计发现的缺口，接入
        core/settings.py 通用设置存储后补齐）——对齐 obsidian-rag/config.py
        的 fusion_dense_weight/fusion_bm25_weight，用一个记录调用参数的
        假 fuse() 替身验证 search() 真的把设置里的值传下去了，不是纸面
        改了签名没真的接线。"""
        fusion_instance = self.runtime.plugins["official-fusion-rrf"].instance
        calls = []
        original_fuse = fusion_instance.fuse

        def _spy_fuse(ranked_lists, weights=None):
            calls.append(weights)
            return original_fuse(ranked_lists, weights=weights)

        fusion_instance.fuse = _spy_fuse
        self.addCleanup(lambda: setattr(fusion_instance, "fuse", original_fuse))

        self.runtime.settings.set("fusion_dense_weight", 2.5)
        self.runtime.settings.set("fusion_bm25_weight", 0.5)
        self.pipeline.search("test-lib", "插件 架构", top_k=5)

        self.assertTrue(calls, "fuse() 应该至少被调用一次")
        for weights in calls:
            self.assertEqual(weights, [0.5, 2.5])  # [bm25权重, dense权重]，和 [lexical, vector] 顺序对齐

    def test_search_fusion_weights_default_to_equal_when_unset(self):
        fusion_instance = self.runtime.plugins["official-fusion-rrf"].instance
        calls = []
        original_fuse = fusion_instance.fuse

        def _spy_fuse(ranked_lists, weights=None):
            calls.append(weights)
            return original_fuse(ranked_lists, weights=weights)

        fusion_instance.fuse = _spy_fuse
        self.addCleanup(lambda: setattr(fusion_instance, "fuse", original_fuse))

        self.pipeline.search("test-lib", "插件 架构", top_k=5)
        self.assertTrue(calls)
        for weights in calls:
            self.assertEqual(weights, [1.0, 1.0])

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

    def test_empty_and_tbd_terminal_states_are_stable_until_content_changes(self):
        (self.vault / "empty.md").write_text("   \n", encoding="utf-8")
        (self.vault / "draft.md").write_text("[TBD]\nTODO —\n正文占位", encoding="utf-8")
        first = self.pipeline.index_library("test-lib")
        self.assertEqual(first.failed, 2)
        states = {row.path: row.failure_state for row in first.files if row.failure_state}
        self.assertEqual(states, {"empty.md": "empty", "draft.md": "tbd"})
        extractor = self.runtime.plugins["official-extractor-text"].instance
        with patch.object(extractor, "extract", side_effect=AssertionError("stable terminal re-extracted")):
            second = self.pipeline.index_library("test-lib")
        self.assertEqual(second.retried, 0)
        (self.vault / "draft.md").write_text("# 完成稿\n\n正式内容", encoding="utf-8")
        third = self.pipeline.index_library("test-lib")
        self.assertEqual(third.failed, 1)
        self.assertEqual(third.succeeded, 3)
        self.assertEqual(
            {row.path: row.failure_state for row in third.files if row.failure_state},
            {"empty.md": "empty"},
        )


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
        self.assertEqual(
            set(zf.namelist()),
            {
                "manifest.json",
                "vectors.json",
                "bm25.json",
                "index.json",
                "extracted.json",
                "relations.json",
                "failures.json",
            },
        )

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
        imported_manifest = self.pipeline._manifest(
            "test-lib-restored", self.pipeline._generations.active("test-lib-restored")
        )
        imported_ids = [
            chunk_id
            for record in imported_manifest["files"].values()
            for chunk_id in record.get("chunk_ids", [])
        ]
        self.assertTrue(imported_ids)
        self.assertTrue(all(chunk_id.startswith("test-lib-restored:") for chunk_id in imported_ids))

    def test_import_preserves_terminal_failure_diagnostics(self):
        (self.vault / "empty.md").write_text("", encoding="utf-8")
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")
        self.pipeline.import_library(archive, root_path=str(self.vault), library_id="restored-failures")
        manifest = self.pipeline._manifest(
            "restored-failures", self.pipeline._generations.active("restored-failures")
        )
        self.assertEqual(manifest["files"]["empty.md"]["status"], "terminal")
        self.assertEqual(manifest["files"]["empty.md"]["failure_state"], "empty")
        self.assertEqual(self.pipeline.index_failures("restored-failures")["failures"][0]["path"], "empty.md")

    def test_import_without_explicit_library_id_reuses_original(self):
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")
        self.lib_mgr.store.remove_library("test-lib")

        new_id = self.pipeline.import_library(archive, root_path="/new/machine/vault")
        self.assertEqual(new_id, "test-lib")
        self.assertIsNotNone(self.lib_mgr.store.get("test-lib"))

    def test_import_carries_over_selection_and_policy(self):
        self.lib_mgr.store.set_selection("test-lib", selection_out=["cooking.md"])
        self.lib_mgr.store.set_policy(
            "test-lib",
            new_file_default="exclude",
            enabled_extensions=[".md"],
            exclude_dirs=["private"],
            exclude_files=["secret.txt"],
            exclude_patterns=["*.tmp"],
        )
        self.lib_mgr.store.set_agent_formats("test-lib", [".pdf", ".docx"])
        self.pipeline.index_library("test-lib")
        archive = self.pipeline.export_library("test-lib")

        self.pipeline.import_library(archive, root_path="/new/machine/vault", library_id="test-lib-2")
        cfg = self.lib_mgr.store.get("test-lib-2")
        self.assertEqual(cfg.selection_out, ["cooking.md"])
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".md"])
        self.assertEqual(cfg.exclude_dirs, ["private"])
        self.assertEqual(cfg.exclude_files, ["secret.txt"])
        self.assertEqual(cfg.exclude_patterns, ["*.tmp"])
        self.assertEqual(cfg.agent_formats, [".pdf", ".docx"])
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
        self.runtime.settings.set("pdf_scan_backend", "mineru-local")
        plugin_ids = OFFICIAL_PHASE1_PLUGINS + ["official-ocr-mineru-cloud", "official-ocr-mineru-local"]
        for plugin_id in plugin_ids:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)
            state = self.runtime.plugins[plugin_id]
            self.assertEqual(state.state, PluginState.ENABLED, f"{plugin_id}: {state.error}")
        self.addCleanup(lambda: self.runtime.disable("official-ocr-mineru-local"))
        self.addCleanup(self.runtime.close)

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_DeterministicFakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_DeterministicFakeReranker())

        self.pipeline = Pipeline(self.runtime)
        self.lib_mgr = self.runtime.plugins["official-library-manager"].instance
        self.lib_mgr.store.add_library("scan-lib", "扫描件库", str(self.vault))
        self.lib_mgr.store.set_policy("scan-lib", enabled_extensions=[".md", ".txt", ".pdf"])

    def _restore_env(self) -> None:
        if self._fake_ocr_env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_OCR", None)
        else:
            os.environ["RAG_REDO_FAKE_OCR"] = self._fake_ocr_env_backup
        if self._api_key_backup is not None:
            os.environ["MINERU_API_KEY"] = self._api_key_backup

    def test_default_none_keeps_mixed_pdf_as_scanned_terminal(self):
        self.runtime.settings.set("pdf_scan_backend", "none")
        report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.succeeded, 0)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.files[0].extract_failure, "scanned")

    def test_stable_scanned_terminal_does_not_repeat_provider_work(self):
        self.runtime.settings.set("pdf_scan_backend", "none")
        first = self.pipeline.index_library("scan-lib")
        self.assertEqual(first.failed, 1)
        self.assertEqual(first.retried, 0)
        local = self.runtime.plugins["official-ocr-mineru-local"].instance
        cloud = self.runtime.plugins["official-ocr-mineru-cloud"].instance
        with (
            patch.object(local, "extract", side_effect=AssertionError("local OCR should not run")),
            patch.object(cloud, "extract", side_effect=AssertionError("cloud OCR should not run")),
        ):
            second = self.pipeline.index_library("scan-lib")
        self.assertEqual(second.failed, 1)
        self.assertEqual(second.retried, 0)
        manifest = self.pipeline._manifests.read(
            "scan-lib", self.pipeline._generations.active("scan-lib")
        )
        record = manifest["files"]["scanned-contract.pdf"]
        self.assertEqual(record["status"], "terminal")
        self.assertEqual(record["failure_state"], "scanned")
        self.assertFalse(self.pipeline.failure_will_retry({"path": "scanned-contract.pdf", **record}))

    def test_capability_change_retries_stable_scanned_terminal(self):
        self.runtime.settings.set("pdf_scan_backend", "none")
        self.pipeline.index_library("scan-lib")
        self.runtime.settings.set("pdf_scan_backend", "mineru-cloud")
        self.assertEqual(self.pipeline.stale_libraries("scan-lib"), ["scan-lib"])
        report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.retried, 1)
        self.assertEqual(report.files[0].failure_state, "scanned")

    def test_deferred_is_not_a_terminal_failure_and_remains_stale(self):
        self.runtime.settings.set("pdf_scan_backend", "mineru-local")
        local = self.runtime.plugins["official-ocr-mineru-local"].instance

        def deferred(library_id, path, root):
            return ExtractedDocument(
                library_id=library_id,
                path=path,
                text=None,
                failure_reason="deferred",
                extracted_by="official-ocr-mineru-local",
                extractor_version="test",
                content_hash="unused",
                failure_state="deferred",
            )

        with patch.object(local, "extract", side_effect=deferred):
            report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.deferred, 1)
        self.assertEqual(self.pipeline.index_failures("scan-lib")["failures"], [])
        self.assertEqual(self.pipeline.stale_libraries("scan-lib"), ["scan-lib"])

    def test_cloud_without_key_keeps_exact_scanned_terminal(self):
        self.runtime.settings.set("pdf_scan_backend", "mineru-cloud")
        report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.files[0].extract_failure, "scanned")

    def test_mixed_pdf_routes_whole_document_to_selected_local_ocr(self):
        (self.vault / "scanned-contract.pdf").unlink()
        import pymupdf

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "native text page content")
        doc.new_page()
        doc.save(str(self.vault / "mixed-contract.pdf"))
        doc.close()
        report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.succeeded, 1, report.files)
        generation = self.pipeline._generations.active("scan-lib")
        manifest = self.pipeline._manifests.read("scan-lib", generation)
        self.assertEqual(manifest["files"]["mixed-contract.pdf"]["extractor_id"], "official-ocr-mineru-local")

    def test_existing_ocr_cache_is_reused_after_backend_changes_to_none(self):
        self.pipeline.index_library("scan-lib")
        self.runtime.settings.set("pdf_scan_backend", "none")
        local = self.runtime.plugins["official-ocr-mineru-local"].instance
        with patch.object(local, "extract", side_effect=AssertionError("OCR provider should not run")):
            report = self.pipeline.index_library("scan-lib")
        self.assertEqual(report.succeeded, 1)
        self.assertEqual(report.changed, 1)

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
        text, fingerprint, provider_id = self.pipeline.generate_library_summary("test-lib")
        self.assertEqual(provider_id, "official-llm-openai-compatible")
        self.assertEqual(text, self.fake_llm.response)
        # 指纹对齐旧项目 content_fingerprint：全部已索引文件的 path:hash 聚合，
        # 非空且随内容变化（这里校验存在性，变化性由指纹专项测试覆盖）
        self.assertTrue(fingerprint)
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
        # 对齐旧项目 server.py:508-509：AI 提交时指纹随写落盘（非空），
        # 且内容未变时 is_stale=False、内容变化后 stale=True
        self.assertTrue(summary.fingerprint)
        self.assertFalse(
            self.pipeline._singleton("library_summary").is_stale(
                "test-lib", self.pipeline.library_content_fingerprint("test-lib")
            )
        )

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
