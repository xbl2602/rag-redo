"""official-reranker 插件：生命周期钩子的薄封装，真实逻辑在 rerank.py。

空闲卸载守护线程的道理同 official-embedder-bge-m3/plugin.py，这里不重复
展开。
"""
from __future__ import annotations

import threading

from core.gpu_arbiter import CudaCooldownGate
from .rerank import IDLE_UNLOAD_SECONDS, MODEL_VERSION, Reranker, RerankerEngine

_IDLE_CHECK_INTERVAL_S = min(30, max(1, IDLE_UNLOAD_SECONDS)) if IDLE_UNLOAD_SECONDS > 0 else 30


class RerankerPlugin:
    def __init__(self, reranker: Reranker | None = None) -> None:
        self._injected_reranker = reranker
        self.engine: RerankerEngine | None = None
        self._idle_stop: threading.Event | None = None
        self._idle_thread: threading.Thread | None = None

    def on_load(self, ctx):
        gate = CudaCooldownGate(
            cooldown_seconds=ctx.settings.get("cuda_cooldown_seconds", 300),
            log=ctx.logger.info,
            state_file=ctx.storage.file("device_state.json", legacy="device_state.json"),
        )
        self.engine = RerankerEngine(
            reranker=self._injected_reranker,
            resource_arbiter=ctx.resource_arbiter,
            cooldown_gate=gate,
            logger=ctx.logger,
        )
        ctx.logger.info("重排器已加载（模型懒加载，型号 %s）", MODEL_VERSION)

    def on_enable(self, ctx):
        self._idle_stop = threading.Event()

        def _loop():
            while not self._idle_stop.wait(_IDLE_CHECK_INTERVAL_S):
                try:
                    self.engine.idle_check()
                except Exception:  # noqa: BLE001 - 后台守护线程绝不能带崩宿主进程
                    pass

        self._idle_thread = threading.Thread(target=_loop, daemon=True, name="reranker-idle-unload")
        self._idle_thread.start()
        ctx.logger.info("重排器已启用")

    def on_disable(self, ctx):
        if self._idle_stop is not None:
            self._idle_stop.set()
        self._idle_stop = None
        self._idle_thread = None
        if self.engine is not None:
            try:
                self.engine.release_gpu_slot()
            except Exception:  # noqa: BLE001 - 禁用收口失败不拖垮宿主，同守护线程的宽容纪律
                ctx.logger.warning("重排器禁用时归还GPU名额失败（忽略）", exc_info=True)
        ctx.logger.info("重排器已禁用")

    def on_unload(self, ctx):
        self.engine = None

    def rerank(self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10):
        assert self.engine is not None
        return self.engine.rerank(query, chunk_id_text_pairs, top_k=top_k)
