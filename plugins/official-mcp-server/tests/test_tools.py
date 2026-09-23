"""真实通过 MCPServer.call_tool() 走一遍注册-调用路径（不是绕过 MCP SDK
直接调用底层 Python 函数）——这样才能真的验证"工具签名/docstring 能被
MCP SDK 正确解析成 schema、参数校验和分发链路走得通"，而不是只测业务
逻辑本身。业务逻辑（Pipeline 是否真的搜到对的东西）已经在
tests/test_pipeline_e2e.py 覆盖过，这里的重点是 MCP 这一层薄封装接得对。
"""
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
for other_plugin_dir in (_REPO_ROOT / "plugins").glob("*"):
    if other_plugin_dir.is_dir() and str(other_plugin_dir) not in sys.path:
        sys.path.insert(0, str(other_plugin_dir))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402
from official_mcp_server.plugin import McpServerPlugin  # noqa: E402

REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
]


class _FakeEncoder:
    KEYWORDS = ["插件", "架构"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


class _FakeReranker:
    def score(self, query, texts):
        terms = [t for t in query if t.strip()]
        return [sum(text.count(term) for term in terms) for text in texts]


class TestMcpToolsAsyncBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        vault = self.tmp / "vault"
        vault.mkdir()
        (vault / "notes.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计。", encoding="utf-8"
        )

        self.runtime = PluginRuntime(
            _REPO_ROOT / "plugins",
            state_file=self.tmp / "plugins_state.json",
            data_dir=self.tmp / "data",
        )
        self.runtime.scan()
        for plugin_id in REQUIRED_PLUGINS:
            self.runtime.load(plugin_id)
            self.runtime.enable(plugin_id)
            state = self.runtime.plugins[plugin_id]
            assert state.state.value == "enabled", f"{plugin_id}: {state.error}"

        from official_embedder_bge_m3.embed import BGEM3Embedder

        self.runtime.plugins["official-embedder-bge-m3"].instance.embedder = BGEM3Embedder(
            encoder=_FakeEncoder()
        )
        from official_reranker.rerank import RerankerEngine

        self.runtime.plugins["official-reranker"].instance.engine = RerankerEngine(reranker=_FakeReranker())

        lib_mgr = self.runtime.plugins["official-library-manager"].instance
        lib_mgr.store.add_library("test-lib", "测试库", str(vault))

        self.pipeline = Pipeline(self.runtime)
        self.lib_mgr = lib_mgr

        self.server = MCPServer(name="rag-redo-test")
        mcp_plugin = McpServerPlugin()
        mcp_plugin.register_tools(self.server, self.pipeline, lib_mgr)


class TestMcpTools(TestMcpToolsAsyncBase):
    async def test_list_libraries_tool(self):
        result = await self.server.call_tool("list_libraries", {})
        self.assertFalse(result.is_error)
        libs = result.structured_content["result"]
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["library_id"], "test-lib")

    async def test_reindex_then_search_knowledge_tool(self):
        reindex_result = await self.server.call_tool("reindex_knowledge", {"library_id": "test-lib"})
        self.assertFalse(reindex_result.is_error)
        # 注意：dict[str, Any] 返回类型的 structured_content 就是这个字典
        # 本身，不像 list/标量返回那样被包一层 {"result": ...}——一个JSON
        # 对象已经是合法的顶层结构，SDK不需要再包一层。这也是真实踩出来的
        # 行为差异，不是猜的。
        report = reindex_result.structured_content
        self.assertTrue(report["ok"])
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["failed"], 0)

        search_result = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "library_id": "test-lib"}
        )
        self.assertFalse(search_result.is_error)
        payload = search_result.structured_content
        self.assertTrue(payload["ok"])
        hits = payload["results"]
        self.assertGreater(len(hits), 0)
        self.assertEqual(hits[0]["path"], "notes.md")
        self.assertIn("confidence", hits[0])

    async def test_search_knowledge_default_top_k(self):
        await self.server.call_tool("reindex_knowledge", {"library_id": "test-lib"})
        result = await self.server.call_tool("search_knowledge", {"query": "插件", "library_id": "test-lib"})
        self.assertFalse(result.is_error)

    async def test_unknown_library_id_reports_error_not_crash(self):
        """实测过：MCP SDK 2.2.0 的工具函数如果裸抛异常，call_tool() 会
        把异常原样往上炸、不会自动转成 is_error 结果（这一层"友好化"发生
        在更外层的真实 JSON-RPC 请求处理里，MCPServer.call_tool() 这个
        Python 便捷方法本身不做）。所以 search_knowledge 自己必须显式
        try/except，绝不能指望 SDK 兜底——这条断言就是钉住这一点：调用
        本身要能正常返回（不抛异常），且结果里明确标 ok=False。"""
        result = await self.server.call_tool("search_knowledge", {"query": "x", "library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])
        self.assertIn("error", result.structured_content)

    async def test_tools_are_discoverable_with_schema(self):
        tools = await self.server.list_tools()
        names = {t.name for t in tools}
        self.assertEqual(names, {"search_knowledge", "list_libraries", "reindex_knowledge"})
        search_tool = next(t for t in tools if t.name == "search_knowledge")
        self.assertIn("query", search_tool.input_schema["properties"])
        self.assertIn("library_id", search_tool.input_schema["properties"])


if __name__ == "__main__":
    unittest.main()
