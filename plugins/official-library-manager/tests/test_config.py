from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent.parent
_REPO_ROOT = _PLUGIN_DIR.parent.parent
for p in (_REPO_ROOT, _PLUGIN_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from core.runtime import PluginRuntime, PluginState  # noqa: E402
from core.settings import SettingsStore  # noqa: E402
from official_library_manager.config import LibraryConfigStore  # noqa: E402
from official_library_manager.plugin import LibraryManagerPlugin  # noqa: E402


class TestLibraryConfigStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "libraries.json"

    def test_add_and_list(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        self.assertEqual([c.library_id for c in store.list_libraries()], ["lib1"])

    def test_duplicate_id_raises(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        with self.assertRaises(ValueError):
            store.add_library("lib1", "重复", "/vaults/lib1")

    def test_persists_across_instances(self):
        store1 = LibraryConfigStore(self.path)
        store1.add_library("lib1", "我的库", "/vaults/lib1")
        store2 = LibraryConfigStore(self.path)
        self.assertEqual(len(store2.list_libraries()), 1)
        self.assertEqual(store2.get("lib1").name, "我的库")

    def test_set_selection_persists(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        store.set_selection("lib1", selection_in=["a.md"], selection_out=["b.md"])
        store2 = LibraryConfigStore(self.path)
        cfg = store2.get("lib1")
        self.assertEqual(cfg.selection_in, ["a.md"])
        self.assertEqual(cfg.selection_out, ["b.md"])

    def test_set_selection_unknown_library_raises(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(KeyError):
            store.set_selection("nope", selection_in=["a.md"])

    def test_set_policy_persists(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        store.set_policy("lib1", new_file_default="exclude", enabled_extensions=[".pdf", ".docx"])
        store2 = LibraryConfigStore(self.path)
        cfg = store2.get("lib1")
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".pdf", ".docx"])

    def test_set_policy_partial_update_leaves_other_field_untouched(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        store.set_policy("lib1", new_file_default="exclude")
        cfg = store.get("lib1")
        self.assertEqual(cfg.new_file_default, "exclude")
        self.assertEqual(cfg.enabled_extensions, [".md", ".txt"])  # 默认值没被动过

    def test_set_policy_unknown_library_raises(self):
        store = LibraryConfigStore(self.path)
        with self.assertRaises(KeyError):
            store.set_policy("nope", new_file_default="exclude")

    def test_corrupted_file_degrades_to_empty_not_crash(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])

    def test_remove_library(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        store.remove_library("lib1")
        self.assertEqual(store.list_libraries(), [])


class TestResolveLibraries(unittest.TestCase):
    """`LibraryManagerPlugin.resolve_libraries` 是多库检索选库语法的唯一
    权威实现（对齐 obsidian-rag/library.py::resolve_entries），这里直接
    测这个方法本身——不需要走完整 PluginRuntime/Pipeline，`resolve_libraries`
    只碰 `self.store`，直接赋值即可，比端到端测试更聚焦。跨库检索的真实
    端到端行为（真的搜到两个库的内容）在 tests/test_pipeline_e2e.py。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plugin = LibraryManagerPlugin()
        self.plugin.store = LibraryConfigStore(self.tmp / "libraries.json")
        self.plugin.store.add_library("lib-a", "库A", "/vaults/a")
        self.plugin.store.add_library("lib-b", "库B", "/vaults/b")
        self.plugin.store.add_library("lib-c", "库C", "/vaults/c")
        self.plugin._settings = SettingsStore(self.tmp / "settings.json")

    def test_empty_libraries_returns_all(self):
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_all_keyword_case_insensitive_returns_all(self):
        result = self.plugin.resolve_libraries("ALL")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_comma_separated_list_preserves_order_and_dedups(self):
        result = self.plugin.resolve_libraries("lib-b,lib-a,lib-b")
        self.assertEqual([c.library_id for c in result], ["lib-b", "lib-a"])

    def test_exclude_subtracts_from_selection(self):
        result = self.plugin.resolve_libraries("all", exclude="lib-b")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-c"})

    def test_unknown_library_name_raises_and_lists_available(self):
        with self.assertRaises(ValueError) as ctx:
            self.plugin.resolve_libraries("lib-a,no-such-lib")
        message = str(ctx.exception)
        self.assertIn("no-such-lib", message)
        self.assertIn("lib-a", message)
        self.assertIn("lib-b", message)
        self.assertIn("lib-c", message)

    def test_exclude_everything_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.plugin.resolve_libraries("lib-a", exclude="lib-a")

    def test_no_registered_libraries_raises_value_error(self):
        empty_plugin = LibraryManagerPlugin()
        empty_plugin.store = LibraryConfigStore(self.tmp / "empty.json")
        with self.assertRaises(ValueError):
            empty_plugin.resolve_libraries("")

    def test_empty_libraries_uses_default_libraries_setting_when_configured(self):
        """对齐 obsidian-rag/config.py 的 default_libraries：libraries 留空
        时优先用这份设置里的范围，而不是不由分说查全部库。"""
        self.plugin._settings.set("default_libraries", ["lib-a", "lib-c"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-c"})

    def test_all_keyword_ignores_default_libraries_setting(self):
        self.plugin._settings.set("default_libraries", ["lib-a"])
        result = self.plugin.resolve_libraries("all")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_default_libraries_with_deleted_library_silently_skips_it(self):
        self.plugin._settings.set("default_libraries", ["lib-a", "no-longer-exists"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a"})

    def test_default_libraries_all_invalid_falls_back_to_all(self):
        """对齐 obsidian-rag resolve_entries："默认库全部失效→回退全部库
        （旧行为）"——不是报错，是安静地退回"全部库"这个更宽松的默认。"""
        self.plugin._settings.set("default_libraries", ["no-longer-exists"])
        result = self.plugin.resolve_libraries("")
        self.assertEqual({c.library_id for c in result}, {"lib-a", "lib-b", "lib-c"})

    def test_explicit_libraries_param_overrides_default_libraries_setting(self):
        self.plugin._settings.set("default_libraries", ["lib-a"])
        result = self.plugin.resolve_libraries("lib-b")
        self.assertEqual({c.library_id for c in result}, {"lib-b"})


class TestSelectionWriteGate(unittest.TestCase):
    """get_selection/propose_selection_changes/apply_selection_changes——
    对齐 obsidian-rag 的同名三件套（2026-09-23 全面功能审计B类缺口）：
    路径级勾选变更永远走写权限门禁确认，没有"此前非用户手写就直接生效"
    的快捷分支（同 official-library-summary::propose() 不一样，见
    plugin.py 里这三个方法的说明）。真实通过 PluginRuntime 走一遍完整
    生命周期，拿到真实 ctx.write_gate，不是假的（同
    official-library-summary/tests/test_plugin.py 的验证方式）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.rt = PluginRuntime(
            _REPO_ROOT / "plugins", state_file=self.tmp / "state.json", data_dir=self.tmp / "data"
        )
        self.rt.scan()
        self.rt.load("official-library-manager")
        self.rt.enable("official-library-manager")
        self.assertEqual(
            self.rt.plugins["official-library-manager"].state,
            PluginState.ENABLED,
            self.rt.plugins["official-library-manager"].error,
        )
        self.instance = self.rt.plugins["official-library-manager"].instance
        self.instance.store.add_library("lib1", "库1", "/vaults/lib1")

    def test_get_selection_on_fresh_library_is_empty(self):
        result = self.instance.get_selection("lib1")
        self.assertEqual(result, {"selection_in": [], "selection_out": []})

    def test_get_selection_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.instance.get_selection("no-such-lib")

    def test_propose_never_applies_directly(self):
        """硬性确认门禁：不管此前状态如何，propose 永远只生成提案，绝不
        直接生效——这是与库简介 propose() 最大的行为差异，必须显式钉住。"""
        result = self.instance.propose_selection_changes("lib1", [{"path": "private/", "action": "out"}])
        self.assertTrue(result["ok"])
        self.assertIn("proposal_id", result)
        self.assertIn("confirmation_code", result)
        # 还没 apply，get_selection 应该看不到任何变化
        self.assertEqual(self.instance.get_selection("lib1"), {"selection_in": [], "selection_out": []})

    def test_propose_then_apply_with_correct_code_takes_effect(self):
        propose_result = self.instance.propose_selection_changes(
            "lib1", [{"path": "private", "action": "out"}, {"path": "private/keep.md", "action": "in"}]
        )
        apply_result = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertTrue(apply_result["ok"])
        self.assertEqual(apply_result["selection_in"], ["private/keep.md"])
        self.assertEqual(apply_result["selection_out"], ["private"])
        self.assertEqual(
            self.instance.get_selection("lib1"),
            {"selection_in": ["private/keep.md"], "selection_out": ["private"]},
        )

    def test_apply_with_wrong_code_is_rejected_and_does_not_take_effect(self):
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        apply_result = self.instance.apply_selection_changes("lib1", propose_result["proposal_id"], "000000")
        self.assertFalse(apply_result["ok"])
        self.assertEqual(self.instance.get_selection("lib1"), {"selection_in": [], "selection_out": []})

    def test_apply_proposal_twice_second_time_rejected(self):
        """一次性有效——同 core/write_gate.py 的一次性提案纪律，这里只是
        确认它真的接上了。"""
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        first = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertTrue(first["ok"])
        second = self.instance.apply_selection_changes(
            "lib1", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertFalse(second["ok"])

    def test_propose_with_illegal_path_raises_before_creating_proposal(self):
        with self.assertRaises(ValueError):
            self.instance.propose_selection_changes("lib1", [{"path": "../escape.md", "action": "in"}])

    def test_propose_unknown_library_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.instance.propose_selection_changes("no-such-lib", [{"path": "a.md", "action": "in"}])

    def test_apply_with_mismatched_library_id_is_rejected(self):
        self.instance.store.add_library("lib2", "库2", "/vaults/lib2")
        propose_result = self.instance.propose_selection_changes("lib1", [{"path": "a.md", "action": "out"}])
        apply_result = self.instance.apply_selection_changes(
            "lib2", propose_result["proposal_id"], propose_result["confirmation_code"]
        )
        self.assertFalse(apply_result["ok"])


if __name__ == "__main__":
    unittest.main()
