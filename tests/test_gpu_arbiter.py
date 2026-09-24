"""core/gpu_arbiter.py 回归测试：显存探测/等待/驱逐全部 fail-open，不碰
真实 GPU/子进程（探测函数与 HTTP 全部 mock），移植自旧项目
obsidian-rag/tests/test_gpu_arbiter.py 验证过的覆盖点，风格改用本仓库
统一的 unittest.TestCase。
"""
from __future__ import annotations

import sys
import types
import tempfile
import shutil
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.gpu_arbiter as ga  # noqa: E402
from core.gpu_arbiter import CudaCooldownGate  # noqa: E402


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


class TestCudaCooldownGate(unittest.TestCase):
    """CUDA 冷却期状态机（对齐 obsidian-rag/index.py::_cuda_probe/_cuda_ready/
    _cooldown_cuda/_report_device，2026-09-25 按旧项目原行为补齐 embed/rerank
    的"单次降级永不切回"已知简化）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _gate(self, **kwargs) -> CudaCooldownGate:
        return CudaCooldownGate(**kwargs)

    def test_cooldown_window_forces_cpu_until_expiry(self):
        gate = self._gate(cooldown_seconds=300)
        gate.cooldown("boom")
        with patch("core.gpu_arbiter.time") as fake_time:
            fake_time.time.return_value = gate._cooldown_until - 1
            self.assertFalse(gate.ready(), "冷却期内必须直接用 CPU")
            fake_time.time.return_value = gate._cooldown_until + 1
        # 到期后探测：探测失败 → 重新进入冷却
        with patch.object(gate, "probe", return_value=False):
            self.assertFalse(gate.ready())
            self.assertGreater(gate._cooldown_until, 0)

    def test_probe_pass_allows_cuda_after_cooldown_expires(self):
        gate = self._gate(cooldown_seconds=300)
        gate.cooldown("boom")
        expiry = gate._cooldown_until
        with patch("core.gpu_arbiter.time") as fake_time, patch.object(gate, "probe", return_value=True):
            fake_time.time.return_value = expiry + 1  # 冷却到期后探测通过 → 允许 CUDA
            self.assertTrue(gate.ready())

    def test_probe_failure_reenters_cooldown(self):
        gate = self._gate(cooldown_seconds=300)
        with patch.object(gate, "probe", return_value=False) as probe:
            self.assertFalse(gate.ready())
            probe.assert_called_once()
            first_until = gate._cooldown_until
            self.assertFalse(gate.ready())
            self.assertEqual(gate._cooldown_until, first_until, "冷却期内不得重复探测刷新窗口")

    def test_cooldown_writes_failure_diagnostics(self):
        state_file = self.tmp / "device_state.json"
        gate = self._gate(state_file=state_file)
        gate.cooldown("CUDA out of memory")
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(data["device"], "cuda")
        self.assertIn("out of memory", data["reason"])

    def test_report_device_success_overwrites_failure_record(self):
        state_file = self.tmp / "device_state.json"
        gate = self._gate(state_file=state_file)
        gate.cooldown("boom")
        gate.report_device("cuda", note="auto-switched-back")
        data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertTrue(data["healthy"])
        self.assertEqual(data["note"], "auto-switched-back")

    def test_probe_allocates_64mb_tensor_only_when_cuda_available(self):
        gate = self._gate()
        with patch("torch.cuda.is_available", return_value=False):
            self.assertFalse(gate.probe())
        allocated = {}
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: True),
            empty=lambda n, *, dtype, device: allocated.setdefault("n", n),
            uint8="uint8",
        )
        with patch.dict("sys.modules", {"torch": fake_torch}):
            self.assertTrue(gate.probe())
        self.assertEqual(allocated["n"], CudaCooldownGate.PROBE_TENSOR_BYTES)


class _StubGateReady:
    """始终 ready 的冷却门替身——CUDA 分支测试注入用。"""

    def ready(self):
        return True

    def cooldown(self, reason):
        pass

    def report_device(self, device, note=""):
        pass
