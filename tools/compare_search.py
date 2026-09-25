# -*- coding: utf-8 -*-
"""对比 rag-redo 与旧项目同一批查询的检索结果（tmp_search_new/old.json）。

对齐口径：top-5 的 (库, 路径) 序列重合度 + 块级命中 + 置信度分布。
切块/嵌入模型相同的前提下，排序差异主要来自 BM25/RRF/重排细节。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str) -> dict:
    # tmp_search_new.json 由 search_probe_new 写在仓库根；old 写在本目录
    for candidate in (HERE / name, HERE.parent / name):
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            break
    else:
        raise FileNotFoundError(name)
    return {q: [f"{h['library']}/{h['path']}#{h['chunk']}" for h in v["hits"]]
            for q, v in data.items()}


def main() -> int:
    new = load("tmp_search_new.json")
    old = load("tmp_search_old.json")
    total_overlap = 0
    total_pairs = 0
    for q in old:
        o, n = old.get(q, []), new.get(q, [])
        # 路径级（去块号）对比：文件命中是否一致
        o_paths = [h.split("#")[0] for h in o]
        n_paths = [h.split("#")[0] for h in n]
        overlap = len(set(o_paths) & set(n_paths))
        top1 = "✓" if (o_paths and n_paths and o_paths[0] == n_paths[0]) else "✗"
        # 有序重合（Kendall tau 风格的简易版：共同文件的相对顺序）
        common_order = [p for p in o_paths if p in set(n_paths)]
        # 逐位对比
        pos_match = sum(1 for a, b in zip(o_paths, n_paths) if a == b)
        total_overlap += overlap
        total_pairs += len(o_paths)
        print(f"Q: {q}")
        print(f"  文件级重合 {overlap}/{len(o_paths)} | 逐位一致 {pos_match}/{len(o_paths)} | top1 一致 {top1}")
        for i in range(max(len(o), len(n))):
            ol = o_paths[i] if i < len(o_paths) else "-"
            nl = n_paths[i] if i < len(n_paths) else "-"
            mark = "  " if ol == nl else "≠ "
            print(f"  {mark}{i+1}. 旧={ol}")
            print(f"       新={nl}")
    print(f"\n总计: 文件级重合 {total_overlap}/{total_pairs}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
