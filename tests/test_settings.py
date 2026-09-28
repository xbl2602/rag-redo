"""core/settings.py 的单元测试：真实写盘、真实重启后读回，不是纸面设计。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class TestSettingsHotReload(unittest.TestCase):
    """缺陷5：长驻进程（GUI / MCP）此前只在构造时把 settings.json 读进内存，
    另一个进程改完设置后**永远看不到**——旧项目靠
    `obsidian-rag/server.py:950-961 _wemm_cfg()` 每次现读（`reload_config()`）
    纪律兜住（"长驻 MCP 进程配置一律经 config.reload_config() 现读"）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "settings.json"

    def _write_external(self, data: dict) -> None:
        """模拟"另一个进程（GUI）改了同一个 settings.json"。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_reload_picks_up_external_change(self):
        store = SettingsStore(self.path)
        store.set("wemm_backend", "off")
        self._write_external({"wemm_backend": "on"})
        # 显式 reload 是旧项目 reload_config() 的形状：进程边界处主动现读
        self.assertTrue(store.reload())
        self.assertEqual(store.get("wemm_backend", "off"), "on")

    def test_reload_reports_false_when_file_unchanged(self):
        store = SettingsStore(self.path)
        store.set("k", "v")
        # 本进程刚写完盘时戳被标成"未知"（为了收进可能并发的外部写入），
        # 第一次现读必须真读一次；之后文件没变就不再读盘。
        self.assertTrue(store.reload())
        self.assertFalse(store.reload(), "文件没变就不该重复读盘")
        self.assertEqual(store.get("k"), "v")

    def test_auto_reload_sees_external_change_without_explicit_reload(self):
        store = SettingsStore(self.path)
        store.set("wemm_backend", "off")
        self._write_external({"wemm_backend": "on"})
        self.assertEqual(store.get("wemm_backend", "off"), "on")

    def test_auto_reload_does_not_reread_while_file_unchanged(self):
        """不能让每次 get() 都读盘（热路径上会被打爆）——只在 mtime/size 变了
        才真读一次。"""
        store = SettingsStore(self.path, auto_reload=True)
        store.set("k", "v")
        real_load = store._load
        calls = []

        def counting_load():
            calls.append(1)
            return real_load()

        with patch.object(store, "_load", side_effect=counting_load):
            for _ in range(5):
                store.get("k")
            self.assertEqual(store.get("k"), "v")
            after_construction = len(calls)
            self._write_external({"k": "v2"})
            self.assertEqual(store.get("k"), "v2")
            after_change = len(calls)
            for _ in range(5):
                store.get("k")
            self.assertEqual(store.get("k"), "v2")
            self.assertEqual(len(calls), after_change, "文件没再变就不该继续读盘")
        self.assertGreater(after_change, after_construction)

    def test_auto_reload_disabled_keeps_startup_snapshot(self):
        """LEGACY 纪律的另一半：长任务（一次索引要跑几分钟）期间不该被另一个
        进程的设置改动带着跑偏，所以提供显式 opt-out。"""
        store = SettingsStore(self.path, auto_reload=False)
        store.set("k", "v1")
        self._write_external({"k": "v2"})
        self.assertEqual(store.get("k"), "v1")
        store.reload()
        self.assertEqual(store.get("k"), "v2")

    def test_reload_is_safe_when_file_vanished(self):
        """stat/读盘失败必须 fail-open 保留内存值：原子写（tmp+replace）保证
        正常路径下文件不会消失，但杀软/只读挂载这类环境问题不该把长驻进程的
        全部设置清空（对齐 core/singleton.py 的 fail-open 原则）。"""
        store = SettingsStore(self.path)
        store.set("k", "v")
        self.path.unlink()
        self.assertFalse(store.reload())
        self.assertEqual(store.get("k", "default"), "v")

    def test_reload_after_external_removal_of_key_falls_back_to_default(self):
        store = SettingsStore(self.path)
        store.set("k", "v")
        self._write_external({"other": 1})
        store.reload()
        self.assertEqual(store.get("k", "default"), "default")

    def test_set_still_wins_over_stale_disk_and_is_visible_to_others(self):
        store = SettingsStore(self.path, auto_reload=True)
        store.set("k", "v1")
        self._write_external({"k": "external"})
        store.reload()
        self.assertEqual(store.get("k"), "external")
        store.set("k", "v2")
        other = SettingsStore(self.path)
        self.assertEqual(other.get("k"), "v2")

    def test_corrupted_external_file_degrades_without_crash(self):
        store = SettingsStore(self.path, auto_reload=True)
        store.set("k", "v")
        self.path.write_text("{not json", encoding="utf-8")
        store.reload()
        self.assertEqual(store.get("k", "default"), "default")


if __name__ == "__main__":
    unittest.main()
