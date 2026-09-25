# -*- coding: utf-8 -*-
"""对比新旧检索参数矩阵（tmp_matrix_new/old.json）：按组合汇总
top1 一致率 / 文件级重合率，并单列不一致项。"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str) -> dict:
    data = json.loads((HERE / name).read_text(encoding="utf-8"))
    return {k: [f"{h['library']}/{h['path']}" for h in v["hits"]] for k, v in data.items()}


def main() -> int:
    new = load("tmp_matrix_new.json")
    old = load("tmp_matrix_old.json")
    keys = sorted(set(old) & set(new))
    by_combo = defaultdict(lambda: {"top1": 0, "n": 0, "overlap": 0, "slots": 0})
    mismatches = []
    for k in keys:
        combo = k.split("|")[0]
        o, n = old[k], new[k]
        stat = by_combo[combo]
        stat["n"] += 1
        stat["slots"] += len(o)
        if o and n and o[0] == n[0]:
            stat["top1"] += 1
        ov = len(set(o) & set(n))
        stat["overlap"] += ov
        if not o or not n or o[0] != n[0]:
            mismatches.append((k, o[:3], n[:3]))
    print(f"{'组合':<22} {'top1一致':>8} {'文件级重合':>10}")
    total_top1 = total_n = 0
    for combo in sorted(by_combo):
        s = by_combo[combo]
        total_top1 += s["top1"]
        total_n += s["n"]
        print(f"{combo:<22} {s['top1']}/{s['n']:>5}  {s['overlap']}/{s['slots']}")
    print(f"\n总计 top1 一致: {total_top1}/{total_n}")
    print(f"\ntop1 不一致 {len(mismatches)} 项：")
    for k, o, n in mismatches:
        print(f"  {k}")
        print(f"    旧 top3: {o}")
        print(f"    新 top3: {n}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
