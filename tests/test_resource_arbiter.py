from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.resource_arbiter import ResourceArbiter
from core.runtime import PluginRuntime


class TestResourceArbiter(unittest.TestCase):
    def test_first_acquire_succeeds(self):
        arb = ResourceArbiter()
        self.assertTrue(arb.acquire("gpu:0", "holder-a", priority=1))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_lower_priority_cannot_preempt(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=5)
        self.assertFalse(arb.acquire("gpu:0", "holder-b", priority=1))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_higher_priority_preempts_and_calls_callback(self):
        arb = ResourceArbiter()
        preempted = []
        arb.acquire("gpu:0", "holder-a", priority=1, on_preempt=lambda: preempted.append("a"))
        self.assertTrue(arb.acquire("gpu:0", "holder-b", priority=5))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-b")
        self.assertEqual(preempted, ["a"])

    def test_release_frees_resource(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        arb.release("gpu:0", "holder-a")
        self.assertIsNone(arb.holder_of("gpu:0"))

    def test_release_by_non_holder_is_noop(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        arb.release("gpu:0", "holder-b")
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_reacquire_by_same_holder_is_idempotent(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=1)
        self.assertTrue(arb.acquire("gpu:0", "holder-a", priority=1))

    def test_probe_fail_open_on_exception(self):
        def broken_check():
            raise RuntimeError("探测失败")

        self.assertTrue(ResourceArbiter.probe(broken_check))

    def test_probe_returns_actual_result_when_no_exception(self):
        self.assertFalse(ResourceArbiter.probe(lambda: False))
        self.assertTrue(ResourceArbiter.probe(lambda: True))

    def test_equal_priority_does_not_preempt_by_default(self):
        arb = ResourceArbiter()
        arb.acquire("gpu:0", "holder-a", priority=5)
        self.assertFalse(arb.acquire("gpu:0", "holder-b", priority=5))
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")

    def test_equal_priority_preempts_when_opted_in(self):
        """preempt_equal=True 对应同一层级里"谁刚需要谁拿"（旧项目 WEMM/MinerU
        互相抢占显存），不需要靠优先级数值分高低才能互相驱逐对方。"""
        arb = ResourceArbiter()
        preempted = []
        arb.acquire("gpu:0", "wemm", priority=10, on_preempt=lambda: preempted.append("wemm-evicted"), preempt_equal=True)
        self.assertTrue(arb.acquire("gpu:0", "mineru", priority=10, preempt_equal=True))
        self.assertEqual(preempted, ["wemm-evicted"])
        self.assertEqual(arb.holder_of("gpu:0"), "mineru")
        # 反过来 wemm 再申请一次同样能把 mineru 挤回去——双向、不是单向的
        preempted.clear()
        self.assertTrue(arb.acquire("gpu:0", "wemm", priority=10, preempt_equal=True))
        self.assertEqual(arb.holder_of("gpu:0"), "wemm")

    def test_full_acquire_preempt_release_flow(self):
        """完整流程：A拿到→B抢占A→B释放→资源空闲，对应 ROADMAP.md Phase 0
        验收标准"资源仲裁器能演示一次申请锁→抢占→释放的完整流程"。"""
        arb = ResourceArbiter()
        preempted = []
        self.assertTrue(
            arb.acquire("gpu:0", "plugin-a", priority=1, on_preempt=lambda: preempted.append("a-evicted"))
        )
        self.assertTrue(arb.acquire("gpu:0", "plugin-b", priority=9))
        self.assertEqual(preempted, ["a-evicted"])
        self.assertEqual(arb.holder_of("gpu:0"), "plugin-b")
        arb.release("gpu:0", "plugin-b")
        self.assertIsNone(arb.holder_of("gpu:0"))


class TestCrossProcessResourceArbiter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock_dir = self.tmp / "resource_locks"

    def _child_result(
        self,
        resource_id: str,
        holder_id: str,
        priority: int = 0,
        *,
        preempt_equal: bool = False,
        timeout: float = 0.3,
    ):
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from core.resource_arbiter import ResourceArbiter\n"
            f"arb = ResourceArbiter(lock_dir=Path({str(self.lock_dir)!r}), "
            f"preempt_timeout_s={timeout}, poll_interval_s=0.01)\n"
            f"acquired = arb.acquire({resource_id!r}, {holder_id!r}, priority={priority}, "
            f"preempt_equal={preempt_equal}, on_preempt=lambda: print('PREEMPTED', flush=True))\n"
            "print('ACQUIRED' if acquired else 'REJECTED')\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_higher_priority_process_preempts_and_transfers_file_lock(self):
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        preempted = []
        self.assertTrue(
            arb.acquire(
                "gpu:0",
                "holder-a",
                priority=10,
                on_preempt=lambda: preempted.append("holder-a"),
            )
        )

        result = self._child_result("gpu:0", "holder-b", priority=100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["ACQUIRED"])
        self.assertEqual(preempted, ["holder-a"])
        self.assertIsNone(arb.holder_of("gpu:0"))
        self.assertTrue(arb.acquire("gpu:0", "holder-parent-again", priority=100))
        arb.release("gpu:0", "holder-parent-again")

    def test_equal_priority_across_processes_transfers_when_preempt_equal(self):
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        preempted = []
        self.assertTrue(
            arb.acquire(
                "gpu:0",
                "wemm",
                priority=10,
                on_preempt=lambda: preempted.append("wemm"),
                preempt_equal=True,
            )
        )
        result = self._child_result("gpu:0", "mineru", priority=10, preempt_equal=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["ACQUIRED"])
        self.assertEqual(preempted, ["wemm"])

    def test_same_holder_across_processes_refreshes_old_process_lease(self):
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        preempted = []
        self.assertTrue(
            arb.acquire(
                "gpu:0",
                "official-text-retrieval-gpu",
                priority=100,
                on_preempt=lambda: preempted.append("unloaded"),
            )
        )
        result = self._child_result("gpu:0", "official-text-retrieval-gpu", priority=100)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["ACQUIRED"])
        self.assertEqual(preempted, ["unloaded"])

    def test_lower_priority_process_times_out_without_preempting(self):
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        self.assertTrue(arb.acquire("gpu:0", "holder-a", priority=100))
        result = self._child_result("gpu:0", "holder-b", priority=10, timeout=0.1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["REJECTED"])
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")
        arb.release("gpu:0", "holder-a")

    def test_process_death_releases_file_lock(self):
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from pathlib import Path\n"
            "from core.resource_arbiter import ResourceArbiter\n"
            f"arb = ResourceArbiter(lock_dir=Path({str(self.lock_dir)!r}))\n"
            "if not arb.acquire('gpu:0', 'holder-child'):\n"
            "    raise SystemExit(2)\n"
            "print('LOCKED', flush=True)\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout = process.stdout
            self.assertIsNotNone(stdout)
            if stdout is None:
                raise AssertionError("missing child stdout")
            self.assertEqual(stdout.readline().strip(), "LOCKED")
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            else:
                process.kill()
            process.wait(timeout=10)

            arb = ResourceArbiter(lock_dir=self.lock_dir)
            self.assertTrue(arb.acquire("gpu:0", "holder-parent"))
            self.assertEqual(arb.holder_of("gpu:0"), "holder-parent")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_non_holder_release_does_not_close_file_lock(self):
        arb = ResourceArbiter(lock_dir=self.lock_dir)
        self.assertTrue(arb.acquire("gpu:0", "holder-a"))

        arb.release("gpu:0", "holder-b")
        self.assertEqual(arb.holder_of("gpu:0"), "holder-a")
        self.assertFalse(self._child_result("gpu:0", "holder-b").stdout.strip().endswith("ACQUIRED"))

        arb.release("gpu:0", "holder-a")
        self.assertTrue(self._child_result("gpu:0", "holder-b").stdout.strip().endswith("ACQUIRED"))

    def test_lock_keys_are_safe_and_distinct(self):
        first = ResourceArbiter(lock_dir=self.lock_dir)
        second = ResourceArbiter(lock_dir=self.lock_dir)
        self.assertTrue(first.acquire("../../gpu:0", "holder-a"))
        self.assertTrue(second.acquire("gpu:0", "holder-b"))

        names = sorted(path.name for path in self.lock_dir.glob("*.lock"))
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertRegex(name, r"^[0-9a-f]{64}\.lock$")
        first.release("../../gpu:0", "holder-a")
        second.release("gpu:0", "holder-b")

    def test_plugin_runtime_uses_default_resource_lock_dir(self):
        runtime = PluginRuntime(self.tmp / "plugins", data_dir=self.tmp / "data")
        self.assertEqual(
            runtime.resource_arbiter.lock_dir,
            self.tmp / "data" / "resource_locks",
        )


if __name__ == "__main__":
    unittest.main()
