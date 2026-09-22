from __future__ import annotations

import sys
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


if __name__ == "__main__":
    unittest.main()
