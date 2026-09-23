"""core/settings.py 的单元测试：真实写盘、真实重启后读回，不是纸面设计。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.settings import SettingsStore  # noqa: E402


class TestSettingsStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "settings.json"

    def test_get_missing_key_returns_default(self):
        store = SettingsStore(self.path)
        self.assertEqual(store.get("nope", "fallback"), "fallback")

    def test_set_then_get_round_trips(self):
        store = SettingsStore(self.path)
        store.set("fusion_dense_weight", 1.5)
        self.assertEqual(store.get("fusion_dense_weight", 1.0), 1.5)

    def test_persists_across_new_instance(self):
        store = SettingsStore(self.path)
        store.set("default_libraries", ["a", "b"])
        store2 = SettingsStore(self.path)
        self.assertEqual(store2.get("default_libraries", []), ["a", "b"])

    def test_file_is_readable_json(self):
        store = SettingsStore(self.path)
        store.set("k", "v")
        import json

        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["k"], "v")

    def test_type_mismatch_falls_back_to_default_not_crash(self):
        store = SettingsStore(self.path)
        store.set("rerank_candidates", "fifty")  # 错误类型：字符串而不是 int
        self.assertEqual(store.get("rerank_candidates", 50), 50)

    def test_bool_not_treated_as_int(self):
        """Python 里 bool 是 int 子类——校验必须先判 bool，否则 True/False
        会被误判成合法的 int 设置值，反过来 int 值也不该被当成合法 bool。"""
        store = SettingsStore(self.path)
        store.set("count", 3)
        self.assertEqual(store.get("count", True), True)  # 类型不符，回退 default
        store.set("flag", 1)
        self.assertEqual(store.get("flag", False), False)

    def test_unset_removes_key_and_falls_back(self):
        store = SettingsStore(self.path)
        store.set("k", "v")
        store.unset("k")
        self.assertEqual(store.get("k", "default"), "default")

    def test_unset_missing_key_is_noop_not_error(self):
        store = SettingsStore(self.path)
        store.unset("never-set")  # 不该抛异常

    def test_all_returns_snapshot_not_live_reference(self):
        store = SettingsStore(self.path)
        store.set("k", "v")
        snapshot = store.all()
        snapshot["k"] = "mutated"
        self.assertEqual(store.get("k", None), "v")

    def test_corrupted_file_degrades_to_empty_not_crash(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        store = SettingsStore(self.path)
        self.assertEqual(store.get("anything", "default"), "default")

    def test_missing_file_on_first_use_is_fine(self):
        store = SettingsStore(self.tmp / "does-not-exist-yet.json")
        self.assertEqual(store.get("k", "d"), "d")
        store.set("k", "v")
        self.assertTrue((self.tmp / "does-not-exist-yet.json").is_file())

    def test_none_default_skips_type_check(self):
        store = SettingsStore(self.path)
        store.set("k", 42)
        self.assertEqual(store.get("k"), 42)


if __name__ == "__main__":
    unittest.main()
