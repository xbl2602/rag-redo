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
from core.index_generation import IndexGenerationStore

from .bm25 import BM25Index, INDEXER_VERSION


class LexicalBM25Plugin:
    def __init__(self) -> None:
        self.indexes: dict[tuple[str, str], BM25Index] | None = None
        self._index_file_identities: dict[tuple[str, str], tuple[int, int] | None] | None = None
        self._data_dir: Path | None = None
        self._generations: IndexGenerationStore | None = None

    def on_load(self, ctx):
        self.indexes = {}
        self._index_file_identities = {}
        self._data_dir = ctx.storage.directory("bm25", legacy="bm25")
        self._generations = IndexGenerationStore(
            ctx.storage.directory("index_generations", legacy="index_generations")
        )
        ctx.logger.info("BM25词法索引已加载")

    def on_enable(self, ctx):
        ctx.logger.info("BM25词法索引已启用")

    def on_disable(self, ctx):
        ctx.logger.info("BM25词法索引已禁用")

    def on_unload(self, ctx):
        self.indexes = None
        self._index_file_identities = None
        self._data_dir = None
        self._generations = None

    def _path_for(self, library_id: str, generation: str | None = None) -> Path:
        assert self._data_dir is not None
        if generation:
            safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in library_id)
            return self._data_dir / "generations" / safe / f"{generation}.json"
        return self._data_dir / f"{library_id}.json"

    def _file_identity(self, path: Path) -> tuple[int, int] | None:
        try:
            file_stat = path.stat()
        except OSError:
            return None
        return file_stat.st_mtime_ns, file_stat.st_size

    def _index_for(self, library_id: str, generation: str | None = None) -> BM25Index:
        assert self.indexes is not None
        assert self._index_file_identities is not None
        if generation is None and self._generations is not None:
            generation = self._generations.active(library_id)
        key = (library_id, generation or "")
        path = self._path_for(library_id, generation)
        identity = self._file_identity(path)
        if key not in self.indexes or self._index_file_identities.get(key) != identity:
            self.indexes[key] = BM25Index.load(path)
            self._index_file_identities[key] = identity
        return self.indexes[key]

    def index_signature(self) -> str:
        return INDEXER_VERSION

    def index_chunk(self, chunk: Chunk, generation: str | None = None) -> None:
        self._index_for(chunk.library_id, generation).add(chunk.chunk_id, chunk.text)

    def remove_chunk(self, library_id: str, chunk_id: str, generation: str | None = None) -> None:
        self._index_for(library_id, generation).remove(chunk_id)

    def search(
        self,
        library_id: str,
        query: str,
        top_k: int = 10,
        generation: str | None = None,
    ) -> list[tuple[str, float]]:
        return self._index_for(library_id, generation).search(query, top_k=top_k)

    def save(self, library_id: str, generation: str | None = None) -> None:
        """索引一个库结束后调用一次（见 core/pipeline.py），不是每加一个
        chunk 就存一次——批量索引一个库可能有几百个chunk，逐个落盘是不
        必要的 I/O 开销，攒到这一批全部处理完再写一次。"""
        key = (library_id, generation or "")
        if (
            self.indexes is not None
            and self._index_file_identities is not None
            and key in self.indexes
        ):
            path = self._path_for(library_id, generation)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.indexes[key].save(path)
            self._index_file_identities[key] = self._file_identity(path)

    def delete_generation(self, library_id: str, generation: str) -> None:
        key = (library_id, generation)
        if self.indexes is not None:
            self.indexes.pop(key, None)
        if self._index_file_identities is not None:
            self._index_file_identities.pop(key, None)
        try:
            self._path_for(library_id, generation).unlink(missing_ok=True)
        except OSError:
            pass

    def export_state(self, library_id: str, generation: str | None = None) -> dict:
        """给 official-import-export 插件用：拿这个库的 BM25 索引状态
        （纯 JSON 兼容字典），打包进导出归档，不用先落盘再读文件。"""
        return self._index_for(library_id, generation).to_dict()

    def import_state(
        self,
        library_id: str,
        data: dict,
        generation: str | None = None,
    ) -> None:
        """从导出归档恢复这个库的 BM25 索引状态，并立刻落盘（恢复完的
        状态不该只活在内存里，否则马上重启又得重新导入一遍）。"""
        assert self.indexes is not None
        self.indexes[(library_id, generation or "")] = BM25Index.from_dict(data)
        self.save(library_id, generation)
