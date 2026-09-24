"""通用具名资源租约仲裁器（核心服务，不是插件——见 AGENTS.md 架构红线5）。

不懂任何 RAG 领域知识，不知道"GPU"或"BGE-M3"是什么，只知道"谁在占用一个
具名资源、优先级更高的能不能抢占"。这是把旧 obsidian-rag 项目 gpu_arbiter.py
验证过的策略（同一时刻只让一个模型驻留显存、检索侧优先、等不到就降级）抽象
成不专属 GPU 的通用原语。
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

from .singleton import FileByteLock, pid_alive


@dataclass
class _Holder:
    holder_id: str
    priority: int
    on_preempt: Callable[[], None] | None
    preempt_equal: bool = False


def _atomic_write_json(path: Path, data: dict) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class ResourceArbiter:
    lock_dir: Path | None = None
    preempt_timeout_s: float = 15.0
    poll_interval_s: float = 0.05
    _holders: dict[str, _Holder] = field(default_factory=dict)
    _locks: dict[str, FileByteLock] = field(default_factory=dict)
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
        """
        with self._guard:
            current = self._holders.get(resource_id)
            if current is None:
                return self._acquire_shared(resource_id, holder_id, priority, on_preempt, preempt_equal)
            if current.holder_id == holder_id:
                return True
            if self._can_preempt(priority, holder_id, current.holder_id, current.priority, preempt_equal):
                if current.on_preempt is not None:
                    current.on_preempt()
                self._holders[resource_id] = _Holder(holder_id, priority, on_preempt, preempt_equal)
                self._write_holder(resource_id, self._holders[resource_id])
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
        if self.lock_dir is None:
            self._holders[resource_id] = _Holder(holder_id, priority, on_preempt, preempt_equal)
            return True
        lock = self._locks.get(resource_id)
        if lock is None:
            lock = FileByteLock(self._lock_path(resource_id))
            self._locks[resource_id] = lock
        deadline = time.monotonic() + max(0.0, float(self.preempt_timeout_s))
        request_token = uuid.uuid4().hex
        request_path: Path | None = None
        while True:
            try:
                acquired = lock.acquire()
            except OSError:
                return False
            if acquired:
                self._clear_request(resource_id)
                holder = _Holder(holder_id, priority, on_preempt, preempt_equal)
                if self._write_holder(resource_id, holder):
                    self._holders[resource_id] = holder
                    self._ensure_monitor()
                    return True
                self._locks.pop(resource_id, None)
                lock.release()
                return False
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
            if time.monotonic() >= deadline:
                if request_path is not None:
                    self._clear_request_path(request_path)
                self._locks.pop(resource_id, None)
                return False
            time.sleep(max(0.005, float(self.poll_interval_s)))

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
        with self._guard:
            current = self._holders.get(resource_id)
            if current is None or current.holder_id != holder_id:
                return
            self._release_locked(resource_id, holder_id)
            if not self._holders and self._monitor_stop is not None:
                stop = self._monitor_stop
                self._monitor_stop = None
                self._monitor_thread = None
        if stop is not None:
            stop.set()

    def _release_locked(self, resource_id: str, holder_id: str) -> None:
        current = self._holders.get(resource_id)
        if current is None or current.holder_id != holder_id:
            return
        del self._holders[resource_id]
        self._clear_holder(resource_id)
        lock = self._locks.pop(resource_id, None)
        if lock is not None:
            lock.release()

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
            stop = None
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
                try:
                    if current.on_preempt is not None:
                        current.on_preempt()
                except Exception:
                    pass
                self._release_locked(resource_id, current.holder_id)
                if not self._holders and self._monitor_stop is not None:
                    stop = self._monitor_stop
                    self._monitor_stop = None
                    self._monitor_thread = None
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            if stop is not None:
                stop.set()

    def _ensure_monitor(self) -> None:
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
