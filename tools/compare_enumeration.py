# -*- coding: utf-8 -*-
"""枚举对齐验证：rag-redo 的 resolve_included_files vs 旧项目
collect_md_files（问题44/47 裁决的唯一权威实现）逐库对比。

只读操作：旧项目仅导入其 library/index 模块做文件枚举，不触碰任何状态。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OLD_ROOT = Path(r"C:\Users\xbl26\projects\obsidian-rag")

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "official-library-manager"))
sys.path.insert(0, str(OLD_ROOT))

from official_library_manager.config import LibraryConfigStore  # noqa: E402
from official_library_manager.plugin import LibraryManagerPlugin  # noqa: E402


def old_included_set(entry: dict) -> set[str]:
    """旧项目同款枚举（import 旧 index.py 的 collect_md_files 原函数）。"""
    from index import collect_md_files

    exclude_dirs = entry.get("exclude_dirs")
    exclude_files = entry.get("exclude_files")
    exclude_patterns = entry.get("exclude_patterns")
    extensions = entry.get("extensions")
    if exclude_dirs is None:
        # 条目 null → 旧全局 config.json 生效值
        from config import CFG

        exclude_dirs = CFG["exclude_dirs"]
        exclude_files = CFG["exclude_files"]
        exclude_patterns = CFG["exclude_patterns"]
    sel_in = entry.get("selection_in") or []
    sel_out = entry.get("selection_out") or []
    vault = entry["path"]
    files = collect_md_files(
        vault,
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
        exclude_patterns=exclude_patterns,
        extensions=extensions,
        selection=(sel_in, sel_out) if (sel_in or sel_out) or entry.get("selection_in") is not None else None,
        selection_default="follow",
    )
    return {str(Path(p).relative_to(Path(vault))).replace("\\", "/") for p in files}


def new_included_set(plugin: LibraryManagerPlugin, library_id: str) -> set[str]:
    decisions = plugin.resolve_included_files(library_id)
    return {path for path, included, _ in decisions if included}


def main() -> int:
    old_registry = json.loads(
        (OLD_ROOT / "data" / "libraries.json").read_text(encoding="utf-8")
    )["libraries"]
    old_by_name = {e["name"]: e for e in old_registry}

    store = LibraryConfigStore(REPO_ROOT / "data-real" / "libraries.json")
    plugin = LibraryManagerPlugin()
    plugin.store = store

    exit_code = 0
    for cfg in store.list_libraries():
        old_entry = old_by_name.get(cfg.name)
        if old_entry is None:
            print(f"[{cfg.name}] 旧注册表里没有同名库，跳过")
            continue
        old_set = old_included_set(old_entry)
        new_set = new_included_set(plugin, cfg.library_id)
        only_old = sorted(old_set - new_set)
        only_new = sorted(new_set - old_set)
        status = "一致" if not only_old and not only_new else "有差异"
        print(
            f"[{cfg.name}] 旧={len(old_set)} 新={len(new_set)} -> {status}"
        )
        for path in only_old[:20]:
            print(f"  仅旧项目收录: {path}")
        for path in only_new[:20]:
            print(f"  仅 rag-redo 收录: {path}")
        if only_old or only_new:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
