"""official-lexical-bm25 插件：生命周期钩子的薄封装，真实逻辑在 bm25.py。

**一个库一个 BM25Index**——理由和 official-vector-store-chroma"一个库一个
collection"完全一样：库与库之间的隔离性必须由结构保证，不能靠"反正
chunk_id 前面带了 library_id，大概率不会搜串"这种字符串巧合。早期实现
只有一个全局 BM25Index，会导致"搜库B，却搜到库A的内容"这种真实的跨库
数据泄漏——这是端到端测试之外、单独写GUI层测试时靠推理发现的（见
tests/test_pipeline_e2e.py 的 test_lexical_search_is_isolated_per_library
补的回归测试）。
"""
from __future__ import annotations

from core.contracts import Chunk

from .bm25 import BM25Index


class LexicalBM25Plugin:
    def __init__(self) -> None:
        self.indexes: dict[str, BM25Index] | None = None

    def on_load(self, ctx):
        self.indexes = {}
        ctx.logger.info("BM25词法索引已加载")

    def on_enable(self, ctx):
        ctx.logger.info("BM25词法索引已启用")

    def on_disable(self, ctx):
        ctx.logger.info("BM25词法索引已禁用")

    def on_unload(self, ctx):
        self.indexes = None

    def _index_for(self, library_id: str) -> BM25Index:
        assert self.indexes is not None
        return self.indexes.setdefault(library_id, BM25Index())

    def index_chunk(self, chunk: Chunk) -> None:
        self._index_for(chunk.library_id).add(chunk.chunk_id, chunk.text)

    def remove_chunk(self, library_id: str, chunk_id: str) -> None:
        self._index_for(library_id).remove(chunk_id)

    def search(self, library_id: str, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self.indexes is None or library_id not in self.indexes:
            return []
        return self.indexes[library_id].search(query, top_k=top_k)
