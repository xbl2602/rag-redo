"""official-embedder-bge-m3 插件：生命周期钩子的薄封装，真实逻辑在 embed.py。

**GPU 名额只在"真要装/已装着模型"时占，不是插件启用即占**（见 embed.py 模块
docstring 的"GPU 租约语义"一节）：`on_enable` 只起空闲卸载守护线程，绝不
申请 "gpu:0" 名额；名额由 `_ensure_loaded` 在真的要往显存里装模型时申请，
由卸载/降级/on_disable/on_unload 归还。

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
from core.gpu_arbiter import CudaCooldownGate

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
        gate = CudaCooldownGate(
            cooldown_seconds=ctx.settings.get("cuda_cooldown_seconds", 300),
            log=ctx.logger.info,
            state_file=ctx.storage.file("device_state.json", legacy="device_state.json"),
        )
        self.embedder = BGEM3Embedder(
            encoder=self._injected_encoder,
            resource_arbiter=ctx.resource_arbiter,
            cooldown_gate=gate,
            logger=ctx.logger,
        )
        ctx.logger.info("BGE-M3向量化插件已加载（模型懒加载，首次编码时才真正下载/加载）")

    def on_enable(self, ctx):
        # 刻意不碰 "gpu:0" 名额：名额语义是"模型此刻真的在显存里"（embed.py
        # 模块 docstring）。插件启用只是"待命"，此刻没有任何模型在显存里，
        # 在这里占名额会让 WEMM/OCR-local 在显存明明空着时也抢不到。
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
        self._release_gpu_slot(ctx)
        ctx.logger.info("BGE-M3向量化插件已禁用")

    def on_unload(self, ctx):
        # on_unload 也要收口：宿主可能不经过 on_disable 直接卸载插件（热重载
        # 路径）。release_gpu_slot 幂等，重复调用是安全空操作。
        self._release_gpu_slot(ctx)
        self.embedder = None

    def _release_gpu_slot(self, ctx) -> None:
        """卸载模型 + 归还 GPU 名额，幂等；失败不拖垮宿主（同守护线程的宽容
        纪律）。"""
        if self.embedder is None:
            return
        try:
            self.embedder.release_gpu_slot()
        except Exception:  # noqa: BLE001 - 禁用收口失败不拖垮宿主
            ctx.logger.warning("BGE-M3插件收尾时归还GPU名额失败（忽略）", exc_info=True)

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
