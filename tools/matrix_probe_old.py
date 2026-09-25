# -*- coding: utf-8 -*-
"""检索参数矩阵实测（旧项目侧）：与 matrix_probe_new 完全同参数组合，
逐字解析旧 hybrid_search 的返回（整段字符串：建议行打头 + [来源] 行）。"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

OLD_ROOT = Path(r"C:\Users\xbl26\projects\obsidian-rag")
OUT = Path(__file__).resolve().parent / "tmp_matrix_old.json"

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

SRC_RE = re.compile(
    r"^\[来源\] (.+?)/(.+?) \(## (.*?)\)(?: \[块 (\d+)/(\d+)\])?(?: \[置信度 ([0-9.]+)·([^\]]+)\])?"
)


def main() -> int:
    os.chdir(OLD_ROOT)
    sys.path.insert(0, str(OLD_ROOT))
    from retriever import hybrid_search  # noqa: E402

    out = {}
    for combo in COMBOS:
        for q in QUERIES:
            t0 = time.time()
            text = hybrid_search(
                q, top_k=5, libraries=combo["libraries"], exclude=combo["exclude"],
                folder=combo["folder"],
                include_body=(combo["mode"] == "body"),
                with_scores=True, small_to_big=True,
            )
            elapsed = time.time() - t0
            lines = text.split("\n") if isinstance(text, str) else list(text)
            hits = []
            for line in lines:
                m = SRC_RE.match(line)
                if m:
                    lib, path, _heading, k, _n, conf, _tier = m.groups()
                    hits.append({
                        "library": lib,
                        "path": path,
                        "chunk": int(k) - 1 if k else None,
                        "confidence": float(conf) if conf else None,
                    })
            out[f"{combo['key']}|{q}"] = {"elapsed_s": round(elapsed, 2), "hits": hits}
        print(f"[{combo['key']}] 完成 10 查询")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
