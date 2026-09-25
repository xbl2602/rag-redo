# -*- coding: utf-8 -*-
"""实测：rag-redo 检索输出（真实库、真模型），落盘 JSON 供与旧项目对比。"""
from __future__ import annotations

import json
import sys
import time
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import REQUIRED_PLUGINS, _boot_pipeline  # noqa: E402

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


def main() -> int:
    args = Namespace(
        plugins_dir=REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data" / "plugins_state.json",
        data_root=REPO_ROOT / "data",
    )
    runtime, pipeline = _boot_pipeline(args)
    out = {}
    for q in QUERIES:
        t0 = time.time()
        results = pipeline.search(LIBS, q, top_k=5)
        elapsed = time.time() - t0
        out[q] = {
            "elapsed_s": round(elapsed, 2),
            "hits": [
                {
                    "library": r.library_id,
                    "path": r.path,
                    "chunk": r.chunk_index,
                    "total": r.total_chunks,
                    "confidence": round(r.confidence, 4),
                    "truncated": r.truncated,
                }
                for r in results
            ],
        }
        print(f"{q} -> {len(results)} 条 ({elapsed:.1f}s)")
        for r in results:
            print(f"  [{r.confidence:.3f}] {r.library_id}/{r.path} 块{r.chunk_index + 1}/{r.total_chunks}")
    (REPO_ROOT / "tmp_search_new.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("已写 tmp_search_new.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
