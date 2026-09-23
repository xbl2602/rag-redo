"""official-lexical-bm25 插件：生命周期钩子的薄封装，真实逻辑在 bm25.py。

**一个库一个 BM25Index**——理由和 official-vector-store-chroma"一个库一个
collection"完全一样：库与库之间的隔离性必须由结构保证，不能靠"反正
chunk_id 前面带了 library_id，大概率不会搜串"这种字符串巧合。早期实现
只有一个全局 BM25Index，会导致"搜库B，却搜到库A的内容"这种真实的跨库
数据泄漏——这是端到端测试之外、单独写GUI层测试时靠推理发现的（见
tests/test_pipeline_e2e.py 的 test_lexical_search_is_isolated_per_library
补的回归测试）。

**落盘持久化**：早期实现只在内存里，进程一重启索引就没了——Chroma 的
向量数据是持久化的，BM25 却不是，两路数据"重启后一个还在一个没了"这种
不一致状态本身就是一个真实的正确性缺口（搜索会悄悄退化，不是报错，比
报错更难发现）。现在用 ctx.data_dir/bm25/<library_id>.json 落盘，`search`
和 `index_chunk`/`remove_chunk` 都走同一个 `_index_for` 懒加载入口——
只要磁盘上有文件就会被读到，不会因为进程刚启动、这个库还没被访问过就
误判成"这个库没有索引"。
"""
from __future__ import annotations

from pathlib import Path

from core.contracts import Chunk

from .bm25 import BM25Index


class LexicalBM25Plugin:
    def __init__(self) -> None:
        self.indexes: dict[str, BM25Index] | None = None
        self._data_dir: Path | None = None

    def on_load(self, ctx):
        self.indexes = {}
        self._data_dir = ctx.data_dir / "bm25"
        ctx.logger.info("BM25词法索引已加载")

    def on_enable(self, ctx):
        ctx.logger.info("BM25词法索引已启用")

    def on_disable(self, ctx):
        ctx.logger.info("BM25词法索引已禁用")

    def on_unload(self, ctx):
        self.indexes = None
        self._data_dir = None

    def _path_for(self, library_id: str) -> Path:
        assert self._data_dir is not None
        return self._data_dir / f"{library_id}.json"

    def _index_for(self, library_id: str) -> BM25Index:
        assert self.indexes is not None
        if library_id not in self.indexes:
            self.indexes[library_id] = BM25Index.load(self._path_for(library_id))
        return self.indexes[library_id]

    def index_chunk(self, chunk: Chunk) -> None:
        self._index_for(chunk.library_id).add(chunk.chunk_id, chunk.text)

    def remove_chunk(self, library_id: str, chunk_id: str) -> None:
        self._index_for(library_id).remove(chunk_id)

    def search(self, library_id: str, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        return self._index_for(library_id).search(query, top_k=top_k)

    def save(self, library_id: str) -> None:
        """索引一个库结束后调用一次（见 core/pipeline.py），不是每加一个
        chunk 就存一次——批量索引一个库可能有几百个chunk，逐个落盘是不
        必要的 I/O 开销，攒到这一批全部处理完再写一次。"""
        if self.indexes is not None and library_id in self.indexes:
            self.indexes[library_id].save(self._path_for(library_id))
