"""MCP 工具定义：把 Pipeline / library-manager 的能力包装成 MCP tool。

只定义"给定 server/pipeline/lib_mgr，注册哪些工具"，不关心 stdio/http
传输——那是仓库根目录 mcp_stdio.py（真正的可执行入口）的事。这个模块可以
在不启动任何 stdio 服务的情况下被直接测试（见 ../tests/test_tools.py）。
"""
from __future__ import annotations

from typing import Any

from core.pipeline import Pipeline


def register_tools(server, pipeline: Pipeline, lib_mgr) -> None:
    @server.tool()
    def search_knowledge(query: str, library_id: str, top_k: int = 5) -> dict[str, Any]:
        """在指定库里做混合检索（词法 BM25 + 向量 + RRF 融合 + 重排），返回最相关的片段。

        实测过：MCP SDK（本项目锁定版本 2.2.0）的工具函数里裸抛异常
        （比如 library_id 打错触发的 KeyError）不会被自动折叠成一个干净
        的 is_error 结果——会原样往上炸。AI agent 调用这类工具时打错参数
        是完全正常会发生的事，不能让它变成服务端异常，所以这里显式
        try/except，把"库不存在"这类可预期的失败折叠成 {"ok": False,
        "error": ...} 返回，绝不裸抛（同 official-gui-shell 的 Api 类
        用的是一模一样的防御模式，两边都是"给同一个 Pipeline 包一层协议
        外壳"，错误处理纪律也该一致）。

        Args:
            query: 查询文本
            library_id: 要搜索的库的 id（用 list_libraries 查看有哪些库）
            top_k: 最多返回几条结果
        """
        try:
            results = pipeline.search(library_id, query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - 见上方 docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "results": [
                {
                    "path": r.path,
                    "heading": r.heading_breadcrumb,
                    "text": r.text,
                    "confidence": round(r.confidence, 3),
                }
                for r in results
            ],
        }

    @server.tool()
    def list_libraries() -> list[dict]:
        """列出所有已注册的库及其基本信息。"""
        return [
            {"library_id": cfg.library_id, "name": cfg.name, "root_path": cfg.root_path}
            for cfg in lib_mgr.store.list_libraries()
        ]

    @server.tool()
    def reindex_knowledge(library_id: str) -> dict[str, Any]:
        """重新索引指定的库。

        注：MCP SDK 对裸 `dict` 返回类型标注推不出结构化输出 schema
        （实测 `structured_content` 会是 None，只能从 content[0].text
        里解析JSON），必须写成 `dict[str, Any]` 这种带参数的形式——这是
        真实踩过的坑，不是随手加的类型标注。异常处理策略同
        search_knowledge，见其 docstring。

        Phase 1 现状是全量重跑（不做"内容没变就跳过"的增量判断——虽然
        ExtractedDocument 已经带 content_hash，真正的增量跳过逻辑要等
        docs/LESSONS.md 第3条的版本号机制落地后再接，这里先如实说明，
        不假装已经支持增量）。

        Args:
            library_id: 要重建索引的库的 id
        """
        try:
            report = pipeline.index_library(library_id)
        except Exception as exc:  # noqa: BLE001 - 见 search_knowledge docstring
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "library_id": library_id,
            "succeeded": report.succeeded,
            "failed": report.failed,
            "failures": [
                {"path": f.path, "reason": f.extract_failure}
                for f in report.files
                if f.included and not f.extracted
            ],
        }
