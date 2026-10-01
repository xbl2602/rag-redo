"""几本 PDF 同时转（2026-10-01 操作者确认，BC-01）。

真机 Y2S1 增量时看到：转文字阶段后台索引进程只有一个线程在干活（20 线程的电脑只用着 1 个核），
一本 901 页的教材用“快速方式”转要 110 秒，期间显卡闲着、进度条不动。快速方式（`pymupdf_rag`）
本身只用一个核，所以这里在后台开几个子进程，把接下来要转的几本提前同时转好；编排层走到哪一本
就取哪一本的结果，结果与在本进程里转完全相同（同一个 `extract()`）。

只对会走快速方式的 PDF 提前转：精细方式（每页 AI 版面分析）自己就占满所有核，几本同时转只会
互相抢。子进程用 spawn 方式起（与后台索引进程相同），一轮转完就收掉，不在常驻进程里空占内存；
停止索引时整棵进程树一起被关掉（`core/index_progress.py` 的 `_terminate_worker`）。"""
from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import wait as wait_futures
from pathlib import Path

import pymupdf

from .extract import extract, uses_fast_mode

#: 同时转几本：线程数的四分之一、最多 4。快速方式每本只用一个核，再多内存涨得比速度快
#: （每个子进程要把整本 PDF 读进来，一本教材 100 多 MB）。
DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 4))


def _convert(library_id: str, path: str, root: str, mode: str, image_rule: str):
    """子进程里跑：与本进程里转的是同一个函数；日志先收着，回到本进程再写进插件日志。"""
    lines: list[str] = []
    doc = extract(library_id, path, Path(root), mode=mode, image_rule=image_rule, log=lines.append)
    return doc, lines


def _page_count(full_path: Path) -> int | None:
    try:
        doc = pymupdf.open(full_path)
    except Exception:  # noqa: BLE001 - 打不开的交给正常转换路径去报失败
        return None
    try:
        return doc.page_count
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001 - 收尾失败不能盖掉已经拿到的页数（AGENTS.md §5）
            pass


class AheadConverter:
    """提前转的几本：键是（库, 路径, 根目录, 转换方式, 图片页判法）——设置变了的旧结果不会被拿错。"""

    def __init__(self, workers: int = DEFAULT_WORKERS) -> None:
        self._workers = max(1, int(workers))
        self._pool: ProcessPoolExecutor | None = None
        self._jobs: dict[tuple[str, str, str, str, str], Future] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(library_id: str, path: str, root: Path, mode: str, image_rule: str) -> tuple[str, str, str, str, str]:
        return (library_id, path, str(root), mode, image_rule)

    def submit(self, library_id: str, paths: list[str], root: Path, mode: str, image_rule: str) -> list[str]:
        """按顺序收下能提前转的，返回收下（或早已在转）的那些。同时在转的不超过 2 倍子进程数，
        满了就停——后面的等下次再交。不会走快速方式的不收。"""
        accepted: list[str] = []
        for path in paths:
            key = self.key(library_id, path, root, mode, image_rule)
            with self._lock:
                if key in self._jobs:
                    accepted.append(path)
                    continue
                if sum(1 for job in self._jobs.values() if not job.done()) >= self._workers * 2:
                    break
            pages = _page_count(Path(root) / path)
            if pages is None or not uses_fast_mode(mode, pages):
                continue
            with self._lock:
                if self._pool is None:
                    self._pool = ProcessPoolExecutor(
                        max_workers=self._workers, mp_context=multiprocessing.get_context("spawn")
                    )
                self._jobs[key] = self._pool.submit(_convert, library_id, path, str(root), mode, image_rule)
            accepted.append(path)
        return accepted

    def take(self, library_id: str, path: str, root: Path, mode: str, image_rule: str) -> Future | None:
        with self._lock:
            return self._jobs.pop(self.key(library_id, path, root, mode, image_rule), None)

    def wait(self, library_id: str, path: str, timeout: float) -> bool:
        """最多等 `timeout` 秒；这一本已经转好（或根本没提前转）返回 True。"""
        with self._lock:
            jobs = [job for key, job in self._jobs.items() if key[0] == library_id and key[1] == path]
        if not jobs:
            return True
        done, _ = wait_futures(jobs, timeout=timeout)
        return len(done) == len(jobs)

    def ready(self, library_id: str) -> int:
        """这个库已经提前转好、还没被取走的有几本（进度条用）。"""
        with self._lock:
            return sum(1 for key, job in self._jobs.items() if key[0] == library_id and job.done())

    def cancel(self, library_id: str, paths: list[str] | None = None) -> None:
        """不要了：还没开始的取消，正在转的转完结果丢掉。一个库都不剩了就把子进程收掉。"""
        with self._lock:
            for key in [k for k in self._jobs if k[0] == library_id and (paths is None or k[1] in paths)]:
                self._jobs.pop(key).cancel()
            idle = not self._jobs
        if idle:
            self.close()

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
            jobs, self._jobs = list(self._jobs.values()), {}
        for job in jobs:
            job.cancel()
        if pool is None:
            return
        try:
            pool.terminate_workers()  # 正在转的一并停掉：插件停用、索引收尾都不该再等它
        except Exception:  # noqa: BLE001 - 子进程已经没了等情况，收尾不能抛
            pass
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except Exception:  # noqa: BLE001 - 同上
            pass
