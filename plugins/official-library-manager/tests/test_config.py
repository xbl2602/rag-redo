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

    def test_corrupted_file_degrades_to_empty_not_crash(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        store = LibraryConfigStore(self.path)
        self.assertEqual(store.list_libraries(), [])

    def test_remove_library(self):
        store = LibraryConfigStore(self.path)
        store.add_library("lib1", "我的库", "/vaults/lib1")
        store.remove_library("lib1")
        self.assertEqual(store.list_libraries(), [])


if __name__ == "__main__":
    unittest.main()
