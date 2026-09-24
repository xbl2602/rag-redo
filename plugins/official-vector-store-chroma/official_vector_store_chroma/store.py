"""向量存储的薄封装，底层是 chromadb.PersistentClient。

一个库一个 collection（`lib_<library_id>`）——库与库之间物理隔离，一个库
的向量查询绝不可能命中另一个库的数据，不需要额外的 library_id 过滤逻辑，
隔离性由 Chroma 的 collection 边界天然保证。写入是 upsert 语义：重复写
同一个 chunk_id 是更新，不是报错或产生重复条目（同 docs/DATA_FLOW.md
"写入一律 upsert"的约定）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import chromadb

from core.index_generation import IndexGenerationStore

VECTOR_STORE_VERSION = "0.1.0"


class ChromaVectorStore:
    def __init__(
        self,
        persist_dir: Path,
        generation_store: IndexGenerationStore | None = None,
    ) -> None:
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._generations = generation_store

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _collection(self, library_id: str, generation: str | None = None):
        if generation is None and self._generations is not None:
            generation = self._generations.active(library_id)
        if generation:
            key = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
            return self._client.get_or_create_collection(name=f"libg_{key}")
        return self._client.get_or_create_collection(name=f"lib_{library_id}")

    def upsert(
        self,
        library_id: str,
        chunk_ids: list[str],
        vectors: list[list[float]],
        documents: list[str] | None = None,
        metadatas: list[dict] | None = None,
        generation: str | None = None,
    ) -> None:
        if not chunk_ids:
            return
        self._collection(library_id, generation).upsert(
            ids=chunk_ids, embeddings=vectors, documents=documents, metadatas=metadatas
        )

    def get_by_ids(
        self,
        library_id: str,
        chunk_ids: list[str],
        generation: str | None = None,
    ) -> dict[str, dict]:
        """按 chunk_id 直接取记录（不是相似度查询）——查询管道融合词法/
        向量两路排名后，需要把任意来源（哪怕只被 BM25 命中、没进向量
        Top-K）的 chunk_id 都能取到完整文本+元数据用于装配最终结果，
        Chroma 原生的按 id get() 正好承担这个"chunk 存储"的角色，不用
        另起一个并行的数据结构维护同一份东西两份拷贝。"""
        if not chunk_ids:
            return {}
        result = self._collection(library_id, generation).get(
            ids=chunk_ids,
            include=["documents", "metadatas"],
        )
        return {
            chunk_id: {"document": doc, "metadata": meta}
            for chunk_id, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
        }

    def get_all(self, library_id: str, generation: str | None = None) -> dict[str, dict]:
        """取这个库 collection 里的全部记录（含向量）——给
        official-import-export 插件导出用。Chroma 的 get() 不传 ids/where
        过滤条件就是官方支持的"整表读出"用法，不是非正式的偏门用法。
        故意不走"直接打包 Chroma 的底层 sqlite 文件"这条路——那样导出
        文件的格式会和 Chroma 具体版本的内部存储细节绑死，Chroma 升级
        换了内部格式，旧导出包可能读不出来；走公开 API 读出记录、用我们
        自己定义的格式重新打包，格式自己说了算，不随第三方库实现细节
        变化。"""
        result = self._collection(library_id, generation).get(
            include=["documents", "metadatas", "embeddings"]
        )
        return {
            chunk_id: {"document": doc, "metadata": meta, "embedding": list(vec)}
            for chunk_id, doc, meta, vec in zip(
                result["ids"], result["documents"], result["metadatas"], result["embeddings"]
            )
        }

    def delete(
        self,
        library_id: str,
        chunk_ids: list[str],
        generation: str | None = None,
    ) -> None:
        if not chunk_ids:
            return
        self._collection(library_id, generation).delete(ids=chunk_ids)

    def delete_generation(self, library_id: str, generation: str) -> None:
        key = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
        try:
            self._client.delete_collection(name=f"libg_{key}")
        except Exception:
            pass

    def query(
        self,
        library_id: str,
        query_vector: list[float],
        top_k: int = 10,
        generation: str | None = None,
    ) -> list[tuple[str, float]]:
        coll = self._collection(library_id, generation)
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

    def count(self, library_id: str, generation: str | None = None) -> int:
        return self._collection(library_id, generation).count()

    @staticmethod
    def _sample_rows(rows: list[dict], k: int) -> list[dict]:
        vectors = [
            list(value) if (value := row.get("embedding")) is not None else []
            for row in rows
        ]
        if not vectors or any(not vector for vector in vectors):
            return []
        k = min(k, len(rows))
        if k <= 0:
            return []

        def _dist2(a: list[float], b: list[float]) -> float:
            return sum((x - y) ** 2 for x, y in zip(a, b))

        chosen = [0]
        distances = [_dist2(vectors[0], vector) for vector in vectors]
        while len(chosen) < k:
            nxt = max(range(len(vectors)), key=lambda i: distances[i])
            if nxt in chosen:
                break
            chosen.append(nxt)
            for i, vector in enumerate(vectors):
                distance = _dist2(vectors[nxt], vector)
                if distance < distances[i]:
                    distances[i] = distance
        result = []
        for i in chosen:
            metadata = rows[i].get("metadata") or {}
            result.append(
                {
                    "path": metadata.get("path", ""),
                    "heading": metadata.get("heading_breadcrumb", ""),
                    "text": (rows[i].get("document") or "")[:400],
                }
            )
        return result

    def sample_records(self, records: dict[str, dict], k: int = 20) -> list[dict]:
        return self._sample_rows(
            [records[chunk_id] for chunk_id in sorted(records)],
            k,
        )

    def sample(
        self,
        library_id: str,
        k: int = 20,
        generation: str | None = None,
    ) -> list[dict]:
        coll = self._collection(library_id, generation)
        n = coll.count()
        if n == 0:
            return []
        result = coll.get(include=["documents", "metadatas", "embeddings"])
        docs = result.get("documents")
        metas = result.get("metadatas")
        embs = result.get("embeddings")
        if docs is None or len(docs) == 0 or embs is None or len(embs) == 0:
            return []
        if metas is None:
            metas = [{}] * len(docs)
        return self._sample_rows(
            [
                {"document": doc, "metadata": meta, "embedding": vector}
                for doc, meta, vector in zip(docs, metas, embs)
            ],
            k,
        )
