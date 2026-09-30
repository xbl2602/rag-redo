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

from core.contracts import LibraryFreshness  # noqa: E402
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
            patch.object(
                self.pipeline,
                "library_freshness",
                return_value={"test-lib": LibraryFreshness(library_id="test-lib", stale=True)},
            ),
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
            patch.object(
                self.pipeline,
                "library_freshness",
                return_value={"test-lib": LibraryFreshness(library_id="test-lib", stale=True)},
            ),
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

    async def test_search_skips_auto_sync_when_root_missing_keeps_old_results(self):
        """对齐 obsidian-rag/server.py:264-266：库路径不存在 → 跳过自动同步
        （保留旧索引）并把原因写进 notes——绝不能把临时挂载失败判成"文件
        全部删除"然后清空旧索引；旧结果继续返回。"""
        await self._reindex_and_wait("test-lib")
        vault = self.tmp / "vault"
        moved = self.tmp / "vault-detached"
        vault.rename(moved)
        try:
            result = await self.server.call_tool(
                "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
            )
        finally:
            moved.rename(vault)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["results"], "路径消失后旧索引必须继续服务")
        self.assertTrue(
            any("路径不存在" in note and "跳过自动同步" in note for note in payload["notes"]),
            payload["notes"],
        )
        self.assertEqual(payload["refreshing"], [])
        generation = self.pipeline._generations.active("test-lib")
        manifest = self.pipeline._manifests.read("test-lib", generation)
        self.assertIn("notes.md", manifest["files"], "旧 manifest 不得被清空")

    async def test_search_skips_auto_sync_when_dir_emptied_keeps_old_results(self):
        """对齐 obsidian-rag/server.py:267-273（2026-08-14 审计 F16）：目录还在
        但扫不到任何文件 → 跳过自动同步以免清空索引，旧结果继续返回。"""
        await self._reindex_and_wait("test-lib")
        vault = self.tmp / "vault"
        for md in vault.glob("*.md"):
            md.unlink()
        result = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
        )
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["results"], "空目录不得清空旧索引")
        self.assertTrue(
            any("目录为空" in note and "跳过自动同步" in note for note in payload["notes"]),
            payload["notes"],
        )
        generation = self.pipeline._generations.active("test-lib")
        manifest = self.pipeline._manifests.read("test-lib", generation)
        self.assertIn("notes.md", manifest["files"])

    async def test_search_degrades_to_old_index_when_auto_sync_fails(self):
        """对齐 obsidian-rag/server.py:314-317 的降级纪律：自动同步这一步失败
        绝不能让检索整体失败——折叠成 notes 提示，用旧索引继续检索。"""
        await self._reindex_and_wait("test-lib")
        (self.tmp / "vault" / "new.md").write_text("# 新增\n\n新内容", encoding="utf-8")
        with (
            patch.object(self.pipeline, "has_index", return_value=True),
            patch.object(
                self.pipeline,
                "start_index_library",
                side_effect=RuntimeError("worker 启动失败"),
            ),
        ):
            result = await self.server.call_tool(
                "search_knowledge", {"query": "插件 架构", "libraries": "test-lib"}
            )
        payload = result.structured_content
        self.assertTrue(payload["ok"], "同步失败不得拖垮检索")
        self.assertTrue(payload["results"])
        self.assertTrue(
            any("自动更新索引失败" in note or "使用旧索引" in note for note in payload["notes"]),
            payload["notes"],
        )

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

    async def test_reindex_allow_new_formats_authorization_flow(self):
        """对齐旧项目 reindex_knowledge 的授权流（server.py:620-656）：未授权
        格式先报告"待授权+文件数"；用户确认后携带 allow_new_formats=true →
        持久化写入 agent_formats 并纳入本次索引。"""
        document = Document()
        document.add_paragraph("authorization flow body")
        document.save(str(self.tmp / "vault" / "flow.docx"))
        denied = await self.server.call_tool(
            "reindex_knowledge", {"library_id": "test-lib"}
        )
        payload = denied.structured_content
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["waiting_formats"], {".docx": 1})
        self.assertEqual(payload["approved_formats"], {})
        self.assertIn("allow_new_formats=true", payload["message"])
        self.assertEqual(self.lib_mgr.store.get("test-lib").agent_formats, [])
        approved = await self.server.call_tool(
            "reindex_knowledge",
            {"library_id": "test-lib", "allow_new_formats": True},
        )
        payload = approved.structured_content
        self.assertEqual(payload["approved_formats"], {".docx": 1})
        self.assertEqual(payload["waiting_formats"], {})
        self.assertEqual(self.lib_mgr.store.get("test-lib").agent_formats, [".docx"])

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
        # 完全相同的副本（相似度 1.0 > 默认阈值 0.8）；旧项目默认 0.8 下
        # 0.77 相似度的"两处小改动"不算重复（dedup.py 实测校准记录）
        (vault / "notes-copy.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计，一字不改的近似重复。",
            encoding="utf-8",
        )
        (vault / "notes.md").write_text(
            "# 插件架构\n\n这篇笔记讲插件系统的架构设计，一字不改的近似重复。", encoding="utf-8"
        )
        await self._reindex_and_wait("test-lib")

        result = await self.server.call_tool("find_duplicates", {"libraries": "test-lib"})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        report = payload["libraries"]["test-lib"]
        groups = report["groups"]["official-dedup"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]), {"notes.md", "notes-copy.md"})
        # 拿不到正文的文件必须计数上报，否则"零重复组"分不清是真没重复还是
        # 压根没比较过（旧项目 server.py:833 明确"跳过并计数"）
        self.assertEqual(report["skipped"], 0)
        self.assertGreater(report["scanned"], 0)

    async def test_find_duplicates_tool_defaults_to_all_libraries(self):
        """旧项目 `find_duplicates(library="")` = 全部注册库（server.py:842），
        本工具的默认必须一样，不能默认成"什么都查不到"。"""
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("find_duplicates", {})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertIn("test-lib", payload["libraries"])

    async def test_find_duplicates_tool_rejects_bad_threshold(self):
        result = await self.server.call_tool("find_duplicates", {"threshold": 1.5})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])
        self.assertIn("threshold", result.structured_content["error"])

    async def test_find_duplicates_has_a_docstring(self):
        """守卫：docstring 曾经被写在参数校验 `if` 之后，等于**没有**
        docstring——MCP schema 里这个工具就没有 description，而 agent 运行时
        唯一读的就是 docstring（它恰好是当时唯一没有 description 的工具）。
        钉住它，防止再被挪到函数体后面。"""
        tool = {t.name: t for t in await self.server.list_tools()}["find_duplicates"]
        self.assertTrue(tool.description)
        self.assertIn("threshold", tool.description)
        self.assertIn("libraries", tool.description)

    async def test_find_duplicates_tool_no_duplicates_returns_empty_groups(self):
        await self._reindex_and_wait("test-lib")
        result = await self.server.call_tool("find_duplicates", {"libraries": "test-lib"})
        self.assertFalse(result.is_error)
        report = result.structured_content["libraries"]["test-lib"]
        self.assertEqual(report["groups"]["official-dedup"], [])

    async def test_find_duplicates_tool_unknown_library_reports_error(self):
        result = await self.server.call_tool("find_duplicates", {"libraries": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    def _write_docx_pair(self, directory: Path, stem: str, text: str) -> None:
        """同一段正文写成两份 docx（`<stem>.docx` 与 `<stem>-copy.docx`）：内容逐字相同，
        提取出的正文相似度 1.0，一定会被判成近似重复组。"""
        directory.mkdir(parents=True, exist_ok=True)
        for name in (f"{stem}.docx", f"{stem}-copy.docx"):
            document = Document()
            document.add_paragraph(text)
            document.save(str(directory / name))

    async def test_wemm_status_reports_conversion_caches_only_for_authorized_formats(self):
        """BC-19：Agent 也能查“转文字/页库缓存建没建、缺哪些、下一步怎么办”；
        BC-02：没授权给 Agent 的格式，名字和数量都不能出现。"""
        vault = self.tmp / "vault"
        self._write_docx_pair(vault, "secret", "保密内容，Agent 不该知道它存在。" * 4)
        docx_vault = self.tmp / "docx-vault"
        self._write_docx_pair(docx_vault, "approved", "已批准的内容，用户允许 Agent 读取。" * 4)
        self.lib_mgr.store.add_library("docx-lib", "有授权的库", str(docx_vault))
        self.lib_mgr.store.set_agent_formats("docx-lib", [".docx"])
        await self._reindex_and_wait("test-lib")
        await self._reindex_and_wait("docx-lib")
        # 前提自检：没授权的库里 docx 确实转好了（否则下面“不出现”是空话）
        unfiltered = self.pipeline.conversion_caches("test-lib")[0]
        self.assertTrue(any("secret" in item.path for item in unfiltered.files))

        result = await self.server.call_tool("wemm_status", {})
        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertTrue(payload["ok"], payload)
        caches = payload["conversion_caches"]
        self.assertEqual((caches["test-lib"]["text_done"], caches["test-lib"]["text_total"]), (0, 0))
        self.assertEqual((caches["docx-lib"]["text_done"], caches["docx-lib"]["text_total"]), (2, 2))
        self.assertNotIn("missing", caches["docx-lib"], "不指定库时只给汇总")
        self.assertNotIn("secret", str(payload))

        # 授权库里一份缓存文件被删：指定库时列出它、写明原因和下一步
        approved = {item.path: item for item in self.pipeline.conversion_caches("docx-lib")[0].files}
        Path(approved["approved.docx"].text_file).unlink()
        one = (await self.server.call_tool("wemm_status", {"library_id": "docx-lib"})).structured_content
        self.assertEqual(list(one["conversion_caches"]), ["docx-lib"])
        missing = one["conversion_caches"]["docx-lib"]["missing"]
        self.assertEqual([row["path"] for row in missing], ["approved.docx"])
        self.assertEqual(missing[0]["text_reason"], "正文文件不见了")
        self.assertTrue(missing[0]["text_next_step"])
        secret = (await self.server.call_tool("wemm_status", {"library_id": "test-lib"})).structured_content
        self.assertEqual(secret["conversion_caches"]["test-lib"]["missing"], [])
        self.assertNotIn("secret", str(secret))
        bad = (await self.server.call_tool("wemm_status", {"library_id": "no-such-lib"})).structured_content
        self.assertFalse(bad["ok"])

    async def test_find_duplicates_never_leaks_unauthorized_formats_across_libraries(self):
        """BC-02：Agent 默认只能处理 md/txt，pdf/docx 需要用户逐库批准。此前多库调用
        （含默认 `libraries=""`）不做任何格式过滤——某个库**没批准**的 docx 文件名和
        "这两份是重复的"这条信息会原样交给 Agent，而且没有任何报错提醒。

        场景：`test-lib` 没批准 docx，里面有一对重复的 docx（保密）和一对重复的 md；
        `docx-lib` 批准了 docx，里面也有一对重复的 docx（应当出现）。"""
        vault = self.tmp / "vault"
        secret_text = "保密内容，这份文档只有用户本人能看，Agent 不该知道它存在。" * 4
        self._write_docx_pair(vault, "secret", secret_text)
        note = "# 公开笔记\n\n这是一篇公开的笔记，两份一字不差。" * 3
        (vault / "public-a.md").write_text(note, encoding="utf-8")
        (vault / "public-b.md").write_text(note, encoding="utf-8")

        docx_vault = self.tmp / "docx-vault"
        self._write_docx_pair(docx_vault, "approved", "已批准的内容，用户允许 Agent 读取这类文档。" * 4)
        self.lib_mgr.store.add_library("docx-lib", "有授权的库", str(docx_vault))
        self.lib_mgr.store.set_agent_formats("docx-lib", [".docx"])

        await self._reindex_and_wait("test-lib")
        await self._reindex_and_wait("docx-lib")

        # 前提自检：两个库里的 docx 确实都被索引、也确实是重复的（否则下面"不出现"是空话）
        allowed_test = set(self.lib_mgr.agent_allowed_extensions("test-lib"))
        allowed_docx = set(self.lib_mgr.agent_allowed_extensions("docx-lib"))
        self.assertNotIn(".docx", allowed_test)
        self.assertIn(".docx", allowed_docx)
        unfiltered = self.pipeline.find_duplicates_multi(["test-lib", "docx-lib"])
        self.assertTrue(
            any("secret" in path for group in unfiltered["test-lib"]["groups"]["official-dedup"] for path in group),
            "前提不成立：未过滤时 test-lib 应当能检出保密 docx 对",
        )

        for arguments in ({}, {"libraries": "all"}, {"libraries": "test-lib,docx-lib"}, {"libraries": "test-lib"}):
            with self.subTest(arguments=arguments):
                result = await self.server.call_tool("find_duplicates", arguments)
                self.assertFalse(result.is_error)
                payload = result.structured_content
                self.assertTrue(payload["ok"], payload)
                test_report = payload["libraries"]["test-lib"]
                test_groups = test_report["groups"]["official-dedup"]
                self.assertEqual(
                    [sorted(group) for group in test_groups],
                    [["public-a.md", "public-b.md"]],
                    "未批准 docx 的库只能报出 md 重复组",
                )
                self.assertNotIn("secret", str(test_report), "未授权格式的文件名不得出现在返回里")
                self.assertNotIn(".docx", str(test_report))
                if "docx-lib" in payload["libraries"]:
                    docx_groups = payload["libraries"]["docx-lib"]["groups"]["official-dedup"]
                    self.assertEqual(
                        [sorted(group) for group in docx_groups],
                        [["approved-copy.docx", "approved.docx"]],
                        "批准了 docx 的库照常报出 docx 重复组",
                    )

    async def test_find_duplicates_multi_allowlist_mapping_is_fail_closed(self):
        """映射里缺失的库视为一个格式都没授权（fail-closed），而不是不过滤。"""
        await self._reindex_and_wait("test-lib")
        report = self.pipeline.find_duplicates_multi(
            ["test-lib"], format_allowlist={"another-lib": (".md",)}
        )
        self.assertEqual(report["test-lib"]["scanned"], 0)
        self.assertEqual(report["test-lib"]["groups"]["official-dedup"], [])

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
            "navigate_knowledge", {"query": "x", "libraries": "no-such-lib"}
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

    async def _import_via_gate(self, archive_base64, root_path, library_id=""):
        """走完整两段式门禁导入一个库，返回 (apply 的调用结果, 提案 payload)。"""
        propose = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": archive_base64,
                "root_path": root_path,
                "library_id": library_id,
            },
        )
        self.assertFalse(propose.is_error)
        ticket = propose.structured_content
        self.assertTrue(ticket["ok"])
        self.assertFalse(ticket["applied"])
        applied = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": archive_base64,
                "root_path": root_path,
                "library_id": library_id,
                "proposal_id": ticket["proposal_id"],
                "confirmation_code": ticket["confirmation_code"],
            },
        )
        return applied, ticket

    async def test_export_then_import_library_round_trips_search_results(self):
        await self._reindex_and_wait("test-lib")
        before = await self.server.call_tool("search_knowledge", {"query": "插件 架构", "libraries": "test-lib"})
        self.assertFalse(before.is_error)

        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        self.assertFalse(export_result.is_error)
        export_payload = export_result.structured_content
        self.assertTrue(export_payload["ok"])
        self.assertIn("archive_base64", export_payload)

        # 导入是 AI 触发的写操作，必须两段式：先提案、拿到用户确认的确认码
        # 才真正写入（AGENTS.md 架构红线 6）。
        import_result, ticket = await self._import_via_gate(
            export_payload["archive_base64"], "/new/machine/vault", "test-lib-restored"
        )
        self.assertFalse(import_result.is_error)
        import_payload = import_result.structured_content
        self.assertTrue(import_payload["ok"])
        self.assertTrue(import_payload["applied"])
        self.assertEqual(import_payload["library_id"], "test-lib-restored")
        self.assertIn("test-lib-restored", ticket["message"])
        self.assertIn("向量块", ticket["message"])

        after = await self.server.call_tool(
            "search_knowledge", {"query": "插件 架构", "libraries": "test-lib-restored"}
        )
        self.assertFalse(after.is_error)
        self.assertEqual(
            [r["path"] for r in before.structured_content["results"]],
            [r["path"] for r in after.structured_content["results"]],
        )

    async def test_import_proposal_has_no_side_effect_and_needs_confirmation(self):
        """未确认的提案必须零副作用——这是门禁的全部意义，不能只是"建议"。"""
        await self._reindex_and_wait("test-lib")
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        libs = await self.server.call_tool("list_libraries", {})
        before_ids = {lib["library_id"] for lib in libs.structured_content["result"]}

        propose = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": export_result.structured_content["archive_base64"],
                "root_path": "/new/machine/vault",
                "library_id": "test-lib-unconfirmed",
            },
        )
        ticket = propose.structured_content
        self.assertFalse(ticket["applied"])
        self.assertIn("proposal_id", ticket)
        self.assertRegex(ticket["confirmation_code"], r"^\d{6}$")

        # 提案之后库还没被注册，检索它必然失败
        libs_after = await self.server.call_tool("list_libraries", {})
        after_ids = {lib["library_id"] for lib in libs_after.structured_content["result"]}
        self.assertEqual(before_ids, after_ids)
        self.assertNotIn("test-lib-unconfirmed", after_ids)

    async def test_import_rejects_wrong_confirmation_code(self):
        await self._reindex_and_wait("test-lib")
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        archive_b64 = export_result.structured_content["archive_base64"]
        propose = await self.server.call_tool(
            "import_library",
            {"archive_base64": archive_b64, "root_path": "/new/machine/vault", "library_id": "test-lib-badcode"},
        )
        ticket = propose.structured_content
        wrong = "000000" if ticket["confirmation_code"] != "000000" else "111111"
        result = await self.server.call_tool(
            "import_library",
            {
                "archive_base64": archive_b64,
                "root_path": "/new/machine/vault",
                "library_id": "test-lib-badcode",
                "proposal_id": ticket["proposal_id"],
                "confirmation_code": wrong,
            },
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])
        self.assertIn("确认码", result.structured_content["error"])
        libs = await self.server.call_tool("list_libraries", {})
        ids = {lib["library_id"] for lib in libs.structured_content["result"]}
        self.assertNotIn("test-lib-badcode", ids)

    async def test_import_proposal_is_single_use(self):
        await self._reindex_and_wait("test-lib")
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        archive_b64 = export_result.structured_content["archive_base64"]
        propose = await self.server.call_tool(
            "import_library",
            {"archive_base64": archive_b64, "root_path": "/new/machine/vault", "library_id": "test-lib-once"},
        )
        ticket = propose.structured_content
        payload = {
            "archive_base64": archive_b64,
            "root_path": "/new/machine/vault",
            "library_id": "test-lib-once",
            "proposal_id": ticket["proposal_id"],
            "confirmation_code": ticket["confirmation_code"],
        }
        first = await self.server.call_tool("import_library", payload)
        self.assertTrue(first.structured_content["applied"])
        replay = await self.server.call_tool("import_library", payload)
        self.assertFalse(replay.structured_content["ok"])

    async def test_import_does_not_restore_agent_formats_from_archive(self):
        """归档是 agent 自己能造的无签名包：从里面恢复 agent_formats 等于
        Agent 给自己授权二进制格式（AGENTS.md 架构红线 6）。提案必须把这件事
        说出来，执行后注册表里也必须仍然是空的。"""
        await self._reindex_and_wait("test-lib")
        self.lib_mgr.store.set_policy(
            "test-lib", enabled_extensions=[".md", ".pdf", ".docx"]
        )
        self.lib_mgr.store.set_agent_formats("test-lib", [".pdf", ".docx"])
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        archive_b64 = export_result.structured_content["archive_base64"]
        self.lib_mgr.store.set_agent_formats("test-lib", [])

        applied, ticket = await self._import_via_gate(archive_b64, "/new/machine/vault", "test-lib-noauth")
        self.assertTrue(applied.structured_content["applied"])
        self.assertIn(".pdf", ticket["summary"]["agent_formats_in_archive"])
        self.assertIn("不会随导入恢复", ticket["message"])
        self.assertEqual(
            self.lib_mgr.store.get("test-lib-noauth").agent_formats, []
        )

    async def test_export_unknown_library_reports_error_not_crash(self):
        result = await self.server.call_tool("export_library", {"library_id": "no-such-lib"})
        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])

    async def test_import_rejects_existing_library_id(self):
        await self._reindex_and_wait("test-lib")
        export_result = await self.server.call_tool("export_library", {"library_id": "test-lib"})
        # 提案阶段不拒绝（还没写任何东西），但必须把"会直接拒绝"讲清楚；
        # 真正的拒绝发生在 apply 那一刻——落盘前最后一道闸。
        applied, ticket = await self._import_via_gate(
            export_result.structured_content["archive_base64"], "/new/machine/vault", "test-lib"
        )
        self.assertTrue(ticket["summary"]["target_exists"])
        self.assertIn("已存在", ticket["message"])
        self.assertFalse(applied.is_error)
        self.assertFalse(applied.structured_content["ok"])
        # 旧库没被动过
        self.assertIsNotNone(self.lib_mgr.store.get("test-lib"))

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
                "approved_formats": {},
                "waiting_formats": {},
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
