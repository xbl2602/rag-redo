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

    def test_write_bytes_roundtrip(self):
        target = self.tmp / "archive.zip"
        atomic_write_bytes(target, b"PK\x03\x04 payload")
        self.assertEqual(target.read_bytes(), b"PK\x03\x04 payload")


if __name__ == "__main__":
    unittest.main()
