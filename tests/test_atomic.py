"""core/atomic.py 的单元测试：原子写助手的行为边界。"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.atomic import atomic_write_bytes, atomic_write_text


class TestAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_write_text_creates_file_with_content(self):
        target = self.tmp / "nested" / "dir" / "value.json"
        atomic_write_text(target, json.dumps({"a": 1}, ensure_ascii=False))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})

    def test_replacement_leaves_no_tmp_residue(self):
        target = self.tmp / "state.json"
        atomic_write_text(target, "first")
        atomic_write_text(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_failed_write_leaves_previous_target_intact_and_no_tmp(self):
        target = self.tmp / "state.json"
        atomic_write_text(target, "previous")

        def _boom(tmp_path: Path, _target: Path) -> None:
            tmp_path.write_text("half-written garbage", encoding="utf-8")
            raise OSError("disk on fire")

        with patch("core.atomic._replace_with_retry", side_effect=_boom):
            with self.assertRaises(OSError):
                atomic_write_text(target, "new content")
        self.assertEqual(target.read_text(encoding="utf-8"), "previous")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

    def test_permission_error_is_retried_then_replaced(self):
        target = self.tmp / "state.json"
        real_replace = __import__("os").replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("handle still open")
            return real_replace(src, dst)

        with patch("core.atomic.os.replace", side_effect=flaky_replace):
            atomic_write_text(target, "after retry")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "after retry")

    def test_unique_tmp_name_allows_concurrent_writers_to_same_target(self):
        """同一目标文件被多线程并发写时，各线程用各自唯一命名的临时文件，
        互不踩踏（index_progress 心跳/进度并发的真实场景）。"""
        import threading

        target = self.tmp / "concurrent.json"
        errors: list[Exception] = []

        def _worker(tag: str) -> None:
            try:
                for round_index in range(30):
                    atomic_write_text(target, json.dumps({"tag": tag, "i": round_index}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(f"t{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn(data["tag"], {"t0", "t1", "t2", "t3"})
        residue = [path for path in self.tmp.glob("*.tmp")]
        self.assertEqual(residue, [])

    def test_write_bytes_roundtrip(self):
        target = self.tmp / "archive.zip"
        atomic_write_bytes(target, b"PK\x03\x04 payload")
        self.assertEqual(target.read_bytes(), b"PK\x03\x04 payload")

    def test_concurrent_writers_survive_a_long_sharing_violation_storm(self):
        """并发写 + 目标句柄被长时间占用时也必须扛住（2026-09-27 全量回归
        实测到的偶发 `PermissionError(13, 'Access is denied')` 的护栏）。

        原退避总预算只有 30ms（0/0.01/0.02 三档），机器繁忙时 120 次并发
        replace 必然有写炸的——一个偶发红会让整道回归门禁不可信。这里让
        replace 前 6 次都抛"句柄被占"，仍必须最终写成功。
        """
        import os as _os
        import threading

        target = self.tmp / "storm.json"
        real_replace = _os.replace
        calls = {"n": 0}
        lock = threading.Lock()

        def _stormy_replace(src, dst):
            with lock:
                calls["n"] += 1
                attempt = calls["n"]
            if attempt <= 6:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        errors: list[Exception] = []

        def _worker() -> None:
            try:
                atomic_write_text(target, json.dumps({"written": True}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with patch("core.atomic.os.replace", side_effect=_stormy_replace):
            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [], f"扛不住 6 次连续占用：{errors}")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"written": True})

    def test_non_sharing_oserror_is_not_retried(self):
        """非"句柄被占"的 OSError（磁盘满、只读文件系统）必须**立即**抛出，
        不能被拖成 0.8s 的静默重试——那会把真实故障伪装成"慢"。"""
        import os as _os
        import time as _time

        target = self.tmp / "readonly.json"
        real_replace = _os.replace
        calls = {"n": 0}

        def _no_space(src, dst):
            calls["n"] += 1
            raise OSError(28, "No space left on device")

        started = _time.monotonic()
        with patch("core.atomic.os.replace", side_effect=_no_space):
            with self.assertRaises(OSError) as caught:
                atomic_write_text(target, "content")
        elapsed = _time.monotonic() - started

        self.assertEqual(calls["n"], 1, "磁盘满重试没有意义，必须一次就抛")
        self.assertNotIsInstance(caught.exception, PermissionError)
        self.assertLess(elapsed, 0.2, f"不该退避这么久，实际 {elapsed:.3f}s")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])
        del real_replace


if __name__ == "__main__":
    unittest.main()
