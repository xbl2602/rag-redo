# -*- coding: utf-8 -*-
"""实测：旧 obsidian-rag 检索输出（同一批查询），落盘 JSON 供对比。
只读：跑旧项目检索（其自身索引），不改任何旧项目状态。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

OLD_ROOT = Path(r"C:\Users\xbl26\projects\obsidian-rag")
OUT = OLD_ROOT / "tmp_search_old.json"  # 写到旧项目目录外的本文件
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

SRC_RE = re.compile(r"^\[来源\] (.+?)/(.+?) \(## (.*?)\)(?: \[块 (\d+)/(\d+)\])?")


def main() -> int:
    import os

    os.chdir(OLD_ROOT)
    sys.path.insert(0, str(OLD_ROOT))
    from retriever import hybrid_search  # noqa: E402

    out = {}
    for q in QUERIES:
        t0 = time.time()
        lines = hybrid_search(
            q, top_k=5, libraries=LIBS, exclude="", include_body=False,
            with_scores=True, small_to_big=True,
        )
        elapsed = time.time() - t0
        hits = []
        for line in lines:
            m = SRC_RE.match(line)
            if not m:
                continue
            lib, path, heading, k, n = m.groups()
            conf_m = re.search(r"置信度[:：]\s*([0-9.]+)", line)
            hits.append({
                "library": lib,
                "path": path,
                "heading": heading,
                "chunk": int(k) - 1 if k else None,
                "total": int(n) if n else None,
                "confidence": float(conf_m.group(1)) if conf_m else None,
                "raw": line,
            })
        out[q] = {"elapsed_s": round(elapsed, 2), "hits": hits}
        print(f"{q} -> {len(hits)} 条 ({elapsed:.1f}s)")
        for h in hits:
            print(f"  [{h['confidence']}] {h['library']}/{h['path']} 块{h['chunk']}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
