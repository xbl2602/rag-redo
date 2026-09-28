"""core/singleton.py 的单元测试：真实文件锁、真实跨进程存活探测。"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.singleton import FileByteLock, ProcessSingletonGuard, pid_alive  # noqa: E402


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


class TestFileByteLock(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock_file = self.tmp / "resource.lock"

    def test_second_lock_in_same_process_fails_while_first_holds(self):
        first = FileByteLock(self.lock_file)
        self.assertTrue(first.acquire())
        second = FileByteLock(self.lock_file)
        self.assertFalse(second.acquire())
        first.release()

    def test_after_release_a_new_lock_can_acquire(self):
        first = FileByteLock(self.lock_file)
        self.assertTrue(first.acquire())
        first.release()
        first.release()

        second = FileByteLock(self.lock_file)
        self.assertTrue(second.acquire())
        second.release()

    def test_real_second_process_cannot_acquire_while_first_holds(self):
        first = FileByteLock(self.lock_file)
        self.assertTrue(first.acquire())
        try:
            script = (
                "import sys; sys.path.insert(0, r'%s');"
                "from core.singleton import FileByteLock;"
                "from pathlib import Path;"
                "lock = FileByteLock(Path(r'%s'));"
                "print('ACQUIRED' if lock.acquire() else 'REJECTED')"
            ) % (str(REPO_ROOT), str(self.lock_file))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
            )
            self.assertIn("REJECTED", result.stdout, result.stderr)
        finally:
            first.release()

    def test_open_oserror_propagates(self):
        lock = FileByteLock(self.lock_file)
        with patch("core.singleton._open_lock_file", side_effect=OSError("open failed")):
            with self.assertRaises(OSError):
                lock.acquire()

    def test_lock_oserror_propagates(self):
        lock = FileByteLock(self.lock_file)
        with patch("core.singleton._lock_try_acquire", side_effect=OSError("lock failed")):
            with self.assertRaises(OSError):
                lock.acquire()


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

    def test_release_keeps_pid_file_for_next_diagnostic_overwrite(self):
        guard = ProcessSingletonGuard(self.pid_file)
        guard.acquire()
        self.assertTrue(self.pid_file.exists())
        guard.release()
        self.assertTrue(self.pid_file.exists())

    # 2026-09-27 修正：原用例 test_lock_unavailable_fails_closed 把错误行为
    # 固化成了预期（守卫自身抛 OSError 时返回 False），与 LEGACY 相反——
    # obsidian-rag/singleton.py:96-98 明确"守卫失败（继续启动）"返回 True，
    # guiweb/app.py:62-66 同样是"锁文件打开失败（忽略，可能重复实例）"。
    # fail-closed 会让只读数据目录、杀软锁文件、磁盘满这类环境问题把 MCP
    # 进程变成 sys.exit(0) 静默消失，调用方只看到"服务没了"。故改写为断言
    # fail-open + 告警，而不是删掉。
    def test_guard_self_failure_fails_open_with_warning(self):
        guard = ProcessSingletonGuard(self.pid_file)
        buf = io.StringIO()
        with patch.object(guard._lock, "acquire", side_effect=OSError("lock failed")):
            with redirect_stderr(buf):
                self.assertTrue(guard.acquire())
        err = buf.getvalue()
        self.assertIn("单例守卫失败（继续启动）", err)
        self.assertIn(str(self.pid_file), err)
        self.assertIn("OSError", err)
        # 放行 ≠ 拿到锁：不得留下"我以为自己是唯一实例"的假象
        # （AGENTS.md §7"所有权未知不能宣称资源空闲"）。
        self.assertIsNone(guard._lock._f)

    def test_unusable_pid_file_parent_path_fails_open(self):
        """真实 OSError 而非 mock：父路径是个已存在的文件，mkdir 必然抛
        NotADirectoryError（OSError 子类）。数据目录被换成文件、挂载点掉了
        这类真实环境问题都必须放行，不能让服务静默退出。"""
        blocked = self.tmp / "not_a_dir"
        blocked.write_text("x", encoding="utf-8")
        pid_file = blocked / "server.pid"
        guard = ProcessSingletonGuard(pid_file)
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertTrue(guard.acquire())
        err = buf.getvalue()
        self.assertIn("单例守卫失败（继续启动）", err)
        self.assertIn(str(pid_file), err)

    def test_pid_file_path_pointing_at_directory_fails_open(self):
        """锁文件路径本身是个目录：os.open 打不开（Windows PermissionError /
        POSIX IsADirectoryError），同样是守卫故障而不是"已有实例在跑"。"""
        guard = ProcessSingletonGuard(self.tmp)
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertTrue(guard.acquire())
        err = buf.getvalue()
        self.assertIn("单例守卫失败（继续启动）", err)
        self.assertIn(str(self.tmp), err)

    def test_contention_is_not_reported_as_guard_failure(self):
        """回归保护：抢不到锁仍然 fail-closed（另一个实例真的在跑），而且
        守卫自己必须保持安静——"已有实例退出"由调用方打印，stderr 里不该同时
        出现"守卫失败"这种自相矛盾的两套结论。"""
        first = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(first.acquire())
        try:
            second = ProcessSingletonGuard(self.pid_file)
            buf = io.StringIO()
            with redirect_stderr(buf):
                self.assertFalse(second.acquire())
            self.assertEqual("", buf.getvalue())
        finally:
            first.release()

    def test_live_pid_record_does_not_replace_file_lock_authority(self):
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        guard = ProcessSingletonGuard(self.pid_file)
        self.assertTrue(guard.acquire())
        guard.release()

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
                [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
            )
            self.assertIn("REJECTED", result.stdout, result.stderr)
        finally:
            guard.release()


class TestGuiMainSingletonWiring(unittest.TestCase):
    """gui_main.py::main 的单例守卫接线（对齐 obsidian-rag gui/app.py:46-69 /
    guiweb/app.py:46-87——旧项目两个 GUI 入口都接了锁，rag-redo 此前只给
    MCP 接了）。真实起子进程走 gui_main.main()：锁被占时必须在 build_runtime
    之前谦让退出——用"运行标记文件是否被创建"区分，不真开 GUI 窗口。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_root = self.tmp / "data"
        self.data_root.mkdir()
        self.marker = self.tmp / "runtime-built.marker"

    def _child_script(self, marker: Path) -> str:
        return (
            "import sys, os;"
            "sys.path.insert(0, r'%s');"
            "os.environ['RAG_REDO_DATA_ROOT'] = r'%s';"
            "import gui_main;"
            "gui_main.build_runtime = lambda: (open(r'%s', 'w').write('built'), None)[1];"
            "import webview;"
            "webview.start = lambda *a, **k: None;"
            "gui_main.main();"
            "print('CHILD_DONE')"
        ) % (str(REPO_ROOT), str(self.data_root), str(marker))

    def test_second_gui_instance_exits_before_building_runtime_while_first_holds(self):
        guard = ProcessSingletonGuard(self.data_root / "gui.pid")
        self.assertTrue(guard.acquire())
        try:
            result = subprocess.run(
                [sys.executable, "-c", self._child_script(self.marker)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            self.assertFalse(self.marker.exists(), "守卫被占时不得执行 build_runtime")
            self.assertIn("单例守卫", result.stderr)
        finally:
            guard.release()

    def test_first_gui_instance_proceeds_to_build_runtime(self):
        # 桩只负责写标记文件；标记写入后 main() 的后续真实构造（Pipeline
        # 等）在桩环境下会失败退出——这不影响本测试：标记文件存在本身
        # 就证明守卫没有拦截无冲突的首次启动。
        result = subprocess.run(
            [sys.executable, "-c", self._child_script(self.marker)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertTrue(self.marker.exists(), "无既有实例时守卫应当放行，build_runtime 应被执行")


if __name__ == "__main__":
    unittest.main()
