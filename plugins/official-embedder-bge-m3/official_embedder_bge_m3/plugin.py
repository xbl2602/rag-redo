"""official-embedder-bge-m3 插件：生命周期钩子的薄封装，真实逻辑在 embed.py。

**空闲卸载守护线程**：GPU 生命周期管理（见 embed.py 模块 docstring）需要
一个后台线程周期性检查"模型是否已经空闲太久该卸载了"——不能只在有新
请求进来时才检查，空闲的定义恰恰是没有请求，只在请求路径里检查会让
"多久没用就卸载"这条规则在长时间无请求的场景下形同虚设（同
official-visual-wemm/server.py::_idle_unload_daemon 的同一条教训，那边
是子进程自己的线程，这里是核心进程里插件自己的线程，道理一样）。
"""
from __future__ import annotations

import threading

from core.contracts import Chunk, EmbeddingVector

from .embed import IDLE_UNLOAD_SECONDS, MODEL_VERSION, BGEM3Embedder, Encoder

PLUGIN_ID = "official-embedder-bge-m3"
_IDLE_CHECK_INTERVAL_S = min(30, max(1, IDLE_UNLOAD_SECONDS)) if IDLE_UNLOAD_SECONDS > 0 else 30


class EmbedderPlugin:
    def __init__(self, encoder: Encoder | None = None) -> None:
        # 测试可以在构造期注入假 encoder；核心真实加载插件时用默认的
        # None（走懒加载真实模型的路径），见 embed.py 模块 docstring。
        self._injected_encoder = encoder
        self.embedder: BGEM3Embedder | None = None
        self._idle_stop: threading.Event | None = None
        self._idle_thread: threading.Thread | None = None

    def on_load(self, ctx):
        self.embedder = BGEM3Embedder(encoder=self._injected_encoder, resource_arbiter=ctx.resource_arbiter, logger=ctx.logger)
        ctx.logger.info("BGE-M3向量化插件已加载（模型懒加载，首次编码时才真正下载/加载）")

    def on_enable(self, ctx):
        self._idle_stop = threading.Event()

        def _loop():
            while not self._idle_stop.wait(_IDLE_CHECK_INTERVAL_S):
                try:
                    self.embedder.idle_check()
                except Exception:  # noqa: BLE001 - 后台守护线程绝不能带崩宿主进程
                    pass

        self._idle_thread = threading.Thread(target=_loop, daemon=True, name="bge-m3-idle-unload")
        self._idle_thread.start()
        ctx.logger.info("BGE-M3向量化插件已启用")

    def on_disable(self, ctx):
        if self._idle_stop is not None:
            self._idle_stop.set()
        self._idle_stop = None
        self._idle_thread = None
        if self.embedder is not None:
            try:
                self.embedder.release_gpu_slot()
            except Exception:  # noqa: BLE001 - 禁用收口失败不拖垮宿主，同守护线程的宽容纪律
                ctx.logger.warning("BGE-M3插件禁用时归还GPU名额失败（忽略）", exc_info=True)
        ctx.logger.info("BGE-M3向量化插件已禁用")

    def on_unload(self, ctx):
        self.embedder = None

    def index_signature(self) -> str:
        return MODEL_VERSION

    def embed_texts(self, texts: list[str]) -> list[tuple[float, ...]]:
        """给编排层(core/pipeline.py)直接对查询字符串编码用——查询不是
        Chunk，不需要为它硬凑一个假 Chunk 才能复用 embed_chunks。"""
        assert self.embedder is not None
        return [tuple(vector) for vector in self.embedder.embed(texts)]

    def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddingVector]:
        vectors = self.embed_texts([c.text for c in chunks])
        return [
            EmbeddingVector(
                chunk_id=chunk.chunk_id,
                vector=vector,
                model_id=PLUGIN_ID,
                model_version=MODEL_VERSION,
                dim=len(vector),
            )
            for chunk, vector in zip(chunks, vectors)
        ]
