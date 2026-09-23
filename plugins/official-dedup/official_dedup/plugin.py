"""official-dedup 插件：生命周期钩子的薄封装，一个库一个 DedupIndex
（同 official-lexical-bm25 的隔离模式），真实逻辑在 dedup.py。"""
from __future__ import annotations

from .dedup import DedupIndex


class DedupPlugin:
    def __init__(self) -> None:
        self.indexes: dict[str, DedupIndex] | None = None

    def on_load(self, ctx):
        self.indexes = {}
        ctx.logger.info("近似重复检测已加载")

    def on_enable(self, ctx):
        ctx.logger.info("近似重复检测已启用")

    def on_disable(self, ctx):
        ctx.logger.info("近似重复检测已禁用")

    def on_unload(self, ctx):
        self.indexes = None

    def _index_for(self, library_id: str, *, threshold: float = 0.7) -> DedupIndex:
        # 0.7 是用测试数据实测校准过的值（见 tests/test_dedup.py：两处小
        # 幅改词的"近似重复"文本，5字符shingle算出来的真实Jaccard相似度
        # 是0.77——不是凭空定的数）。这不等于对真实笔记语料也是最优阈值，
        # 真实语料上的最佳阈值要等有真实使用反馈后再调，见docs/ROADMAP.md
        # 已经如实记录的"无法在当前环境验证与旧项目实际检索质量对比"这条
        # 限制，dedup阈值属于同一类"需要真实数据才能调准"的参数。
        assert self.indexes is not None
        return self.indexes.setdefault(library_id, DedupIndex(threshold=threshold))

    def add_document(self, library_id: str, doc_id: str, text: str) -> None:
        self._index_for(library_id).add(doc_id, text)

    def remove_document(self, library_id: str, doc_id: str) -> None:
        self._index_for(library_id).remove(doc_id)

    def find_duplicate_groups(self, library_id: str) -> list[list[str]]:
        if self.indexes is None or library_id not in self.indexes:
            return []
        return self.indexes[library_id].find_duplicate_groups()
