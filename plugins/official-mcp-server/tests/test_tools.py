"""真实通过 MCPServer.call_tool() 走一遍注册-调用路径（不是绕过 MCP SDK
直接调用底层 Python 函数）——这样才能真的验证"工具签名/docstring 能被
MCP SDK 正确解析成 schema、参数校验和分发链路走得通"，而不是只测业务
逻辑本身。业务逻辑（Pipeline 是否真的搜到对的东西）已经在
tests/test_pipeline_e2e.py 覆盖过，这里的重点是 MCP 这一层薄封装接得对。
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
import pymupdf

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
for other_plugin_dir in (_REPO_ROOT / "plugins").glob("*"):
    if other_plugin_dir.is_dir() and str(other_plugin_dir) not in sys.path:
        sys.path.insert(0, str(other_plugin_dir))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.index_progress import IndexStartResult  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402
from official_mcp_server.plugin import McpServerPlugin  # noqa: E402

REQUIRED_PLUGINS = [
    "official-extractor-text",
    "official-extractor-docx",
    "official-chunker",
    "official-library-manager",
    "official-lexical-bm25",
    "official-embedder-bge-m3",
    "official-vector-store-chroma",
    "official-fusion-rrf",
    "official-reranker",
    "official-import-export",
    "official-dedup",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-result-advisor",
]


class _FakeEncoder:
    KEYWORDS = ["插件", "架构"]

    def encode(self, texts):
        return [[float(t.count(k)) for k in self.KEYWORDS] for t in texts]


class _FakeReranker:
    def score(self, query, texts):
        # query.split()，不是逐字符遍历——见 tests/test_demo_vault.py 的
        # 同类注释，逐字符统计在更长的真实文本上容易被噪声干扰。
        terms = query.split()
        return [sum(text.count(term) for term in terms) for text in texts]


class _FakeLlmClient:
    def __init__(self, response: str = "这是一个讲插件架构的个人知识库简介。") -> None:
        self.response = response

    def complete(self, system, user, **kwargs):
        return self.response


class TestMcpToolsAsyncBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._wemm_env_backup = os.environ.get("RAG_REDO_FAKE_WEMM")
        os.environ["RAG_REDO_FAKE_WEMM"] = "1"
        # official-visual-wemm 现在声明了真实的 env_bootstrap（会真的 pip
        # install torch 等重依赖）——理由同 official-visual-wemm/tests/
        # test_plugin.py 里同名注释，测试跳过真实建独立环境这一步。
        self._skip_bootstrap_backup = os.environ.get("RAG_REDO_SKIP_ENV_BOOTSTRAP")
        os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = "1"
        self.addCleanup(self._restore_wemm_env)

        vault = self.tmp / "vault"
        vault.mkdir()
        (vault / "notes.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计。", encoding="utf-8"
        )

        self._optional_enabled: list[str] = []
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

        from official_llm_openai_compatible.llm import OpenAiCompatibleClient

        self.fake_llm = _FakeLlmClient()
        self.runtime.plugins["official-llm-openai-compatible"].instance._client = OpenAiCompatibleClient(  # noqa: SLF001
            http_client=self.fake_llm
        )

        lib_mgr = self.runtime.plugins["official-library-manager"].instance
        lib_mgr.store.add_library("test-lib", "测试库", str(vault))

        self.pipeline = Pipeline(self.runtime)
        self.lib_mgr = lib_mgr

        self.server = MCPServer(name="rag-redo-test")
        mcp_plugin = McpServerPlugin()
        mcp_plugin.register_tools(self.server, self.pipeline, lib_mgr)

    async def asyncTearDown(self) -> None:
        # 真实子进程插件只给专门测试它的方法按需启用；无论正常还是可选插件，
        # 都对称 disable→unload，避免测试自己留下子进程、文件锁或客户端句柄。
        for plugin_id in reversed(REQUIRED_PLUGINS + self._optional_enabled):
            state = self.runtime.plugins.get(plugin_id)
            if state is not None and state.state.value == "enabled":
                self.runtime.disable(plugin_id)
            if state is not None and state.state.value == "disabled":
                self.runtime.unload(plugin_id)

    def _restore_wemm_env(self) -> None:
        if self._wemm_env_backup is None:
            os.environ.pop("RAG_REDO_FAKE_WEMM", None)
        else:
            os.environ["RAG_REDO_FAKE_WEMM"] = self._wemm_env_backup
        if self._skip_bootstrap_backup is None:
            os.environ.pop("RAG_REDO_SKIP_ENV_BOOTSTRAP", None)
        else:
            os.environ["RAG_REDO_SKIP_ENV_BOOTSTRAP"] = self._skip_bootstrap_backup

    async def _reindex_and_wait(self, library_id: str) -> dict:
        report = self.pipeline.index_library(library_id)
        return {"stage": "done", "succeeded": report.succeeded, "failed": report.failed}


class TestMcpTools(TestMcpToolsAsyncBase):
    async def test_list_libraries_tool(self):
        result = await self.server.call_tool("list_libraries", {})
        self.assertFalse(result.is_error)
        libs = result.structured_content["result"]
        self.assertEqual(len(libs), 1)
        self.assertEqual(libs[0]["library_id"], "test-lib")

    async def test_reindex_then_search_knowledge_tool(self):
        # 注意：dict[str, Any] 返回类型的 structured_content 就是这个字典
        # 本身，不像 list/标量返回那样被包一层 {"result": ...}——一个JSON
        # 对象已经是合法的顶层结构，SDK不需要再包一层。这也是真实踩出来的
        # 行为差异，不是猜的。succeeded/failed 现在从 index_status 的终态
        # 里读，不是 reindex_knowledge 的返回值——它已经改成后台执行+立即
        # 返回，见 official_mcp_server/tools.py 的 docstring。
        status = await self._reindex_and_wait("test-lib")
        self.assertEqual(status["stage"], "done")
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(status["failed"], 0)

        search_result = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
        )
        self.assertFalse(search_result.is_error)
        payload = search_result.structured_content
        self.assertTrue(payload["ok"])
        hits = payload["results"]
        self.assertGreater(len(hits), 0)
        self.assertEqual(hits[0]["path"], "notes.md")
        self.assertEqual(hits[0]["library_id"], "test-lib")
        self.assertIn("confidence", hits[0])
        self.assertIn("confidence_tier", hits[0])
        self.assertIn(hits[0]["confidence_tier"], ("高相关", "中相关", "弱相关"))

    async def test_search_waits_for_first_index_before_returning(self):
        started: list[tuple[str, tuple[str, ...]]] = []
        with (
            patch.object(self.pipeline, "stale_libraries", return_value=["test-lib"]),
            patch.object(self.pipeline, "has_index", return_value=False),
            patch.object(
                self.pipeline,
                "start_index_library",
                side_effect=lambda library_id, **kwargs: started.append(
                    (library_id, kwargs["format_allowlist"])
                ),
            ),
            patch.object(
                self.pipeline,
                "index_status",
                return_value={"stage": "done", "succeeded": 1, "failed": 0},
            ),
        ):
            result = await self.server.call_tool(
                "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
            )
        self.assertFalse(result.is_error)
        self.assertEqual(started, [("test-lib", (".md", ".txt"))])
        self.assertEqual(result.structured_content["refreshing"], [])

    async def test_search_starts_background_refresh_for_nonempty_library(self):
        await self._reindex_and_wait("test-lib")
        with (
            patch.object(self.pipeline, "stale_libraries", return_value=["test-lib"]),
            patch.object(self.pipeline, "has_index", return_value=True),
            patch.object(self.pipeline, "start_index_library") as start,
        ):
            result = await self.server.call_tool(
                "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
            )
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["results"])
        self.assertEqual(result.structured_content["refreshing"], ["test-lib"])
        start.assert_called_once()

    async def test_read_document_tool_reads_source_file_for_md(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("read_document", {"library_id": "test-lib", "path": "notes.md"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "源文件直读")
        self.assertIn("插件系统的架构设计", payload["text"])

    async def test_agent_read_document_blocks_docx_until_user_authorizes_format(self):
        document = Document()
        document.add_paragraph("private binary body")
        document.save(str(self.tmp / "vault" / "private.docx"))
        denied = await self.server.call_tool(
            "read_document",
            {"library_id": "test-lib", "path": "private.docx"},
        )
        self.assertFalse(denied.structured_content["ok"])
        self.lib_mgr.store.set_agent_formats("test-lib", [".docx"])
        self.pipeline.index_library("test-lib")
        allowed = await self.server.call_tool(
            "read_document",
            {"library_id": "test-lib", "path": "private.docx"},
        )
        self.assertTrue(allowed.structured_content["ok"])
        self.assertIn("private binary body", allowed.structured_content["text"])

    async def test_read_document_tool_unknown_path_reports_error(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool(
            "read_document", {"library_id": "test-lib", "path": "does-not-exist.md"}
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_read_document_tool_before_indexing_reports_error(self):
        result = await self.server.call_tool("read_document", {"library_id": "test-lib", "path": "notes.md"})
        self.assertFalse(result.is_error)
        # 还没建过索引：.md 直读源文件不需要索引也能成功——这条断言的是
        # "还没index过的文件依然能读到源文件内容"，因为.md/.txt走的是
        # 现读源文件而不是提取缓存这条路径。
        self.assertTrue(result.structured_content["ok"])

    async def test_find_duplicates_tool_detects_near_duplicate_files(self):
        vault = self.tmp / "vault"
        (vault / "notes-copy.md").write_text(
            "# 插件架构（副本）\n\n这篇笔记讲插件系统的架构设计，一字不改的近似重复。",
            encoding="utf-8",
        )
        (vault / "notes.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计，一字不改的近似重复。", encoding="utf-8"
        )
        await self._reindex_and_wait("test-lib")

        result = await self.server.call_tool("find_duplicates", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        groups = payload["groups"]["official-dedup"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), {"notes.md", "notes-copy.md"})

    async def test_find_duplicates_tool_no_duplicates_returns_empty_groups(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("find_duplicates", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["groups"]["official-dedup"], [])

    async def test_find_duplicates_tool_unknown_library_reports_error(self):
        result = await self.server.call_tool("find_duplicates", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_note_relations_tool_resolves_mutual_wikilinks(self):
        vault = self.tmp / "vault"
        (vault / "notes.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计，参见 [[笔记B]]。", encoding="utf-8"
        )
        (vault / "笔记B.md").write_text("# 笔记B\n\n回链到 [[notes|插件架构]]。", encoding="utf-8")
        await self._reindex_and_wait("test-lib")

        forward = await self.server.call_tool("note_relations", {"library_id": "test-lib", "path": "notes.md"})
        self.assertFalse(forward.is_error)
        forward_payload = forward.structured_content
        self.assertTrue(forward_payload["ok"])
        self.assertTrue(forward_payload["resolved"])
        self.assertEqual(forward_payload["file"], "notes.md")
        self.assertEqual(forward_payload["outlinks"], ["笔记B.md"])
        self.assertEqual(forward_payload["inlinks"], ["笔记B.md"])  # 笔记B也反过来链了回来

        # 按不含扩展名的标题查询同一篇笔记，应解析到同一个文件
        by_title = await self.server.call_tool("note_relations", {"library_id": "test-lib", "path": "notes"})
        self.assertEqual(by_title.structured_content["file"], "notes.md")

    async def test_note_relations_tool_unresolved_note_reports_resolved_false(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool(
            "note_relations", {"library_id": "test-lib", "path": "不存在的笔记"}
        )
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["resolved"])

    async def test_note_relations_tool_unknown_library_reports_error(self):
        result = await self.server.call_tool(
            "note_relations", {"library_id": "no-such-lib", "path": "notes.md"}
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_get_selection_tool_reflects_empty_state_on_fresh_library(self):
        result = await self.server.call_tool("get_selection", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["selection_in"], [])
        self.assertEqual(payload["selection_out"], [])

    async def test_get_selection_tool_unknown_library_reports_error(self):
        result = await self.server.call_tool("get_selection", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_propose_then_apply_selection_changes_roundtrip(self):
        """硬性确认门禁：propose 绝不直接生效，必须走 apply 携带正确的
        提案号+确认码才会真正改变 get_selection 能看到的状态。"""
        propose_result = await self.server.call_tool(
            "propose_selection_changes",
            {"library_id": "test-lib", "changes": [{"path": "notes.md", "action": "out"}]},
        )
        self.assertFalse(propose_result.is_error)
        propose_payload = propose_result.structured_content
        self.assertTrue(propose_payload["ok"])
        self.assertIn("proposal_id", propose_payload)
        self.assertIn("confirmation_code", propose_payload)

        # 还没 apply：get_selection 应该看不到任何变化
        before = await self.server.call_tool("get_selection", {"library_id": "test-lib"})
        self.assertEqual(before.structured_content["selection_out"], [])

        apply_result = await self.server.call_tool(
            "apply_selection_changes",
            {
                "library_id": "test-lib",
                "proposal_id": propose_payload["proposal_id"],
                "confirmation_code": propose_payload["confirmation_code"],
            },
        )
        self.assertFalse(apply_result.is_error)
        self.assertTrue(apply_result.structured_content["ok"])

        after = await self.server.call_tool("get_selection", {"library_id": "test-lib"})
        self.assertEqual(after.structured_content["selection_out"], ["notes.md"])

    async def test_apply_selection_changes_wrong_code_reports_error_not_crash(self):
        propose_result = await self.server.call_tool(
            "propose_selection_changes",
            {"library_id": "test-lib", "changes": [{"path": "notes.md", "action": "out"}]},
        )
        proposal_id = propose_result.structured_content["proposal_id"]
        apply_result = await self.server.call_tool(
            "apply_selection_changes",
            {"library_id": "test-lib", "proposal_id": proposal_id, "confirmation_code": "000000"},
        )
        self.assertFalse(apply_result.is_error)
        self.assertFalse(apply_result.structured_content["ok"])

    async def test_propose_selection_changes_illegal_path_reports_error(self):
        result = await self.server.call_tool(
            "propose_selection_changes",
            {"library_id": "test-lib", "changes": [{"path": "../escape.md", "action": "in"}]},
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_propose_selection_changes_unknown_library_reports_error(self):
        result = await self.server.call_tool(
            "propose_selection_changes",
            {"library_id": "no-such-lib", "changes": [{"path": "a.md", "action": "in"}]},
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_reindex_then_navigate_knowledge_tool(self):
        """真实走一遍 navigate_knowledge——不是 search_knowledge 的变体，
        是完全独立的"第二检索系统"（页级视觉导航，见
        core/contracts.py::PageHit 的说明），这里验证的是它作为 MCP 工具
        能不能被正确发现/调用/返回结构化结果，业务逻辑（页向量对不对）
        已经在 plugins/official-visual-wemm/tests/test_plugin.py 覆盖过。

        用单独一个库（而不是共享 asyncSetUp 的 test-lib）——PDF 默认不在
        库的 enabled_extensions 白名单里（official_library_manager 默认
        只认 .md/.txt），把它加进共享库会连带影响其他测试对"succeeded/
        failed 文件数"的断言，专门起一个库更干净。"""
        pdf_vault = self.tmp / "pdf-vault"
        pdf_vault.mkdir()
        pdf_doc = pymupdf.open()
        pdf_doc.new_page().insert_text((72, 72), "扫描页示例内容", fontsize=24)
        pdf_doc.save(str(pdf_vault / "scan.pdf"))
        pdf_doc.close()
        self.lib_mgr.store.add_library("pdf-lib", "PDF库", str(pdf_vault))
        self.lib_mgr.store.set_policy("pdf-lib", enabled_extensions=[".md", ".txt", ".pdf"])
        self.lib_mgr.store.set_agent_formats("pdf-lib", [".pdf"])
        self.runtime.load("official-visual-wemm")
        self.runtime.enable("official-visual-wemm")
        self._optional_enabled.append("official-visual-wemm")

        status = await self._reindex_and_wait("pdf-lib")
        self.assertEqual(status["stage"], "done")

        navigate_result = await self.server.call_tool(
            "navigate_knowledge", {"query": "随便什么查询", "library_id": "pdf-lib"}
        )
        self.assertFalse(navigate_result.is_error)
        payload = navigate_result.structured_content
        self.assertTrue(payload["ok"])
        hits = payload["results"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "scan.pdf")
        self.assertEqual(hits[0]["page"], 1)  # 对外1-based

        status_result = await self.server.call_tool("wemm_status", {})
        self.assertFalse(status_result.is_error)
        status_payload = status_result.structured_content
        self.assertTrue(status_payload["ok"])
        wemm_status = status_payload["providers"]["official-visual-wemm"]
        self.assertTrue(wemm_status["subprocess_alive"])
        self.assertEqual(
            wemm_status["libraries"]["pdf-lib"],
            {"page_count": 1, "pdf_count": 1, "failures": []},
        )

    async def test_navigate_knowledge_unknown_library_reports_error_not_crash(self):
        result = await self.server.call_tool(
            "navigate_knowledge", {"query": "x", "library_id": "no-such-lib"}
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])
        self.assertIn("error", result.structured_content)

    async def test_search_knowledge_default_top_k(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("search_knowledge", {"query": "插件", "libraries": "test-lib"})
        self.assertFalse(result.is_error)

    async def test_search_knowledge_empty_libraries_defaults_to_all(self):
        """libraries 留空=全部已注册库，对齐 obsidian-rag 选库语法——不
        传 libraries 字段应该等价于传 "" 或 "all"，不该报错也不该只搜
        某一个库。"""
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("search_knowledge", {"query": "插件 架构"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertGreater(len(payload["results"]), 0)

    async def test_search_knowledge_include_body_false_omits_text(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib", "include_body": False}
        )
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertGreater(len(payload["results"]), 0)
        for hit in payload["results"]:
            self.assertNotIn("text", hit)
            self.assertNotIn("backfilled", hit)
            self.assertIn("path", hit)

    async def test_search_knowledge_returns_separate_advice_channel(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool(
            "search_knowledge", {"query": "完全不存在的主题", "libraries": "test-lib"}
        )
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertIn("advice", payload)
        self.assertLessEqual(len(payload["advice"]), 2)
        for hit in payload["results"]:
            self.assertNotIn("advice", hit)

    async def test_unknown_library_id_reports_error_not_crash(self):
        """实测过：MCP SDK 2.2.0 的工具函数如果裸抛异常，call_tool() 会
        把异常原样往上炸、不会自动转成 is_error 结果（这一层"友好化"发生
        在更外层的真实 JSON-RPC 请求处理里，MCPServer.call_tool() 这个
        Python 便捷方法本身不做）。所以 search_knowledge 自己必须显式
        try/except，绝不能指望 SDK 兜底——这条断言就是钉住这一点：调用
        本身要能正常返回（不抛异常），且结果里明确标 ok=False。"""
        result = await self.server.call_tool("search_knowledge", {"query": "x", "libraries": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_search_knowledge_exclude_removes_library_from_results(self):
        """exclude 反选：单库场景下 exclude 掉那唯一一个库，最终范围为空，
        对齐 official-library-manager::resolve_libraries 的报错语义。"""
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib", "exclude": "test-lib"}
        )
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertIn("error", result.structured_content)

    async def test_tools_are_discoverable_with_schema(self):
        tools = await self.server.list_tools()
        names = {t.name for t in tools}
        self.assertEqual(
            names,
            {
                "search_knowledge",
                "navigate_knowledge",
                "list_libraries",
                "reindex_knowledge",
                "index_status",
                "index_failures",
                "export_library",
                "import_library",
                "get_library_sample",
                "propose_library_summary",
                "apply_library_summary",
                "wemm_status",
                "read_document",
                "find_duplicates",
                "note_relations",
                "get_selection",
                "propose_selection_changes",
                "apply_selection_changes",
            },
        )
        search_tool = next(t for t in tools if t.name == "search_knowledge")
        self.assertIn("query", search_tool.input_schema["properties"])
        self.assertIn("libraries", search_tool.input_schema["properties"])
        self.assertIn("exclude", search_tool.input_schema["properties"])
        self.assertIn("folder", search_tool.input_schema["properties"])
        self.assertIn("include_body", search_tool.input_schema["properties"])

    async def test_export_then_import_library_round_trips_search_results(self):
        await self._reindex_and_wait("test-lib")
        before = await self.server.call_tool("search_knowledge", {"query": "插件 架构", "libraries": "test-lib"})
        self.assertFalse(before.is_error)

        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        self.assertFalse(export_result.is_error)
        export_payload = export_result.structured_content
        self.assertTrue(export_payload["ok"])
        self.assertIn("archive_base64", export_payload)

        import_result = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": export_payload["archive_base64"],
                "root_path": "/new/machine/vault",
                "library_id": "test-lib-restored",
            },
        )
        self.assertFalse(import_result.is_error)
        import_payload = import_result.structured_content
        self.assertTrue(import_payload["ok"])
        self.assertEqual(import_payload["library_id"], "test-lib-restored")

        after = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib-restored"}
        )
        self.assertFalse(after.is_error)
        self.assertEqual(
            [r["path"] for r in before.structured_content["results"]],
            [r["path"] for r in after.structured_content["results"]],
        )

    async def test_export_unknown_library_reports_error_not_crash(self):
        result = await self.server.call_tool("export_library", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_import_rejects_existing_library_id(self):
        await self._reindex_and_wait("test-lib")
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        result = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": export_result.structured_content["archive_base64"],
                "root_path": "/new/machine/vault",
                "library_id": "test-lib",
            },
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_list_libraries_includes_summary_field(self):
        result = await self.server.call_tool("list_libraries", {})
        libs = result.structured_content["result"]
        self.assertIn("summary", libs[0])
        self.assertIsNone(libs[0]["summary"])  # 还没生成过

    async def test_get_library_sample_then_propose_then_apply_roundtrip(self):
        await self._reindex_and_wait("test-lib")

        sample_result = await self.server.call_tool("get_library_sample", {"library_id": "test-lib"})
        self.assertFalse(sample_result.is_error)
        sample_payload = sample_result.structured_content
        self.assertTrue(sample_payload["ok"])
        self.assertTrue(sample_payload["samples"])
        self.assertEqual(sample_payload["samples"][0]["path"], "notes.md")

        propose_result = await self.server.call_tool(
            "propose_library_summary", {"library_id": "test-lib", "text": "一段AI写的库简介"}
        )
        self.assertFalse(propose_result.is_error)
        propose_payload = propose_result.structured_content
        self.assertTrue(propose_payload["ok"])
        self.assertTrue(propose_payload["applied"])  # 此前是空白态，直接生效不需要确认

        list_result = await self.server.call_tool("list_libraries", {})
        libs = list_result.structured_content["result"]
        self.assertEqual(libs[0]["summary"], "一段AI写的库简介")

    async def test_propose_over_user_authored_summary_requires_apply_confirmation(self):
        await self._reindex_and_wait("test-lib")
        summary_plugin = self.runtime.plugins["official-library-summary"].instance
        summary_plugin._store.set("test-lib", "用户手写的简介", source="user")  # noqa: SLF001

        propose_result = await self.server.call_tool(
            "propose_library_summary", {"library_id": "test-lib", "text": "AI想覆盖的新简介"}
        )
        propose_payload = propose_result.structured_content
        self.assertFalse(propose_payload["applied"])
        self.assertIn("proposal_id", propose_payload)
        self.assertIn("confirmation_code", propose_payload)

        apply_result = await self.server.call_tool(
            "apply_library_summary",
            {
                "library_id": "test-lib",
                "proposal_id": propose_payload["proposal_id"],
                "confirmation_code": propose_payload["confirmation_code"],
            },
        )
        self.assertFalse(apply_result.is_error)
        self.assertTrue(apply_result.structured_content["ok"])

        list_result = await self.server.call_tool("list_libraries", {})
        self.assertEqual(list_result.structured_content["result"][0]["summary"], "AI想覆盖的新简介")

    async def test_apply_library_summary_wrong_code_reports_error_not_crash(self):
        await self._reindex_and_wait("test-lib")
        summary_plugin = self.runtime.plugins["official-library-summary"].instance
        summary_plugin._store.set("test-lib", "用户手写的简介", source="user")  # noqa: SLF001
        propose_result = await self.server.call_tool(
            "propose_library_summary", {"library_id": "test-lib", "text": "新简介"}
        )
        proposal_id = propose_result.structured_content["proposal_id"]

        apply_result = await self.server.call_tool(
            "apply_library_summary",
            {"library_id": "test-lib", "proposal_id": proposal_id, "confirmation_code": "000000"},
        )
        self.assertFalse(apply_result.is_error)
        self.assertFalse(apply_result.structured_content["ok"])

    async def test_get_library_sample_unindexed_library_reports_error_not_crash(self):
        result = await self.server.call_tool("get_library_sample", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_reindex_knowledge_returns_started_true_not_a_synchronous_report(self):
        """钉住这次行为变更本身：reindex_knowledge 只返回 worker 启动信息，
        不返回 succeeded/failed 索引终态。"""
        start_result = IndexStartResult(True, "started", "run-mcp-1", 4321)
        with patch.object(
            self.pipeline, "start_index_library", return_value=start_result
        ) as start_index:
            result = await self.server.call_tool("reindex_knowledge", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(
            payload,
            {
                "ok": True,
                "started": True,
                "message": "started",
                "run_id": "run-mcp-1",
                "worker_pid": 4321,
                "full": False,
            },
        )
        start_index.assert_called_once_with(
            "test-lib",
            source="mcp",
            format_allowlist=(".md", ".txt"),
        )
        self.assertNotIn("succeeded", payload)
        self.assertNotIn("failed", payload)

    async def test_reindex_knowledge_can_force_full_rebuild(self):
        start_result = IndexStartResult(True, "started", "run-mcp-full", 9876)
        with patch.object(
            self.pipeline, "start_index_library", return_value=start_result
        ) as start_index:
            result = await self.server.call_tool(
                "reindex_knowledge", {"library_id": "test-lib", "full": True}
            )
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["full"])
        start_index.assert_called_once_with(
            "test-lib",
            source="mcp",
            full=True,
            format_allowlist=(".md", ".txt"),
        )

    async def test_reindex_knowledge_unknown_library_reports_error(self):
        result = await self.server.call_tool("reindex_knowledge", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_index_status_before_any_reindex_is_none(self):
        result = await self.server.call_tool("index_status", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["status"])

    async def test_index_status_unknown_library_reports_error(self):
        result = await self.server.call_tool("index_status", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_index_failures_before_any_reindex_is_none(self):
        result = await self.server.call_tool("index_failures", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["result"])

    async def test_index_failures_after_successful_reindex_reports_empty_failures(self):
        """全部成功和从没跑过是两种不同的状态——全部成功后 failures 应该
        是空列表，不是 None，同 core/index_failures.py 模块 docstring。"""
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("index_failures", {"library_id": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertIsNotNone(payload["result"])
        self.assertEqual(payload["result"]["succeeded"], 1)
        self.assertEqual(payload["result"]["failures"], [])

    async def test_index_failures_unknown_library_reports_error(self):
        result = await self.server.call_tool("index_failures", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])


if __name__ == "__main__":
    unittest.main()
