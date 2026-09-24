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


if __name__ == "__main__":
    unittest.main()
