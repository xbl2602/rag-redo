"""official-vector-store-chroma 插件：生命周期钩子的薄封装，真实逻辑在 store.py。"""
from __future__ import annotations

from .store import ChromaVectorStore


class ChromaVectorStorePlugin:
    def __init__(self) -> None:
        self.store: ChromaVectorStore | None = None

    def on_load(self, ctx):
        self.store = ChromaVectorStore(ctx.data_dir / "chroma")
        ctx.logger.info("Chroma向量库已加载")

    def on_enable(self, ctx):
        ctx.logger.info("Chroma向量库已启用")

    def on_disable(self, ctx):
        ctx.logger.info("Chroma向量库已禁用")

    def on_unload(self, ctx):
        self.store = None

    def upsert(
        self,
        library_id: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        documents=None,
        metadatas=None,
    ) -> None:
        assert self.store is not None
        self.store.upsert(library_id, chunk_ids, vectors, documents, metadatas)

    def delete(self, library_id: str, chunk_ids: list[str]) -> None:
        assert self.store is not None
        self.store.delete(library_id, chunk_ids)

    def get_by_ids(self, library_id: str, chunk_ids: list[str]) -> dict[str, dict]:
        assert self.store is not None
        return self.store.get_by_ids(library_id, chunk_ids)

    def query(self, library_id: str, query_vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        assert self.store is not None
        return self.store.query(library_id, query_vector, top_k=top_k)
