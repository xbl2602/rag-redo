#!/usr/bin/env python3
"""桌面 GUI 入口——双击/命令行启动这个文件打开窗口。

和 mcp_stdio.py 是同一种"胶水脚本"模式：不含任何 RAG 逻辑，只做三件事：
①启动插件运行时、扫描并启用官方插件集；②把 Pipeline / library-manager
交给 official-gui-shell 插件构造出 js_api 桥（Api 类）；③用 pywebview
打开窗口，加载插件自带的 index.html。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: 打包后（PyInstaller）跑的是冻结的 exe，__file__ 指向的是打包器内部
#: 临时/内嵌路径，不是发行目录——这时候必须以 exe 自己的位置为准，因为
#: plugins/ 按设计必须是发行目录里一个真实、用户能自己增删的文件夹（见
#: AGENTS.md"这个项目不是什么"一节：不做插件市场，用户手动获取插件文件
#: 夹），不能被打包进冻结产物内部。
if getattr(sys, "frozen", False):
    REPO_ROOT = Path(sys.executable).parent
else:
    REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))
for _plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if _plugin_dir.is_dir():
        sys.path.insert(0, str(_plugin_dir))

import webview  # noqa: E402

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
    "official-import-export",
    "official-gui-shell",
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
    gui_plugin = runtime.plugins.get("official-gui-shell")
    if lib_mgr_plugin is None or gui_plugin is None or gui_plugin.instance is None:
        print("致命错误：library-manager 或 gui-shell 插件未能启用，无法打开界面", file=sys.stderr)
        sys.exit(1)

    api = gui_plugin.instance.make_api(pipeline, lib_mgr_plugin.instance)
    html_path = REPO_ROOT / "plugins" / "official-gui-shell" / "official_gui_shell" / "assets" / "index.html"

    webview.create_window("RAG REDO", url=str(html_path), js_api=api, width=1100, height=720, background_color="#0f1115")
    webview.start()


if __name__ == "__main__":
    main()
