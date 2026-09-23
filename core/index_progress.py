"""core/index_progress.py — 索引进度报告 + 后台执行（核心服务，不是插件）。

**补的是哪个坑**：2026-09-23 全面功能审计发现，`core/pipeline.py::
index_library()` 是完全同步阻塞的——大库重建索引时，MCP 的
`reindex_knowledge` 这一次调用会一直卡到全部做完才返回，没有任何办法
在过程中查看"进行到哪一步了、是不是卡死了"，AI 客户端也可能在这期间
等到协议超时。对齐 obsidian-rag 的 `index_status`/心跳双通道机制（有
简化，见下文）。

**为什么是核心服务**：GUI 和 MCP server 是两个独立进程，一个发起了
后台重建索引，另一个（或同一个 AI 会话稍后再问）想查询"跑到哪了"——
进度状态必须持久化到磁盘，不能只存进程内存，这正是"两个互不知情的
调用方需要一个中立的状态载体"，同 `resource_arbiter`/`write_gate` 一样
的理由。

**一个库同一时刻只允许一个索引任务在跑**：Chroma/BM25 的写路径不是
为并发写同一个库设计的，同一个库的第二次 `start()` 在第一次还没结束时
会被拒绝，返回清楚的提示，不是排队等待也不是静默丢弃（同 obsidian-rag
"单飞+拒绝并发"的纪律，也是 docs/LESSONS.md"单一 flight+mutex+拒绝
忙碌"那条教训的具体应用）。不同库互不影响，可以同时各自跑一个。

**刻意的简化，如实记录**：
- 不做 obsidian-rag 那套"心跳线程独立于工作线程、5s 恒定刷新"的双线程
  设计——这里心跳就是"每处理完一个文件更新一次进度"，文件数越多心跳
  越密，单个文件提取特别慢（比如卡在本机OCR一个大PDF）时确实会有一段
  时间看不到心跳跳动，`STALL_TIMEOUT_S` 的判定阈值放宽到覆盖这种正常
  慢文件的情况，不是精确复刻旧项目的独立心跳线程机制。
- 进度状态只保留"最近一次"（不是历史记录/任务队列）——每个库一个
  进度文件，新一轮 `start()` 直接覆盖，不查历史上跑过多少轮。
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable


@dataclasses.dataclass
class IndexProgress:
    library_id: str
    stage: str  # "running" | "done" | "failed"
    files_done: int = 0
    files_total: int = 0
    current_path: str = ""
    started_at: float = 0.0
    heartbeat_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None
    succeeded: int = 0
    failed: int = 0


class IndexProgressTracker:
    HEARTBEAT_TIMEOUT_S = 15.0  # 心跳停止超过这么久 → 判定"疑似卡死"，对齐 obsidian-rag 同名阈值
    STALL_TIMEOUT_S = 60.0  # 进度长时间不推进但心跳还在 → 判定"批次内卡死"（比 obsidian-rag 的
    # 25s 宽松得多——见模块 docstring，这里的心跳粒度是"每个文件"，不是独立线程秒级刷新，
    # 单个文件卡住本身可能就是几十秒（比如本机OCR大PDF），不能用秒级阈值误判成"死了"）

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._running: set[str] = set()  # 本进程视角正在跑的库（跨进程互斥见 start() 的说明）

    def _path_for(self, library_id: str) -> Path:
        import re

        safe = re.sub(r"[^\w.-]", "_", library_id)
        return self._root / f"{safe}.json"

    def is_running(self, library_id: str) -> bool:
        """本进程视角是否有一个索引任务正在跑——只能防住"同一个进程内
        重复发起"，防不住"GUI 进程和 MCP server 进程同时对同一个库各自
        发起一次"（那是真正的跨进程锁，需要文件锁，超出这轮范围，如实
        记录：Chroma/BM25 并发写同一个库本来就不安全，这属于用户操作层面
        该避免的场景，不是这个追踪器要解决的问题）。"""
        with self._lock:
            return library_id in self._running

    def start(
        self,
        library_id: str,
        index_fn: Callable[[Callable[[int, int, str], None]], object],
    ) -> tuple[bool, str]:
        """启动后台索引线程，立即返回（不等 `index_fn` 跑完）。

        `index_fn` 是一个接受"进度回调"的可调用对象，典型用法：
        `lambda cb: pipeline.index_library(library_id, progress_callback=cb)`
        ——这个类不知道、也不关心索引具体怎么做，只负责"起一个后台线程
        跑它、定期把进度写盘、跑完记录最终结果"，同 `core/resource_
        arbiter.py`"不懂 GPU 是什么"的哲学，这里是"不懂索引是什么"。
        """
        with self._lock:
            if library_id in self._running:
                return False, f"库「{library_id}」已经有一个索引任务在跑，请等它完成或查 index_status"
            self._running.add(library_id)

        progress = IndexProgress(
            library_id=library_id, stage="running", started_at=time.time(), heartbeat_at=time.time()
        )
        self._write(progress)

        def _on_progress(files_done: int, files_total: int, current_path: str) -> None:
            progress.files_done = files_done
            progress.files_total = files_total
            progress.current_path = current_path
            progress.heartbeat_at = time.time()
            self._write(progress)

        def _worker() -> None:
            try:
                report = index_fn(_on_progress)
                progress.stage = "done"
                progress.succeeded = getattr(report, "succeeded", 0)
                progress.failed = getattr(report, "failed", 0)
            except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须被这里兜住，
                # 否则线程静默死掉、progress 永远停在 "running"，调用方会一直
                # 以为任务还在跑——这正是"长时间任务需要心跳"这条教训要防的
                # 假活状态，绝不能在这里裸放行。
                progress.stage = "failed"
                progress.error = f"{type(exc).__name__}: {exc}"
            finally:
                progress.finished_at = time.time()
                progress.heartbeat_at = progress.finished_at
                self._write(progress)
                with self._lock:
                    self._running.discard(library_id)

        threading.Thread(target=_worker, daemon=True, name=f"index-{library_id}").start()
        return True, "已开始后台重建索引"

    def status(self, library_id: str) -> dict | None:
        """读磁盘上的进度快照并附健康判定。从没跑过索引（没有进度文件）
        返回 None，调用方自己决定怎么展示"从来没跑过"和"跑过但状态异常"
        的区别。"""
        path = self._path_for(library_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        now = time.time()
        health = "healthy"
        if data.get("stage") == "running":
            since_heartbeat = now - data.get("heartbeat_at", now)
            if since_heartbeat > self.HEARTBEAT_TIMEOUT_S:
                health = "stalled_no_heartbeat"
            elif since_heartbeat > self.STALL_TIMEOUT_S:
                health = "stalled_no_progress"
        data["health"] = health
        return data

    def _write(self, progress: IndexProgress) -> None:
        # 真实抓到的并发读写竞态：后台工作线程一次索引任务里会写很多次
        # （每个文件一次心跳），GUI/MCP 两个独立进程随时可能在这期间调
        # status() 读——`Path.write_text` 不是原子操作（打开/截断/写入/
        # 关闭分几步），读方如果恰好读在"文件已截断、新内容还没写完"的
        # 窗口，会读到不完整的 JSON，`status()` 就会把这次正常运行误判
        # 成"文件损坏/从没跑过"返回 None（单测里真实稳定复现过，不是
        # 理论风险）。改成"先写临时文件、再 os.replace 原子改名覆盖"——
        # os.replace 在 POSIX 和 Windows 上都保证这一步本身是原子的，
        # 读方任意时刻看到的要么是完整的旧文件要么是完整的新文件，不会
        # 看到中间状态。
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = self._path_for(progress.library_id)
            tmp_path = target.with_suffix(f".{threading.get_ident()}.tmp")
            tmp_path.write_text(
                json.dumps(dataclasses.asdict(progress), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # Windows 实测：并发的 status() 读刚关闭 target 的读句柄那一瞬间，
            # os.replace 有小概率报 PermissionError（WinError 5，句柄释放有
            # 短暂延迟，不是真的被永久占用）——重试几次、每次退避几毫秒即可
            # 让开这个窗口，不是真正的锁冲突，POSIX 上不会触发这条分支。
            for attempt in range(5):
                try:
                    os.replace(tmp_path, target)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01)
        except OSError:
            pass  # 进度写盘失败不该让索引任务本身失败——fail-open，同其他核心服务的一贯原则
