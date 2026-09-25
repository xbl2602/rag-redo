# -*- coding: utf-8 -*-
"""检索参数矩阵实测（rag-redo 侧）：模式/库选择/folder/排除 参数组合 × 10 查询，
落盘 JSON 供与旧项目同矩阵对比——把"7/8 top1 一致"变成系统性结论。"""
from __future__ import annotations

import json
import sys
import time
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import _boot_pipeline  # noqa: E402

QUERIES = [
    "火箭飞行过程中的机动与控制",
    "可持续航空燃料的应用前景",
    "奖学金申请需要准备哪些材料",
    "静力学 压力测量装置",
    "量纲分析的基本步骤",
    "并行任务调度 角色权限",
    "图检索增强架构解析",
    "本地大模型部署调优",
    "CFD 仿真工具选择策略",
    "表达能力与说服力提升",
]
ALL_LIBS = "Obsidian Vault,agents,skills,LECTURE NOTE"

COMBOS = [
    {"key": "body_all", "mode": "body", "folder": "", "libraries": ALL_LIBS, "exclude": ""},
    {"key": "list_all", "mode": "list", "folder": "", "libraries": ALL_LIBS, "exclude": ""},
    {"key": "body_folder_10Areas", "mode": "body", "folder": "10-Areas", "libraries": ALL_LIBS, "exclude": ""},
    {"key": "body_two_libs", "mode": "body", "folder": "", "libraries": "Obsidian Vault,LECTURE NOTE", "exclude": ""},
    {"key": "body_exclude_skills", "mode": "body", "folder": "", "libraries": ALL_LIBS, "exclude": "skills"},
]


def main() -> int:
    runtime, pipeline = _boot_pipeline(Namespace(
        plugins_dir=REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data-real" / "plugins_state.json",
        data_root=REPO_ROOT / "data-real",
    ))
    out = {}
    for combo in COMBOS:
        for q in QUERIES:
            t0 = time.time()
            results = pipeline.search(
                combo["libraries"], q, top_k=5,
                exclude=combo["exclude"], folder=combo["folder"],
                include_body=(combo["mode"] == "body"),
            )
            elapsed = time.time() - t0
            out[f"{combo['key']}|{q}"] = {
                "elapsed_s": round(elapsed, 2),
                "hits": [
                    {
                        "library": r.library_id,
                        "path": r.path,
                        "chunk": r.chunk_index,
                        "confidence": round(r.confidence, 4),
                    }
                    for r in results
                ],
            }
        print(f"[{combo['key']}] 完成 10 查询")
    (REPO_ROOT / "tools" / "tmp_matrix_new.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("已写 tools/tmp_matrix_new.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
