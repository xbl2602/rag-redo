"""core/note_relations.py — 双链关系（wikilink 出链/入链）持久化 + 查询。

**补的是哪个坑**：2026-09-23 全面功能审计发现，obsidian-rag 的
`note_relations` MCP 工具（基于 Obsidian `[[wikilink]]` 语法的笔记间
双链关系）在 rag-redo 里完全没有对应实现——不是简化，是彻底缺失。

**设计直接照抄 obsidian-rag 的 `index.py::extract_wikilink_targets`/
`resolve_note_relations`**（`obsidian-rag/index.py` 第1314/1353行）：
- 只在索引阶段记录每个文件的**出链**目标列表（`[[目标]]`/`[[目标|别名]]`/
  `[[目标#标题]]` 都取"目标"，`![[嵌入]]` 是附件不计入）；
- **入链永远现算，不持久化反向索引**——个人笔记库规模下现算一遍全部
  文件的出链列表、找谁指向目标文件，成本可忽略，比维护一份"任何文件
  出链变化都要连带更新别人入链缓存"的反向索引简单可靠得多。
- 标题重名时按字典迭代顺序任取其一命中，与 Obsidian 本身处理同名笔记
  的方式一样存在这种歧义，不追求消歧。

**为什么是核心服务而不是插件**：出链数据由 `core/pipeline.py::
index_library()` 在提取阶段顺手生成（复用同一份 `doc.text`，不新增一次
提取或额外的插件调用），查询侧被 MCP 工具直接使用——同 `extract_cache`/
`index_progress` 一样，这是编排层自己的数据流责任，不是"可插拔的第三方
实现"，没有理由做成扩展点。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_WIKILINK_RE = re.compile(r"!?\[\[([^\]]*)\]\]")


def extract_wikilink_targets(text: str) -> list[str]:
    """抽取正文中出现的 wiki 链接目标笔记名（去重、排序）——逐字对齐
    obsidian-rag `index.py::extract_wikilink_targets` 的解析规则：
    `[[目标|别名]]` 取目标，`[[目标#标题]]` 去锚点取目标，`![[嵌入]]`
    是附件嵌入不计入关系，`[[#本文件标题]]`（无目标头）不计入。
    """
    targets: set[str] = set()
    for m in _WIKILINK_RE.finditer(text):
        if m.group(0).startswith("!"):
            continue
        inner = m.group(1).replace("\\|", "|")
        target = inner.split("|", 1)[0].strip()
        head = target.partition("#")[0].strip()
        if head:
            targets.add(head.rsplit("/", 1)[-1].strip())
    return sorted(targets)


class NoteRelationsStore:
    """按库持久化"每个文件→出链目标列表"，供 `resolve()` 现算入链。

    每次 `index_library()` 全量重跑都通过 `write_library()` 整库覆盖写
    一次（不是逐文件增量写）——同索引本身"不做增量、全量重跑"的节奏
    一致，不需要单独的失效判断。
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, library_id: str) -> Path:
        safe = re.sub(r"[^\w.-]", "_", library_id)
        return self._root / f"{safe}.json"

    def write_library(self, library_id: str, links_by_path: dict[str, list[str]]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = self._path_for(library_id)
            tmp_path = target.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(links_by_path, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, target)
        except OSError:
            pass  # 双链关系写盘失败不该让索引任务本身失败——fail-open，同其他核心服务的一贯原则

    def resolve(self, library_id: str, target: str) -> dict:
        """给定笔记标识（库内相对路径，或不含扩展名的标题），返回其出链
        （本文链接到谁）与入链（谁链接到本文）。库从没索引过（没有出链
        数据文件）时返回 `resolved=False`，同"找不到这篇笔记"一致处理，
        调用方不需要区分这两种情况。"""
        store_path = self._path_for(library_id)
        data: dict[str, list[str]] = {}
        if store_path.is_file():
            try:
                data = json.loads(store_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

        by_stem: dict[str, str] = {}
        for rel in data:
            by_stem[Path(rel).stem] = rel  # 重名时后出现的覆盖前面的，同 obsidian-rag 一样不追求消歧

        def _resolve_name(name: str) -> str | None:
            if name in data:
                return name
            return by_stem.get(Path(name).stem)

        rel = _resolve_name(target)
        if rel is None:
            return {"resolved": False, "file": None, "outlinks": [], "inlinks": []}

        outlinks: set[str] = set()
        for name in data.get(rel, []):
            r = _resolve_name(name)
            if r and r != rel:
                outlinks.add(r)

        inlinks: set[str] = set()
        for other, links in data.items():
            if other == rel:
                continue
            for name in links:
                if _resolve_name(name) == rel:
                    inlinks.add(other)
                    break

        return {"resolved": True, "file": rel, "outlinks": sorted(outlinks), "inlinks": sorted(inlinks)}
