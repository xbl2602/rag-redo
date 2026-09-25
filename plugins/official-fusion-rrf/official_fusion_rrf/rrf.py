"""RRF（Reciprocal Rank Fusion）：把词法检索和向量检索两路排名融合成一路。

公式：score(d) = sum，对每一路包含 d 的排名列表，加 weight/(k + rank(d))，
rank 从1开始计数。默认等权（两路排名地位相同）。

k=2 不是业界教科书值而是旧 obsidian-rag 的刻意选择（retriever.py::
_rrf_combine 的 k=2 默认，TASK_LOG.md:920"RRF(k=2) 排名融合"）：k 越小，
头部排名的得分差距越大——排名第1的贡献 1/3，第2名只有 1/4；k=60 时两者
几乎无差别（1/61 vs 1/62）。检索场景里"第一名和第二名谁前谁后"正是融合
最需要表达的信号，k=60 会把头部顺序抹平。RRF 在旧项目里的引入动机
（BM25 无界分恒主导、固定 alpha 加权不可靠）在 rag-redo 完全同样成立。

RRF 只看排名位置、不看原始分数，这正是它的优点：词法检索的 BM25 分数和
向量检索的余弦相似度量纲完全不同，没法直接相加，RRF 不需要做归一化就能
公平融合两路排名。
"""
from __future__ import annotations

from collections import defaultdict

FUSION_VERSION = "0.1.0"
DEFAULT_K = 2  # 对齐 obsidian-rag retriever.py::_rrf_combine 的 k=2（见模块 docstring）


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    *,
    k: int = DEFAULT_K,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """ranked_lists 是多路排名结果，每路是按相关性降序排列的 doc_id 列表
    （不带分数——RRF 只看排名位置）。"""
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights 长度必须和 ranked_lists 一致")

    scores: dict[str, float] = defaultdict(float)
    for ranked_list, weight in zip(ranked_lists, weights):
        for rank, doc_id in enumerate(ranked_list, start=1):
            scores[doc_id] += weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
