# -*- coding: utf-8 -*-
"""实测：旧 obsidian-rag 检索输出（同一批查询），落盘 JSON 供对比。
只读：跑旧项目检索（其自身索引），不改任何旧项目状态。

旧 hybrid_search 返回的是整段 join 后的字符串（建议行打头，[来源] 行随后），
按行切分解析。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

OLD_ROOT = Path(r"C:\Users\xbl26\projects\obsidian-rag")
OUT = Path(__file__).resolve().parent / "tmp_search_old.json"

QUERIES = [
    "火箭飞行过程中的机动与控制",
    "可持续航空燃料的应用前景",
    "奖学金申请需要准备哪些材料",
    "静力学 压力测量装置",
    "量纲分析的基本步骤",
    "并行任务调度 角色权限",
    "图检索增强架构解析",
    "本地大模型部署调优",
]
LIBS = "Obsidian Vault,agents,skills,LECTURE NOTE"

SRC_RE = re.compile(
    r"^\[来源\] (.+?)/(.+?) \(## (.*?)\)(?: \[块 (\d+)/(\d+)\])?(?: \[置信度 ([0-9.]+)·([^\]]+)\])?"
)


def main() -> int:
    import os

    os.chdir(OLD_ROOT)
    sys.path.insert(0, str(OLD_ROOT))
    from retriever import hybrid_search  # noqa: E402

    out = {}
    for q in QUERIES:
        t0 = time.time()
        text = hybrid_search(
            q, top_k=5, libraries=LIBS, exclude="", include_body=True,
            with_scores=True, small_to_big=True,
        )
        elapsed = time.time() - t0
        hits, advice = [], []
        if isinstance(text, str):
            lines = text.split("\n")
        else:  # 防御：旧版本个别分支直接返回列表
            lines = list(text)
        for line in lines:
            m = SRC_RE.match(line)
            if m:
                lib, path, heading, k, n, conf, tier = m.groups()
                hits.append({
                    "library": lib,
                    "path": path,
                    "heading": heading,
                    "chunk": int(k) - 1 if k else None,
                    "total": int(n) if n else None,
                    "confidence": float(conf) if conf else None,
                    "tier": tier,
                    "raw": line,
                })
            elif line.startswith("（") and line.endswith("）"):
                advice.append(line[1:-1])
        out[q] = {"elapsed_s": round(elapsed, 2), "hits": hits, "advice": advice}
        print(f"{q} -> {len(hits)} 条 ({elapsed:.1f}s)")
        for h in hits:
            print(f"  [{h['confidence']}] {h['library']}/{h['path']} 块{h['chunk']}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
