"""向量存储的薄封装，底层是 chromadb.PersistentClient。

一个库一个 collection（`lib_<library_id>`）——库与库之间物理隔离，一个库
的向量查询绝不可能命中另一个库的数据，不需要额外的 library_id 过滤逻辑，
隔离性由 Chroma 的 collection 边界天然保证。写入是 upsert 语义：重复写
同一个 chunk_id 是更新，不是报错或产生重复条目（同 docs/DATA_FLOW.md
"写入一律 upsert"的约定）。
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import chromadb

from core.index_generation import IndexGenerationStore

VECTOR_STORE_VERSION = "0.1.0"

_LOGGER = logging.getLogger("rag_redo.vector_store_chroma")

#: Chroma 对集合名的**硬约束**（实测 chromadb 1.5 的
#: `InvalidArgumentError: Validation error: name: Expected a name containing
#: 3-512 characters from [a-zA-Z0-9._-], starting and ending with a character
#: in [a-zA-Z0-9]`）。注意它**不接受中文**——而 `library_id` 是用户可见的
#: 自由文本，`add_library` 只挡 `/\:*?"<>|` 与控制字符，中文/空格都是合法
#: 库 id。原先直接 `f"lib_{library_id}"` 拼名字，于是"用中文名建库"这条路
#: 在第一次写向量时必然抛 InvalidArgumentError：库注册得进去、配置存得下、
#: 一索引就炸，且错误信息完全指不到"库名里有中文"这个真因。
_LEGAL_CHROMA_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,510}[a-zA-Z0-9]$")


def chroma_collection_name(library_id: str, generation: str | None = None) -> str:
    """库 id / (库 id, generation) → 集合名。**唯一的命名算法出口**。

    - 有 generation：`libg_<sha256(库id\\0generation)[:40]>`。本来就只含
      十六进制与下划线，永远合法，不需要额外处理。
    - 无 generation：`lib_<库id>`，**但只在它本身合法时**才这么拼。
      不合法（中文、空格、以 `_`/`.`/`-` 结尾、超长等）改用
      `libk_<sha256(库id)[:32]>`——纯十六进制、37 字符、确定性、单射
      （哈希保证不同 id 不撞名；`libk_` 前缀也不会和合法的 `lib_<id>` 撞，
      因为后者第 4 个字符必是 `_` 而前者是 `k`）。

    **向后兼容**：ASCII 且合法的库 id 仍然拼出与改动前一字不差的
    `lib_<id>`，已有集合名不用迁移；只有原本建不出来的那些 id 走新方案。

    写路径与读路径、以及 `core/pipeline.py::_prune_stale_collections` 的
    存活白名单都必须走这里（AGENTS.md §4.5「同一个业务判断只能有一个
    权威实现」），否则会出现"回收时判定活着"与"查询时找集合"对不上。
    """
    if generation:
        digest = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
        return f"libg_{digest}"
    plain = f"lib_{library_id}"
    if _LEGAL_CHROMA_NAME.match(plain):
        return plain
    digest = hashlib.sha256(library_id.encode("utf-8")).hexdigest()[:32]
    return f"libk_{digest}"

#: "集合不存在"的异常类型。Chroma 1.5 起是 `NotFoundError`；老版本
#: （以及部分封装）用 `InvalidCollectionException`，一并兼容，别让它变成
#: "漏网 → 被当成基础设施故障抛出去"。
_COLLECTION_NOT_FOUND: tuple[type[BaseException], ...]
try:  # pragma: no cover - 取决于装的是哪个 chromadb 版本
    from chromadb.errors import NotFoundError as _NotFoundError

    _COLLECTION_NOT_FOUND = (_NotFoundError,)
except ImportError:  # pragma: no cover
    _COLLECTION_NOT_FOUND = ()
try:  # pragma: no cover
    from chromadb.errors import InvalidCollectionException as _InvalidCollection

    _COLLECTION_NOT_FOUND = (*_COLLECTION_NOT_FOUND, _InvalidCollection)
except ImportError:  # pragma: no cover
    pass
if not _COLLECTION_NOT_FOUND:  # pragma: no cover
    # 认不出任何一个"不存在"类型时宁可保守：只吞 ValueError/KeyError 这类
    # 明确表示"查无此项"的错误，其余一律上抛。
    _COLLECTION_NOT_FOUND = (ValueError, KeyError)


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

    def _collection_name(self, library_id: str, generation: str | None = None) -> str:
        """集合名——正算，不从名字反推。算法本体在模块级
        `chroma_collection_name()`，那里是唯一权威实现。"""
        if generation is None and self._generations is not None:
            generation = self._generations.active(library_id)
        return chroma_collection_name(library_id, generation)

    def collection_name_for(self, library_id: str, generation: str | None = None) -> str:
        """公开的命名出口：给 `core/pipeline.py::_prune_stale_collections`
        算存活白名单用。它过去自己复算了一遍 `f"lib_{library_id}"`——那既是
        §4.5 的重复实现，也会让本模块的合法化改动漏掉回收侧。现在回收侧
        问插件要名字，两边永远一致。"""
        return self._collection_name(library_id, generation)

    def _ensure_collection(self, library_id: str, generation: str | None = None):
        """**写路径专用**：集合不存在就建。"""
        return self._client.get_or_create_collection(
            name=self._collection_name(library_id, generation)
        )

    def _existing_collection(self, library_id: str, generation: str | None = None):
        """**读路径专用**：集合不存在返回 `None`，绝不创建。

        读路径一律不能用 `get_or_create_collection`——那会在"探测一段数据
        到底在不在"的时候凭空造出一个**空集合**。空集合在 Chroma 的 Rust
        实现里没有落盘的 HNSW 段文件，之后任何一次真正查它都会抛
        `InternalError: Error creating hnsw segment reader: Nothing found on
        disk`，把一次无害的探测变成用户侧的检索崩溃（2026-09-27 全量回归
        实测到的随机 flake：一致性自愈的 count 探测建出空集合，同进程后续
        用例查询该段直接炸）。所以读和写必须分开。

        **只把"集合不存在"当 None**：其它异常（客户端连不上、目录损坏、
        权限）必须继续往上抛。否则 `count()` 会把一次基础设施故障报成"这段
        0 块"，`core/index_integrity.py` 的一致性自愈据此判定"块丢了"并
        触发一次本不该发生的全量重建——把一次可恢复的探测故障放大成数据
        事件。"""
        try:
            return self._client.get_collection(
                name=self._collection_name(library_id, generation)
            )
        except _COLLECTION_NOT_FOUND as exc:
            _LOGGER.debug(
                "集合不存在（按无数据处理）：库 %r 的 generation %r（%s）",
                library_id, generation, exc,
            )
            return None

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
        self._ensure_collection(library_id, generation).upsert(
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
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return {}
        result = coll.get(
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
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return {}
        try:
            result = coll.get(
                include=["documents", "metadatas", "embeddings"]
            )
        except Exception as exc:  # noqa: BLE001 - 见 query() 同一处置
            _LOGGER.warning(
                "读取向量库 %r 的 generation %r 失败（%s: %s），按空处理。",
                library_id, generation, type(exc).__name__, exc,
            )
            return {}
        return {
            chunk_id: {"document": doc, "metadata": meta, "embedding": list(vec)}
            for chunk_id, doc, meta, vec in zip(
                result["ids"], result["documents"], result["metadatas"], result["embeddings"]
            )
        }

    def file_vectors(
        self,
        library_id: str,
        chunk_groups: "dict[str, list[str]]",
        generation: str | None = None,
        *,
        batch: int = 2000,
    ):
        """按文件汇总内容向量（BC-18 总览星图用）：每个文件 = 它全部块向量的平均再归一化。

        只按 id 分批取向量（不取正文、不取元数据），内存只随"文件数 × 维度"增长，
        不随块数增长——`get_all` 会把整库正文和向量全变成 Python 对象，大库能吃掉几百 MB。
        读路径：集合不存在返回空结果，绝不创建空集合（理由见 `_existing_collection`）。
        Chroma 1.x 返回的向量是 numpy 数组，一律 `np.asarray` 统一处理，不做真值判断
        （numpy 数组的真值判断会抛"歧义真值"错误，AGENTS.md §9）。"""
        import numpy as np

        from core.contracts import FileVectorSet

        paths = [path for path, ids in chunk_groups.items() if ids]
        coll = self._existing_collection(library_id, generation) if paths else None
        owner: dict[str, int] = {}
        for index, path in enumerate(paths):
            for chunk_id in chunk_groups[path]:
                owner.setdefault(chunk_id, index)
        sums: "np.ndarray | None" = None
        counts = np.zeros(len(paths), dtype=np.int64)
        if coll is not None:
            ids = list(owner)
            step = max(1, int(batch))
            for start in range(0, len(ids), step):
                result = coll.get(ids=ids[start:start + step], include=["embeddings"])
                got_ids = result.get("ids")
                got_vecs = result.get("embeddings")
                if got_ids is None or got_vecs is None or len(got_ids) == 0:
                    continue
                vecs = np.asarray(got_vecs, dtype=np.float32)
                if vecs.ndim != 2 or vecs.shape[0] != len(got_ids):
                    continue
                if sums is None:
                    sums = np.zeros((len(paths), vecs.shape[1]), dtype=np.float32)
                rows = np.fromiter((owner[i] for i in got_ids), dtype=np.int64, count=len(got_ids))
                np.add.at(sums, rows, vecs)
                np.add.at(counts, rows, 1)
        keep = np.nonzero(counts > 0)[0]
        dim = 0 if sums is None else int(sums.shape[1])
        if sums is None or len(keep) == 0:
            vectors = np.zeros((0, dim), dtype=np.float32)
        else:
            vectors = sums[keep] / counts[keep, None].astype(np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = (vectors / np.maximum(norms, 1e-12)).astype(np.float32)
        return FileVectorSet(
            library_id=library_id,
            generation=generation,
            paths=tuple(paths[i] for i in keep.tolist()),
            vectors=vectors,
            dim=dim,
            produced_by="official-vector-store-chroma",
            store_version=VECTOR_STORE_VERSION,
        )

    def delete(
        self,
        library_id: str,
        chunk_ids: list[str],
        generation: str | None = None,
    ) -> None:
        if not chunk_ids:
            return
        # 删除也是一种"读"——集合不存在就是无需删除，删一个凭空建出来的空
        # 集合只会留下垃圾（见 `_existing_collection` 的 docstring）。
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return
        coll.delete(ids=chunk_ids)

    def delete_generation(self, library_id: str, generation: str) -> None:
        key = hashlib.sha256(f"{library_id}\0{generation}".encode("utf-8")).hexdigest()[:40]
        try:
            self._client.delete_collection(name=f"libg_{key}")
        except Exception:
            pass

    def list_collection_names(self) -> list[str]:
        """全部集合名（问题49 全局回收用：找出不属于任何已注册库的残留）。"""
        try:
            return [getattr(c, "name", c) for c in self._client.list_collections()]
        except Exception:
            return []

    def ensure_collection_by_name(self, name: str) -> str:
        """按名建一个**空**集合，返回它的名字（`delete_collection_by_name`
        的写侧对偶）。失败不抛，返回原名——同 `delete_collection_by_name`
        的容错纪律。

        **为什么这不是"只为测试存在的接口"**：全局回收那一族能力（按名列举 /
        按名删除）只能作用于"已经存在的集合"，而这些集合有两类在生产写路径
        上**根本造不出来**——① 崩溃/被杀那一轮留下的、没有任何 manifest 引用的
        残留段（`libg_<sha(库, 未发布 generation)>`，正是 AGENTS.md §5
        "成功提交后必须回收不再被引用的集合"要治的对象）；② 共用同一个
        Chroma 目录的**别的插件**建的集合（`visual_*` 之外的任意命名空间）。
        `upsert` 只会写到由 `library_id + generation` 派生的 `lib_/libg_` 上，
        造不出这两类。没有"按名建"这一族接口，上面两条回收边界（该删的删掉、
        不该删的一个都不碰）就只能靠测试伸手进 `chromadb.PersistentClient`
        去验证——那验证的是测试自己的私有通路，不是插件契约。所以它进生产
        代码，且与既有两条一起构成同一族"集合命名空间维护"接口。"""
        try:
            collection = self._client.get_or_create_collection(name=name)
        except Exception:
            return name
        return str(getattr(collection, "name", name) or name)

    def delete_collection_by_name(self, name: str) -> None:
        """按名删除集合（只用于全局回收的残留清理；任何失败静默吞掉，
        同 delete_generation 的容错纪律——回收绝不能拖垮索引主流程）。"""
        try:
            self._client.delete_collection(name=name)
        except Exception:
            pass

    def query(
        self,
        library_id: str,
        query_vector: list[float],
        top_k: int = 10,
        generation: str | None = None,
    ) -> list[tuple[str, float]]:
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return []
        try:
            n = coll.count()
            if n == 0:
                return []
            result = coll.query(query_embeddings=[query_vector], n_results=min(top_k, n))
        except Exception as exc:  # noqa: BLE001
            # 段文件损坏/被外部工具动过（Chroma 会抛
            # `InternalError: Error creating hnsw segment reader: Nothing found
            # on disk`）不该让用户的检索整体崩掉：这一路降级为空命中，词法路
            # 和其它库照常返回，并打一条可定位的 warning。对齐 LEGACY
            # 「查不动就少这一路，不要让整轮查询失败」的降级纪律；一致性自愈
            # （core/index_integrity.py）会在下一轮发现块数不符并重建。
            _LOGGER.warning(
                "向量查询失败，库 %r 的 generation %r 本路降级为空（%s: %s）。",
                library_id, generation, type(exc).__name__, exc,
            )
            return []
        ids = result["ids"][0]
        distances = result["distances"][0]
        # Chroma 默认用 L2 距离（越小越相似），转成"越大越相似"的分数，
        # 和 BM25/RRF 等其他阶段"分数越大越好"的约定保持一致，调用方不用
        # 为向量检索这一路单独记一套相反的排序方向。
        return [(chunk_id, 1.0 / (1.0 + dist)) for chunk_id, dist in zip(ids, distances)]

    def count(self, library_id: str, generation: str | None = None) -> int:
        """集合里有多少块。

        集合不存在返回 **0**（这是真的"这段没有数据"，与"集合空了"同义），
        不创建集合；真实的读取异常**继续往上抛**——`core/index_integrity.py
        ::count_store_chunks` 靠捕获异常来 fail-open（返回 `None` 表示"数不
        出来，别据此判 stale"）。如果这里把异常也吞成 0，一致性自愈会把
        "探测失败"误读成"块丢了"，进而触发一次本不该发生的全量重建。"""
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return 0
        return coll.count()

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
        coll = self._existing_collection(library_id, generation)
        if coll is None:
            return []
        try:
            n = coll.count()
            if n == 0:
                return []
            result = coll.get(include=["documents", "metadatas", "embeddings"])
        except Exception as exc:  # noqa: BLE001 - 见 query() 同一处置
            _LOGGER.warning(
                "采样向量库 %r 的 generation %r 失败（%s: %s），按空处理。",
                library_id, generation, type(exc).__name__, exc,
            )
            return []
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
