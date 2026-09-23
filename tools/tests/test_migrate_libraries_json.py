"""见 ../../AGENTS.md 测试纪律。旧格式 fixture 对照真实 obsidian-rag
项目 library.py 的 schema 核对过（`_blank_entry`/`save_registry` 的真实
字段名），不是凭印象编的。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent.parent
_REPO_ROOT = _TOOLS_DIR.parent
for p in (_REPO_ROOT, _REPO_ROOT / "plugins" / "official-library-manager"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from official_library_manager.config import LibraryConfigStore  # noqa: E402
from migrate_libraries_json import migrate  # noqa: E402

OLD_REGISTRY = {
    "libraries": [
        {
            "name": "工作笔记",
            "path": "/home/user/vaults/work",
            "selection_in": ["projects/rag-redo/notes.md"],
            "selection_out": ["archive", "private/secret.md"],
            "extensions": ["md", "pdf", "docx"],
            "summary": None,
            "agent_formats": None,
        },
        {
            "name": "读书笔记",
            "path": "/home/user/vaults/reading",
            "selection_in": [],
            "selection_out": [],
            "extensions": None,  # 旧项目允许为 None，缺省时用 DEFAULT_EXTENSIONS
            "summary": {"text": "一些摘要"},
            "agent_formats": ["pdf"],
        },
    ]
}


class TestMigrateLibrariesJson(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = LibraryConfigStore(self.tmp / "libraries.json")

    def test_migrates_basic_fields(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        libs = {c.name: c for c in self.store.list_libraries()}
        self.assertEqual(set(libs), {"工作笔记", "读书笔记"})
        work = libs["工作笔记"]
        self.assertEqual(work.root_path, "/home/user/vaults/work")
        self.assertEqual(work.selection_in, ["projects/rag-redo/notes.md"])
        self.assertEqual(work.selection_out, ["archive", "private/secret.md"])

    def test_extensions_get_dot_prefix(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        work = next(c for c in self.store.list_libraries() if c.name == "工作笔记")
        self.assertEqual(work.enabled_extensions, [".md", ".pdf", ".docx"])

    def test_missing_extensions_falls_back_to_old_default(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        reading = next(c for c in self.store.list_libraries() if c.name == "读书笔记")
        self.assertEqual(reading.enabled_extensions, [".md", ".pdf", ".docx"])

    def test_old_follow_maps_to_new_include(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        for cfg in self.store.list_libraries():
            self.assertEqual(cfg.new_file_default, "include")

    def test_old_exclude_maps_to_new_exclude(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="exclude")
        for cfg in self.store.list_libraries():
            self.assertEqual(cfg.new_file_default, "exclude")

    def test_old_include_maps_to_new_include(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="include")
        for cfg in self.store.list_libraries():
            self.assertEqual(cfg.new_file_default, "include")

    def test_library_id_slugified_from_name(self):
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        ids = {c.library_id for c in self.store.list_libraries()}
        self.assertEqual(len(ids), 2)

    def test_pure_chinese_name_gets_stable_hash_based_id_not_generic_fallback(self):
        """全中文库名（这个项目的实际用户很可能全是这种）不该落到清一色
        "library"/"library-2"这种和原名毫无关系、还依赖迁移顺序才能区分
        的编号——应该是"library-<8位哈希>"这种可追溯、和顺序无关的形式。"""
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        work = next(c for c in self.store.list_libraries() if c.name == "工作笔记")
        self.assertRegex(work.library_id, r"^library-[0-9a-f]{8}$")

    def test_same_chinese_name_always_hashes_to_same_id(self):
        """哈希是确定性的——同一个库名不管跑几次迁移都应该得到同一个id
        （这样重复运行迁移脚本、或者以后要核对id对不对时是可预测的，不是
        看运气）。"""
        entries = [{"name": "工作笔记", "path": "/a", "selection_in": [], "selection_out": []}]
        store2 = LibraryConfigStore(self.tmp / "other.json")
        migrate(self.store, entries, global_selection_new_files="follow")
        migrate(store2, entries, global_selection_new_files="follow")
        id1 = self.store.list_libraries()[0].library_id
        id2 = store2.list_libraries()[0].library_id
        self.assertEqual(id1, id2)

    def test_duplicate_names_get_unique_ids(self):
        entries = [
            {"name": "Notes", "path": "/a", "selection_in": [], "selection_out": []},
            {"name": "Notes", "path": "/b", "selection_in": [], "selection_out": []},
        ]
        migrate(self.store, entries, global_selection_new_files="follow")
        libs = self.store.list_libraries()
        self.assertEqual(len(libs), 2)
        ids = {c.library_id for c in libs}
        self.assertEqual(len(ids), 2)
        self.assertIn("notes", ids)
        self.assertIn("notes-2", ids)

    def test_entry_missing_path_is_skipped_not_crash(self):
        entries = [{"name": "缺路径", "selection_in": [], "selection_out": []}]
        migrate(self.store, entries, global_selection_new_files="follow")
        self.assertEqual(self.store.list_libraries(), [])

    def test_migration_output_immediately_usable_by_selection_algorithm(self):
        """迁移完的数据要能被真实 decide_included 算法直接吃——不是"格式
        对了就算过"，是真的能拿来判定文件该不该收。"""
        migrate(self.store, OLD_REGISTRY["libraries"], global_selection_new_files="follow")
        from official_library_manager.selection import decide_included

        work = next(c for c in self.store.list_libraries() if c.name == "工作笔记")
        included, reason = decide_included(
            "private/secret.md",
            selection_in=work.selection_in,
            selection_out=work.selection_out,
            new_file_default=work.new_file_default,
            enabled_extensions=work.enabled_extensions,
        )
        self.assertFalse(included)


if __name__ == "__main__":
    unittest.main()
