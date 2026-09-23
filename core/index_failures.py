"""core/index_failures.py — 索引失败溯源的极简持久化（核心服务，不是插件）。

**补的是哪个坑**：2026-09-23 全面功能审计发现，obsidian-rag 的
`index_failures` MCP 工具（只读诊断——逐文件列出库内"没转成/没索引上"
的文档及原因）在 rag-redo 里完全没有对应实现。`core/pipeline.py::
index_library()` 跑完后产出的 `IndexReport` 本来就带着这些信息
（`IndexFileReport.extract_failure`），但只在那一次同步调用的返回值里
昙花一现——`start_index_library()` 后台执行那条路径下，调用方根本拿
不到这个返回值，即使是同步调用，跑完之后这份信息也没有任何持久化，
问不出"上次索引到底哪些文件没转成"。

**为什么是核心服务**：同 `extract_cache`/`index_progress`/
`note_relations`——`index_library()` 是编排层自己的方法，产出的诊断
数据也该由编排层自己持久化，不是某个插件的职责，也不需要做成扩展点
（没有"可插拔的失败诊断算法"这种需求）。

**刻意的简化，如实记录**：obsidian-rag 的 `index_failures` 基于持久化
的 per-file meta（`index_meta_*.json`），额外能判断"这条失败下一轮
会不会自动重试"（对照当前 OCR 能力签名 `current_backend_sig()`）——
这依赖它"增量索引、跳过内容没变的文件"的机制。rag-redo 目前每次
`index_library()` 都是全量重跑（Phase 1 既定简化，见 `docs/ROADMAP.md`），
不存在"这次失败下次会不会重试"的问题——反正下次全量重跑本来就会
重新试一遍所有文件，这个问题在 rag-redo 里不成立，不是简化掉答不出来。
这里只回答"上一次全量重跑后，谁没转成、为什么"，不做重试预测。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


class IndexFailuresStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, library_id: str) -> Path:
        safe = re.sub(r"[^\w.-]", "_", library_id)
        return self._root / f"{safe}.json"

    def write_library(self, library_id: str, *, succeeded: int, failures: list[dict]) -> None:
        """整库覆盖写一次（不是逐文件增量写）——同索引本身"不做增量、
        全量重跑"的节奏一致。`index_library()` 每次跑完（不管成功还是
        部分失败）都应该调用一次，让这份诊断数据始终反映"最近一次"。"""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = self._path_for(library_id)
            tmp_path = target.with_suffix(".tmp")
            data = {"succeeded": succeeded, "failures": failures}
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, target)
        except OSError:
            pass  # 诊断数据写盘失败不该让索引任务本身失败——fail-open，同其他核心服务的一贯原则

    def read(self, library_id: str) -> dict | None:
        """返回 `{"succeeded": N, "failures": [{"path", "reason"}, ...]}`。
        库从没索引过（没有诊断数据文件）时返回 `None`——调用方自己决定
        怎么展示"从没跑过"和"跑过但全部成功"（`failures` 为空列表）的
        区别。"""
        path = self._path_for(library_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
