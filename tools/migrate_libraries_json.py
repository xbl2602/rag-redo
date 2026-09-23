#!/usr/bin/env python3
"""一次性迁移工具：把旧 obsidian-rag 项目的 data/libraries.json 转换成
RAG REDO 的 official-library-manager 格式（<data_dir>/libraries.json）。

只迁移"哪些库、库在哪、路径级勾选/排除、格式白名单、新文件默认策略"这类
纯配置数据——旧库的向量索引/embedding 不在迁移范围内，新架构下 chunk/
embedding schema 不同，索引本身预期要全量重建（这是设计决策，不是遗漏，
迁移完成后对每个库跑一次 reindex_knowledge 就行）。

字段映射（对照旧 obsidian-rag 项目 library.py 的真实 schema 核对过，
不是凭印象猜的）：
- 旧 `{"libraries": [{name, path, selection_in, selection_out, extensions,
  ...}]}` 的列表结构 → 新按 library_id 为键的字典结构；library_id 由
  name 派生（slugify），重名时自动加序号避免冲突
- path → root_path；selection_in/selection_out 两边都是正斜杠、无首尾
  斜杠的相对路径，格式一致，直接照搬不用转换（旧项目 `norm_sel_path` 和
  本项目 `official_library_manager.selection._segments` 的路径规范化
  结果是同一种形态）
- extensions（旧：不带点，如 "md"，缺省时旧项目用
  `DEFAULT_EXTENSIONS = ["md","pdf","docx"]`）→ enabled_extensions
  （新：带点，如 ".md"）
- 全局 `selection_new_files`（旧项目这是全局配置项，不是逐库存的，默认值
  "follow"）→ 逐库 `new_file_default`：对照旧项目 index.py 里
  `collect_md_files` 的真实分支逻辑核对过——旧"follow"含义是"未勾选文件
  按本库 extensions 列表过滤"，这和新架构"new_file_default=include 时依然
  会过一遍 enabled_extensions"的语义完全一致，直接映射成新"include"；旧
  "exclude"映射新"exclude"；旧"include"（更宽松，忽略本库 extensions、
  只要是系统支持格式就收）没有直接等价物，退化映射成新"include"——如果你
  确实用过这个更宽松的模式，迁移后请自己检查一下 enabled_extensions 列全
  了没有
- agent_formats（旧项目"AI 已获授权可写的二进制格式清单"）：**不迁移**——
  新架构的 AI 写权限模型是通用的 WriteGate 两段式确认（见
  core/write_gate.py），和旧项目"预先长期授权格式清单"是不同机制，没有
  直接对应关系，迁移后按新流程重新授权
- exclude_dirs/exclude_files/exclude_patterns/chunk_char_limit/
  short_doc_char_limit/collection：**不迁移**——这些是旧项目的全局可覆盖
  配置项，新架构对应概念（比如切块大小）目前是 official-chunker 插件的
  模块级默认值，还没做成逐库可配置，真有这个需求时再补

用法：
    python tools/migrate_libraries_json.py <旧 libraries.json 路径> [选项]

    --old-selection-new-files {follow,include,exclude}
        旧项目 config.json 里 selection_new_files 的全局值，默认 follow
        （绝大多数用户从没改过这个值）；如果你确实改过，从旧项目的
        data/config.json 里查一下真实值传进来
    --data-dir DIR   新项目的数据根目录，默认仓库根目录下的 data/
    --dry-run        只打印迁移结果，不实际写入
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "official-library-manager"))

from official_library_manager.config import LibraryConfigStore  # noqa: E402

OLD_DEFAULT_EXTENSIONS = ["md", "pdf", "docx"]


def _slugify(name: str) -> str:
    """纯 ASCII 库名能直接得到可读的 id；中文/日文等非 ASCII 库名（这个
    项目的实际用户很可能全是这种）slugify 后会是空字符串——这种情况下
    退化用名字的短哈希拼出一个稳定、可追溯的 id（"library-<8位哈希>"），
    不要落到清一色"library"/"library-2"/"library-3"这种和原名毫无关系、
    还依赖迁移顺序才能区分的编号上。"""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if base:
        return base
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return f"library-{digest}"


def _unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def migrate(store: LibraryConfigStore, old_entries: list[dict], global_selection_new_files: str) -> list[str]:
    """把 old_entries 逐条写进 store（真实公开 API，不绕过去自己拼 JSON——
    避免"迁移脚本的输出格式"和"真实代码写出来的格式"两边各写一份、以后
    代码改了迁移脚本忘了跟着改的经典坑）。返回给人看的日志行。"""
    logs: list[str] = []
    taken: set[str] = {c.library_id for c in store.list_libraries()}

    for entry in old_entries:
        name = entry.get("name")
        path = entry.get("path")
        if not name or not path:
            logs.append(f"跳过一条缺 name/path 的条目：{entry!r}")
            continue

        library_id = _unique_id(_slugify(name), taken)
        taken.add(library_id)

        extensions = entry.get("extensions") or OLD_DEFAULT_EXTENSIONS
        enabled_extensions = [f".{ext.lstrip('.').lower()}" for ext in extensions]
        new_default = "exclude" if global_selection_new_files == "exclude" else "include"

        store.add_library(library_id, name, path)
        store.set_policy(library_id, new_file_default=new_default, enabled_extensions=enabled_extensions)
        cfg = store.set_selection(
            library_id,
            selection_in=sorted(entry.get("selection_in") or []),
            selection_out=sorted(entry.get("selection_out") or []),
        )

        logs.append(
            f"{library_id}: {name} -> {path} "
            f"(纳入{len(cfg.selection_in)}条/排除{len(cfg.selection_out)}条规则，"
            f"格式={enabled_extensions}，新文件默认={new_default})"
        )
        if entry.get("agent_formats"):
            logs.append(
                f"  [{name}] 旧库有 agent_formats 授权记录，新架构没有直接等价物，"
                "不迁移——需要时请通过新的 WriteGate 流程重新授权"
            )

    return logs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old_libraries_json", type=Path, help="旧项目 data/libraries.json 的路径")
    parser.add_argument(
        "--old-selection-new-files",
        default="follow",
        choices=["follow", "include", "exclude"],
        help="旧项目 config.json 里的 selection_new_files 全局值（默认 follow）",
    )
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data", help="新项目的数据根目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印迁移结果，不实际写入")
    args = parser.parse_args()

    try:
        raw = json.loads(args.old_libraries_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取/解析旧 libraries.json 失败：{exc}", file=sys.stderr)
        return 1

    old_entries = raw.get("libraries", []) if isinstance(raw, dict) else []
    if not old_entries:
        print('旧文件里没有找到任何库条目（预期结构：{"libraries": [...]}）', file=sys.stderr)
        return 1

    target_dir = REPO_ROOT / "tmp-migrate-preview" if args.dry_run else args.data_dir
    store = LibraryConfigStore(target_dir / "libraries.json")
    logs = migrate(store, old_entries, args.old_selection_new_files)

    print(f"迁移了 {len(store.list_libraries())} 个库：")
    for line in logs:
        print(f"  {line}")

    if args.dry_run:
        import shutil

        shutil.rmtree(target_dir, ignore_errors=True)
        print("\n--dry-run：以上是预览，没有写入任何正式文件。")
        return 0

    print(f"\n已写入 {store.path}")
    print("注意：索引本身没有迁移，需要对每个库跑一次 reindex_knowledge（MCP工具/GUI'重建索引'按钮）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
