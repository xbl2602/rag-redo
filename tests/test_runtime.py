"""插件运行时集成测试：发现→加载→启用→禁用→卸载全流程，对应
../docs/ROADMAP.md Phase 0 的全部验收标准。全程用临时目录构造合成插件，
不依赖 examples/ 保持字节不变，也不碰任何真实数据（继承旧项目"索引集成
测试用隔离环境"的纪律）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.registry import ExtensionConflictError
from core.runtime import PluginRuntime, PluginState

_LIFECYCLE_BODY = """
class {cls}:
    def on_load(self, ctx): pass
    def on_enable(self, ctx): pass
    def on_disable(self, ctx): pass
    def on_unload(self, ctx): pass
"""

_BROKEN_BODY = """
class {cls}:
    def on_load(self, ctx): pass
    def on_enable(self, ctx):
        raise RuntimeError("{msg}")
    def on_disable(self, ctx): pass
    def on_unload(self, ctx): pass
"""


_SUBPROCESS_SERVER_BODY = '''
import http.server, json, sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        self._json(200, {"pong": True})

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

port = int(sys.argv[sys.argv.index("--port") + 1])
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

_SUBPROCESS_LIFECYCLE_BODY = """
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.subprocess_service import SubprocessServiceHandle


class {cls}:
    def on_load(self, ctx):
        self._handle = None

    def on_enable(self, ctx):
        self._handle = SubprocessServiceHandle(
            ctx.runtime.command,
            health_check=ctx.runtime.health_check,
            cwd=Path(__file__).parent,
        )
        self._handle.start()

    def on_disable(self, ctx):
        if self._handle is not None:
            self._handle.stop()
            self._handle = None

    def on_unload(self, ctx):
        pass

    def ping(self):
        return self._handle.call("ping", {{}})

    @property
    def process_pid(self):
        return self._handle._process.pid if self._handle is not None and self._handle.is_alive else None
"""


def _make_subprocess_plugin(root: Path, plugin_id: str, module_name: str, class_name: str) -> None:
    """构造一个真实的 subprocess_service 测试插件：真的会 Popen 一个只用
    标准库 http.server 的子进程，不是伪造一个假对象充当"看起来像子进程
    服务"——验证的是 PluginRuntime 真的按 docs/PLUGIN_SPEC.md 第3节的
    生命周期表在 on_enable 拉起子进程、on_disable 收掉它，而不是这条路径
    本身能不能跑通全凭猜测。"""
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        f"""
id = "{plugin_id}"
name = "{plugin_id}"
version = "0.1.0"
api_version = ">=0.1,<0.2"

[provides]

[requires]

[runtime]
kind = "subprocess_service"
entry = "{module_name}:{class_name}"
command = [{json.dumps(sys.executable)}, "server.py", "--port", "{{port}}"]
health_check = "http://127.0.0.1:{{port}}/health"

[permissions]
network = false
""",
        encoding="utf-8",
    )
    (plugin_dir / f"{module_name}.py").write_text(_SUBPROCESS_LIFECYCLE_BODY.format(cls=class_name), encoding="utf-8")
    (plugin_dir / "server.py").write_text(_SUBPROCESS_SERVER_BODY, encoding="utf-8")


def _make_plugin(root: Path, plugin_id: str, module_name: str, class_name: str, body: str, provides: str = "") -> None:
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        f"""
id = "{plugin_id}"
name = "{plugin_id}"
version = "0.1.0"
api_version = ">=0.1,<0.2"

[provides]
{provides}

[requires]

[runtime]
kind = "in_process"
entry = "{module_name}:{class_name}"

[permissions]
network = false
""",
        encoding="utf-8",
    )
    (plugin_dir / f"{module_name}.py").write_text(body, encoding="utf-8")


class TestPluginRuntimeLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plugins_dir = self.tmp / "plugins"
        self.plugins_dir.mkdir()
        self.state_file = self.tmp / "data" / "plugins_state.json"

    def _runtime(self) -> PluginRuntime:
        return PluginRuntime(self.plugins_dir, state_file=self.state_file)

    def test_scan_discovers_valid_plugin(self):
        _make_plugin(self.plugins_dir, "t-hello", "t_hello_mod", "Hello", _LIFECYCLE_BODY.format(cls="Hello"))
        rt = self._runtime()
        rt.scan()
        self.assertEqual(rt.plugins["t-hello"].state, PluginState.DISCOVERED)

    def test_full_lifecycle_load_enable_disable_unload(self):
        _make_plugin(self.plugins_dir, "t-cycle", "t_cycle_mod", "Cycle", _LIFECYCLE_BODY.format(cls="Cycle"))
        rt = self._runtime()
        rt.scan()
        rt.load("t-cycle")
        self.assertEqual(rt.plugins["t-cycle"].state, PluginState.LOADED)
        rt.enable("t-cycle")
        self.assertEqual(rt.plugins["t-cycle"].state, PluginState.ENABLED)
        rt.disable("t-cycle")
        self.assertEqual(rt.plugins["t-cycle"].state, PluginState.DISABLED)
        rt.unload("t-cycle")
        self.assertEqual(rt.plugins["t-cycle"].state, PluginState.DISCOVERED)

    def test_broken_on_enable_isolated_as_failed(self):
        _make_plugin(
            self.plugins_dir, "t-broken", "t_broken_mod", "Broken",
            _BROKEN_BODY.format(cls="Broken", msg="boom"),
        )
        rt = self._runtime()
        rt.scan()
        rt.load("t-broken")
        rt.enable("t-broken")
        self.assertEqual(rt.plugins["t-broken"].state, PluginState.FAILED)
        self.assertIn("boom", rt.plugins["t-broken"].error)

    def test_broken_plugin_does_not_affect_others(self):
        _make_plugin(
            self.plugins_dir, "t-broken2", "t_broken2_mod", "Broken2",
            _BROKEN_BODY.format(cls="Broken2", msg="boom2"),
        )
        _make_plugin(self.plugins_dir, "t-fine", "t_fine_mod", "Fine", _LIFECYCLE_BODY.format(cls="Fine"))
        rt = self._runtime()
        rt.scan()
        rt.load("t-broken2")
        rt.enable("t-broken2")
        rt.load("t-fine")
        rt.enable("t-fine")
        self.assertEqual(rt.plugins["t-broken2"].state, PluginState.FAILED)
        self.assertEqual(rt.plugins["t-fine"].state, PluginState.ENABLED)

    def test_singleton_conflict_surfaced_not_silent(self):
        _make_plugin(
            self.plugins_dir, "t-emb-a", "t_emb_a_mod", "EmbA",
            _LIFECYCLE_BODY.format(cls="EmbA"), provides='embedder = "singleton"',
        )
        _make_plugin(
            self.plugins_dir, "t-emb-b", "t_emb_b_mod", "EmbB",
            _LIFECYCLE_BODY.format(cls="EmbB"), provides='embedder = "singleton"',
        )
        rt = self._runtime()
        rt.scan()
        for pid in ("t-emb-a", "t-emb-b"):
            rt.load(pid)
            rt.enable(pid)
        self.assertIn("embedder", rt.registry.conflicts())
        with self.assertRaises(ExtensionConflictError):
            rt.registry.active_of("embedder")
        # 显式指定后冲突解除——对应AGENTS.md"单例扩展点切换必须是配置层面操作"
        rt.registry.set_active("embedder", "t-emb-b")
        self.assertEqual(rt.registry.active_of("embedder"), "t-emb-b")

    def test_invalid_manifest_marks_invalid_not_crash(self):
        plugin_dir = self.plugins_dir / "t-bad"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.toml").write_text("not valid [[[ toml", encoding="utf-8")
        rt = self._runtime()
        rt.scan()  # 不应该抛异常
        self.assertEqual(rt.plugins["t-bad"].state, PluginState.INVALID)

    def test_enable_state_persists_across_runtime_instances(self):
        _make_plugin(self.plugins_dir, "t-persist", "t_persist_mod", "Persist", _LIFECYCLE_BODY.format(cls="Persist"))
        rt1 = self._runtime()
        rt1.scan()
        rt1.load("t-persist")
        rt1.enable("t-persist")
        self.assertTrue(self.state_file.exists())

        # 模拟"重启核心"：新建一个 PluginRuntime 实例指向同一个 state_file
        rt2 = self._runtime()
        rt2.scan()
        self.assertEqual(rt2.plugins["t-persist"].state, PluginState.ENABLED)

    def test_disable_removes_from_persisted_state(self):
        _make_plugin(self.plugins_dir, "t-toggle", "t_toggle_mod", "Toggle", _LIFECYCLE_BODY.format(cls="Toggle"))
        rt1 = self._runtime()
        rt1.scan()
        rt1.load("t-toggle")
        rt1.enable("t-toggle")
        rt1.disable("t-toggle")

        rt2 = self._runtime()
        rt2.scan()
        self.assertEqual(rt2.plugins["t-toggle"].state, PluginState.DISCOVERED)

    def test_no_state_file_means_pure_in_memory_run(self):
        rt = PluginRuntime(self.plugins_dir, state_file=None)
        rt.scan()  # 不传 state_file 时纯内存运行，不该报错


def _process_is_gone(pid: int) -> bool:
    """跨平台的"这个 pid 是不是真的没了"检查。POSIX 上 os.kill(pid, 0)
    不发信号只探测进程是否存在，进程不在时抛 ProcessLookupError；Windows
    没有这个信号语义，os.kill 在那边会直接抛 OSError（WinError 87），不能
    用同一段代码判断，得走 Win32 OpenProcess API 才是真的问操作系统。"""
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TestSubprocessServicePluginLifecycle(unittest.TestCase):
    """真实验证 subprocess_service 这条运行时路径：on_enable 真的拉起
    子进程、方法调用真的经过本机HTTP走到子进程、on_disable 真的把子进程
    杀干净——对应架构红线6"不产生游离进程"，只有真的问操作系统这个 pid
    还在不在才算数，不是看 Python 对象内部状态自欺欺人。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plugins_dir = self.tmp / "plugins"
        self.plugins_dir.mkdir()
        # 模块名必须每个测试方法唯一——core/runtime.py 的 _instantiate() 用
        # importlib.import_module(module_name) 加载插件入口，Python 的
        # sys.modules 缓存是按模块名（不是按文件路径）键的；如果这个类的
        # 每个测试方法在各自的 setUp 里都用同一个模块名重新造一次插件，
        # 后面的测试会拿到第一个测试留在 sys.modules 里的缓存模块对象，
        # 它的 __file__ 还指向第一个测试早就被 addCleanup 删掉的 tmp 目录
        # ——真实踩到的坑（FileNotFoundError，不是猜的），同
        # core/runtime.py 模块注释里"没做每插件导入隔离"的已知限制。
        module_name = f"t_sub_mod_{self._testMethodName}"
        _make_subprocess_plugin(self.plugins_dir, "t-sub", module_name, "Sub")
        self.rt = PluginRuntime(self.plugins_dir, state_file=self.tmp / "data" / "plugins_state.json")
        self.rt.scan()

    def tearDown(self) -> None:
        # 兜底清掉这个类里任何测试方法真的拉起来但没自己 disable 掉的
        # 子进程——在真实 Windows 机器上跑测试时发现过
        # test_enable_starts_real_subprocess_and_call_round_trips 忘记
        # disable，每跑一次就在系统里留下一个真的游离 server.py 子进程
        # （架构红线6"不产生游离进程"，测试代码自己也不能违反，且这种
        # 泄漏在一次性沙盒环境里几乎不可见，只有在持久化的真实机器上才
        # 会累积暴露）。
        for plugin_id in ("t-sub", "t-sub-broken"):
            plugin = self.rt.plugins.get(plugin_id)
            if plugin is not None and plugin.state == PluginState.ENABLED:
                self.rt.disable(plugin_id)

    def test_enable_starts_real_subprocess_and_call_round_trips(self):
        self.rt.load("t-sub")
        self.assertEqual(self.rt.plugins["t-sub"].state, PluginState.LOADED)
        self.rt.enable("t-sub")
        self.assertEqual(self.rt.plugins["t-sub"].state, PluginState.ENABLED, self.rt.plugins["t-sub"].error)

        instance = self.rt.plugins["t-sub"].instance
        self.assertIsNotNone(instance.process_pid)
        self.assertEqual(instance.ping(), {"pong": True})

    def test_disable_kills_the_real_process_no_orphan(self):
        self.rt.load("t-sub")
        self.rt.enable("t-sub")
        instance = self.rt.plugins["t-sub"].instance
        pid = instance.process_pid
        self.assertIsNotNone(pid)

        self.rt.disable("t-sub")
        self.assertEqual(self.rt.plugins["t-sub"].state, PluginState.DISABLED)
        self.assertTrue(_process_is_gone(pid))

    def test_broken_subprocess_command_marks_plugin_failed_not_crash(self):
        _make_plugin_dir = self.plugins_dir / "t-sub-broken"
        _make_subprocess_plugin(self.plugins_dir, "t-sub-broken", "t_sub_broken_mod", "SubBroken")
        # 故意把 command 改成一个必定失败的命令，验证核心不会被这个插件
        # 的子进程启动失败拖崩——同架构红线4"插件失败必须被隔离折叠"，
        # 只是这次失败发生在子进程启动阶段而不是普通 Python 异常。
        toml_path = _make_plugin_dir / "plugin.toml"
        content = toml_path.read_text(encoding="utf-8")
        content = content.replace(
            f'command = [{json.dumps(sys.executable)}, "server.py", "--port", "{{port}}"]',
            f'command = [{json.dumps(sys.executable)}, "-c", "import sys; sys.exit(1)"]',
        )
        toml_path.write_text(content, encoding="utf-8")

        rt = PluginRuntime(self.plugins_dir, state_file=self.tmp / "data" / "plugins_state2.json")
        rt.scan()
        rt.load("t-sub-broken")
        rt.enable("t-sub-broken")
        self.assertEqual(rt.plugins["t-sub-broken"].state, PluginState.FAILED)
        self.assertIn("立刻退出", rt.plugins["t-sub-broken"].error)


if __name__ == "__main__":
    unittest.main()
