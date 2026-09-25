# -*- coding: utf-8 -*-
"""配置面实测：路径勾选提案流（写门禁两段式）、格式开关、Agent 文档类型
授权（BC-02）、get_selection 输出。全部在 skills 副本上操作，不碰真实库。"""
from __future__ import annotations

import shutil
import sys
from argparse import Namespace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.cli import _boot_pipeline  # noqa: E402

MUT_ROOT = REPO_ROOT / "data-real" / "mutation-vault"
LIB_ID = "mutation"


def report(tag: str, rep):
    print(
        f"[{tag}] 新增{rep.added} 变更{rep.changed} 未变{rep.unchanged} 删除{rep.removed}"
        f" | 成功{rep.succeeded} 失败{rep.failed}"
    )


def included(lib_mgr, lib_id):
    return sorted(p for p, inc, _ in lib_mgr.resolve_included_files(lib_id) if inc)


def main() -> int:
    if MUT_ROOT.exists():
        shutil.rmtree(MUT_ROOT)
    shutil.copytree(r"C:\Users\xbl26\.config\opencode\skills", MUT_ROOT)
    # 造一个测试 PDF（文档类型管理用）
    (MUT_ROOT / "测试文档.pdf").write_bytes(b"%PDF-1.4 fake-for-test")

    runtime, pipeline = _boot_pipeline(Namespace(
        plugins_dir=REPO_ROOT / "plugins",
        state_file=REPO_ROOT / "data-real" / "plugins_state.json",
        data_root=REPO_ROOT / "data-real",
    ))
    lib_mgr = pipeline._singleton("library_manager")
    lib_mgr.store.add_library(LIB_ID, "mutation", str(MUT_ROOT))

    # ---- 1) 基线：pdf 未授权（agent_formats 空）----
    base = included(lib_mgr, LIB_ID)
    print(f"[基线] 收录 {len(base)} 个（pdf 应不在列: {'测试文档.pdf' not in base}）")

    # ---- 2) Agent 文档类型授权（BC-02）：pdf 授权前不可访问、授权后可见 ----
    decisions = {
        p: (inc, r) for p, inc, r in lib_mgr.resolve_included_files(
            LIB_ID, format_allowlist=lib_mgr.agent_allowed_extensions(LIB_ID))
    }
    print(f"[授权前] agent 视角 pdf 被拒: {not decisions['测试文档.pdf'][0]}（{decisions['测试文档.pdf'][1]}）")
    pending = lib_mgr.pending_agent_formats(LIB_ID)
    print(f"[pending_agent_formats] 待授权格式: {pending}")
    lib_mgr.store.set_agent_formats(LIB_ID, [".pdf"])
    decisions = {
        p: (inc, r) for p, inc, r in lib_mgr.resolve_included_files(
            LIB_ID, format_allowlist=lib_mgr.agent_allowed_extensions(LIB_ID))
    }
    print(f"[授权后] agent 视角 pdf 可见: {decisions['测试文档.pdf'][0]}")

    # ---- 3) 路径勾选提案流（写门禁两段式）----
    target_dir = "audit-depth"
    before = len([p for p in base if p.startswith(target_dir + "/")])
    prop = lib_mgr.propose_selection_changes(LIB_ID, [{"path": target_dir, "action": "out"}])
    print(f"[提案] ok={prop['ok']} 提案号={prop['proposal_id']} 变更={prop['changes']}")
    sel = lib_mgr.get_selection(LIB_ID)
    print(f"[apply前] selection_out={sel['selection_out']}（不应包含 {target_dir}: {target_dir not in sel['selection_out']}）")
    applied = lib_mgr.apply_selection_changes(LIB_ID, prop["proposal_id"], prop["confirmation_code"])
    print(f"[apply] ok={applied['ok']} selection_out={applied['selection_out']}")
    after = included(lib_mgr, LIB_ID)
    removed = before - len([p for p in after if p.startswith(target_dir + "/")])
    print(f"[apply后] {target_dir}/ 下文件从索引范围消失: {removed == before}（{before}→{len([p for p in after if p.startswith(target_dir + '/')])}）")

    # ---- 4) 索引尊重勾选：排除目录后增量索引应删除对应块 ----
    rep = pipeline.index_library(LIB_ID)
    report("排除后增量", rep)

    # ---- 5) 恢复 neutral 再验证 ----
    prop2 = lib_mgr.propose_selection_changes(LIB_ID, [{"path": target_dir, "action": "neutral"}])
    lib_mgr.apply_selection_changes(LIB_ID, prop2["proposal_id"], prop2["confirmation_code"])
    rep = pipeline.index_library(LIB_ID)
    report("恢复后增量", rep)

    # ---- 6) 同位置打架拒绝（写路径拦截）----
    lib_mgr.store.set_policy(LIB_ID, exclude_dirs=["brandkit"])
    try:
        lib_mgr.propose_selection_changes(LIB_ID, [{"path": "brandkit", "action": "in"}])
        print("[同位置拦截] 失败：提案未被拒绝！")
    except ValueError as exc:
        print(f"[同位置拦截] 正确拒绝：{str(exc)[:40]}…")

    # 清理
    lib_mgr.store.remove_library(LIB_ID)
    print("mutation 库已注销")
    return 0


if __name__ == "__main__":
    sys.exit(main())
