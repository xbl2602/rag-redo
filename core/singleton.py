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

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_release(f) -> None:
    # msvcrt 按当前文件指针位置锁定/解锁，必须先归位到0，否则解锁位置
    # 与加锁位置不一致会报 PermissionError（同 _lock_try_acquire 对称）。
    f.seek(0)
    if _IS_WINDOWS:
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _record_holder_pid(f) -> None:
    # 先 truncate 清空再写：旧内容若残留会与新 PID 直接拼接（如 "31008"+
    # "999" → "31008999"），读取方解析出完全错误的 PID。
    try:
        f.seek(0)
        f.truncate(0)
        f.write(str(os.getpid()).encode("ascii"))
        f.flush()
        f.seek(0)
    except OSError:
        pass


class ProcessSingletonGuard:
    """一个 PID 文件对应一份单例守卫。`acquire()` 成功后必须持有这个对象
    到进程退出（关闭底层文件即释放锁），调用方通常在 `main()` 里
    `atexit.register(guard.release)` 或用 `with` 语句。"""

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file
        self._f = None

    def acquire(self) -> bool:
        """尝试成为唯一实例。返回 `False` 表示已有存活实例持锁在跑——
        这不是异常状况，是"本实例应该谦让退出"的正常信号，调用方自己
        决定退出方式（`sys.exit(0)`），这个方法不会替调用方退出进程。

        两段判定，都是为了防"文件不存在/未写就绪"窗口期的误判，不是为了
        性能：
        1) 预检：PID 文件已记录一个存活 PID → 视为已有实例（覆盖"持锁
           进程刚死、锁已自动释放，但文件里的 PID 记录还没被清理"这类
           陈旧残留场景）。
        2) 文件锁：对 PID 文件本身加非阻塞字节锁，抢不到锁说明有其他
           实例持锁运行中——这一步才是真正原子的判定，第1步只是快速
           短路，不能替代它（同 obsidian-rag 记录过的教训：只做检查-
           写入两步会有竞态窗口，双实例几乎同时启动时都能通过检查）。

        锁原语本身失败（极端环境限制等）时选择放行继续启动，不让单例
        机制本身成为服务不可用的原因。
        """
        try:
            raw = self._pid_file.read_text(encoding="utf-8").strip().split()[0]
            if raw.isdigit() and int(raw) != os.getpid() and pid_alive(int(raw)):
                return False
        except (OSError, IndexError, ValueError):
            pass

        try:
            self._pid_file.parent.mkdir(parents=True, exist_ok=True)
            f = _open_lock_file(self._pid_file)
            try:
                f.seek(0, 2)
                if f.tell() == 0:
                    f.write(b"0")  # 保证≥1字节，字节锁才有可锁范围
                    f.flush()
            except OSError:
                pass  # 并发启动时对方可能已持锁锁住首字节，写失败不要紧，锁判定会兜底
            f.seek(0)
        except OSError:
            return True  # 锁原语不可用：放行继续启动，见方法 docstring

        try:
            _lock_try_acquire(f)
            _record_holder_pid(f)
        except OSError:
            try:
                f.close()
            except OSError:
                pass
            return False

        self._f = f
        return True

    def release(self) -> None:
        """释放字节锁 + 仅当 PID 文件确实是本进程写的才删除它（不误删
        后来者的记录——理论上不该发生，因为持锁期间没有其他实例能写，
        但删除前多一层确认没有坏处）。"""
        try:
            if self._f is not None:
                self._f.close()  # 关闭文件即释放锁（msvcrt/fcntl 锁随 fd 生命周期）
                self._f = None
        except OSError:
            pass
        try:
            if self._pid_file.exists():
                current = int(self._pid_file.read_text(encoding="utf-8").strip().split()[0])
                if current == os.getpid():
                    self._pid_file.unlink()
        except (OSError, IndexError, ValueError):
            pass
