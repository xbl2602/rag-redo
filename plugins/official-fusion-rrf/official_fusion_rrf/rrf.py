"""RRF（Reciprocal Rank Fusion）：把词法检索和向量检索两路排名融合成一路。

公式：score(d) = sum，对每一路包含 d 的排名列表，加 weight/(k + rank(d))，
rank 从1开始计数。k=60 是 RRF 原论文（Cormack et al. 2009）的经验常数，
业界基本照抄这个值，没有特殊理由偏离。默认等权（两路排名地位相同）——
旧 obsidian-rag 项目 README 里也是"RRF 默认等权"，这个默认延续下来。

RRF 只看排名位置、不看原始分数，这正是它的优点：词法检索的 BM25 分数和
向量检索的余弦相似度量纲完全不同，没法直接相加，RRF 不需要做归一化就能
公平融合两路排名。
"""
from __future__ import annotations

from collections import defaultdict

FUSION_VERSION = "0.1.0"
DEFAULT_K = 60


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
