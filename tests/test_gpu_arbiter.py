"""core/gpu_arbiter.py 回归测试：显存探测/等待/驱逐全部 fail-open，不碰
真实 GPU/子进程（探测函数与 HTTP 全部 mock），移植自旧项目
obsidian-rag/tests/test_gpu_arbiter.py 验证过的覆盖点，风格改用本仓库
统一的 unittest.TestCase。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.gpu_arbiter as ga  # noqa: E402


class TestVramFreeGb(unittest.TestCase):
    def setUp(self):
        ga._cache = (None, 0.0)

    def test_fail_open_when_no_probe_available(self):
        with patch.object(ga, "subprocess") as fake_sp, patch.dict("sys.modules", {"torch": None}):
            fake_sp.run.side_effect = OSError("no nvidia-smi")
            self.assertIsNone(ga.vram_free_gb(max_age=0.0))

    def test_nvidia_smi_fallback_parses_mib(self):
        with patch.object(ga, "subprocess") as fake_sp, patch.dict("sys.modules", {"torch": None}):
            fake_sp.run.return_value.stdout = b"6133\n"
            self.assertAlmostEqual(ga.vram_free_gb(max_age=0.0), 6133 / 1024.0)

    def test_result_cached_within_max_age(self):
        with patch.object(ga, "subprocess") as fake_sp, patch.dict("sys.modules", {"torch": None}):
            fake_sp.run.return_value.stdout = b"1024\n"
            first = ga.vram_free_gb(max_age=60.0)
            fake_sp.run.return_value.stdout = b"9999\n"
            second = ga.vram_free_gb(max_age=60.0)
        self.assertEqual(first, second)  # 第二次探测被缓存挡住，没有真的重新跑


class TestWaitForVram(unittest.TestCase):
    def test_sufficient_vram_returns_immediately(self):
        with patch.object(ga, "vram_free_gb", return_value=7.9):
            self.assertTrue(ga.wait_for_vram(5.5, timeout_s=1, poll_s=0.05))

    def test_insufficient_vram_times_out(self):
        with patch.object(ga, "vram_free_gb", return_value=2.0):
            start = time.time()
            result = ga.wait_for_vram(5.5, timeout_s=0.3, poll_s=0.05)
            self.assertFalse(result)
            self.assertLess(time.time() - start, 2.0)

    def test_probe_failure_fails_open(self):
        with patch.object(ga, "vram_free_gb", return_value=None):
            self.assertTrue(ga.wait_for_vram(5.5, timeout_s=1, poll_s=0.05))

    def test_log_callback_invoked_once_when_waiting(self):
        messages = []
        with patch.object(ga, "vram_free_gb", side_effect=[2.0, 6.0]):
            self.assertTrue(ga.wait_for_vram(5.5, timeout_s=5, poll_s=0.01, log=messages.append))
        self.assertEqual(len(messages), 1)
        self.assertIn("空闲显存", messages[0])


class _FakeResp:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload.encode("utf-8")


class TestRequestEvict(unittest.TestCase):
    def test_ok_response_returns_true(self):
        with patch.object(ga, "urllib") as fake_urllib:
            fake_urllib.request.Request = lambda *a, **k: object()
            fake_urllib.request.urlopen.return_value = _FakeResp('{"ok": true}')
            self.assertTrue(ga.request_evict("http://127.0.0.1:9101"))

    def test_unreachable_service_folds_to_false(self):
        with patch.object(ga, "urllib") as fake_urllib:
            fake_urllib.request.Request = lambda *a, **k: object()
            fake_urllib.request.urlopen.side_effect = OSError("refused")
            self.assertFalse(ga.request_evict("http://127.0.0.1:9101"))


if __name__ == "__main__":
    unittest.main()
