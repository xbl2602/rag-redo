"""core/extract_cache.py — 提取结果缓存（核心服务，不是插件）。

**补的是哪个坑**：2026-09-23 全面功能审计发现，`index_library()` 里
`ExtractedDocument.text` 只在切块前一闪而过——切完块立刻丢弃，从不落盘。
这导致索引完成之后**没有任何办法**再问"这篇文档完整提取出来是什么样"，
obsidian-rag 的 `read_document`（读某文档的完整提取正文）、`find_duplicates`
（近似重复检测——按整篇提取文本的 MinHash 比较，不是按 chunk 片段）两个
MCP 工具都依赖这个能力，rag-redo 此前完全没有。

**为什么是核心服务**：这份缓存由编排层（`core/pipeline.py::index_library()`）
在索引过程中产出、又由编排层（`read_document`/`find_duplicates` 走的
`core/pipeline.py` 方法）消费——两头都是核心自己，不是"两个互不知情的
插件需要中立裁判"的场景，但因为 `core/pipeline.py` 本身不是插件、没有
`ctx` 可用、也不该直接管理原始文件 I/O 细节，所以拆成一个独立的小核心
模块（同 `core/settings.py`/`core/write_gate.py` 的既有先例：核心自己的
状态由核心自己的模块管理，不通过 `DataStore`——那个类的 docstring 自己
写明是 Phase 0 占位，见 `core/settings.py` 模块 docstring 同一条理由）。

**刻意的简化，如实记录**：不做 obsidian-rag 那套"content_hash+extractor
版本号"的精细缓存失效机制（那是配合"增量索引"设计的——旧内容还在缓存
里但源文件已经变了，需要判断"要不要重新提取"）。rag-redo 目前索引
本来就是全量重跑（见 docs/ROADMAP.md"不做增量索引"的已知简化），这里
的缓存语义简单得多："这次重建索引时的最新一份"——`index_library()`
开始时清空该库整份缓存，重跑时逐个覆盖写入，不需要单独判断"这份缓存
是不是过期了"，下一次 `reindex_knowledge` 自然全部刷新。

**文件名用路径哈希而不是转义后的原始路径**：库内相对路径可能又长又含
中文/特殊字符（比如"20-Projects/机器学习论文合集/2024年最新进展详细
笔记.md"），转义成安全文件名后长度不可控，真实可能撞上 Windows 单个
路径分量/MAX_PATH 的限制。改用固定长度的路径哈希做文件名，另外维护
一份 `_index.json`（hash→原始相对路径）供反查，不依赖"从文件名猜回
原路径"这种脆弱的方式。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _hash_for(rel_path: str) -> str:
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()


class ExtractCache:
    """一个库一个缓存目录：`root/<library_id>/<路径哈希>.txt` + 同目录下
    `_index.json`（hash→原始相对路径，供 `list_relative_paths` 反查）。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _dir_for(self, library_id: str) -> Path:
        return self._root / library_id

    def _text_path(self, library_id: str, rel_path: str) -> Path:
        return self._dir_for(library_id) / f"{_hash_for(rel_path)}.txt"

    def _index_path(self, library_id: str) -> Path:
        return self._dir_for(library_id) / "_index.json"

    def _load_index(self, library_id: str) -> dict[str, str]:
        path = self._index_path(library_id)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_index(self, library_id: str, index: dict[str, str]) -> None:
        self._index_path(library_id).write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    def write(self, library_id: str, rel_path: str, text: str) -> None:
        self._dir_for(library_id).mkdir(parents=True, exist_ok=True)
        self._text_path(library_id, rel_path).write_text(text, encoding="utf-8")
        index = self._load_index(library_id)
        index[_hash_for(rel_path)] = rel_path
        self._save_index(library_id, index)

    def read(self, library_id: str, rel_path: str) -> str | None:
        path = self._text_path(library_id, rel_path)
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def clear_library(self, library_id: str) -> None:
        """整库重建索引前先清空——避免被删除/排除出检索范围的文件在缓存
        里留下陈旧正文（`read_document`/`find_duplicates` 会一直"看得到"
        一份用户以为已经移除的文件，属于真实的用户可感知不一致，不是
        无关紧要的内部状态）。幂等：目录本来就不存在也不报错。"""
        import shutil

        library_dir = self._dir_for(library_id)
        if library_dir.is_dir():
            shutil.rmtree(library_dir)

    def list_relative_paths(self, library_id: str) -> list[str]:
        """列出这个库当前缓存里有正文的全部相对路径——`find_duplicates`
        用这个枚举"要比较哪些文件"，不需要重新问 library_manager 一遍
        （那会包含还没成功提取过的文件，这里只关心"确实有正文可比较"
        的子集）。"""
        return sorted(self._load_index(library_id).values())
