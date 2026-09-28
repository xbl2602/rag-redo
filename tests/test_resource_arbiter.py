from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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


class TestArbiterLockOrdering(unittest.TestCase):
    """锁序回归（缺陷 A：on_preempt 回调在持有 `_guard` 时执行 → 与插件侧
    `self._lock → GPU_LOCK → _guard` 的正常编码路径构成 ABBA 死锁）。

    两条腿都钉住：
    1. 结构性断言——回调被调用时 `_guard` 不在**当前线程**手里
       （进程内抢占路径 + 后台监控处理跨进程请求路径各一条）；
    2. 真实多线程复现——一条线程持"插件锁"卡在 acquire()，另一条线程走
       on_preempt 回调去抢同一把"插件锁"，两边都必须在超时内推进完。

    为什么同时要结构断言：死锁一旦发生就是两条线程永久互等，测试只能靠
    join 超时判定"卡住了"，说不清是哪条锁序写反了；结构断言直接把
    "回调执行时持有哪些仲裁器锁"钉死，回归时定位快得多。多线程那条负责
    保证"结构对了之后真的不死锁"。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock_dir = self.tmp / "resource_locks"

    @staticmethod
    def _guard_owned_by_current_thread(arb: ResourceArbiter) -> bool:
        """当前线程是否持有 arb._guard。RLock 没有公开的"是否被持有"，但
        CPython 的 _thread.RLock 一直有 `_is_owned()`；这里做能力探测，探测
        不可用时由测试显式失败，不允许"静默当成没持有"让断言空过。"""
        probe = getattr(arb._guard, "_is_owned", None)
        if not callable(probe):
            self_fail("当前解释器的 RLock 没有 _is_owned()，无法追踪 _guard 持有者")
            return True
        return bool(probe())

    def test_guard_ownership_probe_itself_works(self):
        """守住上面那个能力探测：探针在真持有 `_guard` 时必须报 True，否则
        下面两条"回调时未持锁"的断言会永远空过。"""
        arb = ResourceArbiter()
        self.assertFalse(self._guard_owned_by_current_thread(arb))
        with arb._guard:
            self.assertTrue(self._guard_owned_by_current_thread(arb))
        self.assertFalse(self._guard_owned_by_current_thread(arb))

    def test_in_process_preempt_callback_runs_without_guard(self):
        arb = ResourceArbiter()
        seen: list[bool] = []
        arb.acquire(
            "gpu:0", "official-visual-wemm", priority=10,
            on_preempt=lambda: seen.append(self._guard_owned_by_current_thread(arb)),
        )
        self.assertTrue(arb.acquire("gpu:0", "official-text-retrieval-gpu", priority=100))
        self.assertEqual(seen, [False], "on_preempt 回调绝不能在持有 _guard 时执行")
        self.assertEqual(arb.holder_of("gpu:0"), "official-text-retrieval-gpu")

    def test_monitor_preempt_callback_runs_without_guard(self):
        """跨进程抢占请求走后台监控线程（_process_preempt_requests）——这条
        路径此前同样在 `_guard` 内调用回调，是真实双进程场景（GUI 与 MCP 都
        跑检索侧、同一个 holder_id）下的死锁入口。"""
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        seen: list[bool] = []
        called = threading.Event()

        def _on_preempt() -> None:
            seen.append(self._guard_owned_by_current_thread(arb))
            called.set()

        self.assertTrue(
            arb.acquire("gpu:0", "official-text-retrieval-gpu", priority=100, on_preempt=_on_preempt)
        )
        # 模拟"另一个进程写进来的抢占请求"（用仲裁器自己的写入路径，保证
        # 字段名/存活判定/优先级规则与生产一致）
        arb._write_preempt_request(
            "gpu:0", "official-text-retrieval-gpu", "another-process", 200, False, "token-1"
        )
        self.addCleanup(self._cleanup_request, arb, "token-1")
        self.assertTrue(called.wait(timeout=10), "后台监控线程没有执行 on_preempt 回调")
        self.assertEqual(seen, [False], "on_preempt 回调绝不能在持有 _guard 时执行")
        self.assertIsNone(arb.holder_of("gpu:0"), "让路后原持有者应已交出名额")

    def _cleanup_request(self, arb: ResourceArbiter, token: str) -> None:
        try:
            arb._request_path("gpu:0", os.getpid(), token).unlink(missing_ok=True)
        except OSError:
            pass

    def test_preempt_against_plugin_lock_does_not_deadlock(self):
        """真实多线程复现：模拟 embed.py 的锁序（self._lock → GPU_LOCK →
        acquire()）撞上 on_preempt → _unload（self._lock → GPU_LOCK）。

        - 线程 A 持"插件锁"再去 acquire()：它必须先拿 `_guard`；
        - 监控线程执行 on_preempt，回调里抢同一把"插件锁"。

        修复前：监控线程持 `_guard` 跑回调、回调等 A 的插件锁；A 等 `_guard`
        → 双方永久互等（都是裸锁、无超时），encode() 永不返回。
        修复后：回调在锁外执行，A 先推进完释放插件锁，回调随后完成。
        """
        arb = ResourceArbiter(lock_dir=self.lock_dir, poll_interval_s=0.01)
        plugin_lock = threading.Lock()  # 模拟插件实例锁 self._lock
        callback_entered = threading.Event()
        callback_done = threading.Event()

        def _on_preempt() -> None:
            callback_entered.set()
            plugin_lock.acquire()  # 模拟 _unload 拿 self._lock
            try:
                time.sleep(0.05)
            finally:
                plugin_lock.release()
            callback_done.set()

        self.assertTrue(
            arb.acquire("gpu:0", "official-text-retrieval-gpu", priority=100, on_preempt=_on_preempt)
        )
        arb._write_preempt_request(
            "gpu:0", "official-text-retrieval-gpu", "another-process", 200, False, "token-2"
        )
        self.addCleanup(self._cleanup_request, arb, "token-2")

        encoder_finished = threading.Event()

        def _encoder_thread() -> None:
            plugin_lock.acquire()
            try:
                # 等监控线程进回调（此时它若持着 _guard 就是死锁的前置条件），
                # 再去 acquire()——这一步制造 ABBA 的另一端。
                callback_entered.wait(timeout=10)
                arb.acquire("gpu:0", "official-text-retrieval-gpu", priority=100)
            finally:
                plugin_lock.release()
            encoder_finished.set()

        thread = threading.Thread(target=_encoder_thread, daemon=True, name="test-encoder")
        thread.start()
        self.addCleanup(thread.join, 0.1)
        self.assertTrue(
            encoder_finished.wait(timeout=15),
            "检测到锁序反转（ABBA 死锁）：acquire() 持插件锁等 _guard、"
            "on_preempt 回调持 _guard 等插件锁，两条线程都推不动",
        )
        self.assertTrue(
            callback_done.wait(timeout=15),
            "on_preempt 回调没有完成（同样说明锁序反转导致互相等待）",
        )


def self_fail(message: str) -> None:
    raise AssertionError(message)


if __name__ == "__main__":
    unittest.main()
