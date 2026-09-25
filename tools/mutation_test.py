# -*- coding: utf-8 -*-
"""增量索引实测：用 skills 库的副本做增/改/删，不动真实库。

流程：复制 skills → 注册 mutation 库 → 全量基线 → 依次 新增/修改/删除 文件
并跑增量索引，核对每轮报告与预期一致（对齐旧 index.py 的增量语义）。
"""
from __future__ import annotations

import shutil
import sys
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import _boot_pipeline  # noqa: E402

MUT_ROOT = REPO_ROOT / "data" / "mutation-vault"
LIB_ID = "mutation"


def boot():
    args = Namespace(
        plugins_dir=REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data" / "plugins_state.json",
        data_root=REPO_ROOT / "data",
    )
    return _boot_pipeline(args)


def report(tag: str, rep):
    print(
        f"[{tag}] 新增{rep.added} 变更{rep.changed} 未变{rep.unchanged} "
        f"删除{rep.removed} 重试{rep.retried} | 成功{rep.succeeded} 失败{rep.failed} 延后{rep.deferred}"
    )
    for f in rep.files:
        if f.failure_state:
            print("   失败:", f.path, f.failure_state)


def main() -> int:
    # 0) 复制 skills 副本
    if MUT_ROOT.exists():
        shutil.rmtree(MUT_ROOT)
    shutil.copytree(r"C:\Users\xbl26\.config\opencode\skills", MUT_ROOT)

    pipeline = boot()
    lib_mgr = pipeline._singleton("library_manager")
    if lib_mgr.store.get(LIB_ID) is None:
        lib_mgr.store.add_library(LIB_ID, "mutation", str(MUT_ROOT))

    # 1) 基线全量
    rep = pipeline.index_library(LIB_ID, full=True)
    report("基线全量", rep)
    baseline_files = sorted(p for p, inc, _ in lib_mgr.resolve_included_files(LIB_ID) if inc)
    print(f"基线收录 {len(baseline_files)} 个文件")

    # 2) no-op 复跑：无任何变化
    rep = pipeline.index_library(LIB_ID)
    report("no-op复跑", rep)

    # 3) 新增文件
    new_file = MUT_ROOT / "增量实测-新增.md"
    new_file.write_text("# 增量实测新增\n\n这是一篇用于增量索引实测的新笔记，内容关于火箭机动控制。\n", encoding="utf-8")
    rep = pipeline.index_library(LIB_ID)
    report("新增后", rep)

    # 4) 修改既有文件（改内容 + mtime）
    target = MUT_ROOT / "audit-depth" / "SKILL.md"
    text = target.read_text(encoding="utf-8")
    target.write_text(text + "\n\n增量实测追加段落：量纲分析相似准则补充说明。\n", encoding="utf-8")
    rep = pipeline.index_library(LIB_ID)
    report("修改后", rep)

    # 5) 删除文件（删掉刚才新增的）
    new_file.unlink()
    rep = pipeline.index_library(LIB_ID)
    report("删除后", rep)

    # 6) 清理：注销库（数据保留，同旧语义）
    lib_mgr.store.remove_library(LIB_ID)
    print("mutation 库已注销")
    return 0


if __name__ == "__main__":
    sys.exit(main())
