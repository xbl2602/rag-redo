"""向量存储的薄封装，底层是 chromadb.PersistentClient。

一个库一个 collection（`lib_<library_id>`）——库与库之间物理隔离，一个库
的向量查询绝不可能命中另一个库的数据，不需要额外的 library_id 过滤逻辑，
隔离性由 Chroma 的 collection 边界天然保证。写入是 upsert 语义：重复写
同一个 chunk_id 是更新，不是报错或产生重复条目（同 docs/DATA_FLOW.md
"写入一律 upsert"的约定）。
"""
from __future__ import annotations

from pathlib import Path

import chromadb

VECTOR_STORE_VERSION = "0.1.0"


class ChromaVectorStore:
    def __init__(self, persist_dir: Path) -> None:
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))

    def _collection(self, library_id: str):
        return self._client.get_or_create_collection(name=f"lib_{library_id}")

    def upsert(
        self,
        library_id: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        documents: list[str] | None = None,
    ) -> None:
        if not chunk_ids:
            return
        self._collection(library_id).upsert(ids=chunk_ids, embeddings=vectors, documents=documents)

    def delete(self, library_id: str, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        self._collection(library_id).delete(ids=chunk_ids)

    def query(self, library_id: str, query_vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        coll = self._collection(library_id)
        n = coll.count()
        if n == 0:
            return []
        result = coll.query(query_embeddings=[query_vector], n_results=min(top_k, n))
        ids = result["ids"][0]
        distances = result["distances"][0]
        # Chroma 默认用 L2 距离（越小越相似），转成"越大越相似"的分数，
        # 和 BM25/RRF 等其他阶段"分数越大越好"的约定保持一致，调用方不用
        # 为向量检索这一路单独记一套相反的排序方向。
        return [(chunk_id, 1.0 / (1.0 + dist)) for chunk_id, dist in zip(ids, distances)]

    def count(self, library_id: str) -> int:
        return self._collection(library_id).count()
