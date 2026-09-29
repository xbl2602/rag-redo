#!/usr/bin/env python3
"""桌面 GUI 入口——双击/命令行启动这个文件打开窗口。

和 mcp_stdio.py 是同一种"胶水脚本"模式：不含任何 RAG 逻辑，只做几件事：①启动插件
运行时、扫描并启用官方插件集；②把 Pipeline / library-manager 交给 official-gui-shell
插件构造出 js_api 桥（Api 类）；③用 pywebview 打开窗口、把窗口句柄交给桥（原生选择
弹窗与推送都靠它）、起 1 秒状态推送线程。对应旧项目 `guiweb/app.py::main`。
"""
from __future__ import annotations

import atexit
import multiprocessing
import os
import sys
import threading
from pathlib import Path

#: 打包后（PyInstaller）跑的是冻结的 exe，__file__ 指向的是打包器内部临时/内嵌路径，
#: 不是发行目录——这时候必须以 exe 自己的位置为准，因为 plugins/ 按设计必须是发行目录里
#: 一个真实、用户能自己增删的文件夹（见 AGENTS.md"这个项目不是什么"一节：不做插件市场，
#: 用户手动获取插件文件夹），不能被打包进冻结产物内部。
#: 这里只为把仓库根塞进 sys.path（导入 core.paths 之前的自举）；数据目录与插件目录的
#: 规则统一在 `core/paths.py`，三个入口（GUI/MCP/CLI）共用同一份。
if getattr(sys, "frozen", False):
    _BOOTSTRAP_ROOT = Path(sys.executable).parent
else:
    _BOOTSTRAP_ROOT = Path(__file__).parent
sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from core import paths  # noqa: E402

REPO_ROOT = paths.repo_root()
#: 数据目录：`RAG_REDO_DATA_ROOT` 环境变量 > 打包安装态 `%LOCALAPPDATA%/RAG-Redo/data` >
#: 源码运行态 `<仓库根>/data`。GUI 和 MCP 两个冻结产物各自安装在不同文件夹，但都指向
#: 这同一个 DATA_ROOT，这样两边操作的是同一批库，不是各自维护一份互不相通的数据。
DATA_ROOT = paths.data_root()
for _plugin_dir in (REPO_ROOT / "plugins").glob("*"):
    if _plugin_dir.is_dir():
        sys.path.insert(0, str(_plugin_dir))

import webview  # noqa: E402

from core.pipeline import Pipeline  # noqa: E402
from core.runtime import PluginRuntime  # noqa: E402
from core.singleton import ProcessSingletonGuard  # noqa: E402

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
    "official-gui-shell",
]

#: 窗口规格：与旧项目 `guiweb/app.py::main` 的 `create_window` 一致，冻结在
#: `plugins/official-gui-shell/tests/fixtures/legacy_guiweb_contract.json` 的 `window`
#: 段里（`test_boot.py` 逐项对账）。
WINDOW_SPEC = {
    "title": "Obsidian RAG",
    "width": 1440,
    "height": 900,
    "min_size": (1080, 700),
    "background_color": "#050505",
}

_BAD_PLUGIN_STATES = ("failed", "invalid")

#: subprocess_service 插件——on_enable 会真的 Popen 一个子进程并阻塞轮询
#: health_check（默认最多 10s）。REQUIRED_PLUGINS 按声明顺序同步 enable 时，
#: 这两个在 WEMM 后端默认开、data-real 又配了 `pdf_scan_backend=mineru-local`
#: 的真机上会串行各卡住几秒，窗口在 `_serve()` 创建之前完全打不开——2026-
#: 09-29 用户真实反馈"先黑屏几秒才出界面"，这是主要来源之一（另一半是
#: Popen/nvidia-smi 缺 CREATE_NO_WINDOW 导致的黑屏闪窗，已在
#: core/subprocess_service.py / core/gpu_arbiter.py 修）。这两个插件仍然在
#: `build_runtime()` 里 `load()`（把 provider 注册进 registry、给
#: Pipeline.__init__ 用，成本很低），只是把 `enable()`——真正拉子进程那一步
#: ——挪到窗口已经打开之后，用一个后台线程跑（见 `_serve` 和
#: `_enable_deferred_plugins`）。旧项目 guiweb/app.py::main 本来就是"窗口先
#: 出、后端按需/后台就绪"（Bridge() 构造不碰任何子进程），这里是把 rag-redo
#: 对齐回这个已验证过的旧行为，不是新发明的产品语义。
#: MCP（mcp_stdio.py）和 CLI（core/cli.py）两个入口没有"窗口"这个概念、也
#: 不存在"用户盯着黑屏等"的体验问题，故意不做同样的延后——只改 GUI 这一个
#: 入口，符合"只改完成该行为所需的最小范围"。
DEFERRED_PLUGIN_IDS = ("official-visual-wemm", "official-ocr-mineru-local")


def _enable_and_warn(runtime: PluginRuntime, plugin_id: str) -> None:
    """`runtime.enable()` 之后按状态打警告——`build_runtime()` 的同步路径
    和 `_enable_deferred_plugins` 的后台路径共用同一份判断，不允许出现
    "这两条路径分别写一遍、判断标准慢慢漂开"的重复实现。"""
    runtime.enable(plugin_id)
    state = runtime.plugins[plugin_id]
    if state.state.value in _BAD_PLUGIN_STATES:
        print(f"警告：插件 {plugin_id} {state.state.value}: {state.error}", file=sys.stderr)


def _enable_deferred_plugins(runtime: PluginRuntime) -> None:
    """后台线程体：窗口已经打开之后，才真正启用 `DEFERRED_PLUGIN_IDS`
    这两个 subprocess_service 插件（拉子进程、等 health_check）。

    调用方（`_serve`）必须在 `webview.start()` 返回之后、`runtime.close()`
    之前 `join()` 这个线程——`PluginRuntime.close()` 只处理状态已经是
    "enabled"/"disabled" 的插件，如果这个线程还卡在某个插件的 on_enable
    里没跑完，close() 会直接跳过它（状态还是 "loaded"），子进程就成了没人
    收的游离进程。不 join 就调 close() 是真实会漏杀子进程的竞态，不是
    理论风险。"""
    for plugin_id in DEFERRED_PLUGIN_IDS:
        if plugin_id in runtime.plugins:
            _enable_and_warn(runtime, plugin_id)


def _ensure_stdio() -> None:
    """标准流兜底（旧 guiweb/app.py 文件头："必须在导入会 print 的模块之前"）。

    pythonw / 打包成无控制台的 exe 时 `sys.stdout/stderr` 是 None，任何 `print` 直接
    AttributeError；部分终端代码页是 charmap 家族，中文 `print` 直接 UnicodeEncodeError——
    两者都会炸穿 js_api 调用。所以：流不存在 → 重定向到数据目录下的 utf-8 日志文件；流存在
    但编码不是 utf-8 → 原地改成 utf-8（errors=replace），仍然输出到原来的终端。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            try:
                DATA_ROOT.mkdir(parents=True, exist_ok=True)
                setattr(sys, name, open(DATA_ROOT / "gui_stdio.log", "a", buffering=1,
                                        encoding="utf-8", errors="replace"))
            except OSError:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))
        elif hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def build_runtime() -> PluginRuntime:
    runtime = PluginRuntime(
        REPO_ROOT / "plugins",
        state_file=paths.plugins_state_file(DATA_ROOT),
        data_dir=DATA_ROOT,
    )
    runtime.scan()
    for plugin_id in REQUIRED_PLUGINS:
        if plugin_id not in runtime.plugins:
            print(f"警告：插件 {plugin_id} 未发现，相关能力会缺失", file=sys.stderr)
            continue
        runtime.load(plugin_id)
        if plugin_id in DEFERRED_PLUGIN_IDS:
            # 只 load（注册 provider、给 Pipeline.__init__ 用），真正 enable
            # （拉子进程）推迟到窗口打开之后——见 DEFERRED_PLUGIN_IDS 的说明。
            continue
        # invalid（被运行时的导入边界/能力边界校验拒绝）和 failed（加载/启用抛异常）同样要说：
        # 此前只对 failed 告警，invalid 被静默放过，GUI 插件因此"看似启动、实则没加载"。
        _enable_and_warn(runtime, plugin_id)
    return runtime


def main() -> None:
    # 进程单例守卫（对齐 obsidian-rag gui/app.py:46-69 与 guiweb/app.py:46-87 的
    # _acquire_singleton：两个 GUI 入口都有非阻塞文件锁，锁文件名区分实例；MCP 侧同款
    # 守卫在 mcp_stdio.py::main）。双击两次/重复启动第二个 GUI = 两份 embedder/reranker
    # 模型常驻 + 对同一 data/ 目录的写竞争；已有存活实例时本进程直接谦让退出，不算错误。
    guard = ProcessSingletonGuard(DATA_ROOT / "gui.pid")
    if not guard.acquire():
        print("检测到已有 GUI 实例运行，本实例退出（单例守卫）。", file=sys.stderr)
        sys.exit(0)
    atexit.register(guard.release)

    runtime = build_runtime()
    try:
        _serve(runtime)
    finally:
        # 关窗（或必需插件起不来而退出）时收口整个运行时：插件拉起的子进程（页级视觉的看图
        # 服务等）不会跟着父进程一起走——Windows 上父进程退出不会带走子进程——不在这里回收，
        # 就是每开关一次窗口留下一对孤儿进程（2026-09-28 真实进程冒烟复现）。CLAUDE.md §4.3：
        # 子进程必须能被停止、等待、回收，不能留下进程树残留。
        runtime.close()


def _serve(runtime: PluginRuntime) -> None:
    pipeline = Pipeline(runtime)
    # 界面必需的两个插件必须真的处于"已启用"——只看 instance 非空不够：instance 在加载
    # 成功后就有了，而校验失败（invalid）的插件根本不会有 instance，启用失败（failed）的
    # 有 instance 却不可用。这里统一按状态判，并把真实原因打出来，别让用户对着一个
    # 一闪而过的窗口猜。
    for plugin_id in ("official-library-manager", "official-gui-shell"):
        plugin = runtime.plugins.get(plugin_id)
        if plugin is None or plugin.instance is None or plugin.state.value != "enabled":
            state = "未发现" if plugin is None else plugin.state.value
            reason = "" if plugin is None or not plugin.error else f"（{plugin.error}）"
            print(f"致命错误：插件 {plugin_id} 未能启用：{state}{reason}，无法打开界面", file=sys.stderr)
            sys.exit(1)
    lib_mgr_plugin = runtime.plugins["official-library-manager"]
    gui_plugin = runtime.plugins["official-gui-shell"]

    api = gui_plugin.instance.make_api(pipeline, lib_mgr_plugin.instance)
    html_path = REPO_ROOT / "plugins" / "official-gui-shell" / "official_gui_shell" / "assets" / "index.html"

    window = webview.create_window(
        WINDOW_SPEC["title"],
        url=str(html_path),
        js_api=api,
        width=WINDOW_SPEC["width"],
        height=WINDOW_SPEC["height"],
        min_size=WINDOW_SPEC["min_size"],
        background_color=WINDOW_SPEC["background_color"],
    )
    # 窗口句柄交给桥：pick_path 的原生对话框和推送线程的 evaluate_js 都靠它。
    api.bind_window(window)
    _push_thread, stop_push = gui_plugin.instance.start_push_loop(api, window)
    # 窗口已经建好、推送也起了，这时候才真正启用 WEMM/MinerU-local（拉子
    # 进程、等 health_check）——用户不用盯着黑屏等这两个服务起来，界面先能
    # 看、能翻库，这两个服务在后台追上（对齐旧 guiweb 的"窗口先出"体验）。
    deferred_thread = threading.Thread(
        target=_enable_deferred_plugins, args=(runtime,), name="gui-deferred-enable",
    )
    deferred_thread.start()
    try:
        # RAG_GUIWEB_DEBUG=1 开 DevTools（右键→检查），排障用（旧 guiweb 同名开关）
        webview.start(debug=os.environ.get("RAG_GUIWEB_DEBUG") == "1")
    finally:
        stop_push.set()
        # 必须在 main() 的 finally 调 runtime.close() 之前等这个线程真正跑完
        # ——close() 只收口状态已经是 enabled/disabled 的插件，线程还卡在
        # on_enable 里的话，close() 会直接跳过它，子进程就成了游离进程
        # （见 _enable_deferred_plugins 的说明，这是真实竞态不是假设）。
        deferred_thread.join()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _ensure_stdio()
    main()
