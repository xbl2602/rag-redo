"""core/index_progress.py 的单元测试：真实起后台线程、真实写盘、真实
轮询到完成，不是纸面设计。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.index_progress import IndexProgressTracker  # noqa: E402


class _FakeReport:
    def __init__(self, succeeded: int, failed: int) -> None:
        self.succeeded = succeeded
        self.failed = failed


def _wait_until(predicate, timeout_s: float = 5.0, poll_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


class TestIndexProgressTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tracker = IndexProgressTracker(self.tmp / "progress")

    def test_status_before_any_start_is_none(self):
        self.assertIsNone(self.tracker.status("lib1"))

    def test_start_returns_immediately_not_waiting_for_completion(self):
        gate = threading.Event()

        def slow_index(progress_callback):
            gate.wait(timeout=5.0)  # 卡住直到测试自己放行，证明 start() 真的没等它
            progress_callback(1, 1, "a.md")
            return _FakeReport(succeeded=1, failed=0)

        t0 = time.time()
        started, message = self.tracker.start("lib1", slow_index)
        elapsed = time.time() - t0
        self.assertTrue(started)
        self.assertLess(elapsed, 1.0, "start() 应该立即返回，不该等 index_fn 跑完")
        gate.set()  # 放行，避免线程残留到下个测试

    def test_second_start_while_running_is_rejected(self):
        gate = threading.Event()

        def slow_index(progress_callback):
            gate.wait(timeout=5.0)
            return _FakeReport(succeeded=0, failed=0)

        self.tracker.start("lib1", slow_index)
        started, message = self.tracker.start("lib1", slow_index)
        self.assertFalse(started)
        self.assertIn("已经有一个索引任务在跑", message)
        gate.set()

    def test_different_libraries_can_run_concurrently(self):
        gate = threading.Event()

        def slow_index(progress_callback):
            gate.wait(timeout=5.0)
            return _FakeReport(succeeded=0, failed=0)

        started1, _ = self.tracker.start("lib1", slow_index)
        started2, _ = self.tracker.start("lib2", slow_index)
        self.assertTrue(started1)
        self.assertTrue(started2)
        gate.set()

    def test_progress_updates_are_visible_via_status(self):
        def index_fn(progress_callback):
            progress_callback(1, 3, "a.md")
            progress_callback(2, 3, "b.md")
            progress_callback(3, 3, "c.md")
            return _FakeReport(succeeded=3, failed=0)

        self.tracker.start("lib1", index_fn)
        self.assertTrue(_wait_until(lambda: self.tracker.status("lib1")["stage"] == "done"))
        status = self.tracker.status("lib1")
        self.assertEqual(status["files_done"], 3)
        self.assertEqual(status["files_total"], 3)
        self.assertEqual(status["succeeded"], 3)
        self.assertEqual(status["failed"], 0)
        self.assertEqual(status["health"], "healthy")

    def test_exception_in_index_fn_is_captured_not_left_running_forever(self):
        """这是要防的假活状态：后台线程炸了但没人兜住，progress 永远停在
        "running"，调用方会一直以为任务还在跑——见模块 docstring。"""

        def broken_index(progress_callback):
            raise RuntimeError("模拟索引过程中真的炸了")

        self.tracker.start("lib1", broken_index)
        self.assertTrue(_wait_until(lambda: self.tracker.status("lib1")["stage"] == "failed"))
        status = self.tracker.status("lib1")
        self.assertIn("模拟索引过程中真的炸了", status["error"])

    def test_after_completion_library_can_be_started_again(self):
        def quick_index(progress_callback):
            return _FakeReport(succeeded=1, failed=0)

        self.tracker.start("lib1", quick_index)
        self.assertTrue(_wait_until(lambda: self.tracker.status("lib1")["stage"] == "done"))
        started, _ = self.tracker.start("lib1", quick_index)
        self.assertTrue(started)
        self.assertTrue(_wait_until(lambda: self.tracker.status("lib1")["stage"] == "done"))

    def test_stalled_no_heartbeat_health_when_heartbeat_too_old(self):
        """直接构造一个"运行中但心跳很久没更新"的状态，不真的等
        HEARTBEAT_TIMEOUT_S 那么久——用一个心跳阈值极小的 tracker。"""
        tracker = IndexProgressTracker(self.tmp / "progress2")
        tracker.HEARTBEAT_TIMEOUT_S = 0.05
        tracker.STALL_TIMEOUT_S = 0.05
        gate = threading.Event()

        def hangs_after_first_update(progress_callback):
            progress_callback(1, 5, "a.md")
            gate.wait(timeout=5.0)
            return _FakeReport(succeeded=0, failed=0)

        tracker.start("lib1", hangs_after_first_update)
        self.assertTrue(_wait_until(lambda: tracker.status("lib1")["files_done"] == 1))
        time.sleep(0.15)  # 让心跳真的过期
        status = tracker.status("lib1")
        self.assertEqual(status["stage"], "running")
        self.assertEqual(status["health"], "stalled_no_heartbeat")
        gate.set()

    def test_status_for_unknown_library_after_others_started_is_still_none(self):
        def quick_index(progress_callback):
            return _FakeReport(succeeded=1, failed=0)

        self.tracker.start("lib1", quick_index)
        self.assertIsNone(self.tracker.status("lib-never-started"))


if __name__ == "__main__":
    unittest.main()
