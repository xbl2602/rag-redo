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

from official_library_summary.summary_store import SummaryStore  # noqa: E402


class TestSummaryStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = SummaryStore(self.tmp / "summaries.json")

    def test_get_unknown_library_returns_blank_state(self):
        entry = self.store.get("lib1")
        self.assertEqual(entry["text"], "")
        self.assertEqual(entry["source"], "none")

    def test_set_then_get_roundtrips(self):
        self.store.set("lib1", "这是简介", source="ai", fingerprint="abc123", model="test-model")
        entry = self.store.get("lib1")
        self.assertEqual(entry["text"], "这是简介")
        self.assertEqual(entry["source"], "ai")
        self.assertEqual(entry["fingerprint"], "abc123")
        self.assertEqual(entry["model"], "test-model")
        self.assertIsNotNone(entry["updated_at"])

    def test_set_rejects_invalid_source(self):
        with self.assertRaises(ValueError):
            self.store.set("lib1", "text", source="none")

    def test_set_rejects_overlong_text(self):
        with self.assertRaises(ValueError):
            self.store.set("lib1", "字" * 301, source="ai")

    def test_persists_across_instances(self):
        self.store.set("lib1", "简介", source="user")
        store2 = SummaryStore(self.tmp / "summaries.json")
        self.assertEqual(store2.get("lib1")["text"], "简介")

    def test_libraries_are_independent(self):
        self.store.set("lib1", "库一简介", source="ai")
        self.store.set("lib2", "库二简介", source="ai")
        self.assertEqual(self.store.get("lib1")["text"], "库一简介")
        self.assertEqual(self.store.get("lib2")["text"], "库二简介")

    def test_corrupted_file_falls_back_to_blank_not_crash(self):
        path = self.tmp / "summaries.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{", encoding="utf-8")
        store = SummaryStore(path)
        self.assertEqual(store.get("lib1")["source"], "none")


if __name__ == "__main__":
    unittest.main()
