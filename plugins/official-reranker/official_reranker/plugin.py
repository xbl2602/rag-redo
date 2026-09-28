"""official-reranker 插件：生命周期钩子的薄封装，真实逻辑在 rerank.py。

**GPU 名额只在"真要装/已装着模型"时占，不是插件启用即占**（见 rerank.py 模块
docstring 的"GPU 租约语义"一节）：`on_enable` 只起空闲卸载守护线程，绝不申请
"gpu:0" 名额；名额由 `_ensure_loaded` 在真的要往显存里装模型时申请，由卸载/
降级/on_disable/on_unload 归还。

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
        # 刻意不碰 "gpu:0" 名额：名额语义是"模型此刻真的在显存里"（rerank.py
        # 模块 docstring）。插件启用只是"待命"，此刻没有任何模型在显存里。
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
            self._release_gpu_slot(ctx)
            # 加载失败闩锁必须复位：闩锁的语义是"本进程内不再重试"
            # （对齐 obsidian-rag/retriever.py:249-264），用户停用再启用
            # 重排器就是本项目给的重试入口——不复位的话，用户把模型补下载好
            # 之后重排器在这个进程里永远起不来，只能重启整个进程。
            self._reset_latch(ctx)
        ctx.logger.info("重排器已禁用")

    def on_unload(self, ctx):
        if self.engine is not None:
            # on_unload 也要收口 GPU 名额（宿主可能不经过 on_disable 直接
            # 卸载插件的热重载路径）；release_gpu_slot 幂等。
            self._release_gpu_slot(ctx)
            self._reset_latch(ctx)
        self.engine = None

    def _release_gpu_slot(self, ctx) -> None:
        """卸载模型 + 归还 GPU 名额，幂等；失败不拖垮宿主（同守护线程的宽容
        纪律）。"""
        if self.engine is None:
            return
        try:
            self.engine.release_gpu_slot()
        except Exception:  # noqa: BLE001 - 收口失败不拖垮宿主
            ctx.logger.warning("重排器收尾时归还GPU名额失败（忽略）", exc_info=True)

    def _reset_latch(self, ctx) -> None:
        # getattr 兜底：测试里存在"直接把 engine 换成只有 rerank() 的假对象"
        # 的用法（tests/test_pipeline_e2e.py::_PathRankedReranker），闩锁复位
        # 对它们没有意义，缺方法就跳过而不是刷一条异常日志。
        reset = getattr(self.engine, "reset_load_failure", None)
        if reset is None:
            return
        try:
            reset()
        except Exception:  # noqa: BLE001 - 复位失败不拖垮宿主
            ctx.logger.warning("重排器复位加载失败闩锁时出错（忽略）", exc_info=True)

    def rerank(self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10):
        assert self.engine is not None
        return self.engine.rerank(query, chunk_id_text_pairs, top_k=top_k)
