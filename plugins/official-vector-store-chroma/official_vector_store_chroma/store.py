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
        metadatas: list[dict] | None = None,
    ) -> None:
        if not chunk_ids:
            return
        self._collection(library_id).upsert(
            ids=chunk_ids, embeddings=vectors, documents=documents, metadatas=metadatas
        )

    def get_by_ids(self, library_id: str, chunk_ids: list[str]) -> dict[str, dict]:
        """按 chunk_id 直接取记录（不是相似度查询）——查询管道融合词法/
        向量两路排名后，需要把任意来源（哪怕只被 BM25 命中、没进向量
        Top-K）的 chunk_id 都能取到完整文本+元数据用于装配最终结果，
        Chroma 原生的按 id get() 正好承担这个"chunk 存储"的角色，不用
        另起一个并行的数据结构维护同一份东西两份拷贝。"""
        if not chunk_ids:
            return {}
        result = self._collection(library_id).get(ids=chunk_ids, include=["documents", "metadatas"])
        return {
            chunk_id: {"document": doc, "metadata": meta}
            for chunk_id, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
        }

    def get_all(self, library_id: str) -> dict[str, dict]:
        """取这个库 collection 里的全部记录（含向量）——给
        official-import-export 插件导出用。Chroma 的 get() 不传 ids/where
        过滤条件就是官方支持的"整表读出"用法，不是非正式的偏门用法。
        故意不走"直接打包 Chroma 的底层 sqlite 文件"这条路——那样导出
        文件的格式会和 Chroma 具体版本的内部存储细节绑死，Chroma 升级
        换了内部格式，旧导出包可能读不出来；走公开 API 读出记录、用我们
        自己定义的格式重新打包，格式自己说了算，不随第三方库实现细节
        变化。"""
        result = self._collection(library_id).get(include=["documents", "metadatas", "embeddings"])
        return {
            chunk_id: {"document": doc, "metadata": meta, "embedding": list(vec)}
            for chunk_id, doc, meta, vec in zip(
                result["ids"], result["documents"], result["metadatas"], result["embeddings"]
            )
        }

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

    def sample(self, library_id: str, k: int = 20) -> list[dict]:
        """最远点采样（farthest-point sampling）：从库的全部块向量里选 k
        个语义上分散的代表块，供 official-library-summary 概括库内容用——
        直接复用索引时 embedder 已经算好的向量，免费的副产品，不需要也
        不该为了写一段简介重新读一遍全文（对齐旧项目
        library_summary.py::sample_representative_chunks 的算法和理由，
        见该函数 docstring）。确定性、不需要迭代收敛；纯 Python 实现，
        不为此新增 numpy 依赖——k 通常是十几到二十，向量维度几百到一千，
        规模上完全跑得动，没必要为了这一处引入新的重量依赖。
        """
        coll = self._collection(library_id)
        n = coll.count()
        if n == 0:
            return []
        result = coll.get(include=["documents", "metadatas", "embeddings"])
        docs = result.get("documents")
        metas = result.get("metadatas")
        embs = result.get("embeddings")
        # 注意：embeddings 可能是 numpy 数组（较新版本 Chroma）——绝不能对
        # 可能是数组的值用 `or`/`not` 做真值判断（"the truth value of an
        # array with more than one element is ambiguous"），一律用
        # `is None`/`len()` 判空，这是旧项目 library_summary.py 真实踩过
        # 写进注释的坑，照抄这条纪律。
        if docs is None or len(docs) == 0 or embs is None or len(embs) == 0:
            return []
        if metas is None:
            metas = [{}] * len(docs)
        k = min(k, len(docs))
        if k <= 0:
            return []
        vecs = [list(v) for v in embs]

        def _dist2(a: list[float], b: list[float]) -> float:
            return sum((x - y) ** 2 for x, y in zip(a, b))

        chosen = [0]
        dists = [_dist2(vecs[0], v) for v in vecs]
        while len(chosen) < k:
            nxt = max(range(len(vecs)), key=lambda i: dists[i])
            if nxt in chosen:  # 全部重合的退化情形（比如全部向量相同），提前收手
                break
            chosen.append(nxt)
            for i, v in enumerate(vecs):
                d = _dist2(vecs[nxt], v)
                if d < dists[i]:
                    dists[i] = d

        rows = []
        for i in chosen:
            meta = metas[i] or {}
            rows.append(
                {
                    "path": meta.get("path", ""),
                    "heading": meta.get("heading_breadcrumb", ""),
                    "text": (docs[i] or "")[:400],
                }
            )
        return rows
