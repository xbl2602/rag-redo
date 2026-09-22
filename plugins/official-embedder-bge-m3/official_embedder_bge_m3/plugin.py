"""official-embedder-bge-m3 插件：生命周期钩子的薄封装，真实逻辑在 embed.py。"""
from __future__ import annotations

from core.contracts import Chunk, EmbeddingVector

from .embed import MODEL_VERSION, BGEM3Embedder, Encoder

PLUGIN_ID = "official-embedder-bge-m3"


class EmbedderPlugin:
    def __init__(self, encoder: Encoder | None = None) -> None:
        # 测试可以在构造期注入假 encoder；核心真实加载插件时用默认的
        # None（走懒加载真实模型的路径），见 embed.py 模块 docstring。
        self._injected_encoder = encoder
        self.embedder: BGEM3Embedder | None = None

    def on_load(self, ctx):
        self.embedder = BGEM3Embedder(encoder=self._injected_encoder)
        ctx.logger.info("BGE-M3向量化插件已加载（模型懒加载，首次编码时才真正下载/加载）")

    def on_enable(self, ctx):
        ctx.logger.info("BGE-M3向量化插件已启用")

    def on_disable(self, ctx):
        ctx.logger.info("BGE-M3向量化插件已禁用")

    def on_unload(self, ctx):
        self.embedder = None

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
