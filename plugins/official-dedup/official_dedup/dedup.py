"""近似重复检测：MinHash + LSH。

用 datasketch 库（成熟实现，只依赖 numpy）——LSH 的分桶（banding）技巧
本身不复杂但细节多、容易出不易察觉的召回率 bug，不是"提炼几十行公式"
那种值得自己重写的东西（不像 BM25/RRF 那样是教科书级短公式），复用
成熟实现比自己重新踩一遍坑划算。

只给建议，不自动删——这是刻意的设计：近似重复不等于真的该删，可能是
故意保留的相似版本（修订历史/不同角度笔记），交给用户自己判断，见
docs/FEATURE_TRIAGE.md。

用字符 n-gram（shingle）而不是分词结果喂 MinHash——中英文统一处理，不
依赖 jieba，也不需要判断"两篇文章用词不同但意思一样算不算重复"这种
语义层面的相似（那是 embedder 的向量相似度该管的事，dedup 只管"内容
几乎逐字一样"这种更强的重复，两者互补不重叠）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from datasketch import MinHash, MinHashLSH

DEDUP_VERSION = "0.1.0"
DEFAULT_NUM_PERM = 128
DEFAULT_SHINGLE_SIZE = 5


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _shingles(text: str, size: int = DEFAULT_SHINGLE_SIZE) -> set[str]:
    normalized = _normalize(text)
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[i : i + size] for i in range(len(normalized) - size + 1)}


def make_minhash(
    text: str, *, num_perm: int = DEFAULT_NUM_PERM, shingle_size: int = DEFAULT_SHINGLE_SIZE
) -> MinHash:
    mh = MinHash(num_perm=num_perm)
    for shingle in _shingles(text, shingle_size):
        mh.update(shingle.encode("utf-8"))
    return mh


@dataclass
class DedupIndex:
    """一个库一个 DedupIndex——理由同 official-vector-store-chroma /
    official-lexical-bm25 的"一库一个xxx"隔离哲学：不同库的笔记不该被
    互相比对"是不是重复"，那没有意义，也没有"跨库泄漏"这个说法本身该
    出现的场景（继承 Phase 1 那两个插件补过的隔离教训，这次一次性做对，
    不留同样的坑）。"""

    threshold: float = 0.8
    num_perm: int = DEFAULT_NUM_PERM
    _lsh: MinHashLSH = field(init=False)
    _minhashes: dict[str, MinHash] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._lsh = MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._minhashes:
            self.remove(doc_id)
        mh = make_minhash(text, num_perm=self.num_perm)
        self._lsh.insert(doc_id, mh)
        self._minhashes[doc_id] = mh

    def remove(self, doc_id: str) -> None:
        if doc_id in self._minhashes:
            self._lsh.remove(doc_id)
            del self._minhashes[doc_id]

    def similar_to(self, doc_id: str) -> list[str]:
        """返回和 doc_id 近似重复的其他文档 id（不含自己）。"""
        mh = self._minhashes.get(doc_id)
        if mh is None:
            return []
        return [d for d in self._lsh.query(mh) if d != doc_id]

    def find_duplicate_groups(self) -> list[list[str]]:
        """把全部文档按"近似重复"关系分组（并查集求连通分量），只返回
        真正有重复的组（组大小>=2）。

        注意这是传递闭包：A~B、B~C 但 A~C 不到阈值时，三者仍会被分进
        同一组——这是"建议聚类"该有的行为（LSH 本身只保证"和查询点的
        相似度",不保证组内两两都相似），不是bug；这也是"只给建议不自动
        删"的另一层理由，组内到底哪些真该处理，交给用户自己看。
        """
        parent: dict[str, str] = {d: d for d in self._minhashes}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for doc_id in self._minhashes:
            for other in self.similar_to(doc_id):
                union(doc_id, other)

        groups: dict[str, list[str]] = {}
        for doc_id in self._minhashes:
            groups.setdefault(find(doc_id), []).append(doc_id)

        return [sorted(g) for g in groups.values() if len(g) >= 2]

    def count(self) -> int:
        return len(self._minhashes)
