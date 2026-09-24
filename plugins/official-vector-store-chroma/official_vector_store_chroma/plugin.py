"""official-vector-store-chroma 插件：生命周期钩子的薄封装，真实逻辑在 store.py。"""
from __future__ import annotations

from core.contracts import SampledChunk
from core.index_generation import IndexGenerationStore

from .store import VECTOR_STORE_VERSION, ChromaVectorStore


class ChromaVectorStorePlugin:
    def __init__(self) -> None:
        self.store: ChromaVectorStore | None = None

    def on_load(self, ctx):
        self.store = ChromaVectorStore(
            ctx.storage.directory("chroma", legacy="chroma"),
            generation_store=IndexGenerationStore(
                ctx.storage.directory("index_generations", legacy="index_generations")
            ),
        )
        ctx.logger.info("Chroma向量库已加载")

    def on_enable(self, ctx):
        ctx.logger.info("Chroma向量库已启用")

    def on_disable(self, ctx):
        ctx.logger.info("Chroma向量库已禁用")

    def on_unload(self, ctx):
        if self.store is not None:
            self.store.close()
        self.store = None

    def index_signature(self) -> str:
        return VECTOR_STORE_VERSION

    def upsert(
        self,
        library_id: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        documents=None,
        metadatas=None,
        generation: str | None = None,
    ) -> None:
        assert self.store is not None
        self.store.upsert(
            library_id,
            chunk_ids,
            vectors,
            documents,
            metadatas,
            generation,
        )

    def delete(
        self,
        library_id: str,
        chunk_ids: list[str],
        generation: str | None = None,
    ) -> None:
        assert self.store is not None
        self.store.delete(library_id, chunk_ids, generation)

    def delete_generation(self, library_id: str, generation: str) -> None:
        assert self.store is not None
        self.store.delete_generation(library_id, generation)

    def get_by_ids(
        self,
        library_id: str,
        chunk_ids: list[str],
        generation: str | None = None,
    ) -> dict[str, dict]:
        assert self.store is not None
        return self.store.get_by_ids(library_id, chunk_ids, generation)

    def get_all(self, library_id: str, generation: str | None = None) -> dict[str, dict]:
        assert self.store is not None
        return self.store.get_all(library_id, generation)

    def query(
        self,
        library_id: str,
        query_vector: list[float],
        top_k: int = 10,
        generation: str | None = None,
    ) -> list[tuple[str, float]]:
        assert self.store is not None
        return self.store.query(library_id, query_vector, top_k=top_k, generation=generation)

    def count(self, library_id: str, generation: str | None = None) -> int:
        assert self.store is not None
        return self.store.count(library_id, generation)

    def sample_records(self, records: dict[str, dict], k: int = 20) -> list[SampledChunk]:
        assert self.store is not None
        return [
            SampledChunk(path=row["path"], heading=row["heading"], text=row["text"])
            for row in self.store.sample_records(records, k=k)
        ]

    def sample(
        self,
        library_id: str,
        k: int = 20,
        generation: str | None = None,
    ) -> list[SampledChunk]:
        assert self.store is not None
        return [
            SampledChunk(path=r["path"], heading=r["heading"], text=r["text"])
            for r in self.store.sample(library_id, k=k, generation=generation)
        ]
