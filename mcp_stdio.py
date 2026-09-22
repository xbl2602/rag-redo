#!/usr/bin/env python3
"""MCP 服务入口——AI 客户端（Claude Code / opencode 等）按 stdio 方式启动
这个文件。配置示例（放进对应 AI 工具的 MCP 配置里）：

{
  "type": "stdio",
  "command": "/path/to/rag-redo/.venv/bin/python",
  "args": ["mcp_stdio.py"],
  "cwd": "/path/to/rag-redo"
}

这个脚本本身不含任何 RAG 逻辑——它只做三件事：①启动插件运行时、扫描并
启用官方插件集；②把 Pipeline / library-manager 交给 official-mcp-server
插件去注册工具；③把 MCPServer 跑起来。真正的检索/索引逻辑全在各个
official-* 插件里，这个文件只是"胶水"，对应 docs/PLUGIN_SPEC.md 说的
"MCP 是插件，不是核心"这条设计。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))
for _plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if _plugin_dir.is_dir():
        sys.path.insert(0, str(_plugin_dir))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402

#: MCP 工具实际需要的官方插件集——不含 GUI（gui-shell 目前还不存在，
#: 就算存在，MCP 服务这条路径也用不上它，证明插件之间真的没有硬编码
#: 依赖，见 docs/ROADMAP.md Phase 1 验收标准）。
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
    "official-mcp-server",
]


def build_runtime() -> PluginRuntime:
    runtime = PluginRuntime(
        REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data" / "plugins_state.json",
        data_dir=REPO_ROOT / "data",
    )
    runtime.scan()
    for plugin_id in REQUIRED_PLUGINS:
        if plugin_id not in runtime.plugins:
            print(f"警告：插件 {plugin_id} 未发现，相关能力会缺失", file=sys.stderr)
            continue
        runtime.load(plugin_id)
        runtime.enable(plugin_id)
        state = runtime.plugins[plugin_id]
        if state.state.value == "failed":
            print(f"警告：插件 {plugin_id} 启用失败: {state.error}", file=sys.stderr)
    return runtime


def main() -> None:
    runtime = build_runtime()
    pipeline = Pipeline(runtime)
    lib_mgr_plugin = runtime.plugins.get("official-library-manager")
    mcp_plugin = runtime.plugins.get("official-mcp-server")
    if lib_mgr_plugin is None or mcp_plugin is None or mcp_plugin.instance is None:
        print("致命错误：library-manager 或 mcp-server 插件未能启用，无法提供服务", file=sys.stderr)
        sys.exit(1)

    server = MCPServer(name="rag-redo", version="0.1.0", instructions="本地 Obsidian 笔记语义检索")
    mcp_plugin.instance.register_tools(server, pipeline, lib_mgr_plugin.instance)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
