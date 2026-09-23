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


if __name__ == "__main__":
    unittest.main()
