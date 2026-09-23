"""用仓库自带的 demo-vault/（真实文档，不是临时造的合成 fixture）跑一遍
真实索引+检索——这是 tests/test_pipeline_e2e.py 的补充：那边测的是架构
本身对不对（用短小的合成文本，方便精确断言排除/隔离这类规则），这里测
的是"真实、较长、带标题/表格/列表的文档"经过真实提取+切块之后，产出的
chunk 还能不能被正常检索到，顺便让 demo-vault 本身成为一份持续被验证的
资产——以后改切块/提取逻辑，这份 demo-vault 里任何一篇文档搜不到了，
这个测试就会先叫出来，不用等用户自己发现。

embedder/reranker 仍然注入确定性假实现（原因见
plugins/official-embedder-bge-m3/tests/test_embed.py），extractor/
chunker/library-manager/bm25/chroma/rrf 全部走真实代码处理真实文件。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEMO_VAULT = REPO_ROOT / "demo-vault"
sys.path.insert(0, str(REPO_ROOT))
for plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if plugin_dir.is_dir():
        sys.path.insert(0, str(plugin_dir))

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402

REQUIRED_PLUGINS = [
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

#: 假 encoder 认识的关键词——覆盖 demo-vault 四篇笔记各自的主题词，
#: 保证向量这一路也能区分四篇笔记，不是只靠 BM25 撑着。
_KEYWORDS = ["插件", "架构", "检索", "重排", "数据流", "治理", "显卡", "安装", "云端"]


class _DeterministicFakeEncoder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(t.count(k)) for k in _KEYWORDS] for t in texts]


class _DeterministicFakeReranker:
    def score(self, query: str, texts: list[str]) -> list[float]:
        # query.split()——按空白切词，不是 [t for t in query]（那样是逐字符
        # 遍历，'数据流 治理' 会被拆成 ['数','据','流','治','理'] 五个单字，
        # 在更长的真实文档里逐字符统计很容易被别的文档的散落字符噪声盖过，
        # 之前在小合成语料里没暴露、跑 demo-vault 真实内容时才现形。
        terms = query.split()
        return [sum(text.count(term) for term in terms) for text in texts]


@unittest.skipUnless(DEMO_VAULT.exists(), "demo-vault/ 目录不存在，跳过")
class TestDemoVault(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.runtime = PluginRuntime(
            REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        for plugin_id in REQUIRED_PLUGINS:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)
            state = self.runtime.plugins[plugin_id]
            self.assertEqual(state.state.value, "enabled", f"{plugin_id}: {state.error}")

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_DeterministicFakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(
            reranker=_DeterministicFakeReranker()
        )

        lib_mgr = self.runtime.plugins["official-library-manager"].instance
        lib_mgr.store.add_library("demo", "示例库", str(DEMO_VAULT))
        self.pipeline = Pipeline(self.runtime)

    def test_all_demo_notes_index_successfully(self):
        report = self.pipeline.index_library("demo")
        self.assertEqual(report.failed, 0, f"有文件提取失败: {report.files}")
        self.assertGreaterEqual(report.succeeded, 4, "demo-vault 应该至少有4篇笔记都索引成功")

    def test_search_plugin_architecture_finds_right_note(self):
        self.pipeline.index_library("demo")
        results = self.pipeline.search("demo", "插件 架构", top_k=5)
        self.assertTrue(results)
        self.assertEqual(results[0].path, "插件架构入门.md")

    def test_search_hybrid_retrieval_finds_right_note(self):
        self.pipeline.index_library("demo")
        results = self.pipeline.search("demo", "检索 重排", top_k=5)
        self.assertTrue(results)
        self.assertEqual(results[0].path, "混合检索是怎么工作的.md")

    def test_search_data_flow_finds_right_note(self):
        self.pipeline.index_library("demo")
        results = self.pipeline.search("demo", "数据流 治理", top_k=5)
        self.assertTrue(results)
        self.assertEqual(results[0].path, "数据流治理原则.md")

    def test_search_faq_finds_right_note(self):
        self.pipeline.index_library("demo")
        results = self.pipeline.search("demo", "显卡 安装 云端", top_k=5)
        self.assertTrue(results)
        self.assertEqual(results[0].path, "常见问题.md")

    def test_table_in_plugin_architecture_note_survives_chunking(self):
        """插件架构入门.md 里有一张 Markdown 表格——确认切块没有把它拆得
        面目全非（至少完整的表头行还在同一个 chunk 里）。"""
        self.pipeline.index_library("demo")
        vector_store = self.runtime.plugins["official-vector-store-chroma"].instance
        lexical = self.runtime.plugins["official-lexical-bm25"].instance
        hits = lexical.search("demo", "扩展点", top_k=20)
        chunk_ids = [chunk_id for chunk_id, _ in hits]
        records = vector_store.get_by_ids("demo", chunk_ids)
        table_chunks = [r["document"] for r in records.values() if "| 扩展点 | 类型 |" in r["document"]]
        self.assertTrue(table_chunks, "表头所在的chunk应该能被搜到")
        self.assertIn("| embedder | 单例 |", table_chunks[0])


if __name__ == "__main__":
    unittest.main()
