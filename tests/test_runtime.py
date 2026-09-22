"""插件运行时集成测试：发现→加载→启用→禁用→卸载全流程，对应
../docs/ROADMAP.md Phase 0 的全部验收标准。全程用临时目录构造合成插件，
不依赖 examples/ 保持字节不变，也不碰任何真实数据（继承旧项目"索引集成
测试用隔离环境"的纪律）。
"""
from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
