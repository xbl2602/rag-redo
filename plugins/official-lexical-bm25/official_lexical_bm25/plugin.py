"""official-lexical-bm25 插件：生命周期钩子的薄封装，真实逻辑在 bm25.py。"""
from __future__ import annotations

from core.contracts import Chunk

from .bm25 import BM25Index


class LexicalBM25Plugin:
    def __init__(self) -> None:
        self.index: BM25Index | None = None

    def on_load(self, ctx):
        self.index = BM25Index()
        ctx.logger.info("BM25词法索引已加载")

    def on_enable(self, ctx):
        ctx.logger.info("BM25词法索引已启用")

    def on_disable(self, ctx):
        ctx.logger.info("BM25词法索引已禁用")

    def on_unload(self, ctx):
        self.index = None

    def index_chunk(self, chunk: Chunk) -> None:
        assert self.index is not None
        self.index.add(chunk.chunk_id, chunk.text)

    def remove_chunk(self, chunk_id: str) -> None:
        assert self.index is not None
        self.index.remove(chunk_id)

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        assert self.index is not None
        return self.index.search(query, top_k=top_k)
