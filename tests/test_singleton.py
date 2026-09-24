"""core/singleton.py 的单元测试：真实文件锁、真实跨进程存活探测。"""
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

from core.singleton import ProcessSingletonGuard, pid_alive  # noqa: E402


class TestPidAlive(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_zero_or_negative_pid_is_not_alive(self):
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-1))

    def test_non_numeric_pid_is_not_alive(self):
        self.assertFalse(pid_alive("not-a-pid"))
        self.assertFalse(pid_alive(None))

    def test_implausibly_large_pid_is_not_alive(self):
        # 不保证在所有平台上都不存在，但 99999999 在正常桌面/CI 环境下
        # 极不可能是一个真实存活的进程——同类探测型测试的常见务实做法。
        self.assertFalse(pid_alive(99999999))


class TestProcessSingletonGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pid_file = self.tmp / "server.pid"

    def test_first_guard_acquires_successfully(self):
        guard = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(guard.acquire())
        guard.release()

    def test_second_guard_in_same_process_fails_while_first_holds(self):
        """同一个测试进程模拟"第二个实例想启动"——用两个独立的 guard
        对象对同一个 PID 文件加锁，第二个应该拿不到（同一进程内的文件锁
        在 fcntl/msvcrt 语义下仍然是独占的，不会因为是"自己"就放行）。"""
        first = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(first.acquire())
        second = ProcessSingletonGuard(self.pid_file)
        self.assertFalse(second.acquire())
        first.release()

    def test_after_release_a_new_guard_can_acquire(self):
        first = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(first.acquire())
        first.release()

        second = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(second.acquire())
        second.release()

    def test_release_removes_pid_file_written_by_this_process(self):
        guard = ProcessSingletonGuard(self.pid_file)
        guard.acquire()
        self.assertTrue(self.pid_file.exists())
        guard.release()
        self.assertFalse(self.pid_file.exists())

    def test_stale_pid_file_from_dead_process_does_not_block_acquire(self):
        """PID 文件残留一个已经不存在的进程号（比如上次崩溃/被强杀没走到
        release()）——不该永久卡住后续实例，新 guard 应该能正常抢到锁。"""
        self.pid_file.write_text("99999999", encoding="utf-8")
        guard = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(guard.acquire())
        guard.release()

    def test_real_second_process_cannot_acquire_while_first_holds(self):
        """真实起一个子进程去抢同一把锁（不是同进程模拟）——验证跨进程
        场景下这套机制真的挡得住，不是只在同进程语义下凑巧生效。"""
        guard = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(guard.acquire())
        try:
            script = (
                "import sys; sys.path.insert(0, r'%s');"
                "from core.singleton import ProcessSingletonGuard;"
                "from pathlib import Path;"
                "g = ProcessSingletonGuard(Path(r'%s'));"
                "print('ACQUIRED' if g.acquire() else 'REJECTED')"
            ) % (str(REPO_ROOT), str(self.pid_file))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
            )
            self.assertIn("REJECTED", result.stdout, result.stderr)
        finally:
            guard.release()


if __name__ == "__main__":
    unittest.main()
