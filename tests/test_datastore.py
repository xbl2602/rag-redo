from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.datastore import DataAccessError, DataStore


class TestDataStore(unittest.TestCase):
    def test_owner_can_read_own_private_write(self):
        ds = DataStore()
        ds.write("plugin-a", "contract.x", 123)
        self.assertEqual(ds.read("plugin-a", "contract.x"), 123)

    def test_non_owner_cannot_read_private(self):
        ds = DataStore()
        ds.write("plugin-a", "contract.x", 123)
        with self.assertRaises(DataAccessError):
            ds.read("plugin-b", "contract.x")

    def test_public_write_readable_by_anyone(self):
        ds = DataStore()
        ds.write("plugin-a", "contract.public", "hi", public=True)
        self.assertEqual(ds.read("plugin-b", "contract.public"), "hi")

    def test_non_owner_cannot_overwrite(self):
        ds = DataStore()
        ds.write("plugin-a", "contract.x", 1)
        with self.assertRaises(DataAccessError):
            ds.write("plugin-b", "contract.x", 2)

    def test_owner_can_overwrite_own(self):
        ds = DataStore()
        ds.write("plugin-a", "contract.x", 1)
        ds.write("plugin-a", "contract.x", 2)
        self.assertEqual(ds.read("plugin-a", "contract.x"), 2)

    def test_read_missing_key_returns_none(self):
        ds = DataStore()
        self.assertIsNone(ds.read("plugin-a", "no.such.key"))

    def test_storage_handles_are_owner_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DataStore(root)
            left = store.storage_handle("plugin-a", allowed=True)
            right = store.storage_handle("plugin-b", allowed=True)
            self.assertNotEqual(left.path("state"), right.path("state"))
            self.assertEqual(left.path("state"), left.path("state"))

    def test_storage_handle_denies_plugin_without_data_write_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            handle = DataStore(Path(tmp)).storage_handle("plugin-a", allowed=False)
            with self.assertRaises(DataAccessError):
                handle.directory("state")

    def test_legacy_path_is_preserved_behind_gateway(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handle = DataStore(root).storage_handle("plugin-a", allowed=True)
            self.assertEqual(
                handle.file("libraries.json", legacy="libraries.json"),
                root / "libraries.json",
            )

    def test_issue_path_denies_plugin_without_data_write_grant(self):
        """issue_path 的权限校验以 storage_handle 的授权登记为唯一依据——
        未声明 data_write 的插件即使拿到 DataStore 本体，也没有申领持久化
        路径的通道（此前是公开方法，handle 门禁可被直接绕过）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = DataStore(Path(tmp))
            store.storage_handle("plugin-no-write", allowed=False)
            with self.assertRaises(DataAccessError):
                store.issue_path("plugin-no-write", "state")
            with self.assertRaises(DataAccessError):
                store.issue_path("totally-unknown-plugin", "state")

    def test_granted_storage_plugin_can_issue_its_own_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DataStore(root)
            handle = store.storage_handle("plugin-a", allowed=True)
            self.assertEqual(handle.path("state"), root / "plugin_data" / "plugin-a" / "state")

    def test_shared_legacy_namespace_stays_allowed_for_multiple_owners(self):
        """多插件共享同一 legacy 目录是刻意设计（index_generations 分段存储
        被 lexical-bm25/vector-store-chroma/visual-wemm 共用）——同路径不同
        owner 的重复申领不得报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = DataStore(root)
            for plugin_id in ("plugin-a", "plugin-b", "plugin-c"):
                handle = store.storage_handle(plugin_id, allowed=True)
                self.assertEqual(
                    handle.directory("index_generations", legacy="index_generations"),
                    root / "index_generations",
                )


if __name__ == "__main__":
    unittest.main()
