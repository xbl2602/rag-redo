"""GUI 启动路径门禁：**走真实入口**，而不是直接实例化 `Api`。

**为什么需要这份测试**（2026-09-28 审计 CRIT-1）：`test_api.py` 等 60 多条 GUI 测试全绿，
GUI 却根本起不来——它们都是 `Api(pipeline, lib_mgr)` 直接构造，从来没有让 GUI 插件走过
真实的 `PluginRuntime.load()`。运行时的导入边界校验发现 `contract_bridge.py` 在函数体里
`from official_library_manager... import ...`（跨插件导入），把 `official-gui-shell` 判成
`invalid`，`gui_main.py` 随即"致命错误：无法打开界面"退出。"直接实例化 + 只 grep 前端文本"
的测试给出的是**全绿的假象**。

这里的每条都经过真实的运行时加载与真实的 `gui_main.main()`（只把 `webview` 换成假的，免得
真的弹窗）：

1. 全部 20 个 GUI 插件经真实运行时启用，没有一个 `invalid`/`failed`；
2. 必需插件 `invalid`/`failed` 时入口大声报错并非零退出，不再静默放过；
3. `main()` 按冻结夹具的窗口规格建窗、调用 `bind_window`、起 1 秒推送线程并向前端推送
   真实快照，窗口关闭后线程停止不残留；
4. 推送线程的容错口径（连续 5 次失败才退出、快照出错不算失败）；
5. `pick_path` 用 `webview` 模块上的对话框常量（此前误写成窗口对象的属性，真机上必炸）。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for _p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
for _other in (_REPO_ROOT / "plugins").glob("*"):
    if _other.is_dir() and str(_other) not in sys.path:
        sys.path.insert(0, str(_other))

from core import paths  # noqa: E402
from core.runtime import PluginRuntime, PluginState  # noqa: E402
from official_gui_shell import push  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_guiweb_contract.json").read_text(encoding="utf-8")
)
SNAPSHOT_PREFIX = "window.__push && window.__push('snapshot', "


class _FakeWindow:
    """记录一切的假窗口：`evaluate_js` 记脚本，`create_file_dialog` 记参数。"""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.dialogs: list[dict] = []

    def evaluate_js(self, script: str):
        self.scripts.append(script)

    def create_file_dialog(self, dialog_type=10, directory="", allow_multiple=False,
                           save_filename="", file_types=()):
        self.dialogs.append({"dialog_type": dialog_type, "directory": directory,
                             "allow_multiple": allow_multiple, "file_types": file_types})
        return ("C:/picked/target",)


def _fake_webview(window: _FakeWindow, *, wait_for_push: bool = True) -> types.ModuleType:
    """假的 `webview` 模块：常量取真实 pywebview 的取值；`create_window` 记录参数并返回
    `window`；`start` 等到推送线程真的往窗口推过一条再返回（模拟"窗口存活了一会儿再关闭"）。"""
    module = types.ModuleType("webview")
    module.OPEN_DIALOG = 10
    module.FOLDER_DIALOG = 20
    module.SAVE_DIALOG = 30
    module.created: list[tuple[tuple, dict]] = []  # type: ignore[attr-defined]

    def create_window(*args, **kwargs):
        module.created.append((args, kwargs))
        return window

    def start(*args, **kwargs):
        if not wait_for_push:
            return
        deadline = time.monotonic() + 60
        while not window.scripts and time.monotonic() < deadline:
            time.sleep(0.05)

    module.create_window = create_window
    module.start = start
    return module


@contextlib.contextmanager
def _webview_installed(module: types.ModuleType):
    """只临时换掉 `sys.modules["webview"]` 这一项。

    **不能用 `patch.dict(sys.modules, ...)`**：它退出时会把整张表还原成进入前的样子，
    连带删掉这期间新导入的全部模块——插件加载会导入 chromadb/pymupdf 等带 C 扩展的
    包，被从 `sys.modules` 里删掉后下一次再导入就是 `ImportError: cannot load module
    more than once per process`。"""
    previous = sys.modules.get("webview")
    sys.modules["webview"] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = previous


def _load_gui_main(data_root: Path, webview_module: types.ModuleType):
    """按真实方式加载 `gui_main.py`（数据目录取环境变量，`webview` 换成假的）。"""
    with patch.dict(os.environ, {"RAG_REDO_DATA_ROOT": str(data_root)}), \
            _webview_installed(webview_module):
        spec = importlib.util.spec_from_file_location("gui_main_under_test", _REPO_ROOT / "gui_main.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _TempDataRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gui_boot_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class TestRuntimeBoot(_TempDataRoot):
    """真实运行时加载全部 GUI 插件。"""

    def test_all_gui_plugins_load_and_enable_through_the_real_runtime(self) -> None:
        gm = _load_gui_main(self.tmp / "data", _fake_webview(_FakeWindow()))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            runtime = gm.build_runtime()
            # WEMM/MinerU-local（DEFERRED_PLUGIN_IDS）故意只在 build_runtime() 里
            # load、不 enable——真正 enable 挪到窗口打开之后的后台线程（见
            # gui_main.py 里 DEFERRED_PLUGIN_IDS 的说明），这里手动补跑一次，
            # 断言的是"这两个最终也能干净启用"，不是"build_runtime() 一次性
            # 启用全部 20 个"这个已经不再成立的旧契约。
            gm._enable_deferred_plugins(runtime)
        self.addCleanup(runtime.close)
        bad = {
            plugin_id: (runtime.plugins[plugin_id].state.value if plugin_id in runtime.plugins else "未发现",
                        getattr(runtime.plugins.get(plugin_id), "error", None))
            for plugin_id in gm.REQUIRED_PLUGINS
            if plugin_id not in runtime.plugins
            or runtime.plugins[plugin_id].state != PluginState.ENABLED
        }
        self.assertEqual(bad, {}, f"这些插件没能经真实运行时启用：{bad}\n入口告警：{stderr.getvalue()}")
        self.assertEqual(len(gm.REQUIRED_PLUGINS), 20)
        self.assertNotIn("警告", stderr.getvalue())

    def test_build_runtime_defers_subprocess_service_plugins_so_the_window_is_not_blocked(self) -> None:
        """2026-09-29 用户真实反馈修复：WEMM/MinerU-local 的 on_enable 会真的
        Popen 子进程并阻塞轮询 health_check（最多 10s/个）——build_runtime()
        同步 enable 全部 20 个插件时，窗口在 `_serve()` 建出来之前就要先扛完
        这最多 ~20s，真机上就是"先黑屏好几秒才出界面"。这里钉住
        build_runtime() 返回时，DEFERRED_PLUGIN_IDS 只到 loaded（provider 已
        注册、Pipeline.__init__ 用得到），子进程还没拉起来——真正 enable 挪到
        `_serve()` 建完窗口之后的后台线程（`_enable_deferred_plugins`）。"""
        gm = _load_gui_main(self.tmp / "data", _fake_webview(_FakeWindow()))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            runtime = gm.build_runtime()
        try:
            for plugin_id in gm.DEFERRED_PLUGIN_IDS:
                self.assertEqual(
                    runtime.plugins[plugin_id].state, PluginState.LOADED,
                    f"{plugin_id} 不该在 build_runtime() 里就被 enable（会阻塞窗口打开）",
                )
                self.assertIsNone(getattr(runtime.plugins[plugin_id].instance, "_handle", None))
        finally:
            runtime.close()

    def test_build_runtime_warns_loudly_on_invalid_plugin(self) -> None:
        """`invalid` 与 `failed` 同样要告警——此前只对 failed 说话，invalid 被静默放过。"""
        gm = _load_gui_main(self.tmp / "data", _fake_webview(_FakeWindow()))

        def _boundary(self, manifest):  # noqa: ANN001 - 替换 PluginRuntime._validate_import_boundary
            if manifest.id == "official-gui-shell":
                return ["contract_bridge.py 试图导入其他插件 official_library_manager"]
            return []

        stderr = io.StringIO()
        with patch.object(PluginRuntime, "_validate_import_boundary", _boundary), \
                contextlib.redirect_stderr(stderr):
            runtime = gm.build_runtime()
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.plugins["official-gui-shell"].state, PluginState.INVALID)
        self.assertIn("official-gui-shell invalid", stderr.getvalue())
        self.assertIn("试图导入其他插件", stderr.getvalue())

    def test_gui_plugin_source_does_not_import_other_plugins(self) -> None:
        """静态兜底：与运行时同一条规则，出问题时能直接指到哪一行。"""
        import ast

        own = "official_gui_shell"
        others = {
            p.name.replace("-", "_") for p in (_REPO_ROOT / "plugins").iterdir()
            if p.is_dir() and p.name.startswith("official-")
        } - {own}
        offenders: list[str] = []
        for source in sorted((_PLUGIN_DIR / own).glob("*.py")):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".", 1)[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".", 1)[0]]
                offenders += [f"{source.name}:{node.lineno} {n}" for n in names if n in others]
        self.assertEqual(offenders, [])


class TestMainEntry(_TempDataRoot):
    """真实 `gui_main.main()`（只有 `webview` 是假的）。"""

    def _run_main(self):
        window = _FakeWindow()
        webview_module = _fake_webview(window)
        gm = _load_gui_main(self.tmp / "data", webview_module)
        stderr = io.StringIO()
        with _webview_installed(webview_module), contextlib.redirect_stderr(stderr):
            gm.main()
        return gm, window, webview_module, stderr.getvalue()

    def test_main_builds_window_binds_it_and_pushes_real_snapshots(self) -> None:
        gm, window, webview_module, _stderr = self._run_main()

        # ① 窗口规格与冻结夹具（旧 guiweb/app.py::main）逐项一致
        self.assertEqual(len(webview_module.created), 1)
        args, kwargs = webview_module.created[0]
        spec = FIXTURE["window"]
        self.assertEqual(args[0], spec["redo_title"])  # 标题是唯一经操作者批准的偏离（BC-15）
        self.assertEqual(kwargs["width"], spec["width"])
        self.assertEqual(kwargs["height"], spec["height"])
        self.assertEqual(kwargs["min_size"], (spec["min_width"], spec["min_height"]))
        self.assertEqual(kwargs["background_color"], spec["background_color"])
        self.assertTrue(Path(kwargs["url"]).is_file(), kwargs["url"])

        # ② bind_window 被调用：桥拿到了窗口句柄
        api = kwargs["js_api"]
        self.assertIs(api._window, window)

        # ③ 推送线程真的往前端推了真实快照（键集合满足契约）
        self.assertTrue(window.scripts, "推送线程一条都没推")
        self.assertTrue(window.scripts[0].startswith(SNAPSHOT_PREFIX), window.scripts[0][:80])
        snapshot = json.loads(window.scripts[0][len(SNAPSHOT_PREFIX):-1])
        required = next(m for m in FIXTURE["methods"] if m["name"] == "get_snapshot")["required_keys"]
        self.assertEqual(sorted(set(required) - set(snapshot)), [])

        # ④ 窗口关闭后线程停止、不泄漏：主流程返回后不再有新推送
        time.sleep(0.3)
        settled = len(window.scripts)
        time.sleep(push.PUSH_INTERVAL_S * 1.6)
        self.assertEqual(len(window.scripts), settled, "main() 返回后推送线程仍在推")

    def test_pick_path_uses_module_level_dialog_constants(self) -> None:
        """对话框类型常量在 `webview` 模块上，不在窗口对象上。"""
        gm, window, webview_module, _ = self._run_main()
        api = webview_module.created[0][1]["js_api"]
        with _webview_installed(webview_module):
            picked = api.pick_path("dir", "")
            file_picked = api.pick_path("file", "")
        self.assertEqual(picked, {"path": "C:/picked/target", "error": None})
        self.assertEqual(file_picked["path"], "C:/picked/target")
        self.assertEqual(window.dialogs[0]["dialog_type"], webview_module.FOLDER_DIALOG)
        self.assertEqual(window.dialogs[1]["dialog_type"], webview_module.OPEN_DIALOG)
        self.assertFalse(window.dialogs[0]["allow_multiple"])

    def test_pick_path_without_window_reports_not_ready(self) -> None:
        gm = _load_gui_main(self.tmp / "data2", _fake_webview(_FakeWindow()))
        with contextlib.redirect_stderr(io.StringIO()):
            runtime = gm.build_runtime()
        self.addCleanup(runtime.close)
        from core.pipeline import Pipeline

        api = runtime.plugins["official-gui-shell"].instance.make_api(
            Pipeline(runtime), runtime.plugins["official-library-manager"].instance
        )
        self.assertEqual(api.pick_path("dir", ""), {"path": None, "error": "窗口未就绪"})

    def test_main_exits_nonzero_and_says_why_when_gui_plugin_is_invalid(self) -> None:
        window = _FakeWindow()
        webview_module = _fake_webview(window, wait_for_push=False)
        gm = _load_gui_main(self.tmp / "data", webview_module)

        def _boundary(self, manifest):  # noqa: ANN001
            if manifest.id == "official-gui-shell":
                return ["contract_bridge.py 试图导入其他插件 official_library_manager"]
            return []

        stderr = io.StringIO()
        with patch.object(PluginRuntime, "_validate_import_boundary", _boundary), \
                _webview_installed(webview_module), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                gm.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("official-gui-shell", stderr.getvalue())
        self.assertIn("invalid", stderr.getvalue())
        self.assertEqual(webview_module.created, [], "插件没启用就不该建窗口")

    def _closed_runtimes_during(self, run) -> list[PluginRuntime]:
        """跑 `run()`，返回期间被 `close()` 的运行时（行为不变，只旁路记录）。"""
        closed: list[PluginRuntime] = []
        real_close = PluginRuntime.close

        def _spy(runtime: PluginRuntime) -> None:
            closed.append(runtime)
            real_close(runtime)

        with patch.object(PluginRuntime, "close", _spy):
            run()
        return closed

    def test_closing_the_window_closes_the_runtime_and_leaves_no_plugin_enabled(self) -> None:
        """关窗后必须收口运行时：插件拉起的子进程（页级视觉看图服务）不会跟着父进程走，
        不回收就是每开关一次窗口留下一对孤儿进程（2026-09-28 真实进程冒烟复现）。"""
        window = _FakeWindow()
        webview_module = _fake_webview(window)
        gm = _load_gui_main(self.tmp / "data", webview_module)

        def _run() -> None:
            with _webview_installed(webview_module), contextlib.redirect_stderr(io.StringIO()):
                gm.main()

        closed = self._closed_runtimes_during(_run)
        self.assertEqual(len(closed), 1, "main() 返回前必须且只需收口一次运行时")
        still_enabled = [p for p, plugin in closed[0].plugins.items() if plugin.state == PluginState.ENABLED]
        self.assertEqual(still_enabled, [], "收口后不该还有插件停在 enabled")
        # 收口不擦启用记录：下次启动仍能按记录恢复
        state = json.loads(paths.plugins_state_file(self.tmp / "data").read_text(encoding="utf-8"))
        self.assertIn("official-gui-shell", state["enabled"])

    def test_closing_the_window_leaves_a_reclaim_summary_in_the_gui_log(self) -> None:
        """2026-09-29 操作者反馈“关掉 GUI 之后显存没有及时释放、进程没有关闭”，现场无法复现：
        关窗后必须在 GUI 日志里留下“回收了几个子进程、用时多久”，下次遇到有据可查。"""
        from core.index_progress import INDEX_LOG_NAME

        window = _FakeWindow()
        webview_module = _fake_webview(window)
        gm = _load_gui_main(self.tmp / "data", webview_module)
        with _webview_installed(webview_module), contextlib.redirect_stderr(io.StringIO()):
            gm.main()
        gm._report_shutdown()  # 真实进程里由退出钩子调用；测试进程不退出，直接调
        log_text = (self.tmp / "data" / INDEX_LOG_NAME).read_text(encoding="utf-8")
        self.assertRegex(log_text, r"\[GUI\] 窗口已关闭")
        self.assertIn("用时", log_text)

    def test_reclaim_summary_names_the_children_that_are_still_alive_at_exit(self) -> None:
        from core.index_progress import INDEX_LOG_NAME

        gm = _load_gui_main(self.tmp / "data", _fake_webview(_FakeWindow()))
        (self.tmp / "data").mkdir(parents=True, exist_ok=True)
        gm._CLOSE_REPORT.update(before=[(101, "server.py"), (202, "python.exe")], elapsed=0.4)
        with patch.object(gm, "_child_processes", return_value=[(202, "python.exe")]):
            gm._report_shutdown()
        lines = (self.tmp / "data" / INDEX_LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("回收了 1 个子进程" in line and "用时 0.4 秒" in line for line in lines), lines)
        leftover = [line for line in lines if "仍有 1 个子进程未回收" in line]
        self.assertEqual(len(leftover), 1, lines)
        self.assertIn("[GUI] ERROR", leftover[0])
        self.assertIn("python.exe(pid 202)", leftover[0])

    def test_reclaim_summary_without_psutil_still_reports_the_close(self) -> None:
        from core.index_progress import INDEX_LOG_NAME

        gm = _load_gui_main(self.tmp / "data", _fake_webview(_FakeWindow()))
        (self.tmp / "data").mkdir(parents=True, exist_ok=True)
        gm._CLOSE_REPORT.update(before=None, elapsed=1.2)
        with patch.object(gm, "_child_processes", return_value=None):
            gm._report_shutdown()
        text = (self.tmp / "data" / INDEX_LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("窗口已关闭", text)
        self.assertIn("用时 1.2 秒", text)

    def test_summary_never_recreates_a_deleted_data_dir(self) -> None:
        gm = _load_gui_main(self.tmp / "gone", _fake_webview(_FakeWindow()))
        with patch.object(gm, "_child_processes", return_value=None):
            gm._report_shutdown()  # 数据目录不存在：静默放弃，不抛、不创建
        self.assertFalse((self.tmp / "gone").exists())

    def test_deferred_plugin_enable_is_joined_before_runtime_close(self) -> None:
        """`_serve()` 必须在 `runtime.close()` 之前等后台 enable 线程
        （`_enable_deferred_plugins`）真正跑完——`PluginRuntime.close()` 只收口
        状态已经是 enabled/disabled 的插件，线程还没跑完时插件state还停在
        loaded，close() 会直接跳过它，WEMM/MinerU-local 拉起的子进程就成了
        没人回收的游离进程。这里故意让后台 enable 线程人为变慢，钉住
        "close() 一定发生在它跑完之后"这个顺序，不是靠真实计时凑巧对。"""
        window = _FakeWindow()
        webview_module = _fake_webview(window)
        gm = _load_gui_main(self.tmp / "data", webview_module)

        finished = threading.Event()
        real_deferred = gm._enable_deferred_plugins

        def _slow_deferred(runtime):
            time.sleep(0.3)
            real_deferred(runtime)
            finished.set()

        close_saw_finished: list[bool] = []
        real_close = PluginRuntime.close

        def _spy_close(runtime):
            close_saw_finished.append(finished.is_set())
            real_close(runtime)

        with patch.object(gm, "_enable_deferred_plugins", _slow_deferred), \
                patch.object(PluginRuntime, "close", _spy_close), \
                _webview_installed(webview_module), contextlib.redirect_stderr(io.StringIO()):
            gm.main()

        self.assertEqual(
            close_saw_finished, [True],
            "runtime.close() 在后台 enable 线程跑完之前就执行了——子进程可能被漏收",
        )

    def test_fatal_exit_because_a_required_plugin_is_down_also_closes_the_runtime(self) -> None:
        webview_module = _fake_webview(_FakeWindow(), wait_for_push=False)
        gm = _load_gui_main(self.tmp / "data", webview_module)

        def _boundary(self, manifest):  # noqa: ANN001
            if manifest.id == "official-gui-shell":
                return ["contract_bridge.py 试图导入其他插件 official_library_manager"]
            return []

        def _run() -> None:
            with patch.object(PluginRuntime, "_validate_import_boundary", _boundary), \
                    _webview_installed(webview_module), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    gm.main()

        closed = self._closed_runtimes_during(_run)
        self.assertEqual(len(closed), 1, "致命退出也要先收口运行时，别把已经拉起的子进程丢在后面")


class TestWindowSpecAndPaths(unittest.TestCase):
    def test_window_spec_constants_match_the_frozen_fixture(self) -> None:
        gm = _load_gui_main(Path(tempfile.gettempdir()) / "gui_boot_spec", _fake_webview(_FakeWindow()))
        spec = FIXTURE["window"]
        self.assertEqual(gm.WINDOW_SPEC["title"], spec["redo_title"])  # 标题是唯一经操作者批准的偏离（BC-15）
        self.assertNotEqual(spec["redo_title"], spec["title"], "redo_title 应是与旧标题不同的新名字")
        self.assertEqual(gm.WINDOW_SPEC["width"], spec["width"])
        self.assertEqual(gm.WINDOW_SPEC["height"], spec["height"])
        self.assertEqual(gm.WINDOW_SPEC["min_size"], (spec["min_width"], spec["min_height"]))
        self.assertEqual(gm.WINDOW_SPEC["background_color"], spec["background_color"])
        self.assertEqual(push.PUSH_INTERVAL_S, spec["push_interval_seconds"])
        self.assertEqual(push.PUSH_FAILURE_TOLERANCE, spec["push_failure_tolerance"])

    def test_plugins_state_file_works_without_arguments(self) -> None:
        """此前形参 `data_root` 遮蔽了同名函数，无参调用是 `None()` 的 TypeError。"""
        self.assertEqual(paths.plugins_state_file(), paths.data_root() / paths.PLUGINS_STATE_FILE)
        self.assertEqual(paths.plugins_state_file(Path("x")), Path("x") / paths.PLUGINS_STATE_FILE)

    def test_gui_entry_uses_the_shared_path_rules(self) -> None:
        data_root = Path(tempfile.gettempdir()) / "gui_boot_paths"
        gm = _load_gui_main(data_root, _fake_webview(_FakeWindow()))
        self.assertEqual(Path(gm.DATA_ROOT), data_root)
        self.assertEqual(Path(gm.REPO_ROOT), paths.repo_root())


class TestPushLoop(unittest.TestCase):
    """1 秒推送线程的容错口径（旧 guiweb/app.py::_push_loop，问题47 附记）。"""

    class _Api:
        def __init__(self, snapshots):
            self.snapshots = list(snapshots)
            self.calls = 0

        def get_snapshot(self):
            self.calls += 1
            item = self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

    class _Window:
        def __init__(self, fail_first: int = 0, always_fail: bool = False):
            self.scripts: list[str] = []
            self.attempts = 0
            self.fail_first = fail_first
            self.always_fail = always_fail

        def evaluate_js(self, script):
            self.attempts += 1
            if self.always_fail or self.attempts <= self.fail_first:
                raise RuntimeError("window not ready")
            self.scripts.append(script)

    def _run(self, api, window, *, tolerance=5, seconds=0.5):
        stop = threading.Event()
        thread = threading.Thread(
            target=push.push_loop, args=(api, window, stop),
            kwargs={"interval": 0.01, "tolerance": tolerance}, daemon=True,
        )
        thread.start()
        thread.join(timeout=seconds)
        alive = thread.is_alive()
        stop.set()
        thread.join(timeout=2)
        return alive

    def test_loop_exits_only_after_five_consecutive_failures(self) -> None:
        window = self._Window(always_fail=True)
        alive = self._run(self._Api([{"libs": []}]), window, seconds=2)
        self.assertFalse(alive, "连续失败超限后线程必须自己退出（窗口真关了不能泄漏线程）")
        self.assertEqual(window.attempts, push.PUSH_FAILURE_TOLERANCE)

    def test_a_few_early_failures_do_not_kill_the_loop(self) -> None:
        """页面加载期前几次 evaluate_js 失败（窗口未就绪）不能终结此后全部推送。"""
        window = self._Window(fail_first=3)
        alive = self._run(self._Api([{"libs": []}]), window, seconds=0.4)
        self.assertTrue(alive, "少于 5 次失败时线程应继续运行")
        self.assertGreater(len(window.scripts), 0)

    def test_success_resets_the_failure_counter(self) -> None:
        results: list[object] = []
        window = self._Window()
        state = {"n": 0}

        def flaky(script):
            state["n"] += 1
            if state["n"] % 3 != 0:  # 失败、失败、成功……永远凑不到连续 5 次
                raise RuntimeError("flaky")
            results.append(script)

        window.evaluate_js = flaky  # type: ignore[method-assign]
        alive = self._run(self._Api([{"libs": []}]), window, seconds=0.4)
        self.assertTrue(alive)
        self.assertGreater(len(results), 0)

    def test_snapshot_error_is_skipped_not_pushed_and_not_counted_as_failure(self) -> None:
        api = self._Api([{"error": "快照失败：x"}])
        window = self._Window()
        alive = self._run(api, window, seconds=0.3)
        self.assertTrue(alive)
        self.assertEqual(window.scripts, [])
        self.assertGreater(api.calls, 5, "快照出错时线程应继续每轮重试")

    def test_pushed_script_is_the_legacy_wire_format(self) -> None:
        window = self._Window()
        self._run(self._Api([{"libs": [{"name": "库"}]}]), window, seconds=0.1)
        self.assertTrue(window.scripts)
        script = window.scripts[0]
        self.assertTrue(script.startswith(SNAPSHOT_PREFIX))
        self.assertEqual(json.loads(script[len(SNAPSHOT_PREFIX):-1]), {"libs": [{"name": "库"}]})


if __name__ == "__main__":
    unittest.main()
