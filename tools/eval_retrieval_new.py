# -*- coding: utf-8 -*-
"""rag-redo 侧的检索质量评估——查询集、黄金文件、命中判定逐字对齐旧项目
tests/eval_retrieval.py（问题18 沉淀，"RRF k=2 旧评估集复核"项）。

命中判定：top 结果中任一来源行的文件路径包含任一黄金文件名片段。
"""
from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import _boot_pipeline  # noqa: E402

QUERIES = [
    ("fluent配置", ["20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md"]),
    ("个人能力", ["01-Meta/user-profile/B3_CFD能力评估.md",
                  "01-Meta/user-profile/B4_CAD与编程能力.md",
                  "01-Meta/user-profile/C3_优势风险盲点.md",
                  "01-Meta/user-profile/D2_成长路径建议.md"]),
    ("y+ 控制", ["20-Projects/Summer-2026/ROCKETRY/概念/y+控制与壁面处理策略.md"]),
    ("网格无关性", ["10-Areas/Aerospace/概念/网格无关性验证方法.md",
                   "20-Projects/Summer-2026/ROCKETRY/报告/report_checklist.md"]),
    ("OfficeCLI", ["20-Projects/Summer-2026/AI Dev Workflow/附录/OfficeCLI-SKILL.md"]),
    ("免费在线认证课程", ["20-Projects/Summer-2026/OTHER CERT/论点/免费入门证书清单.md",
                      "20-Projects/Summer-2026/OTHER CERT/论点/AI证书与学习平台清单.md"]),
    ("CFD 近壁面网格 湍流 怎么处理", ["20-Projects/Summer-2026/ROCKETRY/概念/y+控制与壁面处理策略.md",
                                    "20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md"]),
    ("火箭设计怎么学", ["01-Meta/user-profile/B2_Rocketry.md",
                      "20-Projects/Summer-2026/ROCKETRY/目录.md"]),
    ("我的职业方向怎么规划", ["01-Meta/user-profile/D2_成长路径建议.md",
                             "01-Meta/user-profile/C3_优势风险盲点.md",
                             "01-Meta/user-profile/D1_当前阶段与目标.md"]),
    ("CFD 仿真要做哪些准备工作", ["20-Projects/Summer-2026/ROCKETRY/任务节点/任务流程.md",
                                "20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md"]),
    ("写报告要注意什么", ["20-Projects/Summer-2026/ROCKETRY/报告/report_checklist.md",
                        "20-Projects/Summer-2026/ROCKETRY/报告/report_questions.md"]),
    ("软件许可证和授权问题", ["20-Projects/Summer-2026/ROCKETRY/概念/FLUENT配置与求解设置.md",
                           "20-Projects/Summer-2026/AI Dev Workflow/附录/OfficeCLI-SKILL.md"]),
]


def main() -> int:
    runtime, pipeline = _boot_pipeline(Namespace(
        plugins_dir=REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data-real" / "plugins_state.json",
        data_root=REPO_ROOT / "data-real",
    ))
    rows = []
    for q, gold in QUERIES:
        results = pipeline.search("Obsidian Vault", q, top_k=5, include_body=False)
        files = [r.path for r in results]
        pos = next((i + 1 for i, f in enumerate(files)
                    if any(g in f for g in gold)), None)
        rows.append((q, pos))
        print("%-24s pos=%s top1=%s" % (q, pos, files[0].split("/")[-1] if files else "-"))
    t1 = sum(1 for _, p in rows if p == 1)
    t3 = sum(1 for _, p in rows if p and p <= 3)
    t5 = sum(1 for _, p in rows if p and p <= 5)
    print("\n命中: top1=%d/%d top3=%d/%d top5=%d/%d" % (t1, len(rows), t3, len(rows), t5, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
