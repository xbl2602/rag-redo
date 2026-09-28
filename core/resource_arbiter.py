"""通用具名资源租约仲裁器（核心服务，不是插件——见 AGENTS.md 架构红线5）。

不懂任何 RAG 领域知识，不知道"GPU"或"BGE-M3"是什么，只知道"谁在占用一个
具名资源、优先级更高的能不能抢占"。这是把旧 obsidian-rag 项目 gpu_arbiter.py
验证过的策略（同一时刻只让一个模型驻留显存、检索侧优先、等不到就降级）抽象
成不专属 GPU 的通用原语。

================================================================================
全局锁获取顺序（AGENTS.md §7「同一独占资源必须有明确 holder、优先级、获取、
释放、抢占和进程死亡处理」的落地约束；改这个文件前先读这一段）
================================================================================

本模块有两把锁，加上插件侧的锁，全项目**只允许**按下面这个单向顺序获取：

    插件实例锁 self._lock
        → gpu_arbiter.GPU_LOCK
            → ResourceArbiter._file_mutex（租约文件/文件锁的跨进程 I/O）
                → ResourceArbiter._guard（进程内 _holders/_monitor 状态）

**绝对禁止反向获取**，尤其禁止在持有 `_guard` 或 `_file_mutex` 时去拿
`GPU_LOCK` 或任何插件内部锁。三条硬纪律：

1. **绝不在持有 `_guard` / `_file_mutex` 时调用插件回调 `on_preempt`。**
   回调是插件代码，内部会去拿 `self._lock` 和 `gpu_arbiter.GPU_LOCK`（见
   official-embedder-bge-m3/embed.py::_RealEncoder._unload）。而插件的正常
   编码路径是 `self._lock → GPU_LOCK → acquire() → _guard`——只要回调在锁内
   执行，两条路径的锁序就正好相反（ABBA），而两把锁都是裸 acquire 无超时，
   结果是 `_guard` 被永久占住、本进程此后所有 acquire() 无限阻塞、
   encode() 永不返回、search_knowledge 永久挂起。
   正确做法（也是本文件的实现）：锁内只做"判定 + 把要执行的回调取出来"，
   **出锁**后再执行回调，最后回锁收口。
2. **绝不在 `_guard` 内做等待/轮询。** 跨进程文件锁最长要轮询
   `preempt_timeout_s`（默认 15s），sleep 必须在锁外；文件锁尝试本身是
   非阻塞的（`FileByteLock.acquire` 抢不到立刻返回 False），只放在
   `_file_mutex` 这个"短 I/O 临界区"里。
3. **回调期间状态可能已被改变。** 回调在锁外跑，别的线程完全可能已经
   release/换人，所以回调之后必须**回锁复核**"在位者还是不是刚才判定的那个"，
   不是就重新判定（`acquire` 的让路轮次上限 `_MAX_PREEMPT_ROUNDS` 就是这个
   竞态的兜底：让不出、名额被更高优先级者抢走 → 返回 False 由调用方降级）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .atomic import atomic_write_text
from .singleton import FileByteLock, pid_alive

# acquire() 里"判定可抢占 → 出锁执行回调 → 回锁收口"最多重试几轮。
# 正常情况 1~2 轮就收口；这个上限只兜住"回调期间名额被别人抢走"的竞态，
# 抢不到就让调用方降级，绝不无限自旋（自旋会让调用线程永远不返回）。
_MAX_PREEMPT_ROUNDS = 8


@dataclass
class _Holder:
    holder_id: str
    priority: int
    on_preempt: Callable[[], None] | None
    preempt_equal: bool = False


def _atomic_write_json(path: Path, data: dict) -> bool:
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False))
        return True
    except OSError:
        return False


@dataclass
class ResourceArbiter:
    lock_dir: Path | None = None
    preempt_timeout_s: float = 15.0
    poll_interval_s: float = 0.05
    _holders: dict[str, _Holder] = field(default_factory=dict)
    _locks: dict[str, FileByteLock] = field(default_factory=dict)
    # 两把锁的分工与获取顺序见模块 docstring 的"全局锁获取顺序"：
    # _file_mutex 只护"文件锁 + 租约文件"的跨进程 I/O（可含非阻塞试锁），
    # _guard 只护进程内状态字典；_file_mutex → _guard，永不反向。
    _file_mutex: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _guard: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _monitor_stop: threading.Event | None = field(default=None, repr=False)
    _monitor_thread: threading.Thread | None = field(default=None, repr=False)

    def acquire(
        self,
        resource_id: str,
        holder_id: str,
        *,
        priority: int = 0,
        on_preempt: Callable[[], None] | None = None,
        preempt_equal: bool = False,
    ) -> bool:
        """申请一个资源锁。

        - 当前无人占用 → 直接拿到
        - 已经是自己占用 → 幂等返回 True
        - 别人占用、且新请求优先级更高 → 抢占
        - 同优先级且新请求 `preempt_equal=True` → 抢占
        - 跨进程持有 → 写抢占请求，等待持锁进程执行回调并让锁，最多 15 秒
        - 拿不到或等待超时 → 返回 False，由调用方按旧设计降级

        返回值语义（成功 True / 被高优先级者占着或等超时 False）不因本次
        锁序修复而改变——core/pipeline.py 与 official-visual-wemm/plugin.py
        都依赖它。

        **回调在锁外执行**（模块 docstring 纪律 1）：锁内只判定和快照，回调
        出锁执行，之后回锁复核在位者再收口。
        """
        for _round in range(_MAX_PREEMPT_ROUNDS):
            incumbent: _Holder | None = None
            with self._guard:
                current = self._holders.get(resource_id)
                if current is not None and current.holder_id == holder_id:
                    return True
                if current is not None and not self._can_preempt(
                    priority, holder_id, current.holder_id, current.priority, preempt_equal
                ):
                    return False
                # 进程内有人在位（我们能抢）→ 快照出来，出锁后再动他；
                # 进程内没人 → 走跨进程文件锁路径。
                incumbent = current
            if incumbent is None:
                return self._acquire_shared(
                    resource_id, holder_id, priority, on_preempt, preempt_equal
                )
            if incumbent.on_preempt is not None:
                # 插件回调（可能去拿插件内部锁和 GPU_LOCK，见模块 docstring）
                # 绝不在 `_guard` 内执行。这里也不吞异常：与历史行为一致——
                # 回调抛异常就让 acquire 的调用方看到，由它自己决定怎么处理
                # （后台抢占路径才吞，见 _process_preempt_requests）。
                incumbent.on_preempt()
            taken: _Holder | None = None
            with self._guard:
                current = self._holders.get(resource_id)
                if current is not None and current.holder_id == incumbent.holder_id:
                    taken = _Holder(holder_id, priority, on_preempt, preempt_equal)
                    self._holders[resource_id] = taken
            if taken is None:
                # 回调期间在位者已经变了（自己放手了，或被别人抢走）→
                # 重新判定一轮；抢不过就返回 False 让调用方降级。
                continue
            with self._file_mutex:
                self._write_holder(resource_id, taken)
            return True
        return False

    def _acquire_shared(
        self,
        resource_id: str,
        holder_id: str,
        priority: int,
        on_preempt: Callable[[], None] | None,
        preempt_equal: bool,
    ) -> bool:
        """走跨进程文件锁的申请路径。

        轮询等待（最长 `preempt_timeout_s`，默认 15s）整体在 `_guard` 之外：
        每一轮只在 `_file_mutex` 里做"非阻塞试锁 + 读写租约/请求文件"这种
        短 I/O，`_guard` 只在真正要写进程内状态时才短暂持有（模块 docstring
        纪律 2）。
        """
        if self.lock_dir is None:
            with self._guard:
                self._holders[resource_id] = _Holder(holder_id, priority, on_preempt, preempt_equal)
            return True
        with self._file_mutex:
            lock = self._locks.get(resource_id)
            if lock is None:
                lock = FileByteLock(self._lock_path(resource_id))
                self._locks[resource_id] = lock
        deadline = time.monotonic() + max(0.0, float(self.preempt_timeout_s))
        request_token = uuid.uuid4().hex
        request_path: Path | None = None
        result = False
        start_monitor = False
        while True:
            lock_error = False
            with self._file_mutex:
                try:
                    acquired = lock.acquire()
                except OSError:
                    acquired = False
                    lock_error = True
                if acquired:
                    self._clear_request(resource_id)
                    holder = _Holder(holder_id, priority, on_preempt, preempt_equal)
                    if self._write_holder(resource_id, holder):
                        with self._guard:
                            self._holders[resource_id] = holder
                        result = True
                        start_monitor = True
                    else:
                        # 租约写不下去 = 本进程无法对外声明占有，放掉文件锁
                        # 退回"没拿到"（同历史行为）。
                        self._locks.pop(resource_id, None)
                        lock.release()
                    break
                if not lock_error:
                    remote = self._read_holder(resource_id)
                    if remote is not None and self._can_preempt(
                        priority,
                        holder_id,
                        str(remote.get("holder_id") or ""),
                        int(remote.get("priority") or 0),
                        preempt_equal,
                    ):
                        request_path = self._write_preempt_request(
                            resource_id,
                            str(remote.get("holder_id") or ""),
                            holder_id,
                            priority,
                            preempt_equal,
                            request_token,
                        )
            if lock_error:
                return False
            if result:
                break
            if time.monotonic() >= deadline:
                with self._file_mutex:
                    if request_path is not None:
                        self._clear_request_path(request_path)
                    self._locks.pop(resource_id, None)
                return False
            time.sleep(max(0.005, float(self.poll_interval_s)))
        if start_monitor:
            # 拉起抢占监控线程放在所有锁之外：它一启动就会想拿 _file_mutex，
            # 在锁内 start 不会死锁（start 不 join），但会让新线程白白阻塞
            # 在 _guard 上，放外面更干净。
            with self._guard:
                self._ensure_monitor()
        return result

    def _can_preempt(
        self,
        priority: int,
        requester_holder: str,
        current_holder: str,
        current_priority: int,
        preempt_equal: bool,
    ) -> bool:
        if not current_holder:
            return False
        if requester_holder == current_holder:
            return True
        return priority > current_priority or (preempt_equal and priority == current_priority)

    def release(self, resource_id: str, holder_id: str) -> None:
        stop = None
        with self._file_mutex:
            with self._guard:
                current = self._holders.get(resource_id)
                if current is None or current.holder_id != holder_id:
                    return
                lock = self._detach_holder_locked(resource_id, holder_id)
            # 落盘清理与文件锁释放在 _file_mutex 内、_guard 之外。
            self._clear_holder(resource_id)
            if lock is not None:
                lock.release()
            with self._guard:
                stop = self._detach_monitor_locked()
        if stop is not None:
            stop.set()

    def _detach_holder_locked(self, resource_id: str, holder_id: str) -> FileByteLock | None:
        """在 `_guard` 内把持有者从状态里摘掉，返回需要释放的文件锁对象
        （进程内无文件锁时为 None）。调用方负责在 `_file_mutex` 内做落盘清理
        与文件锁释放——那两步不许在 `_guard` 里做。"""
        current = self._holders.get(resource_id)
        if current is None or current.holder_id != holder_id:
            return None
        del self._holders[resource_id]
        return self._locks.pop(resource_id, None)

    def _write_holder(self, resource_id: str, holder: _Holder) -> bool:
        if self.lock_dir is None:
            return True
        return _atomic_write_json(
            self._holder_path(resource_id),
            {
                "resource_id": resource_id,
                "holder_id": holder.holder_id,
                "priority": holder.priority,
                "preempt_equal": holder.preempt_equal,
                "pid": os.getpid(),
                "updated_at": time.time(),
            },
        )

    def _clear_holder(self, resource_id: str) -> None:
        if self.lock_dir is None:
            return
        data = self._read_holder(resource_id)
        if data is not None:
            try:
                holder_pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                holder_pid = 0
            if holder_pid and holder_pid != os.getpid():
                return
        try:
            self._holder_path(resource_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_request(self, resource_id: str) -> None:
        if self.lock_dir is None:
            return
        for path in self.lock_dir.glob(f"{self._resource_key(resource_id)}.*.request"):
            self._clear_request_path(path)

    @staticmethod
    def _clear_request_path(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _read_holder(self, resource_id: str) -> dict | None:
        if self.lock_dir is None:
            return None
        try:
            data = json.loads(self._holder_path(resource_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_preempt_request(
        self,
        resource_id: str,
        current_holder: str,
        requester_holder: str,
        priority: int,
        preempt_equal: bool,
        request_token: str,
    ) -> Path | None:
        if self.lock_dir is None:
            return None
        path = self._request_path(resource_id, os.getpid(), request_token)
        _atomic_write_json(
            path,
            {
                "resource_id": resource_id,
                "current_holder": current_holder,
                "requester_holder": requester_holder,
                "priority": priority,
                "preempt_equal": preempt_equal,
                "requester_pid": os.getpid(),
                "created_at": time.time(),
            },
        )
        return path

    def _process_preempt_requests(self) -> None:
        """处理别的进程写进来的抢占请求（后台监控线程调用）。

        三段式：**锁内判定 → 出锁执行插件回调 → 回锁收口**。回调绝不在
        `_guard` 内执行（模块 docstring 纪律 1）。
        """
        if self.lock_dir is None or not self.lock_dir.is_dir():
            return
        for path in self.lock_dir.glob("*.request"):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                request = None
            if not isinstance(request, dict):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            try:
                requester_pid = int(request.get("requester_pid") or 0)
            except (TypeError, ValueError):
                requester_pid = 0
            if requester_pid and not pid_alive(requester_pid):
                self._clear_request_path(path)
                continue
            try:
                created_at = float(request.get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0.0
            if created_at and time.time() - created_at > max(1.0, self.preempt_timeout_s * 2):
                self._clear_request_path(path)
                continue
            resource_id = str(request.get("resource_id") or "")
            victim: str | None = None
            callback: Callable[[], None] | None = None
            with self._guard:
                current = self._holders.get(resource_id)
                if current is None or current.holder_id != str(request.get("current_holder") or ""):
                    continue
                try:
                    requester_priority = int(request.get("priority") or 0)
                    requester_preempt_equal = bool(request.get("preempt_equal"))
                except (TypeError, ValueError):
                    continue
                if not self._can_preempt(
                    requester_priority,
                    str(request.get("requester_holder") or ""),
                    current.holder_id,
                    current.priority,
                    requester_preempt_equal,
                ):
                    continue
                victim = current.holder_id
                callback = current.on_preempt
            if callback is not None:
                # 插件回调（卸载模型）会去拿插件内部锁和 GPU_LOCK，必须在
                # 仲裁器的锁之外执行；后台线程吞掉回调异常，绝不让一个插件
                # 的问题带崩监控线程（与 acquire() 请求路径的抛异常策略
                # 有意不同：那里调用方需要看见失败）。
                try:
                    callback()
                except Exception:
                    pass
            stop = None
            with self._file_mutex:
                lock: FileByteLock | None = None
                released = False
                with self._guard:
                    current = self._holders.get(resource_id)
                    if current is not None and current.holder_id == victim:
                        # 回调期间在位者没变 → 真的让出名额（收口动作与判定
                        # 之间再复核一次，因为回调是在锁外跑的）。
                        lock = self._detach_holder_locked(resource_id, victim)
                        stop = self._detach_monitor_locked()
                        released = True
                if released:
                    self._clear_holder(resource_id)
                    if lock is not None:
                        lock.release()
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            if stop is not None:
                stop.set()

    def _detach_monitor_locked(self) -> threading.Event | None:
        """在 `_guard` 内判断"已无任何持有者 → 监控线程可以停了"，返回需要
        在锁外 set 的 Event。"""
        if self._holders or self._monitor_stop is None:
            return None
        stop = self._monitor_stop
        self._monitor_stop = None
        self._monitor_thread = None
        return stop

    def _ensure_monitor(self) -> None:
        """拉起抢占监控线程。**调用方必须已持有 `_guard`**（它要写
        `_monitor_stop`/`_monitor_thread`）。"""
        if self.lock_dir is None:
            return
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=self._monitor_loop,
            args=(stop,),
            daemon=True,
            name="resource-arbitMonitor",
        )
        self._monitor_stop = stop
        self._monitor_thread = thread
        thread.start()

    def _monitor_loop(self, stop: threading.Event) -> None:
        while not stop.wait(max(0.005, float(self.poll_interval_s))):
            try:
                self._process_preempt_requests()
            except Exception:
                pass

    def _resource_key(self, resource_id: str) -> str:
        return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()

    def _lock_path(self, resource_id: str) -> Path:
        if self.lock_dir is None:
            raise RuntimeError("文件锁目录未配置")
        return self.lock_dir / f"{self._resource_key(resource_id)}.lock"

    def _holder_path(self, resource_id: str) -> Path:
        if self.lock_dir is None:
            raise RuntimeError("文件锁目录未配置")
        return self.lock_dir / f"{self._resource_key(resource_id)}.holder.json"

    def _request_path(self, resource_id: str, requester_pid: int, request_token: str) -> Path:
        if self.lock_dir is None:
            raise RuntimeError("文件锁目录未配置")
        return self.lock_dir / f"{self._resource_key(resource_id)}.{requester_pid}.{request_token}.request"

    def holder_of(self, resource_id: str) -> str | None:
        with self._guard:
            current = self._holders.get(resource_id)
            return current.holder_id if current else None

    @staticmethod
    def probe(check: Callable[[], bool]) -> bool:
        """执行一次"资源是否可用"探测。

        探测函数自身抛异常时 fail-open——默认判定为"可用/放行"，绝不让探测失败
        变成阻塞。这条焊死在这一个函数里，所有插件复用，不用各自重新踩一遍旧项目
        问题41踩过的坑（架构红线5）。
        """
        try:
            return check()
        except Exception:
            return True
