"""Cross-Encoder 重排器。懒加载契约和 official-embedder-bge-m3 完全同一
套理由，见该插件 embed.py 的模块 docstring——这里不重复展开。

**GPU 生命周期管理**：和 official-embedder-bge-m3 是同一个"检索侧"GPU
消费群体——用同一个 `GPU_HOLDER_ID` 向资源仲裁器申请"gpu:0"名额（同一个
holder_id 意味着两边互相不冲突：谁先加载谁申请到，另一边后来加载时是
"同一持有者再次申请"的幂等续期，不会互相抢占/驱逐），同一套设备选择+
空闲卸载逻辑（详细设计理由见 embed.py 模块 docstring，这里不重复展开，
两个插件各自维护一份小实现，是插件互相隔离原则下的刻意小重复）。

**已知的、刻意的简化（这份文件独有的一条）**：`GPU_HOLDER_ID` 共享同一个
字符串意味着"embedder 和 reranker 只要有一个还启用着，就该一直占着这个
名额"这件事没有真正的引用计数——如果只禁用其中一个插件（比如只关掉
reranker，embedder 还启用着、模型还在显存里），这个插件的 on_disable 会
把共享名额释放掉，理论上给了 WEMM/OCR-local 一个"名额空出来了"的抢占
窗口，即使 embedder 的模型其实还实际占着显存。这是一个真实但很窄的边界
情况（重排器脱离向量检索单独禁用是罕见配置），如实记录不假装解决了——
真正解决需要给"检索侧GPU消费群体"整体加一层核心才能提供的引用计数服务，
超出这一轮"让 WEMM/OCR-local 不再永远抢不到资源"这个核心目标的范围。
"""
from __future__ import annotations

import threading
import time
from typing import Protocol

from core import gpu_arbiter
from core.gpu_arbiter import CudaCooldownGate

MODEL_VERSION = "BAAI/bge-reranker-v2-m3"
GPU_RESOURCE_ID = "gpu:0"
GPU_HOLDER_ID = "official-text-retrieval-gpu"  # 和 official-embedder-bge-m3 共用，见模块 docstring
GPU_PRIORITY = 100
IDLE_UNLOAD_SECONDS = 300
SLOW_BATCH_SECONDS = 30.0  # 对齐旧项目 encode_safe 的慢批阈值


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class _RealReranker:
    def __init__(self, model_name: str = MODEL_VERSION, *, resource_arbiter=None, cooldown_gate: CudaCooldownGate | None = None, logger=None) -> None:
        self._model_name = model_name
        self._model = None
        self._resource_arbiter = resource_arbiter
        self._cooldown_gate = cooldown_gate if cooldown_gate is not None else CudaCooldownGate()
        self._logger = logger
        self._last_use = time.time()
        self._slow_batch_count = 0
        self._lock = threading.RLock()

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(message)

    def _ensure_loaded(self):
        with gpu_arbiter.GPU_LOCK:
            if self._model is None:
                from sentence_transformers import CrossEncoder  # noqa: PLC0415 - 故意懒加载

                device = self._select_device()
                try:
                    self._model = CrossEncoder(self._model_name, device=device)
                except Exception as exc:
                    # 对齐旧项目 get_model：CUDA 初始化失败 → 冷却 + 降级 CPU 重试一次
                    if device != "cuda":
                        raise
                    self._cooldown_gate.cooldown(str(exc))
                    self._log(f"CUDA 初始化失败（{exc}），降级 CPU")
                    device = "cpu"
                    self._model = CrossEncoder(self._model_name, device=device)
                self._log(f"重排器模型已加载（device={device}）")
                self._cooldown_gate.report_device(device)
            self._last_use = time.time()
            return self._model

    def _select_device(self) -> str:
        """同 embed.py::_select_device 的对齐口径：冷却期状态机取代裸
        is_available；拿到"gpu:0"名额后直接加载，不做 VRAM 阻塞等待
        （旧项目检索侧从不阻塞等待，wait 语义只属于 WEMM/MinerU 服务端）。"""
        if not self._cooldown_gate.ready():
            return "cpu"
        if self._resource_arbiter is not None:
            acquired = self._resource_arbiter.acquire(
                GPU_RESOURCE_ID,
                GPU_HOLDER_ID,
                priority=GPU_PRIORITY,
                on_preempt=self._unload,
            )
            if not acquired:
                return "cpu"
        return "cuda"

    def release_gpu_slot(self) -> None:
        """插件 on_disable 时调用——卸载模型并归还"gpu:0"名额，理由同
        embed.py::_RealEncoder.release_gpu_slot。"""
        self._unload()
        if self._resource_arbiter is not None:
            self._resource_arbiter.release(GPU_RESOURCE_ID, GPU_HOLDER_ID)

    def score(self, query: str, texts: list[str]) -> list[float]:
        with self._lock:
            model = self._ensure_loaded()
            pairs = [[query, text] for text in texts]
            started = time.time()
            result = list(model.predict(pairs))
            elapsed = time.time() - started
            self._last_use = time.time()
            self._check_slow_batch(elapsed)
            return result

    def _check_slow_batch(self, elapsed: float) -> None:
        """同 embed.py::_RealEncoder._check_slow_batch（对齐旧项目
        encode_safe 慢批检测），连续两批慢批主动降级 CPU。"""
        if elapsed > SLOW_BATCH_SECONDS:
            self._slow_batch_count += 1
            self._log(
                f"CUDA 单批重排耗时 {elapsed:.1f}s（>{SLOW_BATCH_SECONDS:.0f}s），"
                f"疑似共享显存溢出（连续 {self._slow_batch_count} 次）"
            )
            if self._slow_batch_count >= 2:
                self._log("连续慢批，主动降级 CPU，避免病态运行...")
                self._cooldown_gate.cooldown("slow-batch")
                self._unload()
                self._slow_batch_count = 0
        else:
            self._slow_batch_count = 0

    def idle_check(self) -> None:
        if IDLE_UNLOAD_SECONDS <= 0 or self._model is None:
            return
        if time.time() - self._last_use > IDLE_UNLOAD_SECONDS:
            with self._lock:
                if time.time() - self._last_use > IDLE_UNLOAD_SECONDS and self._model is not None:
                    self._unload_locked()

    def _unload(self) -> None:
        with gpu_arbiter.GPU_LOCK, self._lock:
            self._unload_locked()

    def _unload_locked(self) -> None:
        if self._model is None:
            return
        self._model = None
        try:
            import gc

            gc.collect()
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        self._log("重排器模型空闲卸载，显存已释放")


class RerankerEngine:
    def __init__(self, reranker: Reranker | None = None, *, resource_arbiter=None, logger=None) -> None:
        self._reranker = reranker if reranker is not None else _RealReranker(resource_arbiter=resource_arbiter, logger=logger)

    def rerank(
        self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10
    ) -> list[tuple[str, float]]:
        if not chunk_id_text_pairs:
            return []
        ids = [chunk_id for chunk_id, _ in chunk_id_text_pairs]
        texts = [text for _, text in chunk_id_text_pairs]
        scores = self._reranker.score(query, texts)
        ranked = sorted(zip(ids, scores), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def idle_check(self) -> None:
        check = getattr(self._reranker, "idle_check", None)
        if check is not None:
            check()

    def release_gpu_slot(self) -> None:
        """透传名额归还（on_disable 用）；注入的假 reranker 没有这个方法
        时静默跳过，同 idle_check 的宽容语义。"""
        release = getattr(self._reranker, "release_gpu_slot", None)
        if release is not None:
            release()
