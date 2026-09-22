"""official-reranker 插件：生命周期钩子的薄封装，真实逻辑在 rerank.py。"""
from __future__ import annotations

from .rerank import MODEL_VERSION, Reranker, RerankerEngine


class RerankerPlugin:
    def __init__(self, reranker: Reranker | None = None) -> None:
        self._injected_reranker = reranker
        self.engine: RerankerEngine | None = None

    def on_load(self, ctx):
        self.engine = RerankerEngine(reranker=self._injected_reranker)
        ctx.logger.info("重排器已加载（模型懒加载，型号 %s）", MODEL_VERSION)

    def on_enable(self, ctx):
        ctx.logger.info("重排器已启用")

    def on_disable(self, ctx):
        ctx.logger.info("重排器已禁用")

    def on_unload(self, ctx):
        self.engine = None

    def rerank(self, query: str, chunk_id_text_pairs: list[tuple[str, str]], top_k: int = 10):
        assert self.engine is not None
        return self.engine.rerank(query, chunk_id_text_pairs, top_k=top_k)
