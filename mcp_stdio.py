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

import atexit
import multiprocessing
import os
import sys
from pathlib import Path

#: 打包后（PyInstaller）跑的是冻结的 exe，__file__ 指向的是打包器内部
#: 临时/内嵌路径，不是发行目录——这时候必须以 exe 自己的位置为准，理由
#: 同 gui_main.py 里的同名判断：plugins/ 必须是发行目录里一个真实、用户
#: 能自己增删的文件夹，不能被打包进冻结产物内部。
if getattr(sys, "frozen", False):
    REPO_ROOT = Path(sys.executable).parent
else:
    REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))

#: 数据目录：理由和 GUI/MCP 共享同一份数据的说明，见 gui_main.py 里同名
#: 常量的注释——这里不重复展开，两个入口必须保持完全一致的解析逻辑
#: （同一份数据只能有一处权威路径判定，DATA_FLOW.md 规则4的体现）。
configured_data_root = os.environ.get("RAG_REDO_DATA_ROOT")
if configured_data_root:
    DATA_ROOT = Path(configured_data_root)
elif getattr(sys, "frozen", False):
    DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RAG-Redo" / "data"
else:
    DATA_ROOT = REPO_ROOT / "data"
for _plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if _plugin_dir.is_dir():
        sys.path.insert(0, str(_plugin_dir))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402
from core.singleton import ProcessSingletonGuard  # noqa: E402

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
    "official-import-export",
    "official-visual-wemm",
    "official-ocr-mineru-cloud",
    "official-ocr-mineru-local",
    "official-dedup",
    "official-library-summary",
    "official-llm-openai-compatible",
    "official-query-enhancer-hyde",
    "official-result-advisor",
    "official-mcp-server",
]


def build_runtime() -> PluginRuntime:
    runtime = PluginRuntime(
        REPO_ROOT / "plugins",
        state_file=DATA_ROOT / "plugins_state.json",
        data_dir=DATA_ROOT,
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
    # 进程单例守卫（2026-09-23 全面功能审计发现的缺口，对齐 obsidian-rag
    # singleton.py）：AI 工具用 stdio 方式拉起 MCP 服务时，观察到过启动后
    # 短时间内连续拉起多个实例——双实例=两份 embedder/reranker 模型常驻
    # +对同一个 data/ 目录的写竞争，是真实的资源浪费和数据风险。已有存活
    # 实例时本进程直接谦让退出，不算错误。
    guard = ProcessSingletonGuard(DATA_ROOT / "server.pid")
    if not guard.acquire():
        print("检测到已有 MCP 服务实例运行，本实例退出（单例守卫）。", file=sys.stderr)
        sys.exit(0)
    atexit.register(guard.release)

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
    multiprocessing.freeze_support()
    main()
