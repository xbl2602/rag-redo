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

**增量失败语义**：per-file manifest 记录终态、deferred、能力签名和输入指纹。稳定终态在输入与能力不变时跳过；内容或能力变化后重试。deferred 不进入终态清单，并在后续同步继续尝试；成功后清除失败状态。删除或排除的文件从当前 generation 的失败清单中消失。
"""
from __future__ import annotations

import json

from .atomic import atomic_write_text
from .library_key import library_storage_key
from pathlib import Path

# 终态 reason → 人类可读解释（索引失败溯源用）——逐字对齐旧
# obsidian-rag/server.py::_TERMINAL_LABELS（866-873）。not-pdf 是 WEMM
# 页库语境的额外键，不在索引五常量终态内，保留是为了报告兜底不漏。
TERMINAL_LABELS = {
    "unreadable": "无法读取（疑似损坏/无权限，指纹两轮才判稳）",
    "extract-failed": "提取失败（损坏/加密/云端失败，可按需重试）",
    "empty": "空内容（打开正常但没有可提取的正文）",
    "tbd": "命中 [TBD] 占位（正文还没写完，写完会自动转正）",
    "scanned": "扫描件待 OCR（后端 none 时跳过；开启 MinerU 云端后会自动重试）",
    "not-pdf": "非 PDF（仅 WEMM 页库语境）",
}


class IndexFailuresStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path_for(self, library_id: str, generation: str | None = None) -> Path:
        # 目录名走 `library_storage_key`（安全名 + 短哈希，core/library_key.py），
        # 不用裸 `re.sub` 字符替换——后者不是单射，`"a b"` 与 `"a_b"` 两个不同
        # 的库会共用同一份诊断数据，其中一份还会被全局回收当孤儿删掉。统一到
        # 唯一实现也让 `prune_unreferenced_data` 的孤儿判定只需认一种命名。
        safe = library_storage_key(library_id)
        if generation:
            return self._root / "generations" / safe / f"{generation}.json"
        return self._root / f"{safe}.json"

    def write_library(
        self,
        library_id: str,
        *,
        succeeded: int,
        failures: list[dict],
        generation: str | None = None,
    ) -> None:
        """把本轮有效文件清单的成功/失败汇总原子写入当前 generation。"""
        try:
            target = self._path_for(library_id, generation)
            data = {"succeeded": succeeded, "failures": failures}
            atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError:
            pass  # 诊断数据写盘失败不该让索引任务本身失败——fail-open，同其他核心服务的一贯原则

    def clear_generation(self, library_id: str, generation: str) -> None:
        try:
            self._path_for(library_id, generation).unlink(missing_ok=True)
        except OSError:
            pass

    def read(self, library_id: str, generation: str | None = None) -> dict | None:
        """返回 `{"succeeded": N, "failures": [{"path", "reason", "label"}, ...]}`。
        label 为 reason 的中文释义（旧 _TERMINAL_LABELS；未登记的 reason 回退
        原串，绝不让失败原因无声消失）。库从没索引过（没有诊断数据文件）
        时返回 `None`——调用方自己决定怎么展示"从没跑过"和"跑过但全部成功"
        （`failures` 为空列表）的区别。"""
        path = self._path_for(library_id, generation)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            for row in data.get("failures") or []:
                if isinstance(row, dict) and row.get("reason") is not None:
                    row["label"] = TERMINAL_LABELS.get(str(row["reason"]), str(row["reason"]))
        return data
