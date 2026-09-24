"""core/singleton.py — MCP 服务进程单例守卫（核心工具，`mcp_stdio.py` 入口
直接用，不是插件）。

**补的是哪个坑**：2026-09-23 全面功能审计发现，obsidian-rag 有一条真实
踩过的坑——AI 工具（旧项目记录的是 opencode）用 stdio 方式拉起 MCP 服务
时，观察到启动后 1 秒内连续拉起两个进程实例；rag-redo 的 `mcp_stdio.py`
同样是 stdio 拉起、同样会被同一类调用方连续拉起两次，此前完全没有任何
防护——双实例 = 两份 embedder/reranker 模型常驻（各自几个GB）+ 对同一个
`data/` 目录（Chroma/BM25）产生写竞争，是真实的资源浪费和数据风险，不是
理论风险。

**设计逐字对齐 obsidian-rag 的 `singleton.py`/`index.py` 锁原语**：对 PID
文件本身加非阻塞字节锁（不是"读文件→比较存活→写入"三步检查，那样两个
进程几乎同时启动时都能通过检查、双双常驻——`singleton.py` 模块 docstring
记录过这个真实复现过的竞态，判定必须原子化）。抢到锁的实例把自己的
PID 写进文件、持锁到进程退出；后来者抢不到锁立即知道"已有实例在跑"，
应该主动退出，不是报错崩溃。

**与 obsidian-rag 的一处刻意差异**：这里包成 `ProcessSingletonGuard`
类而不是模块级全局变量（`singleton.py` 用的是模块全局 `_singleton_f`）
——纯粹是为了这份模块自己好测试：每个测试新建一个独立的 guard 实例，
不会因为共享的模块级状态在同一进程里跑多个测试用例时互相污染，不影响
对外行为。
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

_IS_WINDOWS = os.name == "nt"


def pid_alive(pid: object) -> bool:
    """进程是否存活——不能用 `os.kill(pid, 0)`：Windows 上 CPython 的
    `os.kill` 对除 CTRL_C_EVENT/CTRL_BREAK_EVENT 外的任何信号一律走
    `OpenProcess`+`TerminateProcess`，`sig=0` 也不例外——那是"杀掉目标
    进程"，不是"探测存活"（同 obsidian-rag `index.py::_pid_alive` 记录
    过的真实教训：单例守卫会先杀掉正在服务的那个进程，然后自己再退出，
    两个都没了）。Windows 上改用 `OpenProcess(SYNCHRONIZE)` +
    `WaitForSingleObject(0)` 纯只读探测。
    """
    try:
        pid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102
        ERROR_ACCESS_DENIED = 5
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                # 拿不到句柄：ACCESS_DENIED 说明进程确实存在（只是无权限探测），
                # 其余（典型 ERROR_INVALID_PARAMETER）说明该 PID 根本不存在。
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            try:
                return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(handle)
        except OSError:
            return True  # ctypes 不可用等极端情况：宁可视为"存活"，不误判导致双实例
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在，只是不属于当前用户
    except OSError:
        return False


def _open_lock_file(path: Path):
    # "r+b" 不是 "a+b"：append 模式下 seek(0)+write 会被系统强制写到文件尾，
    # 旧 PID 内容永远留在开头，后续读取会拼出错误的 PID——同 obsidian-rag
    # `index.py::_open_lock_file` 记录过的真实教训。
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    return os.fdopen(fd, "r+b")


def _lock_try_acquire(f) -> None:
    """非阻塞尝试获取文件锁。抢不到锁抛 OSError（Windows/POSIX 语义
    不同但都是 OSError 子类，调用方不需要分平台处理）。"""
    if _IS_WINDOWS:
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        getattr(fcntl, "flock")(f.fileno(), getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"))


class FileByteLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._f = None

    def acquire(self) -> bool:
        if self._f is not None:
            return False

        self._path.parent.mkdir(parents=True, exist_ok=True)
        f = _open_lock_file(self._path)
        try:
            try:
                f.seek(0, 2)
                if f.tell() == 0:
                    f.write(b"0")
                    f.flush()
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            f.seek(0)
            try:
                _lock_try_acquire(f)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    f.close()
                    return False
                raise
        except BaseException:
            f.close()
            raise

        self._f = f
        return True

    def write(self, data: bytes) -> None:
        if self._f is None:
            raise RuntimeError("文件锁尚未获取")
        self._f.seek(0)
        self._f.truncate(0)
        self._f.write(data)
        self._f.flush()
        self._f.seek(0)

    def release(self) -> None:
        f = self._f
        self._f = None
        if f is not None:
            f.close()


class ProcessSingletonGuard:
    """一个 PID 文件对应一份单例守卫。`acquire()` 成功后必须持有这个对象
    到进程退出（关闭底层文件即释放锁），调用方通常在 `main()` 里
    `atexit.register(guard.release)` 或用 `with` 语句。"""

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file
        self._lock = FileByteLock(pid_file)

    def acquire(self) -> bool:
        """尝试成为唯一实例。文件锁是唯一权威，PID 内容只用于诊断。"""
        try:
            acquired = self._lock.acquire()
        except OSError:
            return False
        if not acquired:
            return False

        try:
            self._lock.write(str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        return True

    def release(self) -> None:
        """释放字节锁，保留 PID 文件供下一次启动覆盖。"""
        try:
            self._lock.release()
        except OSError:
            pass
